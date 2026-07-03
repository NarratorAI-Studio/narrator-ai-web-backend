"""Unit tests for users.auth.require_web_user_auth — the X-Web-App-Key
middleware introduced in the implementation requirement.

Tests run on a minimal Flask app (not server.py) so failures here cannot
be masked by other middleware/handlers. Integration via the actual
movie-baokuan route is tested in test_movie_baokuan_endpoint.py.
"""

from __future__ import annotations

import sqlite3
from decimal import Decimal

import pytest
from flask import Flask, jsonify
from sqlalchemy import create_engine, text


sqlite3.register_adapter(Decimal, str)


def _users_schema_sql_for_sqlite() -> str:
    # Post-20260526_0002 schema. Mirrors tests/test_users.py — sqlite needs
    # portable types in place of NUMERIC(18,2) / TIMESTAMPTZ; IDENTITY is
    # replaced with plain INTEGER UNIQUE (this file's tests don't read
    # `id`, so leaving it NULL on insert is fine).
    return """
    CREATE TABLE IF NOT EXISTS users (
        app_key TEXT PRIMARY KEY,
        id INTEGER UNIQUE,
        balance_points NUMERIC NOT NULL DEFAULT 1000 CHECK (balance_points >= 0),
        nickname TEXT,
        mobile TEXT,
        email TEXT,
        company_name TEXT,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    """


@pytest.fixture()
def sqlite_engine():
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}
    )
    with engine.begin() as conn:
        conn.execute(text(_users_schema_sql_for_sqlite()))
    yield engine
    engine.dispose()


@pytest.fixture()
def valid_key(sqlite_engine):
    """Seed one valid user row and return its app_key."""
    from users.schema import generate_app_key

    key = generate_app_key()
    with sqlite_engine.begin() as conn:
        conn.execute(
            text("INSERT INTO users (app_key, balance_points) VALUES (:k, 1000)"),
            {"k": key},
        )
    return key


@pytest.fixture()
def app(sqlite_engine):
    """Minimal Flask app exposing one protected route for assertions."""
    from users.auth import require_web_user_auth

    flask_app = Flask(__name__)

    @flask_app.route("/protected")
    def protected():
        err = require_web_user_auth(lambda: sqlite_engine.connect())
        if err is not None:
            return err
        return jsonify({"ok": True})

    return flask_app


@pytest.fixture()
def client(app):
    return app.test_client()


# ---------- missing header ----------


def test_missing_header_returns_401(client):
    res = client.get("/protected")
    assert res.status_code == 401
    body = res.get_json()
    assert body["success"] is False
    assert body["error"]["code"] == "WEB_APP_KEY_MISSING"
    assert body["error"]["retryable"] is False


def test_empty_header_treated_as_missing(client):
    res = client.get("/protected", headers={"X-Web-App-Key": ""})
    assert res.status_code == 401
    assert res.get_json()["error"]["code"] == "WEB_APP_KEY_MISSING"


# ---------- invalid format ----------


def test_malformed_key_returns_401_invalid(client):
    res = client.get("/protected", headers={"X-Web-App-Key": "not-a-grid-key"})
    assert res.status_code == 401
    assert res.get_json()["error"]["code"] == "WEB_APP_KEY_INVALID"


def test_wrong_prefix_returns_401_invalid(client):
    # Right length but wrong prefix
    res = client.get(
        "/protected",
        headers={"X-Web-App-Key": "wrong_AbCdEfGhIjKlMnOpQrStUv"},
    )
    assert res.status_code == 401
    assert res.get_json()["error"]["code"] == "WEB_APP_KEY_INVALID"


# ---------- well-formed but unknown ----------


def test_unknown_key_returns_401_unknown(client):
    from users.schema import generate_app_key

    res = client.get(
        "/protected", headers={"X-Web-App-Key": generate_app_key()}
    )
    assert res.status_code == 401
    body = res.get_json()
    assert body["error"]["code"] == "WEB_APP_KEY_UNKNOWN"
    assert body["error"]["retryable"] is False


# ---------- DB lookup failure ----------


def test_db_failure_returns_503_retryable(app, valid_key):
    """If the connection factory raises, the middleware should return 503
    with retryable=true rather than letting the exception propagate."""

    def broken_factory():
        raise RuntimeError("DB unreachable")

    from users.auth import require_web_user_auth

    flask_app = Flask(__name__)

    @flask_app.route("/probe")
    def probe():
        err = require_web_user_auth(broken_factory)
        if err is not None:
            return err
        return jsonify({"ok": True})

    res = flask_app.test_client().get(
        "/probe", headers={"X-Web-App-Key": valid_key}
    )
    assert res.status_code == 503
    body = res.get_json()
    assert body["error"]["code"] == "USER_LOOKUP_FAILED"
    assert body["error"]["retryable"] is True


# ---------- happy path ----------


def test_valid_key_passes_through(client, valid_key):
    res = client.get("/protected", headers={"X-Web-App-Key": valid_key})
    assert res.status_code == 200
    assert res.get_json() == {"ok": True}
