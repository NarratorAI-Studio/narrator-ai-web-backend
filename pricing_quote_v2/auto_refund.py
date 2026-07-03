"""Fail-fast auto-refund hook for template price v2 master tasks (regression coverage /
Web API contract § 方案 D · case 2).

Trigger: a PUT to /v1/narrator/tasks/<id> moves a v2 master task into
`status=failed` with hard evidence that no upstream task was ever
created (the first subflow was rejected before any `task_id` came
back, and no subtask has produced output). Under V2's atomic-charge
model the user has already been debited the snapshot's
`final_charge_price`; without this hook the only recovery path is
manual customer service (see Web API contract for catalyst).

Conservative by design — every other failure shape (timeout / first
subflow returned a task_id / later subflow failed) stays manual and
goes through the V2 `refund_policy='manual'` path. Future state-machine
work (case 3-5 of the § 方案 D table) lands as separate issues.

Gated by env var `AUTO_REFUND_FAIL_FAST_ENABLED` (default `"false"`).
Flip per environment via fly secret so test traffic can validate before
prod is enabled.

Idempotency: a CAS `UPDATE pricing_snapshots_v2 SET refund_status = ...
WHERE snapshot_id = ? AND refund_status = 'none'` ensures concurrent
PUT retries cannot double-credit the wallet. Skipping when
`narrator_tasks.snapshot_id` is NULL (legacy v1 orders) is intentional —
the v1 codepath has its own refund story.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Optional

from sqlalchemy import select, update
from sqlalchemy.engine import Connection
from sqlalchemy.exc import SQLAlchemyError

from db.tables import narrator_tasks, pricing_snapshots_v2, users


log = logging.getLogger(__name__)


_FEATURE_FLAG_ENV = "AUTO_REFUND_FAIL_FAST_ENABLED"
_REFUND_STATUS_NONE = "none"
_REFUND_STATUS_AUTO = "auto_refunded_fail_fast"
_STEP_STATUS_FAILED = "failed"
_TASK_STATUS_FAILED = "failed"


def _feature_enabled() -> bool:
    return os.environ.get(_FEATURE_FLAG_ENV, "false").lower() == "true"


def detect_fail_fast_no_work(body: dict[str, Any]) -> Optional[str]:
    """Return the first-step key when `body` matches the fail-fast pattern.

    Match criteria — all three must hold (Web API contract § 方案 D
    case 2):

    1. `body.status == "failed"`.
    2. Both no-work signals agree (AND, not OR — see note below):
       a) every entry in `body.steps` has both `output_file_id`
          and `output_url` empty / missing, AND
       b) `body.current_step` equals the first key in
          `body.steps` (insertion order) — i.e. we never
          progressed past the first attempted subflow.
    3. The first attempted step (= the first key in `body.steps`,
       Python ≥3.7 dict iteration is insertion order) has
       `status == "failed"` AND its `task_id` field is absent / null
       / empty (proof the upstream never accepted the request).

    Returns the matched step key on success so the caller can log it,
    or `None` when the pattern does not apply. Pure function — no DB
    access.

    Body shape note: `body` is the master-task object as it travels
    through the PUT /narrator/tasks/<id> route (or as it lives in
    narrator_tasks.data after replace_task). Top-level keys are
    `status`, `current_step`, `steps`, `narrator_task_id`, etc. — the
    same flat shape store.py persists and the web side ships. An
    earlier revision of this detector read `body.data.steps`, which
    matched the unit-test fixture but NOT the real PUT body —
    silently always returning pattern_mismatch. Tests now use
    the flat shape that matches both the PUT route AND the
    orchestrator path.

    Spec interpretation note: Web API contract wrote condition 2 as
    "all outputs empty 或 current_step 仍是第一步" which reads as OR.
    Implementing OR literally accepts inconsistent shapes (e.g. a
    single step entry that carries output_file_id while current_step
    still references the same step). Treating the two signals as AND
    is strictly safer: the real catalyst case in Web API contract
    has BOTH signals aligned, the multi-step + all-no-output case
    still passes (current_step would
    still point at the first step that failed; later entries are
    placeholders, not progress), and any divergent shape falls
    through to the manual path where a human can decide. Revisit if production data shows
    legitimate divergent shapes we want to auto-refund.

    Earlier revisions hard-coded `len(steps) == 1`; that diverged
    from the issue spec by silently rejecting bodies whose first
    subflow failed before any output landed, just because later step
    rows were pre-populated as placeholders.
    """
    if not isinstance(body, dict):
        return None
    if body.get("status") != _TASK_STATUS_FAILED:
        return None

    steps = body.get("steps")
    if not isinstance(steps, dict) or not steps:
        return None

    # First attempted subflow — Python dict preserves insertion order,
    # which mirrors the order subflows actually ran.
    first_step_key = next(iter(steps))
    first_step = steps[first_step_key]
    if not isinstance(first_step, dict):
        return None

    # Condition 2: BOTH no-work signals must agree (AND). See the
    # docstring note for why the spec's literal OR is implemented as
    # AND in this revision. Multi-step bodies with placeholder later
    # entries still pass — those entries carry no output and
    # current_step still references the first step that failed.
    all_no_output = all(
        isinstance(s, dict) and not _has_any_output(s)
        for s in steps.values()
    )
    current_step_at_first = body.get("current_step") in (
        None,
        "",
        first_step_key,
    )
    if not (all_no_output and current_step_at_first):
        return None

    # Condition 3: first step failed, no upstream task_id. task_id is
    # typed as string in the data model; only None / missing / "" count
    # as "no upstream task created" — anything else (numeric 0, dict,
    # list…) is treated as "upstream returned something funny" and we
    # bail to the manual path.
    if first_step.get("status") != _STEP_STATUS_FAILED:
        return None
    task_id = first_step.get("task_id")
    if task_id not in (None, ""):
        return None

    return first_step_key


def _has_any_output(step: dict[str, Any]) -> bool:
    """A step counts as 'produced output' if it carries any non-empty
    `output_file_id` or `output_url`. Empty strings and missing keys
    both count as no-output."""
    for key in ("output_file_id", "output_url"):
        val = step.get(key)
        if val:
            return True
    return False


def apply_auto_refund_if_eligible(
    conn: Connection,
    *,
    narrator_task_id: str,
    user_id: int,
    body: dict[str, Any],
) -> dict[str, Any]:
    """Run inside the same DB transaction as the task PUT.

    The detection step is pure (`detect_fail_fast_no_work`); the apply
    step performs CAS on the snapshot's `refund_status` and credits
    `users.balance_points` only when the CAS update affected exactly
    one row.

    Returns a small dict describing the outcome for ineligible-but-OK
    paths (feature off / pattern miss / v1 row / already refunded).
    **Re-raises `SQLAlchemyError` for any storage failure** so the
    caller's transaction rolls back together with the task PUT — the
    invariant "task marked failed AND refund applied" must be atomic.
    Returning a `db_error` reason while letting `server.py` commit
    would persist `status=failed` while skipping the wallet credit and
    refund_status stamp, leaving the user charged with no recovery.
    The regression coverage on regression coverage (security hardening) caught this directly.

    Reasons (non-applied no-op paths):
      - `feature_disabled`     — env flag off (default).
      - `pattern_mismatch`     — body did not match § 方案 D case 2.
      - `no_snapshot`          — v1 row or snapshot_id never linked.
      - `snapshot_missing`     — snapshot_id stale (data drift —
        very rare; FK normally prevents this. No-op is safer than
        raising here because the snapshot is the row we wanted to
        refund — if it's gone, there's nothing to credit anyway).
      - `already_refunded`     — refund_status != 'none' OR CAS lost.
    """
    if not _feature_enabled():
        return {"applied": False, "reason": "feature_disabled"}

    first_step = detect_fail_fast_no_work(body)
    if first_step is None:
        return {"applied": False, "reason": "pattern_mismatch"}

    # Storage operations below may raise SQLAlchemyError. We let them
    # propagate so the route layer's outer transaction rolls back the
    # task PUT alongside the refund attempt. Logging happens via the
    # route's existing exception handler; we add a contextual log line
    # here so the failed lookup / CAS / credit can be told apart in
    # post-mortem.
    #
    # Note on race: `narrator_tasks.snapshot_id` is written exactly
    # once at commit_master_task_snapshot and immutable thereafter
    # (no UPDATE path touches it). Reading it here then CAS'ing the
    # corresponding snapshot row is race-free in the absence of a
    # rogue admin write — which would be a separate incident class.
    #
    # Tenant isolation: the route already validates that the task row
    # belongs to `user_id` via `nt_replace_task`'s tenant-scoped lock
    # before this hook runs. We additionally pin `user_id` on both
    # this lookup and the snapshot read — defense-in-depth flagged in
    # review — so even a future regression in the route layer
    # cannot trick a cross-tenant refund through this path.
    try:
        snap_row = conn.execute(
            select(narrator_tasks.c.snapshot_id).where(
                narrator_tasks.c.narrator_task_id == narrator_task_id,
                narrator_tasks.c.user_id == user_id,
            )
        ).first()
    except SQLAlchemyError:
        log.exception(
            "auto_refund_snapshot_lookup_failed",
            extra={"narrator_task_id": narrator_task_id},
        )
        raise
    if snap_row is None or snap_row[0] is None:
        # v1 master task (snapshot_id NULL), cross-tenant attempt,
        # or row deleted between PUT and this hook. No v2 refund path.
        return {"applied": False, "reason": "no_snapshot"}
    snapshot_id = snap_row[0]

    try:
        snap = conn.execute(
            select(
                pricing_snapshots_v2.c.final_charge_price,
                pricing_snapshots_v2.c.refund_status,
            ).where(
                pricing_snapshots_v2.c.snapshot_id == snapshot_id,
                pricing_snapshots_v2.c.web_user_id == user_id,
            )
        ).first()
    except SQLAlchemyError:
        log.exception(
            "auto_refund_snapshot_read_failed",
            extra={"snapshot_id": snapshot_id},
        )
        raise
    if snap is None:
        return {"applied": False, "reason": "snapshot_missing"}
    if snap.refund_status != _REFUND_STATUS_NONE:
        return {"applied": False, "reason": "already_refunded"}

    # `final_charge_price` is declared INTEGER on both Postgres and
    # SQLite (see pricing_quote_v2/schema.py). int() handles the
    # SQLite NUMERIC-as-string round-trip case without precision loss.
    amount = int(snap.final_charge_price or 0)

    # CAS on refund_status. Whichever writer gets rowcount==1 owns the
    # subsequent credit step; losers exit with `already_refunded`. The
    # `web_user_id` filter mirrors the snapshot read above — defense-
    # in-depth flagged in review.
    try:
        cas = conn.execute(
            update(pricing_snapshots_v2)
            .where(pricing_snapshots_v2.c.snapshot_id == snapshot_id)
            .where(pricing_snapshots_v2.c.web_user_id == user_id)
            .where(pricing_snapshots_v2.c.refund_status == _REFUND_STATUS_NONE)
            .values(refund_status=_REFUND_STATUS_AUTO)
        )
    except SQLAlchemyError:
        log.exception(
            "auto_refund_cas_failed",
            extra={"snapshot_id": snapshot_id},
        )
        raise
    if cas.rowcount != 1:
        # Concurrent writer won the CAS — their balance_points
        # credit will run on their transaction.
        return {"applied": False, "reason": "already_refunded"}

    if amount > 0:
        try:
            conn.execute(
                update(users)
                .where(users.c.id == user_id)
                .values(balance_points=users.c.balance_points + amount)
            )
        except SQLAlchemyError:
            # Credit failed after the CAS succeeded. The outer
            # transaction rolls back both — refund_status returns to
            # 'none' on rollback, so the next retry can attempt the
            # whole atomic operation cleanly.
            log.exception(
                "auto_refund_credit_failed",
                extra={
                    "snapshot_id": snapshot_id,
                    "user_id": user_id,
                    "amount": amount,
                },
            )
            raise

    log.info(
        "auto_refund_applied",
        extra={
            "narrator_task_id": narrator_task_id,
            "snapshot_id": snapshot_id,
            "user_id": user_id,
            "amount": amount,
            "first_step": first_step,
        },
    )
    return {
        "applied": True,
        "snapshot_id": snapshot_id,
        "amount": amount,
        "first_step": first_step,
    }
