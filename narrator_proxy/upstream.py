"""Generic upstream proxy client for commentaryAPI routes (the implementation requirement).

Mirrors `narrator_metadata.upstream.fetch_narrator_upstream`'s identity model:
the backend's master `OPEN_FASTAPI_APP_KEY` is the only thing sent as the
upstream `app-key` header. The end-user's reseller `X-Web-App-Key` is
validated server-side via `require_web_user_auth` but never forwarded
upstream — reseller keys are not upstream-registered, billing/quota
accrue to the backend's account, and user-level scoping happens in the
backend's `users` table, not upstream (the implementation requirement).

Differs from `fetch_narrator_upstream` in that this module supports both
GET and POST, takes a per-call `timeout_seconds` (the 90 s
search_media_information call coexists with fast routes), and uses a
10 MB body cap because commentary writing payloads can be large.
Reuses `UpstreamNarratorError` so callers get the same error type
regardless of which proxy they hit.
"""

from __future__ import annotations

import json
import os
import socket
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from narrator_metadata.upstream import UpstreamNarratorError

# Commentary task payloads (e.g. full-episode SRT writing content) can
# exceed the metadata 2 MB cap. 10 MB gives enough headroom while still
# protecting Flask workers from OOM on a pathological upstream response.
MAX_UPSTREAM_BYTES = 10 * 1024 * 1024


def proxy_narrator_upstream(
    upstream_path: str,
    *,
    method: str = "GET",
    query_params: dict | None = None,
    body: dict | None = None,
    timeout_seconds: float = 60.0,
) -> dict | list:
    """Proxy a single call to `<OPEN_FASTAPI_BASE><upstream_path>` using
    the backend's master `OPEN_FASTAPI_APP_KEY`. The end-user's
    `X-Web-App-Key` is intentionally NOT forwarded upstream — it is the
    reseller identity used only inside the backend, validated by the
    caller via `require_web_user_auth` (the implementation requirement).

    `query_params` keys with None / empty-string values are dropped.
    `body` is JSON-encoded and sent as the request body for POST calls.

    Raises `UpstreamNarratorError` for: missing config, timeout, non-2xx,
    unreachable, oversized body, non-JSON body.
    """
    base = os.environ.get("OPEN_FASTAPI_BASE", "").rstrip("/")
    master_key = os.environ.get("OPEN_FASTAPI_APP_KEY", "")
    if not base or not master_key:
        raise UpstreamNarratorError(
            503,
            "UPSTREAM_NOT_CONFIGURED",
            "Upstream base URL (OPEN_FASTAPI_BASE) or app-key "
            "(OPEN_FASTAPI_APP_KEY) is not configured.",
            retryable=False,
            details={"base_set": bool(base), "app_key_set": bool(master_key)},
        )

    url = f"{base}{upstream_path}"
    if query_params:
        cleaned = {
            k: str(v) for k, v in query_params.items() if v is not None and str(v) != ""
        }
        if cleaned:
            url = f"{url}?{urlencode(cleaned)}"

    encoded_body = json.dumps(body).encode("utf-8") if body is not None else None

    headers: dict[str, str] = {
        "app-key": master_key,
        "Accept": "application/json",
    }
    if encoded_body is not None:
        headers["Content-Type"] = "application/json"

    req = Request(url, data=encoded_body, headers=headers, method=method.upper())

    try:
        with urlopen(req, timeout=timeout_seconds) as response:
            status = response.status
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
                        f"Upstream response exceeds {MAX_UPSTREAM_BYTES} bytes.",
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
                    f"Upstream response exceeds {MAX_UPSTREAM_BYTES} bytes.",
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
            "Upstream did not respond in time.",
            retryable=True,
            details={
                "timeout_seconds": timeout_seconds,
                "upstream_path": upstream_path,
            },
        ) from error
    except HTTPError as error:
        is_5xx = 500 <= error.code < 600
        raise UpstreamNarratorError(
            503 if is_5xx else 502,
            "UPSTREAM_HTTP_ERROR",
            f"Upstream returned HTTP {error.code}.",
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
            "Upstream is unreachable.",
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
            f"Upstream returned HTTP {status}.",
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
            "Upstream returned a non-JSON body.",
            retryable=False,
            details={"upstream_path": upstream_path},
        ) from error
