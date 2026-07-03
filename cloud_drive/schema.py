"""DDL for Web-user-scoped cloud-drive ownership and quota.

The internal cloud-drive API exposes one expandable master account for
NarratorAI Web. This backend table turns that shared account into personal
Web-user drives by mapping each upstream file_id to users.id and by keeping
quota accounting local to the Web user system.
"""

from __future__ import annotations


DEFAULT_USER_CLOUD_QUOTA_BYTES = 3 * 1024 * 1024 * 1024


CLOUD_DRIVE_SCHEMA_SQL = f"""
ALTER TABLE users
    ADD COLUMN IF NOT EXISTS cloud_drive_quota_bytes BIGINT
    NOT NULL DEFAULT {DEFAULT_USER_CLOUD_QUOTA_BYTES};

CREATE TABLE IF NOT EXISTS user_cloud_files (
    id              BIGSERIAL PRIMARY KEY,
    reservation_id  TEXT NOT NULL UNIQUE,
    user_id         INTEGER NOT NULL REFERENCES users(id),
    app_key         TEXT NOT NULL,
    file_id         TEXT UNIQUE,
    object_key      TEXT,
    file_name       TEXT NOT NULL,
    suffix          TEXT NOT NULL DEFAULT '',
    category        INTEGER NOT NULL DEFAULT 0,
    file_size       BIGINT NOT NULL DEFAULT 0 CHECK (file_size >= 0),
    content_type    TEXT,
    source          TEXT NOT NULL,
    status          TEXT NOT NULL,
    upstream_status INTEGER,
    progress        INTEGER NOT NULL DEFAULT 0 CHECK (progress >= 0 AND progress <= 100),
    srt_file_hash   TEXT,
    upstream_payload JSONB NOT NULL DEFAULT '{{}}'::jsonb,
    upload_id       TEXT,
    parent_reservation_id TEXT,
    settled_at      TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    completed_at    TIMESTAMPTZ,
    deleted_at      TIMESTAMPTZ,
    CHECK (source IN ('local_upload', 'transfer')),
    CHECK (status IN ('reserved', 'completed', 'failed', 'delete_pending', 'deleted', 'transfer_pending', 'transfer_running', 'transfer_completed'))
);

CREATE INDEX IF NOT EXISTS user_cloud_files_owner_status_idx
    ON user_cloud_files (user_id, status, updated_at DESC);

CREATE INDEX IF NOT EXISTS user_cloud_files_owner_file_idx
    ON user_cloud_files (user_id, file_id);

CREATE INDEX IF NOT EXISTS user_cloud_files_unsettled_upload_idx
    ON user_cloud_files (user_id, upload_id)
    WHERE upload_id IS NOT NULL
      AND parent_reservation_id IS NULL
      AND settled_at IS NULL;
"""
