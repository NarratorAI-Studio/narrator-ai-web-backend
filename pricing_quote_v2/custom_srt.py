"""Server-authoritative custom SRT fetch + parse + hash.

Replaces the previous "trust client-provided ``custom_srt_file_hash`` and
``custom_srt_valid_line_count``" contract with a server-side path:

1. Validate ``file_id`` resolves to a cloud-drive row owned by the
   requesting ``web_user_id`` (``cloud_drive.store.get_file`` already
   enforces both ownership and "status in (completed, transfer_completed)").
2. Enforce a byte cap on the upstream payload — SRTs are typically
   under 50 KB, so 2 MB leaves 40x headroom while protecting workers
   from OOM if a non-SRT file slips through.
3. Get a presigned URL via ``cloud_drive.upstream.call_cloud_drive_upstream``
   (same path the ``/cloud-drive/download-url`` route uses).
4. Download the bytes via ``urllib`` with timeout + Content-Length
   guard. The URL is a trusted-source URL (returned by an authenticated
   upstream call, not user input), but we still apply defensive size
   limits in case upstream lies / is compromised.
5. SHA256 the raw bytes → ``srt_file_hash``. This is the audit-grade
   identifier referenced in Web API contract § Implementation Scope
   and the catalog-tier-contract § cost mapping.
6. Decode as UTF-8 (``errors='replace'``; the 5a parser is tolerant of
   garbage bytes — any line that's neither structural nor punctuation-
   nor empty-only counts as text).
7. Run ``pricing.srt_parser.count_valid_lines`` (5a / review) → the
   authoritative ``valid_line_count``.

Error mapping (all subclass ``QuoteValidationError``):

- file_id missing / empty                          → ``CustomSrtFileIdMissing`` (400)
- file_id not owned / not completed / not found     → ``CustomSrtFileNotFound`` (404, same envelope to avoid existence-leak)
- file_size from store metadata exceeds cap         → ``CustomSrtFileTooLarge`` (413)
- upstream presigned-URL call fails / network       → ``CustomSrtDownloadFailed`` (503)
- downloaded payload exceeds cap mid-stream         → ``CustomSrtFileTooLarge`` (413, even if metadata said small)
- parse produces 0 lines                            → ``CustomSrtEmpty`` (422)

The download seam (``_download_bytes``) is a module-level function so
tests can monkeypatch it without standing up a real upstream.
"""

from __future__ import annotations

import hashlib
import socket
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from sqlalchemy.engine import Connection

from cloud_drive.store import get_file as _cd_get_file
from cloud_drive.upstream import (
    UpstreamCloudDriveError,
    call_cloud_drive_upstream as _call_cloud_drive_upstream,
)
from pricing.srt_parser import count_valid_lines

from .errors import (
    CustomSrtDownloadFailed,
    CustomSrtEmpty,
    CustomSrtFileIdMissing,
    CustomSrtFileNotFound,
    CustomSrtFileTooLarge,
)


# SRT files are typically < 50 KB. 2 MB leaves ~40x headroom and matches
# `pricing.upstream_baokuan.MAX_UPSTREAM_BYTES`, so the worker-protection
# semantics are uniform across upstream-fetching code in this package.
MAX_SRT_BYTES = 2 * 1024 * 1024

# Align SRT downloads with the standard upstream API timeout.
DOWNLOAD_TIMEOUT_SECONDS = 60.0


def fetch_and_parse_srt(
    conn: Connection,
    *,
    web_user_id: int,
    file_id: str | None,
    downloader: Callable[[str], bytes] | None = None,
) -> dict:
    """Resolve ``file_id`` to a cloud-drive file owned by ``web_user_id``,
    download the bytes from upstream, and return the authoritative
    ``{valid_line_count, srt_file_hash, file_size_bytes}``.

    ``downloader`` is a test seam — production callers pass ``None``
    and the module's ``_download_bytes`` is used.

    Raises ``CustomSrtFileIdMissing`` / ``CustomSrtFileNotFound`` /
    ``CustomSrtFileTooLarge`` / ``CustomSrtDownloadFailed`` /
    ``CustomSrtEmpty`` per :mod:`pricing_quote_v2.errors`.
    """
    if not isinstance(file_id, str) or not file_id.strip():
        raise CustomSrtFileIdMissing(
            "custom_template_id requires a non-empty custom_srt_file_id.",
            details={},
        )
    file_id = file_id.strip()

    owned = _cd_get_file(conn, user_id=web_user_id, file_id=file_id)
    if owned is None:
        # Same envelope for "not in table", "wrong owner", and "not in
        # completed state" — don't leak existence to the caller.
        raise CustomSrtFileNotFound(
            "Custom SRT file_id not found for this user.",
            details={"file_id": file_id},
        )

    # Store metadata can short-circuit a too-large download before we
    # even ask upstream. The metadata `file_size` is a number of bytes
    # populated by the cloud-drive upload pipeline (`user_cloud_files`
    # schema).
    declared_size = owned.get("file_size")
    if isinstance(declared_size, int) and declared_size > MAX_SRT_BYTES:
        raise CustomSrtFileTooLarge(
            "Custom SRT exceeds the configured byte cap.",
            details={"max_bytes": MAX_SRT_BYTES, "declared_bytes": declared_size},
        )

    try:
        upstream_payload = _call_cloud_drive_upstream(
            "POST",
            "/v2/files/download/presigned-url",
            body={"file_id": file_id},
        )
    except UpstreamCloudDriveError as error:
        raise CustomSrtDownloadFailed(
            "Cloud-drive upstream failed when requesting a download URL.",
            details={"upstream_status": getattr(error, "http_status", None)},
        ) from error

    presigned_url = _extract_download_url(upstream_payload)
    if not presigned_url:
        raise CustomSrtDownloadFailed(
            "Cloud-drive upstream did not return a presigned download URL.",
            details={},
        )

    fetcher = downloader if downloader is not None else _download_bytes
    try:
        srt_bytes = fetcher(presigned_url)
    except CustomSrtFileTooLarge:
        # Bubble the mid-stream cap hit untouched.
        raise
    except Exception as error:  # network / timeout / decode of remote
        raise CustomSrtDownloadFailed(
            "Failed to download SRT bytes from cloud-drive upstream.",
            details={"error_class": error.__class__.__name__},
        ) from error

    if len(srt_bytes) > MAX_SRT_BYTES:
        # Defense in depth — `_download_bytes` already caps, but a
        # custom `downloader` injected by tests may not.
        raise CustomSrtFileTooLarge(
            "Custom SRT exceeds the configured byte cap.",
            details={"max_bytes": MAX_SRT_BYTES, "actual_bytes": len(srt_bytes)},
        )

    srt_file_hash = hashlib.sha256(srt_bytes).hexdigest()
    srt_text = srt_bytes.decode("utf-8", errors="replace")
    valid_line_count = count_valid_lines(srt_text)

    if valid_line_count <= 0:
        raise CustomSrtEmpty(
            "Custom SRT contains no valid subtitle text lines.",
            details={"file_id": file_id, "srt_file_hash": srt_file_hash},
        )

    return {
        "valid_line_count": valid_line_count,
        "srt_file_hash": srt_file_hash,
        "file_size_bytes": len(srt_bytes),
    }


def _extract_download_url(upstream_payload: object) -> str | None:
    """Pull a presigned URL out of the cloud-drive upstream response.

    Upstream's shape today is ``{"code": 10000, "data": {...}}`` where
    ``data`` may carry the URL under any of the documented fields
    (``url`` / ``download_url`` / ``presigned_url``). We accept all to
    stay tolerant of minor upstream contract changes; the field name
    has shifted at least once in past upstream releases.
    """
    if not isinstance(upstream_payload, dict):
        return None
    data = upstream_payload.get("data")
    if not isinstance(data, dict):
        return None
    for key in ("download_url", "presigned_url", "url"):
        value = data.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _download_bytes(url: str) -> bytes:
    """Fetch the SRT bytes from a presigned URL with timeout + size cap.

    Raises ``CustomSrtFileTooLarge`` if the Content-Length header or
    the streamed body exceeds ``MAX_SRT_BYTES``. Any other failure
    (network, HTTP error, socket timeout) is re-raised so the caller
    can map to ``CustomSrtDownloadFailed`` with a redacted envelope.
    """
    request = Request(url, method="GET")
    try:
        with urlopen(request, timeout=DOWNLOAD_TIMEOUT_SECONDS) as response:
            declared_length = response.headers.get("Content-Length")
            if declared_length is not None:
                try:
                    if int(declared_length) > MAX_SRT_BYTES:
                        raise CustomSrtFileTooLarge(
                            "Upstream Content-Length exceeds the configured cap.",
                            details={
                                "max_bytes": MAX_SRT_BYTES,
                                "declared_bytes": int(declared_length),
                            },
                        )
                except ValueError:
                    # Non-integer Content-Length — ignore and rely on
                    # the streamed read cap below.
                    pass
            # Read at most MAX_SRT_BYTES + 1 so we can detect overflow
            # even when the header was missing or misleading.
            data = response.read(MAX_SRT_BYTES + 1)
            if len(data) > MAX_SRT_BYTES:
                raise CustomSrtFileTooLarge(
                    "Upstream streamed body exceeded the configured cap.",
                    details={
                        "max_bytes": MAX_SRT_BYTES,
                        "actual_bytes_observed": len(data),
                    },
                )
            return data
    except (HTTPError, URLError, socket.timeout, TimeoutError):
        # Caller maps to CustomSrtDownloadFailed; we just re-raise so
        # the error-class name lands in the redacted envelope.
        raise
