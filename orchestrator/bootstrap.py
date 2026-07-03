"""APScheduler wiring for the orchestrator poller.

Kept in its own module so `server.py` can call a single function in
its boot path. The Flask app object isn't strictly required — the
scheduler holds onto the engine reference directly — but accepting it
matches the doc's bootstrap signature and lets future hooks attach to
app.config without changing the call site.
"""

from __future__ import annotations

import atexit
import logging
import os

from sqlalchemy.engine import Engine


logger = logging.getLogger(__name__)


def start_orchestrator(engine: Engine):
    """Start the BackgroundScheduler iff ORCHESTRATOR_ENABLED=true.

    Default-off: PR 2 ships the orchestrator into prod-running code
    but idle. PR 3 (Phase 5/6) flips the env var to begin the shadow
    period. The early return when disabled means PR 2 has zero
    behavioral effect on existing routes — the only diff is a new
    module on disk plus the run_auto column we added in PR 2's
    alembic migration.

    Returns the scheduler instance on success (so callers can attach
    it to app.state for introspection), or None when disabled / the
    APScheduler dependency is missing.
    """
    if os.environ.get("ORCHESTRATOR_ENABLED", "false").lower() != "true":
        logger.info("orchestrator disabled (ORCHESTRATOR_ENABLED != true)")
        return None

    # Import APScheduler lazily so the dependency only matters when
    # the feature is enabled. Lets us roll out the new requirements
    # entry without forcing every dev environment to refresh.
    try:
        from apscheduler.schedulers.background import BackgroundScheduler
    except ImportError:
        logger.exception(
            "ORCHESTRATOR_ENABLED=true but APScheduler is not installed; "
            "add `APScheduler>=3.10,<4.0` to requirements.txt and reinstall"
        )
        return None

    from .poller import scan_and_advance

    interval_seconds = int(
        os.environ.get("ORCHESTRATOR_INTERVAL_SECONDS", "30")
    )

    scheduler = BackgroundScheduler(
        executors={
            "default": {"type": "threadpool", "max_workers": 5},
        }
    )
    scheduler.add_job(
        lambda: scan_and_advance(engine),
        trigger="interval",
        seconds=interval_seconds,
        id="scan_and_advance",
        # Don't queue overlapping ticks if a scan ran long.
        max_instances=1,
        # If we miss ticks during a slow scan, collapse them — one
        # extra fire after a long stall is enough.
        coalesce=True,
    )
    scheduler.start()

    # `wait=True` lets in-flight ticks finish on shutdown so we don't
    # leave a half-persisted task. APScheduler's atexit handler does
    # this by default but being explicit means tests and other entry
    # points get the same shutdown semantics.
    atexit.register(lambda: scheduler.shutdown(wait=True))

    logger.info(
        "orchestrator started: interval=%ds advisory_lock_key=482001",
        interval_seconds,
    )
    return scheduler
