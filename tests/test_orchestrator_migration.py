"""Smoke tests for migration 20260611_0001 (run_auto top-level column).

Loads the migration script directly and applies its upgrade against a
sqlite engine seeded with the pre-migration narrator_tasks shape. The
goal is to lock the migration's net effect (column appears, partial
index is creatable, downgrade reverses) so a future refactor that
breaks the alembic op call surfaces in CI.

Postgres-specific behaviour (JSONB backfill) lives in a separate
branch in the script, gated by dialect; we only exercise the
dialect-agnostic path here.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect, text


PRE_MIGRATION_SCHEMA = """
CREATE TABLE users (
    app_key TEXT PRIMARY KEY,
    id INTEGER UNIQUE,
    balance_points NUMERIC NOT NULL DEFAULT 1000
);

CREATE TABLE narrator_tasks (
    narrator_task_id TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    app_key TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    current_step TEXT,
    data TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


def _load_migration_module():
    """Import the migration script by path. Alembic migrations are
    typically run via env.py, but for a unit test we just want the
    upgrade/downgrade functions in isolation."""
    repo_root = Path(__file__).resolve().parents[1]
    path = (
        repo_root / "migrations" / "versions"
        / "20260611_0001_narrator_tasks_run_auto.py"
    )
    spec = importlib.util.spec_from_file_location(
        "_migration_run_auto", str(path)
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def engine():
    eng = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}
    )
    with eng.begin() as conn:
        for stmt in PRE_MIGRATION_SCHEMA.strip().split(";"):
            if stmt.strip():
                conn.execute(text(stmt))
    yield eng
    eng.dispose()


def _apply_upgrade(engine, migration_module):
    """Bind alembic Operations to a single connection and run upgrade.
    Mirrors what `alembic upgrade head` does for a single revision."""
    with engine.begin() as conn:
        ctx = MigrationContext.configure(conn)
        operators = Operations(ctx)
        # Bind the module's `op` reference to our scoped operators so it
        # writes to our test connection.
        original_op = migration_module.op
        migration_module.op = operators
        try:
            migration_module.upgrade()
        finally:
            migration_module.op = original_op


def _apply_downgrade(engine, migration_module):
    with engine.begin() as conn:
        ctx = MigrationContext.configure(conn)
        operators = Operations(ctx)
        original_op = migration_module.op
        migration_module.op = operators
        try:
            migration_module.downgrade()
        finally:
            migration_module.op = original_op


class TestUpgrade:
    def test_run_auto_column_added(self, engine):
        module = _load_migration_module()
        _apply_upgrade(engine, module)

        inspector = inspect(engine)
        cols = {c["name"]: c for c in inspector.get_columns("narrator_tasks")}
        assert "run_auto" in cols
        # SQLite reports type as the storage class string; just make
        # sure it's an integer-class column.
        assert "INT" in cols["run_auto"]["type"].compile().upper()

    def test_partial_index_created(self, engine):
        module = _load_migration_module()
        _apply_upgrade(engine, module)

        inspector = inspect(engine)
        indexes = inspector.get_indexes("narrator_tasks")
        names = {idx["name"] for idx in indexes}
        assert "ix_narrator_tasks_running_auto" in names

    def test_existing_rows_default_to_zero(self, engine):
        # Seed a row pre-migration with run_auto=1 in the data blob but
        # no top-level column. After upgrade the new column should be
        # 0 (default) on sqlite (the Postgres-only backfill branch
        # isn't exercised here).
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO users (app_key, id) VALUES ('k', 1)"
                )
            )
            conn.execute(
                text(
                    "INSERT INTO narrator_tasks "
                    "(narrator_task_id, user_id, app_key, status, data, "
                    "created_at, updated_at) "
                    "VALUES ('t1', 1, 'k', 'running', "
                    "'{\"run_auto\": 1}', 'now', 'now')"
                )
            )

        module = _load_migration_module()
        _apply_upgrade(engine, module)

        with engine.connect() as conn:
            run_auto = conn.execute(
                text(
                    "SELECT run_auto FROM narrator_tasks "
                    "WHERE narrator_task_id = 't1'"
                )
            ).scalar_one()
        assert run_auto == 0  # sqlite path: no JSONB backfill


class TestDowngrade:
    def test_downgrade_removes_column_and_index(self, engine):
        module = _load_migration_module()
        _apply_upgrade(engine, module)
        _apply_downgrade(engine, module)

        inspector = inspect(engine)
        cols = {c["name"] for c in inspector.get_columns("narrator_tasks")}
        indexes = {idx["name"] for idx in inspector.get_indexes("narrator_tasks")}

        assert "run_auto" not in cols
        assert "ix_narrator_tasks_running_auto" not in indexes
