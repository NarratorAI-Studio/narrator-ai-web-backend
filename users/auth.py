"""Web-app-key authentication middleware for backend routes.

Validates the `X-Web-App-Key` header against the `users` table (introduced
in regression coverage). Mirrors the contract of `server.require_pricing_bff_auth`:
returns `None` on success, a Flask `(response, status)` tuple on failure.

Auth pipeline for end-user-facing backend routes (e.g. `/pricing/movie-baokuan`):
  1. `Authorization: Bearer <PRICING_BFF_AUTH_TOKEN>` — service identity.
     Only the web tier holds this token; protects upstream quota at the
     network layer (see review security-sensitive on docs/HP-XX-staging-rollout.md).
  2. `X-Web-App-Key: <users.app_key>` — user identity. Identifies which
     reseller end-user is making the request. Foundation for future
     balance accounting, per-user rate limiting, and audit trails.

Both checks are independent and both must pass. Adding only one of them
leaves a hole: Bearer-only routes can be abused by any internet caller
via the BFF; web-app-key-only routes leak the upstream quota since
anyone with a valid `grid_xxx` key could hit them directly.
"""

from __future__ import annotations

from typing import Callable

from flask import jsonify, request
from sqlalchemy import select

from db.tables import users
from users.schema import is_valid_app_key


_WEB_APP_KEY_HEADER = "X-Web-App-Key"


def _error_response(code: str, message: str, status: int, *, retryable: bool):
    return (
        jsonify(
            {
                "success": False,
                "error": {
                    "code": code,
                    "message": message,
                    "retryable": retryable,
                    "details": {},
                },
            }
        ),
        status,
    )


def require_web_user_auth(connection_factory: Callable):
    """Validate the X-Web-App-Key header against the `users` table.

    `connection_factory` is a zero-arg callable returning an open SQLAlchemy
    Connection — pass `server.get_db_core_connection` from the route handler.
    Injected (rather than imported from server) to avoid a circular import
    and to mirror the WalletService dependency-injection pattern.

    Returns:
        None on success; a Flask `(response, status)` tuple on failure.

    Failure modes:
        - 401 WEB_APP_KEY_MISSING — header absent or empty.
        - 401 WEB_APP_KEY_INVALID — header value fails the issued-key regex.
        - 401 WEB_APP_KEY_UNKNOWN — header value is well-formed but no
          matching row in `users` (revoked / typo'd / not yet issued).
        - 503 USER_LOOKUP_FAILED — DB lookup raised; retryable.
    """
    app_key = request.headers.get(_WEB_APP_KEY_HEADER, "")

    if not app_key:
        return _error_response(
            "WEB_APP_KEY_MISSING",
            f"{_WEB_APP_KEY_HEADER} header is required.",
            401,
            retryable=False,
        )

    if not is_valid_app_key(app_key):
        return _error_response(
            "WEB_APP_KEY_INVALID",
            f"{_WEB_APP_KEY_HEADER} format is invalid.",
            401,
            retryable=False,
        )

    try:
        conn = connection_factory()
        try:
            row = conn.execute(
                select(users.c.app_key).where(users.c.app_key == app_key)
            ).first()
        finally:
            conn.close()
    except Exception:
        return _error_response(
            "USER_LOOKUP_FAILED",
            "Failed to validate web-app-key against the users table.",
            503,
            retryable=True,
        )

    if row is None:
        return _error_response(
            "WEB_APP_KEY_UNKNOWN",
            f"{_WEB_APP_KEY_HEADER} is not recognized.",
            401,
            retryable=False,
        )

    return None
