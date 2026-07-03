"""Internal cloud-drive upstream wrapper.

Uses the same `OPEN_FASTAPI_BASE` and `OPEN_FASTAPI_APP_KEY` deployment
configuration as the existing narrator upstream wrappers. The caller supplies
the concrete method/path and receives parsed JSON.
"""

from __future__ import annotations

import json
import os
import socket
from dataclasses import dataclass
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


MAX_UPSTREAM_BYTES = 2 * 1024 * 1024


class UpstreamCloudDriveError(Exception):
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
        raise UpstreamCloudDriveError(
            503,
            "UPSTREAM_NOT_CONFIGURED",
            "Internal cloud-drive base URL or app-key is not configured.",
            retryable=False,
            details={"base_set": bool(base), "app_key_set": bool(key)},
        )
    timeout_raw = os.environ.get("OPEN_FASTAPI_TIMEOUT_SECONDS", "60")
    try:
        timeout = float(timeout_raw)
    except ValueError:
        timeout = 60.0
    if timeout <= 0:
        timeout = 60.0
    return UpstreamConfig(base_url=base, app_key=key, timeout_seconds=timeout)


def call_cloud_drive_upstream(
    method: str,
    upstream_path: str,
    *,
    query: dict | None = None,
    body: dict | None = None,
    config: UpstreamConfig | None = None,
) -> dict | list:
    cfg = config or load_upstream_config()
    query_string = urlencode(
        {k: v for k, v in (query or {}).items() if v is not None and v != ""},
        doseq=False,
    )
    url = f"{cfg.base_url}{upstream_path}"
    if query_string:
        url = f"{url}?{query_string}"

    payload = None
    headers = {
        "app-key": cfg.app_key,
        "Accept": "application/json",
    }
    if body is not None:
        payload = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"

    request = Request(url, data=payload, headers=headers, method=method.upper())

    try:
        with urlopen(request, timeout=cfg.timeout_seconds) as response:
            status = response.status
            content_length_header = response.headers.get("Content-Length")
            if content_length_header is not None:
                try:
                    declared = int(content_length_header)
                except ValueError:
                    declared = None
                if declared is not None and declared > MAX_UPSTREAM_BYTES:
                    raise UpstreamCloudDriveError(
                        502,
                        "UPSTREAM_RESPONSE_TOO_LARGE",
                        f"Internal cloud-drive response exceeds {MAX_UPSTREAM_BYTES} bytes.",
                        retryable=False,
                        details={
                            "content_length": declared,
                            "max_bytes": MAX_UPSTREAM_BYTES,
                            "upstream_path": upstream_path,
                        },
                    )
            raw = response.read(MAX_UPSTREAM_BYTES + 1)
            if len(raw) > MAX_UPSTREAM_BYTES:
                raise UpstreamCloudDriveError(
                    502,
                    "UPSTREAM_RESPONSE_TOO_LARGE",
                    f"Internal cloud-drive response exceeds {MAX_UPSTREAM_BYTES} bytes.",
                    retryable=False,
                    details={"max_bytes": MAX_UPSTREAM_BYTES, "upstream_path": upstream_path},
                )
    except socket.timeout as error:
        raise UpstreamCloudDriveError(
            504,
            "UPSTREAM_TIMEOUT",
            "Internal cloud-drive did not respond in time.",
            retryable=True,
            details={"timeout_seconds": cfg.timeout_seconds, "upstream_path": upstream_path},
        ) from error
    except HTTPError as error:
        is_5xx = 500 <= error.code < 600
        raise UpstreamCloudDriveError(
            503 if is_5xx else 502,
            "UPSTREAM_HTTP_ERROR",
            f"Internal cloud-drive returned HTTP {error.code}.",
            retryable=is_5xx,
            details={"upstream_status": error.code, "upstream_path": upstream_path},
        ) from error
    except URLError as error:
        raise UpstreamCloudDriveError(
            502,
            "UPSTREAM_UNREACHABLE",
            "Internal cloud-drive is unreachable.",
            retryable=True,
            details={"reason": str(error.reason), "upstream_path": upstream_path},
        ) from error

    if status < 200 or status >= 300:
        raise UpstreamCloudDriveError(
            502,
            "UPSTREAM_HTTP_ERROR",
            f"Internal cloud-drive returned HTTP {status}.",
            retryable=False,
            details={"upstream_status": status, "upstream_path": upstream_path},
        )

    try:
        return json.loads(raw or b"{}")
    except ValueError as error:
        raise UpstreamCloudDriveError(
            502,
            "UPSTREAM_DECODE_ERROR",
            "Internal cloud-drive returned a non-JSON body.",
            retryable=False,
            details={"upstream_path": upstream_path},
        ) from error
