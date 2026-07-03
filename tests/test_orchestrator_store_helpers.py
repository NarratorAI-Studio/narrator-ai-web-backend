"""Tests for the orchestrator-specific additions to narrator_tasks.store:

  - run_auto column writes alongside JSONB on create / upsert / replace
  - list_running_auto_tasks cross-tenant scan
  - _extract_run_auto coercion of the various truthy shapes

These complement (don't duplicate) test_narrator_tasks.py, which covers
the HTTP-route surface. The scope here is the store layer in isolation.
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy import create_engine, text

from narrator_tasks.store import (
    _extract_run_auto,
    create_task,
    list_running_auto_tasks,
    replace_task,
    upsert_task,
)


USERS_SCHEMA = """
CREATE TABLE users (
    app_key TEXT PRIMARY KEY,
    id INTEGER UNIQUE,
    balance_points NUMERIC NOT NULL DEFAULT 1000
);
"""

# Match the prod schema except for JSONB→TEXT (sqlite limitation) and
# TIMESTAMPTZ→TEXT (same).
NARRATOR_TASKS_SCHEMA = """
CREATE TABLE narrator_tasks (
    narrator_task_id TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    app_key TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    current_step TEXT,
    data TEXT NOT NULL,
    run_auto SMALLINT NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""

USER_A = ("key-alpha", 1)
USER_B = ("key-beta", 2)


@pytest.fixture()
def engine():
    eng = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}
    )
    with eng.begin() as conn:
        conn.execute(text(USERS_SCHEMA))
        conn.execute(text(NARRATOR_TASKS_SCHEMA))
        for app_key, user_id in (USER_A, USER_B):
            conn.execute(
                text(
                    "INSERT INTO users (app_key, id, balance_points) "
                    "VALUES (:k, :i, 1000)"
                ),
                {"k": app_key, "i": user_id},
            )
    yield eng
    eng.dispose()


# ─── _extract_run_auto coercion ─────────────────────────────────────────────


class TestExtractRunAuto:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            (1, 1),
            ("1", 1),
            ("1.0", 1),
            (True, 1),
            (0, 0),
            ("0", 0),
            (None, 0),
            ({}, 0),
            ("yes", 0),
            (2, 0),  # out-of-domain ints fall back to manual stepping
        ],
    )
    def test_canonical_forms(self, raw, expected):
        assert _extract_run_auto({"run_auto": raw}) == expected

    def test_missing_field_is_zero(self):
        assert _extract_run_auto({}) == 0


# ─── run_auto column writes in lockstep with JSONB ──────────────────────────


def _get_row(engine, task_id):
    with engine.connect() as conn:
        return conn.execute(
            text(
                "SELECT run_auto, data FROM narrator_tasks "
                "WHERE narrator_task_id = :t"
            ),
            {"t": task_id},
        ).first()


class TestRunAutoColumnWrites:
    def test_create_task_writes_run_auto_one(self, engine):
        with engine.begin() as conn:
            body = create_task(
                conn,
                user_id=USER_A[1],
                app_key=USER_A[0],
                body={"narrator_type": "playlet", "run_auto": 1},
            )
            row = _get_row(engine, body["narrator_task_id"])
            assert row.run_auto == 1
            assert json.loads(row.data)["run_auto"] == 1

    def test_create_task_defaults_run_auto_zero(self, engine):
        with engine.begin() as conn:
            body = create_task(
                conn,
                user_id=USER_A[1],
                app_key=USER_A[0],
                body={"narrator_type": "playlet"},
            )
            row = _get_row(engine, body["narrator_task_id"])
            assert row.run_auto == 0

    def test_replace_task_flips_run_auto(self, engine):
        with engine.begin() as conn:
            seeded = create_task(
                conn,
                user_id=USER_A[1],
                app_key=USER_A[0],
                body={"narrator_type": "playlet", "run_auto": 1, "status": "running"},
            )
            tid = seeded["narrator_task_id"]
            # Flip to manual mode via replace_task.
            seeded["run_auto"] = 0
            seeded["status"] = "paused"
            result = replace_task(
                conn,
                user_id=USER_A[1],
                app_key=USER_A[0],
                narrator_task_id=tid,
                body=seeded,
            )
            assert isinstance(result, dict)
            row = _get_row(engine, tid)
            assert row.run_auto == 0
            assert json.loads(row.data)["run_auto"] == 0

    def test_upsert_task_writes_run_auto(self, engine):
        # Pre-mint a task ID and upsert with run_auto=1.
        tid = "client-minted-id"
        with engine.begin() as conn:
            body = upsert_task(
                conn,
                user_id=USER_A[1],
                app_key=USER_A[0],
                body={
                    "narrator_task_id": tid,
                    "narrator_type": "playlet",
                    "run_auto": 1,
                    "status": "running",
                },
            )
            assert body is not None
            row = _get_row(engine, tid)
            assert row.run_auto == 1

    def test_upsert_task_update_path_overwrites_run_auto(self, engine):
        tid = "client-minted-id"
        with engine.begin() as conn:
            upsert_task(
                conn,
                user_id=USER_A[1],
                app_key=USER_A[0],
                body={
                    "narrator_task_id": tid,
                    "narrator_type": "playlet",
                    "run_auto": 1,
                    "status": "running",
                },
            )
            upsert_task(
                conn,
                user_id=USER_A[1],
                app_key=USER_A[0],
                body={
                    "narrator_task_id": tid,
                    "narrator_type": "playlet",
                    "run_auto": 0,
                    "status": "paused",
                },
            )
            row = _get_row(engine, tid)
            assert row.run_auto == 0


# ─── list_running_auto_tasks scan ───────────────────────────────────────────


class TestListRunningAutoTasks:
    def test_returns_only_running_with_run_auto_one(self, engine):
        with engine.begin() as conn:
            for body, expect_picked in [
                ({"status": "running", "run_auto": 1}, True),
                ({"status": "running", "run_auto": 0}, False),
                ({"status": "pending", "run_auto": 1}, False),
                ({"status": "paused", "run_auto": 1}, False),
                ({"status": "completed", "run_auto": 1}, False),
                ({"status": "failed", "run_auto": 1}, False),
            ]:
                created = create_task(
                    conn, user_id=USER_A[1], app_key=USER_A[0], body=body
                )
                # Cache narrator_task_id so we can assert below.
                body["_id"] = created["narrator_task_id"]
                body["_expect_picked"] = expect_picked

        with engine.connect() as conn:
            picked = list_running_auto_tasks(conn, limit=10)

        # Only one of the six rows above should land in the scan output.
        assert len(picked) == 1
        task_id, user_id, app_key = picked[0]
        assert user_id == USER_A[1]
        assert app_key == USER_A[0]

    def test_cross_tenant_scan_returns_both(self, engine):
        with engine.begin() as conn:
            create_task(
                conn,
                user_id=USER_A[1],
                app_key=USER_A[0],
                body={"status": "running", "run_auto": 1, "name": "task-A"},
            )
            create_task(
                conn,
                user_id=USER_B[1],
                app_key=USER_B[0],
                body={"status": "running", "run_auto": 1, "name": "task-B"},
            )

        with engine.connect() as conn:
            picked = list_running_auto_tasks(conn, limit=10)

        assert len(picked) == 2
        # Each tenant has exactly one row in the scan output.
        users = {tup[1] for tup in picked}
        assert users == {USER_A[1], USER_B[1]}
        # And the orchestrator can use each tuple's app_key for downstream
        # tenant-scoped store calls.
        keys = {tup[2] for tup in picked}
        assert keys == {USER_A[0], USER_B[0]}

    def test_oldest_updated_at_first(self, engine):
        with engine.begin() as conn:
            first = create_task(
                conn,
                user_id=USER_A[1],
                app_key=USER_A[0],
                body={"status": "running", "run_auto": 1, "name": "first"},
            )
            second = create_task(
                conn,
                user_id=USER_A[1],
                app_key=USER_A[0],
                body={"status": "running", "run_auto": 1, "name": "second"},
            )
            # Push `second.updated_at` later than `first.updated_at` so
            # the natural sort order is unambiguous on sqlite where
            # millisecond resolution can collide on rapid inserts.
            conn.execute(
                text(
                    "UPDATE narrator_tasks SET updated_at = :u "
                    "WHERE narrator_task_id = :t"
                ),
                {"u": "2999-01-01T00:00:00+00:00", "t": second["narrator_task_id"]},
            )

        with engine.connect() as conn:
            picked = list_running_auto_tasks(conn, limit=10)

        assert picked[0][0] == first["narrator_task_id"]
        assert picked[1][0] == second["narrator_task_id"]

    def test_limit_caps_results(self, engine):
        with engine.begin() as conn:
            for i in range(5):
                create_task(
                    conn,
                    user_id=USER_A[1],
                    app_key=USER_A[0],
                    body={"status": "running", "run_auto": 1, "name": f"t-{i}"},
                )

        with engine.connect() as conn:
            picked = list_running_auto_tasks(conn, limit=2)
        assert len(picked) == 2

    def test_empty_when_no_candidates(self, engine):
        with engine.begin() as conn:
            create_task(
                conn,
                user_id=USER_A[1],
                app_key=USER_A[0],
                body={"status": "pending", "run_auto": 1},
            )
        with engine.connect() as conn:
            assert list_running_auto_tasks(conn, limit=10) == []
