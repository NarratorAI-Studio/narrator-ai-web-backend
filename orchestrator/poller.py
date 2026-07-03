"""Periodic scan driver for the one-stop delivery orchestrator.

`scan_and_advance(engine)` is the BackgroundScheduler tick handler:
it acquires a Postgres advisory lock for single-leader semantics,
pulls a batch of run_auto=1 master tasks, and runs each through
`advance_one_task` in its own transaction.

Why advisory lock and not just CAS?
  - CAS in `replace_task` already prevents two writers from
    advancing the same step twice. That's the safety floor.
  - But CAS alone leaves two machines each making 20 upstream query
    calls per tick (~40 wasted req/30s). The advisory lock makes
    only one machine drive a given tick; the other returns
    immediately. Leader changes naturally if the lock holder dies
    (TCP closes → Postgres releases the lock → next tick whoever
    wins try_lock takes over).

The lock key is module-private and tied to this scan job alone — any
other scan / cron / one-off should pick its own key from a different
range to avoid coincidental contention.
"""

from __future__ import annotations

import logging
import os
from typing import Iterable

from sqlalchemy import text
from sqlalchemy.engine import Engine

from narrator_tasks.store import list_running_auto_tasks

from .advance import AdvanceOutcome, advance_one_task


logger = logging.getLogger(__name__)


# Identifier reserved for this poller's single-leader advisory lock.
# 482 mirrors the originating web issue; the trailing _001 leaves room
# for future per-scan locks under the same numeric prefix.
ADVISORY_LOCK_KEY = 482_001
DEFAULT_UPSTREAM_TIMEOUT_SECONDS = 60.0


def scan_and_advance(engine: Engine) -> dict[str, int]:
    """Run one scan tick.

    Returns a small dict of per-outcome counters useful for logging
    / Prometheus emission later. Caller (BackgroundScheduler) ignores
    the return value but the test harness asserts on it.

    Lifecycle:
      1. Acquire `pg_try_advisory_lock(ADVISORY_LOCK_KEY)`. Non-leader
         returns the same all-zero counters dict.
      2. Read a batch of task identifiers via
         `list_running_auto_tasks` — short transaction, releases
         immediately.
      3. For each `(task_id, user_id, app_key)`, run `advance_one_task`
         in its own engine.begin() transaction. Per-task exception
         is caught + logged so one bad task doesn't take down the
         scan.
      4. Release the advisory lock.
    """
    counters: dict[str, int] = {outcome.value: 0 for outcome in AdvanceOutcome}
    counters["error"] = 0
    counters["leader"] = 0

    batch_size = int(os.environ.get("ORCHESTRATOR_BATCH_SIZE", "20"))
    upstream_timeout = float(
        os.environ.get(
            "ORCHESTRATOR_UPSTREAM_TIMEOUT_SECONDS",
            str(DEFAULT_UPSTREAM_TIMEOUT_SECONDS),
        )
    )

    # ── leader election ────────────────────────────────────────────
    # pg_try_advisory_lock returns immediately (no waiting). It's a
    # session-level lock — released when the connection closes, even
    # if we crash before reaching pg_advisory_unlock. We still
    # release explicitly for clarity and to free the lock between
    # ticks (so the leader doesn't monopolize across scheduler
    # restarts within the same connection lifetime).
    with engine.connect() as conn:
        got_lock = _try_acquire_lock(conn)
        if not got_lock:
            return counters
        counters["leader"] = 1

        try:
            rows = list_running_auto_tasks(conn, limit=batch_size)
        finally:
            # Release before fanning out — we don't need the lock held
            # while each task does its own upstream + DB work; the CAS
            # in replace_task is the safety floor against the unlikely
            # case where another machine wins the next tick before we
            # finish.
            _release_lock(conn)

    # ── per-task advance ────────────────────────────────────────────
    for task_id, user_id, app_key in rows:
        try:
            result = advance_one_task(
                engine,
                narrator_task_id=task_id,
                user_id=user_id,
                app_key=app_key,
                upstream_timeout_seconds=upstream_timeout,
            )
            counters[result.outcome.value] += 1
        except Exception:  # noqa: BLE001
            logger.exception(
                "advance_one_task raised for task=%s user=%s",
                task_id, user_id,
            )
            counters["error"] += 1

    logger.info("scan tick complete: %s", _summarize(counters))
    return counters


def _try_acquire_lock(conn) -> bool:
    """`pg_try_advisory_lock` is Postgres-only. On sqlite (tests) the
    function doesn't exist; treat absence as "got the lock" so unit
    tests that don't care about leader election can still exercise the
    scan path."""
    dialect = conn.dialect.name
    if dialect != "postgresql":
        return True
    try:
        return bool(
            conn.execute(
                text("SELECT pg_try_advisory_lock(:k)"),
                {"k": ADVISORY_LOCK_KEY},
            ).scalar_one()
        )
    except Exception:  # noqa: BLE001
        logger.exception("pg_try_advisory_lock failed")
        return False


def _release_lock(conn) -> None:
    dialect = conn.dialect.name
    if dialect != "postgresql":
        return
    try:
        conn.execute(
            text("SELECT pg_advisory_unlock(:k)"),
            {"k": ADVISORY_LOCK_KEY},
        )
    except Exception:  # noqa: BLE001
        logger.exception("pg_advisory_unlock failed")


def _summarize(counters: dict[str, int]) -> str:
    """Render a compact `leader=1 advanced=3 in_progress=12 ...` line —
    nicer for logs than the full counter dict repr."""
    parts: Iterable[str] = (
        f"{k}={v}" for k, v in counters.items() if v
    )
    return " ".join(parts) or "noop"
