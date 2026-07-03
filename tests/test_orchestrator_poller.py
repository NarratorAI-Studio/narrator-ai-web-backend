"""Tests for orchestrator.poller.

Covers:
  - sqlite path treats absence of pg_try_advisory_lock as "got the
    lock," so the scan still drives advance on test rigs
  - per-task exception in advance_one_task is caught and counted,
    not allowed to break the batch
  - counters dict shape matches the outcome enum
  - Postgres advisory-lock path: returned False → noop counters
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, text

import orchestrator.poller as poller_module
from narrator_tasks.store import create_task
from orchestrator.advance import AdvanceOutcome, AdvanceResult


USERS_SCHEMA = """
CREATE TABLE users (
    app_key TEXT PRIMARY KEY,
    id INTEGER UNIQUE,
    balance_points NUMERIC NOT NULL DEFAULT 1000
);
"""

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

KEY_A = ("user-a", 1)
KEY_B = ("user-b", 2)


@pytest.fixture()
def engine():
    eng = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}
    )
    with eng.begin() as conn:
        conn.execute(text(USERS_SCHEMA))
        conn.execute(text(NARRATOR_TASKS_SCHEMA))
        for app_key, user_id in (KEY_A, KEY_B):
            conn.execute(
                text(
                    "INSERT INTO users (app_key, id, balance_points) "
                    "VALUES (:k, :i, 1000)"
                ),
                {"k": app_key, "i": user_id},
            )
    yield eng
    eng.dispose()


def _seed_running_auto(engine, *, user_id, app_key, current_step, task_id):
    with engine.begin() as conn:
        return create_task(
            conn,
            user_id=user_id,
            app_key=app_key,
            body={
                "status": "running",
                "run_auto": 1,
                "current_step": current_step,
                "steps": {current_step: {"task_id": task_id}},
            },
        )


# ─── sqlite leader path (treat as got the lock) ────────────────────────────


class TestSqliteLeaderPath:
    def test_empty_batch_returns_zero_counters(self, engine):
        counters = poller_module.scan_and_advance(engine)
        # leader=1 because sqlite path always "gets the lock"
        assert counters["leader"] == 1
        # All outcome buckets are zero — nothing was advanced.
        for outcome in AdvanceOutcome:
            assert counters[outcome.value] == 0
        assert counters["error"] == 0

    def test_one_task_advances_via_mocked_advance(self, engine, monkeypatch):
        _seed_running_auto(
            engine,
            user_id=KEY_A[1],
            app_key=KEY_A[0],
            current_step="popular_learning",
            task_id="rt-1",
        )

        seen_ids: list[str] = []
        seen_timeouts: list[float] = []

        def fake_advance(engine, *, narrator_task_id, user_id, app_key, upstream_timeout_seconds):
            seen_ids.append(narrator_task_id)
            seen_timeouts.append(upstream_timeout_seconds)
            return AdvanceResult(AdvanceOutcome.ADVANCED, next_step="generate_writing")

        monkeypatch.setattr(poller_module, "advance_one_task", fake_advance)
        counters = poller_module.scan_and_advance(engine)

        assert len(seen_ids) == 1
        assert seen_timeouts == [poller_module.DEFAULT_UPSTREAM_TIMEOUT_SECONDS]
        assert counters[AdvanceOutcome.ADVANCED.value] == 1
        assert counters["leader"] == 1

    def test_upstream_timeout_env_override_respected(self, engine, monkeypatch):
        _seed_running_auto(
            engine,
            user_id=KEY_A[1],
            app_key=KEY_A[0],
            current_step="popular_learning",
            task_id="rt-1",
        )

        seen_timeouts: list[float] = []

        def fake_advance(engine, *, narrator_task_id, user_id, app_key, upstream_timeout_seconds):
            seen_timeouts.append(upstream_timeout_seconds)
            return AdvanceResult(AdvanceOutcome.IN_PROGRESS)

        monkeypatch.setenv("ORCHESTRATOR_UPSTREAM_TIMEOUT_SECONDS", "12.5")
        monkeypatch.setattr(poller_module, "advance_one_task", fake_advance)

        poller_module.scan_and_advance(engine)

        assert seen_timeouts == [12.5]

    def test_per_task_exception_isolated(self, engine, monkeypatch):
        # Two seed tasks: first one will throw, second one advances.
        _seed_running_auto(
            engine,
            user_id=KEY_A[1],
            app_key=KEY_A[0],
            current_step="popular_learning",
            task_id="rt-1",
        )
        _seed_running_auto(
            engine,
            user_id=KEY_B[1],
            app_key=KEY_B[0],
            current_step="popular_learning",
            task_id="rt-2",
        )

        first_seen = []

        def fake_advance(engine, *, narrator_task_id, user_id, app_key, upstream_timeout_seconds):
            first_seen.append(narrator_task_id)
            if len(first_seen) == 1:
                raise RuntimeError("simulated crash inside advance_one_task")
            return AdvanceResult(AdvanceOutcome.ADVANCED)

        monkeypatch.setattr(poller_module, "advance_one_task", fake_advance)
        counters = poller_module.scan_and_advance(engine)

        # Both tasks were processed; first errored, second advanced.
        assert len(first_seen) == 2
        assert counters["error"] == 1
        assert counters[AdvanceOutcome.ADVANCED.value] == 1

    def test_batch_size_respected(self, engine, monkeypatch):
        for i in range(5):
            _seed_running_auto(
                engine,
                user_id=KEY_A[1],
                app_key=KEY_A[0],
                current_step="popular_learning",
                task_id=f"rt-{i}",
            )

        seen = []
        monkeypatch.setenv("ORCHESTRATOR_BATCH_SIZE", "2")
        monkeypatch.setattr(
            poller_module,
            "advance_one_task",
            lambda engine, **kw: seen.append(kw["narrator_task_id"])
            or AdvanceResult(AdvanceOutcome.IN_PROGRESS),
        )
        poller_module.scan_and_advance(engine)
        assert len(seen) == 2


# ─── advisory lock leader-election semantics ──────────────────────────────


class TestAdvisoryLockBehavior:
    def test_lock_not_acquired_skips_scan(self, engine, monkeypatch):
        # Simulate the non-leader case: _try_acquire_lock returns False.
        called = []

        def fake_advance(engine, **kw):
            called.append(kw["narrator_task_id"])
            return AdvanceResult(AdvanceOutcome.ADVANCED)

        monkeypatch.setattr(poller_module, "_try_acquire_lock", lambda conn: False)
        monkeypatch.setattr(poller_module, "advance_one_task", fake_advance)

        _seed_running_auto(
            engine,
            user_id=KEY_A[1],
            app_key=KEY_A[0],
            current_step="popular_learning",
            task_id="rt-1",
        )

        counters = poller_module.scan_and_advance(engine)

        # Non-leader: leader counter is 0, no advance called.
        assert counters["leader"] == 0
        assert called == []
        assert counters[AdvanceOutcome.ADVANCED.value] == 0
