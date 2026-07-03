"""create user cloud-drive ownership table and quota column

Revision ID: 20260528_0001
Revises: 20260527_0001
Create Date: 2026-05-28

Wraps the internal cloud-drive master account into Web-user-scoped personal
drives. Each Web user gets a configurable quota, defaulting to 3 GB, and each
upstream file_id is mapped back to users.id for tenant isolation.
"""

from __future__ import annotations

from alembic import op

from cloud_drive.schema import CLOUD_DRIVE_SCHEMA_SQL


revision = "20260528_0001"
down_revision = "20260527_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(CLOUD_DRIVE_SCHEMA_SQL)


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS user_cloud_files_owner_file_idx")
    op.execute("DROP INDEX IF EXISTS user_cloud_files_owner_status_idx")
    op.execute("DROP TABLE IF EXISTS user_cloud_files")
    op.execute("ALTER TABLE users DROP COLUMN IF EXISTS cloud_drive_quota_bytes")
