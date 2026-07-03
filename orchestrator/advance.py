"""Single-task advance routine for the one-stop delivery orchestrator.

1:1 port of narrator-ai-web `sync/route.ts:230-391` (the POST handler
that drives auto-advance when the browser is the caller). The web side
mutates a client-supplied task object and writes it back via dbCasUpdate
+ dbReplaceTask; the backend side reads the task from the store with
the row's own tenant identity and writes it back via store.replace_task
with the same CAS pre-conditions.

Sync (Flask + urllib + threadpool). No asyncio — the upstream client is
sync (`proxy_narrator_upstream`) and the BackgroundScheduler executes
this on a worker thread, so there's no event loop to share.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any
from urllib.parse import quote

from sqlalchemy.engine import Engine

from narrator_metadata.upstream import UpstreamNarratorError
from narrator_proxy.upstream import proxy_narrator_upstream
from narrator_tasks.store import CAS_MISMATCH, get_task, replace_task
from pricing_quote_v2.auto_refund import apply_auto_refund_if_eligible

from .state_machine import extract_step_result, resolve_next_step
from .triggers import (
    DEFAULT_UPSTREAM_TIMEOUT_SECONDS,
    TriggerResult,
    trigger_next_step,
)


logger = logging.getLogger(__name__)


# Upstream query path per step. Subtitle steps live under
# `/v2/task/{ocr_extraction,subtitle_removal}/query/...`; everything
# else is the commentary task query endpoint.
_QUERY_PATHS: dict[str, str] = {
    "subtitle_extract": "/v2/task/ocr_extraction/query/{task_id}",
    "subtitle_removal": "/v2/task/subtitle_removal/query/{task_id}",
}
_DEFAULT_QUERY_PATH = "/v2/task/commentary/query/{task_id}"

# Upstream task lifecycle statuses (mirrors sync/route.ts:294-307).
# 0 = queued, 1 = running, 2 = success, 3 = failed, 4 = cancelled.
_IN_FLIGHT = (0, 1)
_FAILED = (3, 4)
_SUCCESS = 2

# replace_task persist retry budget. Web side uses 3 (sync/route.ts:362);
# keep parity so the failure modes line up.
_PERSIST_RETRIES = 3


class AdvanceOutcome(str, Enum):
    """Coarse outcome class for poller-level logging / metrics.

    Granular enough to distinguish "nothing happened" from "an upstream
    error settled the master task," fine-grained reasons live on
    AdvanceResult.detail."""

    NOOP = "noop"               # No remote_task_id yet, terminal state, etc.
    IN_PROGRESS = "in_progress" # Upstream status ∈ (0, 1).
    ADVANCED = "advanced"       # Triggered next step successfully.
    COMPLETED = "completed"     # Pipeline reached video_composing → None.
    STEP_FAILED = "step_failed" # Upstream returned 3/4 or trigger failed.
    CAS_MISS = "cas_miss"       # Another writer claimed the step.
    PERSIST_FAILED = "persist_failed"  # CAS+trigger ok but final write lost.


@dataclass(frozen=True)
class AdvanceResult:
    outcome: AdvanceOutcome
    detail: str = ""
    next_step: str | None = None


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _patch_step(task: dict, step: str, step_patch: dict, master_patch: dict | None = None) -> dict:
    """Pure in-memory equivalent of sync/route.ts:24-35 applyStepUpdate.

    Returns a new task dict — does not mutate inputs. `master_patch`
    fields land at the task top level (status / current_step /
    error_message); `step_patch` fields land inside
    `task.steps[step]`.
    """
    steps = dict(task.get("steps") or {})
    existing_step = dict(steps.get(step) or {})
    existing_step.update(step_patch)
    steps[step] = existing_step
    out = {**task, "steps": steps, "updated_at": _iso_now()}
    if master_patch:
        out.update(master_patch)
    return out


def _unwrap_upstream_data(resp: Any) -> dict:
    """Upstream commentary/query wraps in `{code, message, data: {...}}`.
    sync/route.ts reads `qj.data?.status` where qj is the BFF response
    that already unwrapped one level; here we unwrap directly from the
    upstream payload. Falls back to the raw response if the upstream
    skipped the wrapping (defensive — current commentary upstreams
    always wrap)."""
    if not isinstance(resp, dict):
        return {}
    data = resp.get("data")
    if isinstance(data, dict):
        return data
    return resp


def _extract_remote_status(remote_data: dict) -> int:
    """Mirrors sync/route.ts:294 `qj.data?.status ?? qj.data?.task_status ?? -1`.
    `-1` flags "couldn't read a status field" so the caller can short
    out without misreading a missing value as IN_FLIGHT."""
    s = remote_data.get("status")
    if isinstance(s, int):
        return s
    t = remote_data.get("task_status")
    if isinstance(t, int):
        return t
    return -1


def advance_one_task(
    engine: Engine,
    *,
    narrator_task_id: str,
    user_id: int,
    app_key: str,
    upstream_timeout_seconds: float = DEFAULT_UPSTREAM_TIMEOUT_SECONDS,
) -> AdvanceResult:
    """Try to advance a single master task by one step.

    Same contract as the web `sync/route.ts` POST handler for run_auto=1
    tasks: query the upstream of the current step, decide what to do
    (continue / fail / advance), and persist. Idempotent — calling this
    repeatedly on the same task is safe (CAS dedups the claim, the
    "next step already triggered" short-circuit handles re-runs after
    a successful trigger but before persist).

    Returns an `AdvanceResult` for the poller's logging. Does NOT raise
    on upstream errors / step failures — those settle the master task
    as failed and return STEP_FAILED. Unexpected exceptions DO
    propagate so the poller's per-task try/except catches them and
    moves on to the next task without taking the whole scan down.
    """
    with engine.begin() as conn:
        task = get_task(
            conn, user_id=user_id, narrator_task_id=narrator_task_id
        )
        if task is None:
            return AdvanceResult(AdvanceOutcome.NOOP, detail="task not found")

        if task.get("status") in ("completed", "failed"):
            return AdvanceResult(
                AdvanceOutcome.NOOP, detail=f"master {task['status']}"
            )

        # run_auto column is the scan filter; double-check inside the
        # txn to keep this routine usable from non-scanner callers.
        if task.get("run_auto") != 1:
            return AdvanceResult(
                AdvanceOutcome.NOOP, detail="run_auto != 1"
            )

        current_step = task.get("current_step")
        if not current_step or current_step == "completed":
            return AdvanceResult(
                AdvanceOutcome.NOOP, detail="no current_step"
            )

        step_record = (task.get("steps") or {}).get(current_step) or {}
        remote_task_id = step_record.get("task_id")
        if not remote_task_id:
            return AdvanceResult(
                AdvanceOutcome.NOOP,
                detail=f"step {current_step} has no upstream task_id yet",
            )

        # Short-circuit: web sync.ts:263-279. If the step is already
        # locally marked completed, just walk current_step forward
        # without re-querying upstream.
        if step_record.get("status") == "completed":
            return _short_circuit_advance(
                conn,
                task=task,
                current_step=current_step,
                user_id=user_id,
                app_key=app_key,
                narrator_task_id=narrator_task_id,
                upstream_timeout_seconds=upstream_timeout_seconds,
            )

        # ── upstream query ──────────────────────────────────────────
        # URL-encode remote_task_id before interpolation. The value
        # comes from the persisted JSON blob (`steps.<step>.task_id`),
        # which is set by upstream `create_*` responses today but could
        # in principle be poisoned via an authenticated write to the
        # task body. A crafted id containing `?`, `&`, `/` etc. would
        # otherwise rewrite the upstream URL's path or query under the
        # backend's master OPEN_FASTAPI_APP_KEY identity. `safe=""`
        # encodes every reserved char including slash.
        query_path = _QUERY_PATHS.get(current_step, _DEFAULT_QUERY_PATH).format(
            task_id=quote(str(remote_task_id), safe="")
        )
        try:
            raw = proxy_narrator_upstream(
                query_path,
                method="GET",
                timeout_seconds=upstream_timeout_seconds,
            )
        except UpstreamNarratorError as exc:
            logger.warning(
                "upstream query failed for task %s step %s: %s",
                narrator_task_id, current_step, exc,
            )
            return AdvanceResult(
                AdvanceOutcome.NOOP,
                detail=f"upstream query failed: {exc.code}",
            )

        remote_data = _unwrap_upstream_data(raw)
        remote_status = _extract_remote_status(remote_data)

        if remote_status in _IN_FLIGHT:
            return AdvanceResult(
                AdvanceOutcome.IN_PROGRESS,
                detail=f"upstream status={remote_status}",
            )

        if remote_status in _FAILED:
            err_msg = (
                f"远程任务 {remote_task_id} 状态: "
                f"{'失败' if remote_status == 3 else '已取消'}"
            )
            failed_task = _patch_step(
                task,
                current_step,
                {
                    "status": "failed",
                    "error": err_msg,
                    "completed_at": _iso_now(),
                },
                master_patch={"status": "failed", "error_message": err_msg},
            )
            _persist_with_retries(
                conn,
                narrator_task_id=narrator_task_id,
                user_id=user_id,
                app_key=app_key,
                body=failed_task,
            )
            return AdvanceResult(
                AdvanceOutcome.STEP_FAILED, detail=err_msg
            )

        if remote_status != _SUCCESS:
            # -1 (couldn't parse) or an unknown status code: treat as in
            # progress rather than fail-marking the task — the next
            # tick will retry. Web side handles the same way implicitly
            # since it only branches on 0/1/3/4 vs else.
            return AdvanceResult(
                AdvanceOutcome.NOOP,
                detail=f"unrecognized upstream status={remote_status}",
            )

        # ── upstream succeeded → record result, decide next step ──
        step_result = extract_step_result(current_step, remote_data)
        task = _patch_step(
            task,
            current_step,
            {
                "status": "completed",
                "completed_at": _iso_now(),
                "result": step_result,
            },
        )

        next_step = resolve_next_step(current_step, task)
        if next_step is None:
            terminal = {**task, "status": "completed", "current_step": "completed",
                        "updated_at": _iso_now()}
            _persist_with_retries(
                conn,
                narrator_task_id=narrator_task_id,
                user_id=user_id,
                app_key=app_key,
                body=terminal,
            )
            return AdvanceResult(AdvanceOutcome.COMPLETED)

        # ── CAS-claim the advance ───────────────────────────────────
        # If web sync or another scheduler instance already advanced
        # this task, current_step in DB no longer matches current_step
        # in memory → CAS_MISMATCH → skip.
        existing_next = (task.get("steps") or {}).get(next_step) or {}
        if (
            existing_next.get("task_id")
            or existing_next.get("status") in ("running", "completed")
        ):
            # Someone (web sync or another orchestrator pass) already
            # triggered next_step; just move the pointer.
            advanced_task = {
                **task, "current_step": next_step, "updated_at": _iso_now()
            }
            _persist_with_retries(
                conn,
                narrator_task_id=narrator_task_id,
                user_id=user_id,
                app_key=app_key,
                body=advanced_task,
            )
            return AdvanceResult(
                AdvanceOutcome.ADVANCED,
                detail="next_step already triggered",
                next_step=next_step,
            )

        advancing = _patch_step(
            task,
            next_step,
            {"status": "running", "started_at": _iso_now()},
            master_patch={"current_step": next_step},
        )
        claim = replace_task(
            conn,
            user_id=user_id,
            app_key=app_key,
            narrator_task_id=narrator_task_id,
            body=advancing,
            expected_status=("running", "paused", "pending"),
            expected_step=current_step,
        )
        if claim == CAS_MISMATCH:
            return AdvanceResult(
                AdvanceOutcome.CAS_MISS,
                detail="another writer advanced the step",
            )
        if claim is None:
            # Row vanished or cross-tenant — treat as noop, log.
            logger.warning(
                "claim failed (None) for task %s step %s",
                narrator_task_id, current_step,
            )
            return AdvanceResult(
                AdvanceOutcome.NOOP, detail="row missing on claim"
            )

        # ── trigger next step's upstream task ────────────────────────
        # The CAS-claim is the source of truth that we own this step
        # now. Even if the trigger fails the row stays at
        # current_step=next_step,status=running with no task_id — we
        # do NOT roll back, identical to sync/route.ts:347-352. A
        # human or retry path reconciles.
        trigger_outcome = trigger_next_step(
            next_step, claim, timeout_seconds=upstream_timeout_seconds
        )

        if trigger_outcome.success:
            final = _patch_step(
                claim,
                next_step,
                {"status": "running", "task_id": trigger_outcome.task_id},
            )
            _persist_with_retries(
                conn,
                narrator_task_id=narrator_task_id,
                user_id=user_id,
                app_key=app_key,
                body=final,
            )
            return AdvanceResult(
                AdvanceOutcome.ADVANCED, next_step=next_step
            )

        # Trigger failed — settle the master task as failed; the step
        # already shows the failure detail so the operator knows where.
        final = _patch_step(
            claim,
            next_step,
            {"status": "failed", "error": trigger_outcome.error},
            master_patch={
                "status": "failed",
                "error_message": trigger_outcome.error,
            },
        )
        _persist_with_retries(
            conn,
            narrator_task_id=narrator_task_id,
            user_id=user_id,
            app_key=app_key,
            body=final,
        )
        return AdvanceResult(
            AdvanceOutcome.STEP_FAILED,
            detail=trigger_outcome.error or "trigger failed",
            next_step=next_step,
        )


def _short_circuit_advance(
    conn,
    *,
    task: dict,
    current_step: str,
    user_id: int,
    app_key: str,
    narrator_task_id: str,
    upstream_timeout_seconds: float,
) -> AdvanceResult:
    """sync/route.ts:263-279 short-circuit. The current step is already
    marked completed locally; either the master task is done or we
    just need to walk current_step forward. No upstream query needed.

    Kept as a separate function to keep advance_one_task's main
    branch readable — the short-circuit's response-mapping is fiddly
    enough on its own.
    """
    next_step = resolve_next_step(current_step, task)
    if next_step is None:
        terminal = {**task, "status": "completed", "current_step": "completed",
                    "updated_at": _iso_now()}
        _persist_with_retries(
            conn,
            narrator_task_id=narrator_task_id,
            user_id=user_id,
            app_key=app_key,
            body=terminal,
        )
        return AdvanceResult(AdvanceOutcome.COMPLETED)

    next_step_record = (task.get("steps") or {}).get(next_step) or {}
    if (
        next_step_record.get("task_id")
        or next_step_record.get("status") in ("running", "completed")
    ):
        advanced = {
            **task, "current_step": next_step, "updated_at": _iso_now()
        }
        _persist_with_retries(
            conn,
            narrator_task_id=narrator_task_id,
            user_id=user_id,
            app_key=app_key,
            body=advanced,
        )
        return AdvanceResult(
            AdvanceOutcome.ADVANCED,
            detail="next_step already triggered (short-circuit)",
            next_step=next_step,
        )

    # The locally-completed-but-next-step-untriggered case is rare
    # (would mean a previous advance succeeded on current_step but
    # failed to trigger next_step and didn't roll forward). Re-enter
    # the main path on the next tick by claiming + triggering here.
    advancing = _patch_step(
        task,
        next_step,
        {"status": "running", "started_at": _iso_now()},
        master_patch={"current_step": next_step},
    )
    claim = replace_task(
        conn,
        user_id=user_id,
        app_key=app_key,
        narrator_task_id=narrator_task_id,
        body=advancing,
        expected_status=("running", "paused", "pending"),
        expected_step=current_step,
    )
    if claim == CAS_MISMATCH:
        return AdvanceResult(
            AdvanceOutcome.CAS_MISS, detail="short-circuit CAS miss"
        )
    if claim is None:
        return AdvanceResult(
            AdvanceOutcome.NOOP, detail="row missing on short-circuit claim"
        )

    trigger_outcome: TriggerResult = trigger_next_step(
        next_step, claim, timeout_seconds=upstream_timeout_seconds
    )
    if trigger_outcome.success:
        final = _patch_step(
            claim,
            next_step,
            {"status": "running", "task_id": trigger_outcome.task_id},
        )
        _persist_with_retries(
            conn,
            narrator_task_id=narrator_task_id,
            user_id=user_id,
            app_key=app_key,
            body=final,
        )
        return AdvanceResult(AdvanceOutcome.ADVANCED, next_step=next_step)

    final = _patch_step(
        claim,
        next_step,
        {"status": "failed", "error": trigger_outcome.error},
        master_patch={
            "status": "failed",
            "error_message": trigger_outcome.error,
        },
    )
    _persist_with_retries(
        conn,
        narrator_task_id=narrator_task_id,
        user_id=user_id,
        app_key=app_key,
        body=final,
    )
    return AdvanceResult(
        AdvanceOutcome.STEP_FAILED,
        detail=trigger_outcome.error or "trigger failed",
        next_step=next_step,
    )


def _persist_with_retries(
    conn,
    *,
    narrator_task_id: str,
    user_id: int,
    app_key: str,
    body: dict,
) -> bool:
    """Mirror sync/route.ts:361-373 — `for i in range(3): if write() break`.

    No CAS here; the caller has already claimed via expected_step.
    Returns True on persist success. On failure logs an error
    (orchestrator runs without a user-facing response channel, so
    there's nothing to return to a client — the next scan tick will
    pick the task up again and reconcile via the upstream query
    path).

    Fail-fast auto-refund hook (regression coverage bug 2): when the body settles a
    master task as `status=failed`, the same transaction runs
    `apply_auto_refund_if_eligible` so orchestrator-driven failures
    receive the same case-2 auto-refund treatment as the PUT
    `/narrator/tasks/<id>` route (review). Detector decides whether
    the refund actually fires; non-matching shapes (later step failed
    with task_id, etc.) stay manual. Re-raises SQLAlchemyError so the
    outer `engine.begin()` transaction rolls back the persist + the
    half-applied refund together — same atomicity guarantee review
    enforced on the route path.
    """
    for attempt in range(_PERSIST_RETRIES):
        result = replace_task(
            conn,
            user_id=user_id,
            app_key=app_key,
            narrator_task_id=narrator_task_id,
            body=body,
        )
        if isinstance(result, dict):
            if body.get("status") == "failed":
                apply_auto_refund_if_eligible(
                    conn,
                    narrator_task_id=narrator_task_id,
                    user_id=user_id,
                    body=body,
                )
            return True
        # CAS_MISMATCH shouldn't happen without preconditions, but if
        # the row went missing (None) we can't recover by retrying.
        if result is None:
            logger.error(
                "persist failed (row missing) task=%s attempt=%d",
                narrator_task_id, attempt + 1,
            )
            return False
    logger.error(
        "persist failed after %d retries task=%s body_status=%s",
        _PERSIST_RETRIES, narrator_task_id, body.get("status"),
    )
    return False
