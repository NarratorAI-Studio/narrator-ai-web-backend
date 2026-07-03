"""CRUD layer for the narrator_tasks table.

Mirrors the semantics of narrator-ai-web `src/lib/master-task-db.ts`:
- create_task: insert with server-assigned ID + timestamps.
- upsert_task: insert-or-update preserving original ID + created_at;
  rejects cross-tenant overwrite via SELECT FOR UPDATE inside a
  transaction (returns None on conflict; route layer maps to 403).
- get_task: fetch one row, scoped to a single owner. Returns None on
  missing OR cross-tenant — the route layer collapses both to 404 so
  existence isn't leaked.
- list_tasks: paginated list scoped to one owner.
- replace_task: PUT semantics. Optional CAS preconditions
  (`expected_status` and `expected_step`) — returns the literal string
  "CAS_MISMATCH" when the row exists but its current state doesn't
  satisfy the preconditions, so the route can return a 409 distinct
  from the 404 (not-found / cross-tenant) case.

All functions take an open SQLAlchemy Connection. The route layer is
responsible for the connection lifecycle AND for committing on success
(SQLAlchemy 2.x autobegin requires an explicit `conn.commit()`; closing
without a commit rolls back). The store relies on the route's outer
transaction to give `SELECT FOR UPDATE` real teeth on Postgres — both
the read and the write must run inside the same transaction.
"""

from __future__ import annotations

import secrets
import time
from datetime import datetime, timezone
from typing import Any, Iterable, Optional

from sqlalchemy import and_, func, select
from sqlalchemy.engine import Connection
from sqlalchemy.exc import IntegrityError

from db.tables import narrator_tasks


def _extract_run_auto(body: dict) -> int:
    """Normalize the body's `run_auto` field to a SMALLINT 0 | 1 for the
    hot column. The JSONB blob has historically held an int (`0` / `1`)
    from web's TypeScript writes, but tolerate the string forms a
    handwritten test fixture might use. Anything else falls back to 0
    (manual stepping) — safer than auto-advance for malformed input."""
    raw = body.get("run_auto")
    if raw in (1, "1", "1.0", True):
        return 1
    return 0


# Sentinel returned by replace_task when the row exists for this tenant
# but its current state doesn't match the CAS preconditions. Route layer
# maps this to HTTP 409. Distinct from the None / NotFound case.
CAS_MISMATCH = "CAS_MISMATCH"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _generate_id() -> str:
    """Mirror master-task-client-store.generateNarratorTaskId on the web side:
    `<ms-since-epoch>_<6 base36 chars>`. Web-generated IDs collide-free at
    millisecond granularity for a single client; the random suffix covers
    the same-ms case. Server-side generation just uses the same shape so
    callers can't tell which side produced the ID.
    """
    ms = int(time.time() * 1000)
    suffix_alphabet = "0123456789abcdefghijklmnopqrstuvwxyz"
    suffix = "".join(secrets.choice(suffix_alphabet) for _ in range(6))
    return f"{ms}_{suffix}"


def create_task(
    conn: Connection,
    *,
    user_id: int,
    app_key: str,
    body: dict,
) -> dict:
    """Insert a new task. `body` is the full client-supplied task minus
    narrator_task_id / created_at / updated_at — those are server-assigned
    and overwritten if the client tried to provide them.

    Returns the persisted task dict with the new ID + timestamps populated.
    """
    now = _utcnow()
    task_id = _generate_id()
    status = body.get("status") or "pending"
    current_step = body.get("current_step")

    full_body = _build_full_body(
        body=body,
        task_id=task_id,
        app_key=app_key,
        created_at_iso=now.isoformat(),
        now_iso=now.isoformat(),
        status=status,
        current_step=current_step,
    )

    conn.execute(
        narrator_tasks.insert().values(
            narrator_task_id=task_id,
            user_id=user_id,
            app_key=app_key,
            status=status,
            current_step=current_step,
            data=full_body,
            run_auto=_extract_run_auto(full_body),
            created_at=now,
            updated_at=now,
        )
    )
    return full_body


def upsert_task(
    conn: Connection,
    *,
    user_id: int,
    app_key: str,
    body: dict,
) -> Optional[dict]:
    """Insert-or-update preserving the client-supplied narrator_task_id.

    Used by narrator-ai-web's one-time localStorage -> DB migration path
    and by step orchestrator paths that already minted a task ID
    client-side. Uses SELECT FOR UPDATE in a transaction to atomically
    reject cross-tenant overwrites — returns None when the existing row
    belongs to a different user_id (route layer maps to 403).

    Preserves the existing `created_at` on update; `updated_at` is bumped
    to now. The full task body is stamped onto `data` verbatim (with
    `app_key` / `narrator_task_id` enforced server-side and `updated_at`
    refreshed).
    """
    task_id = body["narrator_task_id"]
    now = _utcnow()
    status = body.get("status") or "pending"
    current_step = body.get("current_step")

    existing = conn.execute(
        select(
            narrator_tasks.c.user_id,
            narrator_tasks.c.data,
        )
        .where(narrator_tasks.c.narrator_task_id == task_id)
        .with_for_update()
    ).first()

    if existing is not None:
        if existing.user_id != user_id:
            return None
        return _apply_update(
            conn,
            existing=existing,
            task_id=task_id,
            app_key=app_key,
            body=body,
            now=now,
            status=status,
            current_step=current_step,
        )

    # `SELECT ... FOR UPDATE` does not lock a non-existent primary key in
    # Postgres, so two concurrent same-tenant upserts can both observe
    # `existing is None` and race to INSERT. Wrapping the insert in a
    # SAVEPOINT lets us catch the loser's UNIQUE violation and recover
    # with an UPDATE without poisoning the route's outer transaction.
    # `conn.begin_nested()` maps to `SAVEPOINT` on both Postgres and
    # sqlite (used by tests).
    created_at_iso = now.isoformat()
    full_body = _build_full_body(
        body=body,
        task_id=task_id,
        app_key=app_key,
        created_at_iso=created_at_iso,
        now_iso=now.isoformat(),
        status=status,
        current_step=current_step,
    )
    sp = conn.begin_nested()
    try:
        conn.execute(
            narrator_tasks.insert().values(
                narrator_task_id=task_id,
                user_id=user_id,
                app_key=app_key,
                status=status,
                current_step=current_step,
                data=full_body,
                run_auto=_extract_run_auto(full_body),
                created_at=now,
                updated_at=now,
            )
        )
        sp.commit()
        return full_body
    except IntegrityError:
        sp.rollback()

    # Race lost — the row exists now. Re-acquire with the row-lock,
    # validate tenant, and update via the same path as the existing
    # branch above.
    #
    # Capture a fresh `now` for the UPDATE. Reusing the pre-race `now`
    # captured at function entry can let the eventual `updated_at` land
    # earlier than the racing winner's `created_at` — time inversion
    # would break ORDER BY updated_at DESC list ordering.
    recovery_now = _utcnow()

    existing = conn.execute(
        select(
            narrator_tasks.c.user_id,
            narrator_tasks.c.data,
        )
        .where(narrator_tasks.c.narrator_task_id == task_id)
        .with_for_update()
    ).first()
    if existing is None or existing.user_id != user_id:
        return None
    return _apply_update(
        conn,
        existing=existing,
        task_id=task_id,
        app_key=app_key,
        body=body,
        now=recovery_now,
        status=status,
        current_step=current_step,
    )


def _apply_update(
    conn: Connection,
    *,
    existing,
    task_id: str,
    app_key: str,
    body: dict,
    now: datetime,
    status: str,
    current_step: Optional[str],
) -> dict:
    """Shared update path for same-tenant upsert (existing row) and the
    race-recovery branch. Preserves the existing `created_at` string from
    the persisted JSON blob — reading the SQL TIMESTAMPTZ column and
    re-formatting it loses the timezone suffix on sqlite and is a
    round-trip we don't need."""
    existing_data = _coerce_data(existing.data)
    created_at_iso = existing_data.get("created_at") or now.isoformat()
    full_body = _build_full_body(
        body=body,
        task_id=task_id,
        app_key=app_key,
        created_at_iso=created_at_iso,
        now_iso=now.isoformat(),
        status=status,
        current_step=current_step,
    )
    conn.execute(
        narrator_tasks.update()
        .where(narrator_tasks.c.narrator_task_id == task_id)
        .values(
            status=status,
            current_step=current_step,
            data=full_body,
            run_auto=_extract_run_auto(full_body),
            updated_at=now,
        )
    )
    return full_body


def _build_full_body(
    *,
    body: dict,
    task_id: str,
    app_key: str,
    created_at_iso: str,
    now_iso: str,
    status: str,
    current_step: Optional[str],
) -> dict:
    """Server-overridden task body. The client's `app_key` and
    `narrator_task_id` (if any) are deliberately overwritten with the
    authenticated identity and the URL/server-assigned ID — clients must
    not be able to mint cross-tenant attribution via the persisted JSON.
    Documented in openapi.json's NarratorTaskBody schema."""
    return {
        **body,
        "narrator_task_id": task_id,
        "app_key": app_key,
        "created_at": created_at_iso,
        "updated_at": now_iso,
        "status": status,
        "current_step": current_step,
    }


def get_task(
    conn: Connection,
    *,
    user_id: int,
    narrator_task_id: str,
) -> Optional[dict]:
    """Return the task body for a single row owned by `user_id`. Returns
    None for both "row doesn't exist" and "row exists but belongs to a
    different user" — the route layer collapses both to 404 to avoid
    leaking existence."""
    row = conn.execute(
        select(narrator_tasks.c.data).where(
            and_(
                narrator_tasks.c.narrator_task_id == narrator_task_id,
                narrator_tasks.c.user_id == user_id,
            )
        )
    ).first()
    if row is None:
        return None
    return _coerce_data(row.data)


def list_tasks(
    conn: Connection,
    *,
    user_id: int,
    status: Optional[str] = None,
    page: int = 1,
    limit: int = 20,
) -> dict:
    """Paginated list scoped to one owner. Returns `{items, total}` to
    match the web-side response shape (`dbListTasks` in
    master-task-db.ts)."""
    base_where = [narrator_tasks.c.user_id == user_id]
    if status:
        base_where.append(narrator_tasks.c.status == status)

    total = conn.execute(
        select(func.count()).select_from(narrator_tasks).where(and_(*base_where))
    ).scalar_one()

    offset = (page - 1) * limit
    rows = conn.execute(
        select(narrator_tasks.c.data)
        .where(and_(*base_where))
        .order_by(narrator_tasks.c.updated_at.desc())
        .limit(limit)
        .offset(offset)
    ).all()

    items = [_coerce_data(row.data) for row in rows]
    return {"items": items, "total": int(total)}


def replace_task(
    conn: Connection,
    *,
    user_id: int,
    app_key: str,
    narrator_task_id: str,
    body: dict,
    expected_status: Optional[Iterable[str]] = None,
    expected_step: Optional[str] = None,
) -> Optional[dict] | str:
    """Replace the task body. Optional CAS preconditions:
      - expected_status: only write if current row's status is in this list.
      - expected_step: additionally require current row's current_step ==
        this value.

    `app_key` is the authenticated caller's key — overwritten onto the
    persisted JSON body so clients can't submit a spoofed `app_key` field
    and poison downstream attribution (same rule as create_task /
    upsert_task; matches OpenAPI's NarratorTaskBody.app_key contract).

    Return values:
      - dict: success — the updated task body.
      - None: row missing or owned by another user (route returns 404).
      - CAS_MISMATCH sentinel: row exists for this user but its current
        state doesn't satisfy the preconditions (route returns 409).
    """
    now = _utcnow()
    status = body.get("status") or "pending"
    current_step = body.get("current_step")

    existing = conn.execute(
        select(
            narrator_tasks.c.user_id,
            narrator_tasks.c.status,
            narrator_tasks.c.current_step,
            narrator_tasks.c.data,
        )
        .where(narrator_tasks.c.narrator_task_id == narrator_task_id)
        .with_for_update()
    ).first()

    if existing is None or existing.user_id != user_id:
        return None

    if expected_status is not None:
        allowed = list(expected_status)
        if existing.status not in allowed:
            return CAS_MISMATCH
    if expected_step is not None and existing.current_step != expected_step:
        return CAS_MISMATCH

    existing_data = _coerce_data(existing.data)
    # Preserve the exact created_at string from the original insert (see
    # comment in _apply_update for why we read JSON instead of the SQL column).
    created_at_iso = existing_data.get("created_at") or now.isoformat()
    full_body = _build_full_body(
        body=body,
        task_id=narrator_task_id,
        app_key=app_key,
        created_at_iso=created_at_iso,
        now_iso=now.isoformat(),
        status=status,
        current_step=current_step,
    )

    conn.execute(
        narrator_tasks.update()
        .where(narrator_tasks.c.narrator_task_id == narrator_task_id)
        .values(
            status=status,
            current_step=current_step,
            data=full_body,
            run_auto=_extract_run_auto(full_body),
            updated_at=now,
        )
    )

    return full_body


def _coerce_data(raw: Any) -> dict:
    """SQLAlchemy's JSON column round-trips dicts on Postgres but on
    SQLite (used by tests) it round-trips as a string. Normalize to dict
    so route handlers don't need to care which backend they hit.
    """
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8")
    if isinstance(raw, str):
        import json

        return json.loads(raw)
    raise TypeError(f"unexpected data column type: {type(raw).__name__}")


def list_running_auto_tasks(
    conn: Connection,
    *,
    limit: int,
) -> list[tuple[str, int, str]]:
    """Return `(narrator_task_id, user_id, app_key)` triples for every
    row eligible for orchestrator auto-advance, ordered by oldest
    `updated_at` first. Hard-capped at `limit`.

    **Cross-tenant on purpose** — this is the ONE function in store.py
    that bypasses the per-user filter, because the orchestrator scan
    needs to look across all tenants to schedule work. The caller
    (`orchestrator.poller.scan_and_advance`) MUST use the returned
    `user_id` + `app_key` as the authenticated identity for any
    subsequent `replace_task` / `get_task` call so tenant scoping is
    preserved at write time. Do not expose this through any HTTP route.

    Hits `ix_narrator_tasks_running_auto` (partial index added by
    migration 20260611_0001) — the WHERE clause matches the index
    predicate exactly, and the ORDER BY matches the index sort, so
    Postgres can read straight off the index.
    """
    rows = conn.execute(
        select(
            narrator_tasks.c.narrator_task_id,
            narrator_tasks.c.user_id,
            narrator_tasks.c.app_key,
        )
        .where(
            and_(
                narrator_tasks.c.status == "running",
                narrator_tasks.c.run_auto == 1,
            )
        )
        .order_by(narrator_tasks.c.updated_at.asc())
        .limit(limit)
    ).all()

    return [
        (row.narrator_task_id, row.user_id, row.app_key) for row in rows
    ]
