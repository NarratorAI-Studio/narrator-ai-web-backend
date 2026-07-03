"""Generic upstream proxy client for the configured narrator-metadata upstream
read-only `GET` endpoints (the implementation requirement).

Mirrors the structure of `pricing/upstream_baokuan.py`, parameterized on
the upstream path + supported query params so a single helper covers all
8 endpoints in the metadata batch:

- /v1/narrator/types
- /v1/narrator/models
- /v1/narrator/bgm
- /v1/task/get_narrator_types
- /v1/task/get_model_versions
- /v2/res/movie-bgm
- /v2/res/movie-dubbing
- /v2/res/baokuan/meta

Configuration (same env vars as movie-baokuan, deliberately shared so the
deployment doesn't grow a second set of upstream secrets):

- `OPEN_FASTAPI_BASE` (required) — base URL, e.g. https://api.example.com
- `OPEN_FASTAPI_APP_KEY` (required) — sent as `app-key` header upstream
- `OPEN_FASTAPI_TIMEOUT_SECONDS` (optional, default 60) — read timeout
"""

from __future__ import annotations

import json
import os
import socket
from dataclasses import dataclass
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


# Cap on upstream response body size. Real metadata pages are small
# (templates page ~15-20 KB, bgm/dubbing lists similar). 2 MB leaves
# 100× headroom while protecting Flask workers from OOM on a
# pathological / compromised upstream. Same value as movie-baokuan.
MAX_UPSTREAM_BYTES = 2 * 1024 * 1024


class UpstreamNarratorError(Exception):
    """Raised when an upstream narrator-metadata call cannot be completed
    or returns an unusable payload. `http_status` is what the backend
    route should mirror back to its caller; `retryable` flags whether
    the client may retry.

    Distinct class from `UpstreamBaokuanError` even though the shape is
    identical — keeping the per-route family of exceptions clear in
    tracebacks / metrics labels.
    """

    def __init__(
        self,
        http_status: int,
        code: str,
        message: str,
        *,
        retryable: bool = False,
        details: dict | None = None,
    ):
        super().__init__(message)
        self.http_status = http_status
        self.code = code
        self.message = message
        self.retryable = retryable
        self.details = details or {}


@dataclass
class UpstreamConfig:
    base_url: str
    app_key: str
    timeout_seconds: float


def load_upstream_config() -> UpstreamConfig:
    base = os.environ.get("OPEN_FASTAPI_BASE", "").rstrip("/")
    key = os.environ.get("OPEN_FASTAPI_APP_KEY", "")
    if not base or not key:
        raise UpstreamNarratorError(
            503,
            "UPSTREAM_NOT_CONFIGURED",
            "Upstream narrator metadata base URL or app-key is not configured.",
            retryable=False,
            details={
                "base_set": bool(base),
                "app_key_set": bool(key),
            },
        )
    timeout_raw = os.environ.get("OPEN_FASTAPI_TIMEOUT_SECONDS", "60")
    try:
        timeout = float(timeout_raw)
    except ValueError:
        timeout = 60.0
    if timeout <= 0:
        timeout = 60.0
    return UpstreamConfig(base_url=base, app_key=key, timeout_seconds=timeout)


def _build_query_string(params: dict, supported: tuple[str, ...]) -> str:
    """Filter `params` to only the documented supported keys and emit a
    safe url-encoded query string. Defense against SSRF / parameter
    smuggling: anything the upstream doesn't document is dropped here,
    not forwarded blindly.
    """
    cleaned = {
        k: v
        for k, v in params.items()
        if k in supported and v is not None and v != ""
    }
    return urlencode(cleaned, doseq=False)


def fetch_narrator_upstream(
    upstream_path: str,
    params: dict,
    supported_params: tuple[str, ...] = (),
    *,
    config: UpstreamConfig | None = None,
) -> dict | list:
    """GET `<OPEN_FASTAPI_BASE><upstream_path>` with the backend's master
    `OPEN_FASTAPI_APP_KEY`, returning the parsed JSON payload.

    `supported_params` is the allowlist of query keys forwarded upstream.
    Anything else in `params` is silently dropped.

    Raises `UpstreamNarratorError` for: missing config, socket timeout,
    upstream non-2xx, upstream unreachable, oversized body, non-JSON body.
    Upstream business errors (e.g. `{code: 40001, ...}`) are NOT raised —
    the caller decides whether to forward them.

    Return type is `dict | list` because the documented upstream contract
    for some narrator-metadata endpoints (e.g. `/v1/narrator/bgm`) is a
    bare JSON array. Routes pass through verbatim so the web tier's
    existing parsing keeps working.
    """
    cfg = config or load_upstream_config()
    query = _build_query_string(params, supported_params)
    url = f"{cfg.base_url}{upstream_path}"
    if query:
        url = f"{url}?{query}"

    request = Request(
        url,
        headers={
            "app-key": cfg.app_key,
            "Accept": "application/json",
        },
        method="GET",
    )

    try:
        with urlopen(request, timeout=cfg.timeout_seconds) as response:
            status = response.status
            # Defense-in-depth size cap; reject early on a declared
            # Content-Length, then read MAX+1 and trip if the body
            # actually exceeded MAX. Either path returns 502
            # UPSTREAM_RESPONSE_TOO_LARGE so an oversized upstream
            # cannot OOM the Flask worker.
            content_length_header = response.headers.get("Content-Length")
            if content_length_header is not None:
                try:
                    declared = int(content_length_header)
                except ValueError:
                    declared = None
                if declared is not None and declared > MAX_UPSTREAM_BYTES:
                    raise UpstreamNarratorError(
                        502,
                        "UPSTREAM_RESPONSE_TOO_LARGE",
                        f"Upstream narrator-metadata response exceeds {MAX_UPSTREAM_BYTES} bytes.",
                        retryable=False,
                        details={
                            "content_length": declared,
                            "max_bytes": MAX_UPSTREAM_BYTES,
                            "upstream_path": upstream_path,
                        },
                    )
            raw = response.read(MAX_UPSTREAM_BYTES + 1)
            if len(raw) > MAX_UPSTREAM_BYTES:
                raise UpstreamNarratorError(
                    502,
                    "UPSTREAM_RESPONSE_TOO_LARGE",
                    f"Upstream narrator-metadata response exceeds {MAX_UPSTREAM_BYTES} bytes.",
                    retryable=False,
                    details={
                        "max_bytes": MAX_UPSTREAM_BYTES,
                        "upstream_path": upstream_path,
                    },
                )
    except socket.timeout as error:
        raise UpstreamNarratorError(
            504,
            "UPSTREAM_TIMEOUT",
            "Upstream narrator-metadata did not respond in time.",
            retryable=True,
            details={
                "timeout_seconds": cfg.timeout_seconds,
                "upstream_path": upstream_path,
            },
        ) from error
    except HTTPError as error:
        is_5xx = 500 <= error.code < 600
        raise UpstreamNarratorError(
            503 if is_5xx else 502,
            "UPSTREAM_HTTP_ERROR",
            f"Upstream narrator-metadata returned HTTP {error.code}.",
            retryable=is_5xx,
            details={
                "upstream_status": error.code,
                "upstream_path": upstream_path,
            },
        ) from error
    except URLError as error:
        raise UpstreamNarratorError(
            502,
            "UPSTREAM_UNREACHABLE",
            "Upstream narrator-metadata is unreachable.",
            retryable=True,
            details={
                "reason": str(error.reason),
                "upstream_path": upstream_path,
            },
        ) from error

    if status < 200 or status >= 300:
        raise UpstreamNarratorError(
            502,
            "UPSTREAM_HTTP_ERROR",
            f"Upstream narrator-metadata returned HTTP {status}.",
            retryable=False,
            details={
                "upstream_status": status,
                "upstream_path": upstream_path,
            },
        )

    try:
        return json.loads(raw)
    except ValueError as error:
        raise UpstreamNarratorError(
            502,
            "UPSTREAM_DECODE_ERROR",
            "Upstream narrator-metadata returned a non-JSON body.",
            retryable=False,
            details={"upstream_path": upstream_path},
        ) from error
