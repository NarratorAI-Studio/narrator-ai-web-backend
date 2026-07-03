"""Upstream `/v2/res/movie-baokuan` proxy client.

Calls the upstream API to get the template list. The route layer then joins
local v2 catalog rows via item.code ("xy0046") -> template_id ("46").

Configuration via env:
- `OPEN_FASTAPI_BASE` (required) — base URL, e.g. https://api.example.com
- `OPEN_FASTAPI_APP_KEY` (required) — sent as `app-key` header
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


UPSTREAM_PATH = "/v2/res/movie-baokuan"

# Forwarded as-is to upstream; documented by the upstream OpenAPI.
SUPPORTED_QUERY_PARAMS = ("platform_id", "category_id", "name", "page", "size")

# Cap on upstream response body size. A real movie-baokuan page is ~15-20 KB
# (25 items × ~700 B/item); 2 MB leaves 100× headroom while protecting Flask
# workers from OOM if upstream is misconfigured / compromised / returns a
# pathological payload.
MAX_UPSTREAM_BYTES = 2 * 1024 * 1024


class UpstreamBaokuanError(Exception):
    """Raised when the upstream call cannot be completed or returns a usable
    payload. `http_status` is what the upstream endpoint should mirror back to
    its caller; `retryable` flags whether the client may retry."""

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
        raise UpstreamBaokuanError(
            503,
            "UPSTREAM_NOT_CONFIGURED",
            "Upstream movie-baokuan base URL or app-key is not configured.",
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


def _build_query_string(params: dict) -> str:
    cleaned = {
        k: v
        for k, v in params.items()
        if k in SUPPORTED_QUERY_PARAMS and v is not None and v != ""
    }
    return urlencode(cleaned, doseq=False)


def fetch_movie_baokuan(params: dict, *, config: UpstreamConfig | None = None) -> dict:
    """Call upstream and return the parsed JSON payload.

    Raises `UpstreamBaokuanError` for network/decode failures and for the
    upstream-not-configured case. Upstream business errors (code != 10000)
    are NOT raised — the caller decides whether to forward them.
    """
    cfg = config or load_upstream_config()
    query = _build_query_string(params)
    url = f"{cfg.base_url}{UPSTREAM_PATH}"
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
            # Defense-in-depth size cap. (1) Trust a sane Content-Length if
            # present; (2) otherwise read MAX+1 bytes and reject if we got
            # more than MAX. Either path returns 502 RESPONSE_TOO_LARGE so
            # an oversized upstream cannot OOM the Flask worker.
            content_length_header = response.headers.get("Content-Length")
            if content_length_header is not None:
                try:
                    declared = int(content_length_header)
                except ValueError:
                    declared = None
                if declared is not None and declared > MAX_UPSTREAM_BYTES:
                    raise UpstreamBaokuanError(
                        502,
                        "UPSTREAM_RESPONSE_TOO_LARGE",
                        f"Upstream movie-baokuan response exceeds {MAX_UPSTREAM_BYTES} bytes.",
                        retryable=False,
                        details={
                            "content_length": declared,
                            "max_bytes": MAX_UPSTREAM_BYTES,
                        },
                    )
            raw = response.read(MAX_UPSTREAM_BYTES + 1)
            if len(raw) > MAX_UPSTREAM_BYTES:
                raise UpstreamBaokuanError(
                    502,
                    "UPSTREAM_RESPONSE_TOO_LARGE",
                    f"Upstream movie-baokuan response exceeds {MAX_UPSTREAM_BYTES} bytes.",
                    retryable=False,
                    details={"max_bytes": MAX_UPSTREAM_BYTES},
                )
    except socket.timeout as error:
        raise UpstreamBaokuanError(
            504,
            "UPSTREAM_TIMEOUT",
            "Upstream movie-baokuan did not respond in time.",
            retryable=True,
            details={"timeout_seconds": cfg.timeout_seconds},
        ) from error
    except HTTPError as error:
        # Upstream returned non-2xx HTTP. Surface as 502 unless 5xx (then 503).
        is_5xx = 500 <= error.code < 600
        raise UpstreamBaokuanError(
            503 if is_5xx else 502,
            "UPSTREAM_HTTP_ERROR",
            f"Upstream movie-baokuan returned HTTP {error.code}.",
            retryable=is_5xx,
            details={"upstream_status": error.code},
        ) from error
    except URLError as error:
        raise UpstreamBaokuanError(
            502,
            "UPSTREAM_UNREACHABLE",
            "Upstream movie-baokuan is unreachable.",
            retryable=True,
            details={"reason": str(error.reason)},
        ) from error

    if status < 200 or status >= 300:
        raise UpstreamBaokuanError(
            502,
            "UPSTREAM_HTTP_ERROR",
            f"Upstream movie-baokuan returned HTTP {status}.",
            retryable=False,
            details={"upstream_status": status},
        )

    try:
        return json.loads(raw)
    except ValueError as error:
        raise UpstreamBaokuanError(
            502,
            "UPSTREAM_DECODE_ERROR",
            "Upstream movie-baokuan returned a non-JSON body.",
            retryable=False,
        ) from error
