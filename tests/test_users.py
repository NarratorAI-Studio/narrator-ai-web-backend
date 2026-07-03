"""Tests for the reseller `users` table: schema, migration, key format,
and the create_user CLI helper.
"""

from __future__ import annotations

import runpy
import sqlite3
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text


sqlite3.register_adapter(Decimal, str)


REPO_ROOT = Path(__file__).resolve().parents[1]
MIGRATION_PATH = (
    REPO_ROOT / "migrations" / "versions" / "20260526_0001_create_users.py"
)


# ---------- key format ----------


def test_generate_app_key_matches_grid_prefix_and_length():
    from users.schema import KEY_PREFIX, KEY_RANDOM_LEN, generate_app_key

    for _ in range(50):
        key = generate_app_key()
        assert key.startswith(KEY_PREFIX)
        assert len(key) == len(KEY_PREFIX) + KEY_RANDOM_LEN


def test_generate_app_key_is_unique_under_repeated_calls():
    from users.schema import generate_app_key

    keys = {generate_app_key() for _ in range(1000)}
    assert len(keys) == 1000


def test_is_valid_app_key_accepts_generated_keys():
    from users.schema import generate_app_key, is_valid_app_key

    for _ in range(20):
        assert is_valid_app_key(generate_app_key())


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "grid_",
        "grid_short",
        "grid_TOOLONG" + "x" * 30,
        "GRID_" + "a" * 22,  # wrong prefix case
        "wrong_" + "a" * 22,
        "grid_" + "@" * 22,  # bad chars
        "grid_" + "a" * 21,  # off by one short
        "grid_" + "a" * 23,  # off by one long
    ],
)
def test_is_valid_app_key_rejects_malformed(bad: str):
    from users.schema import is_valid_app_key

    assert not is_valid_app_key(bad)


# ---------- schema & migration ----------


def test_users_schema_sql_defines_required_constraints():
    from users.schema import USERS_SCHEMA_SQL

    schema = USERS_SCHEMA_SQL
    assert "CREATE TABLE IF NOT EXISTS users" in schema
    assert "app_key TEXT PRIMARY KEY" in schema
    assert "balance_points NUMERIC(18, 2)" in schema
    assert "CHECK (balance_points >= 0)" in schema
    assert "DEFAULT 1000" in schema
    assert "created_at TIMESTAMPTZ NOT NULL" in schema


def test_alembic_create_users_migration_uses_shared_schema_sql():
    from users.schema import USERS_SCHEMA_SQL

    module = runpy.run_path(str(MIGRATION_PATH))

    class Op:
        def __init__(self):
            self.executed: list[str] = []

        def execute(self, sql):
            self.executed.append(sql)

    op = Op()
    module["upgrade"].__globals__["op"] = op
    module["upgrade"]()

    assert module["revision"] == "20260526_0001"
    assert module["down_revision"] == "20260525_0001"
    assert op.executed == [USERS_SCHEMA_SQL]


def test_alembic_create_users_downgrade_drops_table():
    module = runpy.run_path(str(MIGRATION_PATH))

    class Op:
        def __init__(self):
            self.executed: list[str] = []

        def execute(self, sql):
            self.executed.append(sql)

    op = Op()
    module["downgrade"].__globals__["op"] = op
    module["downgrade"]()

    assert op.executed == ["DROP TABLE IF EXISTS users"]


# ---------- CLI: create_user against in-memory sqlite ----------


def _users_schema_sql_for_sqlite() -> str:
    # Post-20260526_0002 schema. sqlite doesn't know NUMERIC width or
    # TIMESTAMPTZ — substitute portable equivalents; IDENTITY isn't
    # reachable here so `id` is plain INTEGER UNIQUE (tests using this
    # helper supply `id` explicitly when they care, or let it be NULL).
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
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as conn:
        conn.execute(text(_users_schema_sql_for_sqlite()))
    return engine


def test_create_user_inserts_row_with_default_balance(sqlite_engine):
    from scripts.create_user import create_user
    from users.schema import generate_app_key

    key = generate_app_key()
    create_user(sqlite_engine, key, Decimal("1000"))
    with sqlite_engine.begin() as conn:
        row = conn.execute(
            text("SELECT app_key, balance_points FROM users WHERE app_key = :k"),
            {"k": key},
        ).first()
    assert row is not None
    assert row[0] == key
    assert Decimal(row[1]) == Decimal("1000")


def test_create_user_rejects_duplicate_key(sqlite_engine):
    from scripts.create_user import create_user

    key = "grid_" + "a" * 22
    create_user(sqlite_engine, key, Decimal("500"))
    with pytest.raises(ValueError, match="already exists"):
        create_user(sqlite_engine, key, Decimal("999"))


def test_create_user_rejects_negative_balance(sqlite_engine):
    from scripts.create_user import create_user
    from users.schema import generate_app_key

    with pytest.raises(ValueError, match="balance must be >= 0"):
        create_user(sqlite_engine, generate_app_key(), Decimal("-1"))


def test_create_user_rejects_malformed_key(sqlite_engine):
    from scripts.create_user import create_user

    with pytest.raises(ValueError, match="does not match grid_"):
        create_user(sqlite_engine, "not-a-grid-key", Decimal("100"))


def test_create_user_persists_profile_fields_when_supplied(sqlite_engine):
    """Optional profile flags  round-trip into the row when given."""
    from scripts.create_user import create_user
    from users.schema import generate_app_key

    key = generate_app_key()
    create_user(
        sqlite_engine,
        key,
        Decimal("1000"),
        nickname="Demo User",
        mobile="13900000222",
        email="user@example.com",
        company_name="Example Inc",
    )
    with sqlite_engine.begin() as conn:
        row = conn.execute(
            text(
                "SELECT nickname, mobile, email, company_name "
                "FROM users WHERE app_key = :k"
            ),
            {"k": key},
        ).first()
    assert row == ("Demo User", "13900000222", "user@example.com", "Example Inc")


def test_create_user_leaves_profile_fields_null_by_default(sqlite_engine):
    """Existing zero-flag create_user call still works (backwards compat);
    optional fields land as NULL when not supplied."""
    from scripts.create_user import create_user
    from users.schema import generate_app_key

    key = generate_app_key()
    create_user(sqlite_engine, key, Decimal("1000"))
    with sqlite_engine.begin() as conn:
        row = conn.execute(
            text(
                "SELECT nickname, mobile, email, company_name "
                "FROM users WHERE app_key = :k"
            ),
            {"k": key},
        ).first()
    assert row == (None, None, None, None)
