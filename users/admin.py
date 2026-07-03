"""Admin-side user provisioning.

Used by both `scripts/create_user.py` (CLI) and the `POST /admin/users`
HTTP route  so the two surfaces share a single, well-tested code
path for inserting a reseller end-user row.

`get_user_by_app_key` / `update_user`  back the lookup-and-edit
flow on `/admin/create_user` — same engine, same validation, same
error envelope.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Optional

from sqlalchemy import text

from users.schema import is_valid_app_key


DEFAULT_BALANCE_POINTS = Decimal("1000")

# Sentinel for `update_user` to distinguish "caller did not pass this
# field" (leave column unchanged) from "caller passed None" (clear the
# column to NULL). Profile columns accept None; balance does not.
_UNSET: Any = object()


class AppKeyAlreadyExists(ValueError):
    """Raised when the supplied app_key already has a row in `users`."""


class InvalidAppKeyFormat(ValueError):
    """Raised when the supplied app_key doesn't match `grid_<22 base62>`."""


class InvalidBalance(ValueError):
    """Raised when `balance` is negative."""


class UserNotFound(LookupError):
    """Raised when `update_user` / `get_user_by_app_key` cannot find the
    supplied app_key in `users`. The route layer turns this into 404 so
    operators do not silently no-op against a typo'd key."""


def create_user(
    engine,
    app_key: str,
    balance: Decimal,
    *,
    nickname: str | None = None,
    mobile: str | None = None,
    email: str | None = None,
    company_name: str | None = None,
) -> None:
    """Insert a new row in `users` with the given app_key + initial balance.

    Caller is responsible for generating the app_key (use
    `users.schema.generate_app_key()` for the default flow) — this
    separation keeps the function testable without monkey-patching the
    random source.
    """
    if not balance.is_finite():
        raise InvalidBalance(f"balance must be finite, got {balance}")
    if balance < 0:
        raise InvalidBalance(f"balance must be >= 0, got {balance}")
    if not is_valid_app_key(app_key):
        raise InvalidAppKeyFormat(
            f"app_key {app_key!r} does not match grid_<22 base62> format"
        )
    with engine.begin() as conn:
        existing = conn.execute(
            text("SELECT 1 FROM users WHERE app_key = :k"),
            {"k": app_key},
        ).first()
        if existing is not None:
            raise AppKeyAlreadyExists(f"app_key {app_key!r} already exists")
        conn.execute(
            text(
                "INSERT INTO users "
                "(app_key, balance_points, nickname, mobile, email, company_name, created_at) "
                "VALUES (:k, :b, :n, :m, :e, :c, CURRENT_TIMESTAMP)"
            ),
            {
                "k": app_key,
                "b": balance,
                "n": nickname,
                "m": mobile,
                "e": email,
                "c": company_name,
            },
        )


def get_user_by_app_key(engine, app_key: str) -> Optional[dict]:
    """Look up a user by `app_key`. Returns a dict with the editable
    profile fields + balance, or `None` if no such user exists.

    `balance_points` is coerced to `Decimal` so callers can serialize
    consistently across SQLite (str) and Postgres (Decimal) drivers."""
    if not is_valid_app_key(app_key):
        raise InvalidAppKeyFormat(
            f"app_key {app_key!r} does not match grid_<22 base62> format"
        )
    with engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT app_key, balance_points, nickname, mobile, email, "
                "company_name, created_at FROM users WHERE app_key = :k"
            ),
            {"k": app_key},
        ).mappings().first()
    if row is None:
        return None
    return {
        "app_key": row["app_key"],
        "balance": Decimal(str(row["balance_points"])),
        "nickname": row["nickname"],
        "mobile": row["mobile"],
        "email": row["email"],
        "company_name": row["company_name"],
        "created_at": row["created_at"],
    }


def update_user(
    engine,
    app_key: str,
    *,
    balance: Any = _UNSET,
    nickname: Any = _UNSET,
    mobile: Any = _UNSET,
    email: Any = _UNSET,
    company_name: Any = _UNSET,
) -> None:
    """Partially update a user row. Unset kwargs leave the column
    unchanged; explicit `None` clears profile columns.

    `balance` is non-nullable on the table — passing `None` is rejected
    as `InvalidBalance` to keep the column constraint in sync with the
    Python type.
    """
    if not is_valid_app_key(app_key):
        raise InvalidAppKeyFormat(
            f"app_key {app_key!r} does not match grid_<22 base62> format"
        )

    set_clauses: list[str] = []
    params: dict[str, Any] = {"k": app_key}

    if balance is not _UNSET:
        if balance is None:
            raise InvalidBalance("balance cannot be null")
        if not balance.is_finite():
            raise InvalidBalance(f"balance must be finite, got {balance}")
        if balance < 0:
            raise InvalidBalance(f"balance must be >= 0, got {balance}")
        set_clauses.append("balance_points = :b")
        params["b"] = balance
    if nickname is not _UNSET:
        set_clauses.append("nickname = :n")
        params["n"] = nickname
    if mobile is not _UNSET:
        set_clauses.append("mobile = :m")
        params["m"] = mobile
    if email is not _UNSET:
        set_clauses.append("email = :e")
        params["e"] = email
    if company_name is not _UNSET:
        set_clauses.append("company_name = :c")
        params["c"] = company_name

    with engine.begin() as conn:
        existing = conn.execute(
            text("SELECT 1 FROM users WHERE app_key = :k"),
            {"k": app_key},
        ).first()
        if existing is None:
            raise UserNotFound(f"app_key {app_key!r} does not exist")
        if not set_clauses:
            return
        conn.execute(
            text(
                "UPDATE users SET "
                + ", ".join(set_clauses)
                + " WHERE app_key = :k"
            ),
            params,
        )
