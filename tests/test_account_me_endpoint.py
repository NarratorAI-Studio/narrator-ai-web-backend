"""Integration tests for GET /account/me .

Strategy: same in-memory sqlite + monkeypatched DB engine pattern as
test_movie_baokuan_endpoint.py. The sqlite schema mirrors the post-
20260526_0002 migration shape (id + profile fields), so tests exercise
the actual route handler against a realistic-shape store.
"""

from __future__ import annotations

import sqlite3
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, text


sqlite3.register_adapter(Decimal, str)


# Post-20260526_0002 schema, sqlite-flavored (NUMERIC → NUMERIC,
# TIMESTAMPTZ → TEXT, IDENTITY → plain INTEGER UNIQUE; the production
# IDENTITY sequence isn't reachable in sqlite but isn't relevant to the
# endpoint contract — tests pre-seed `id` explicitly).
SQLITE_SCHEMA = """
CREATE TABLE users (
    app_key TEXT PRIMARY KEY,
    id INTEGER NOT NULL UNIQUE,
    balance_points NUMERIC NOT NULL DEFAULT 1000 CHECK (balance_points >= 0),
    nickname TEXT,
    mobile TEXT,
    email TEXT,
    company_name TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""

# Two test keys: one fully populated (mirrors the planned id=1 public sample seed),
# one with all nullable fields left empty (mirrors id=2 prod state).
KEY_FULL = "grid_AbCdEfGhIjKlMnOpQrStUv"
KEY_NULL = "grid_ZyXwVuTsRqPoNmLkJiHgFe"


@pytest.fixture()
def sqlite_engine():
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}
    )
    with engine.begin() as conn:
        conn.execute(text(SQLITE_SCHEMA))
        conn.execute(
            text(
                "INSERT INTO users "
                "(app_key, id, balance_points, nickname, mobile, email, company_name) "
                "VALUES (:k, 1, :b, 'Demo User', '13900000222', 'user@example.com', 'Example Inc')"
            ),
            {"k": KEY_FULL, "b": Decimal("2340.02")},
        )
        conn.execute(
            text(
                "INSERT INTO users (app_key, id, balance_points) "
                "VALUES (:k, 2, :b)"
            ),
            {"k": KEY_NULL, "b": Decimal("1000")},
        )
    yield engine
    engine.dispose()


@pytest.fixture()
def client(sqlite_engine, monkeypatch):
    import server

    monkeypatch.setattr(server, "get_db_engine", lambda: sqlite_engine)
    monkeypatch.setattr(
        server, "get_db_core_connection", lambda: sqlite_engine.connect()
    )
    return server.app.test_client()


# ---------- happy path ----------


def test_returns_full_profile_for_seeded_user(client):
    """Mirrors the the implementation requirement example contract verbatim, including
    masked mobile and stringified balance."""
    res = client.get("/account/me", headers={"X-Web-App-Key": KEY_FULL})
    assert res.status_code == 200
    assert res.get_json() == {
        "success": True,
        "data": {
            "user_id": 1,
            "nickname": "Demo User",
            "mobile": "139*****222",
            "email": "user@example.com",
            "balance": "2340.02",
            "company_name": "Example Inc",
        },
    }


def test_returns_null_fields_for_user_without_profile(client):
    """The unpopulated counterpart: nullable fields render as JSON
    `null` (not omitted, not empty string). Pins the contract."""
    res = client.get("/account/me", headers={"X-Web-App-Key": KEY_NULL})
    assert res.status_code == 200
    assert res.get_json() == {
        "success": True,
        "data": {
            "user_id": 2,
            "nickname": None,
            "mobile": None,
            "email": None,
            "balance": "1000.00",
            "company_name": None,
        },
    }


# ---------- auth failure modes (delegated to require_web_user_auth) ----------


def test_missing_header_returns_401_missing(client):
    res = client.get("/account/me")
    assert res.status_code == 401
    assert res.get_json()["error"]["code"] == "WEB_APP_KEY_MISSING"


def test_malformed_key_returns_401_invalid(client):
    res = client.get("/account/me", headers={"X-Web-App-Key": "not-a-grid-key"})
    assert res.status_code == 401
    assert res.get_json()["error"]["code"] == "WEB_APP_KEY_INVALID"


def test_unknown_key_returns_401_unknown(client):
    # Valid format, not seeded.
    res = client.get(
        "/account/me",
        headers={"X-Web-App-Key": "grid_ZZZZZZZZZZZZZZZZZZZZZZ"},
    )
    assert res.status_code == 401
    assert res.get_json()["error"]["code"] == "WEB_APP_KEY_UNKNOWN"


# ---------- DB failure ----------


def test_db_failure_after_auth_returns_503_retryable(sqlite_engine, monkeypatch):
    """Middleware succeeds (its own connection works), then the route's
    profile query raises. Should return 503 USER_LOOKUP_FAILED rather
    than letting the exception bubble to a 500.
    """
    import server

    # Middleware uses the closure-captured factory (working sqlite). The
    # route handler's second connection factory call raises.
    state = {"calls": 0}

    def maybe_failing_factory():
        state["calls"] += 1
        if state["calls"] >= 2:
            raise RuntimeError("simulated DB outage on second connection")
        return sqlite_engine.connect()

    monkeypatch.setattr(server, "get_db_engine", lambda: sqlite_engine)
    monkeypatch.setattr(server, "get_db_core_connection", maybe_failing_factory)

    res = server.app.test_client().get(
        "/account/me", headers={"X-Web-App-Key": KEY_FULL}
    )
    assert res.status_code == 503
    body = res.get_json()
    assert body["error"]["code"] == "USER_LOOKUP_FAILED"
    assert body["error"]["retryable"] is True
