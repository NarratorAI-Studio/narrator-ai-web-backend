"""narrator_tasks.run_auto top-level column + partial scan index

Revision ID: 20260611_0001
Revises: 20260604_0002
Create Date: 2026-06-11

Adds a SMALLINT `run_auto` top-level column so the orchestrator scan
(`list_running_auto_tasks`) can hit a partial
index instead of casting `data->>'run_auto'` on every 30 s tick.

Before this migration `run_auto` only existed inside the JSONB `data`
blob (mirrors the web `NarratorMasterTask.run_auto: 0 | 1` field).
After this migration the column is the source of truth for scan; the
blob still carries the field for backward compatibility with web sync
writes during the transition window. `narrator_tasks/store.py` is
updated in the same PR to write both consistently — see
`store._extract_run_auto`.

Partial index narrows the scan to the actively-recoverable subset:
status='running' AND run_auto=1. Ordering on updated_at ASC lets the
poller pick the oldest running tasks first, matching sync/route.ts's
implicit "tabs poll oldest pending task" pattern.
"""

from __future__ import annotations

from alembic import op


revision = "20260611_0001"
down_revision = "20260604_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # SMALLINT covers the 0 | 1 domain with the smallest int type Postgres
    # offers; default 0 is a safe "did not opt in to auto-advance" stance
    # so existing rows behave like manual stepping until the backfill
    # below corrects them.
    # Raw SQL with IF NOT EXISTS so this migration is idempotent against
    # `narrator_tasks/schema.py`, whose CREATE TABLE was retrofitted to
    # include this column. A fresh DB (e.g., Backend API contract
    # test cluster) has the column already from the create migration;
    # without IF NOT EXISTS, ADD raises `DuplicateColumn`. Prod has the
    # column either way, so this is a fresh-DB / DR rebuild safety fix.
    bind = op.get_bind()
    dialect = bind.dialect.name
    if dialect == "sqlite":
        existing_columns = {
            row[1]
            for row in bind.exec_driver_sql("PRAGMA table_info(narrator_tasks)")
        }
        if "run_auto" not in existing_columns:
            op.execute(
                "ALTER TABLE narrator_tasks "
                "ADD COLUMN run_auto SMALLINT NOT NULL DEFAULT 0"
            )
    else:
        op.execute(
            "ALTER TABLE narrator_tasks "
            "ADD COLUMN IF NOT EXISTS run_auto SMALLINT NOT NULL DEFAULT 0"
        )

    # Backfill from the JSONB blob. JSONB ->> returns text, so map the
    # set of truthy serializations the web side might have written.
    # SQLite (tests) doesn't have JSONB; existing test data is migrated
    # from in-memory inserts so backfill is a no-op there.
    if dialect == "postgresql":
        op.execute(
            "UPDATE narrator_tasks "
            "SET run_auto = CASE "
            "  WHEN data->>'run_auto' IN ('1', 'true', '1.0') THEN 1 "
            "  ELSE 0 END"
        )

    # Partial index for orchestrator scan. Sized at exactly the set of
    # rows the scan loop touches each tick, so it stays small and
    # selective. updated_at ASC matches sync/route.ts's "oldest first"
    # implicit ordering — keeps starvation off the table for tasks that
    # are stuck for a while.
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_narrator_tasks_running_auto "
        "ON narrator_tasks (updated_at) "
        "WHERE status = 'running' AND run_auto = 1"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_narrator_tasks_running_auto")
    op.drop_column("narrator_tasks", "run_auto")
