"""cloud-drive transfer fan-out: upload_id, parent_reservation_id, settled_at

Revision ID: 20260604_0002
Revises: 20260604_0001
Create Date: 2026-06-04

The implementation requirement. Upstream `/v2/files/user/filelist` and `/v2/files/list` now
accept `?upload_id=` filtering, so the link-transfer reservation can be
fanned out into its real child file_ids. Schema additions:

- `upload_id` carries the upstream upload identifier (e.g. `baidu-…`) on
  both the parent reservation and its child rows (denormalized on
  children for blame).
- `parent_reservation_id` points a child row at its parent reservation.
- `settled_at` marks a parent reservation whose children have all
  reached terminal upstream status (2 success / 3 failed / 4 deleted),
  so the GET handler can hide the parent and the refresh loop can stop
  polling.

The backfill block converts the old wrong-column rows (`file_id LIKE
'baidu-%'`) into the new shape so the next refresh fan-outs them
automatically — see `project_cloud_drive_transfer_id_mismatch.md`.
"""

from __future__ import annotations

from alembic import op


revision = "20260604_0002"
down_revision = "20260604_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # IF NOT EXISTS makes this migration idempotent against `cloud_drive/
    # schema.py`, whose CREATE TABLE was retrofitted to include these
    # three columns. A fresh DB (e.g., the new test cluster from
    # `Backend API contract`) runs `20260528_0001` first, which
    # already creates the columns; without IF NOT EXISTS, the ADD here
    # raises `DuplicateColumn`. Prod has the columns either way, so this
    # is purely a fresh-DB / DR-rebuild fix.
    op.execute("ALTER TABLE user_cloud_files ADD COLUMN IF NOT EXISTS upload_id TEXT")
    op.execute(
        "ALTER TABLE user_cloud_files ADD COLUMN IF NOT EXISTS parent_reservation_id TEXT"
    )
    op.execute(
        "ALTER TABLE user_cloud_files ADD COLUMN IF NOT EXISTS settled_at TIMESTAMPTZ"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS user_cloud_files_unsettled_upload_idx "
        "ON user_cloud_files (user_id, upload_id) "
        "WHERE upload_id IS NOT NULL "
        "AND parent_reservation_id IS NULL "
        "AND settled_at IS NULL"
    )
    # Backfill: the legacy submission worker stored upstream `upload_id`
    # (e.g. `baidu-<hash>`) into the `file_id` column, where the refresh
    # match path looked for a 32-hex real `file_id` and never hit. Move
    # those values into the new `upload_id` column so the new fan-out
    # path picks them up on the next GET refresh.
    op.execute(
        "UPDATE user_cloud_files "
        "SET upload_id = file_id, file_id = NULL "
        "WHERE source = 'transfer' "
        "AND file_id LIKE 'baidu-%'"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS user_cloud_files_unsettled_upload_idx")
    op.execute("ALTER TABLE user_cloud_files DROP COLUMN settled_at")
    op.execute("ALTER TABLE user_cloud_files DROP COLUMN parent_reservation_id")
    op.execute("ALTER TABLE user_cloud_files DROP COLUMN upload_id")
