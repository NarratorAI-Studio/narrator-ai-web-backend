"""create narrator_tasks table for reseller-backed master-task storage

Revision ID: 20260527_0001
Revises: 20260526_0002
Create Date: 2026-05-27

First step of moving narrator-ai-web's master-task storage off the
temporary legacy MySQL `narrator_master_tasks` table and onto Postgres
Postgres (the implementation requirement). Cold-start migration — no rows from MySQL are
backfilled; current data on that side is dev-grade.

Schema mirrors the MySQL version: hot columns for query (status /
current_step / app_key / user_id / timestamps) plus a JSONB `data`
column for the full task body. Tenant boundary is `user_id` (FK to
`users.id`); cross-user reads/writes must be rejected at the route
layer.
"""

from __future__ import annotations

from alembic import op

from narrator_tasks.schema import NARRATOR_TASKS_SCHEMA_SQL


revision = "20260527_0001"
down_revision = "20260526_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(NARRATOR_TASKS_SCHEMA_SQL)


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS narrator_tasks_owner_status_idx")
    op.execute("DROP TABLE IF EXISTS narrator_tasks")
