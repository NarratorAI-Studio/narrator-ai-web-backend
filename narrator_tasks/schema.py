"""DDL for the narrator_tasks table.

Cold-start replacement for the legacy MySQL
`narrator_master_tasks` table. Stores the full task body as a JSONB
blob, plus hot columns (status / current_step / app_key / user_id /
timestamps) for query and tenant scoping. The schema mirrors the
MySQL version 1:1 — keeping the JSON body lets web's
`NarratorMasterTask` type evolve without alembic churn.

Tenant boundary is `user_id` (FK to `users.id`). The `app_key` column
is denormalized for log / debug convenience; row ownership is the FK,
not the app_key string.
"""

from __future__ import annotations


NARRATOR_TASKS_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS narrator_tasks (
    narrator_task_id TEXT PRIMARY KEY,
    user_id          INTEGER NOT NULL REFERENCES users(id),
    app_key          TEXT NOT NULL,
    status           TEXT NOT NULL DEFAULT 'pending',
    current_step     TEXT,
    data             JSONB NOT NULL,
    run_auto         SMALLINT NOT NULL DEFAULT 0,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS narrator_tasks_owner_status_idx
    ON narrator_tasks (user_id, status, updated_at DESC);

CREATE INDEX IF NOT EXISTS ix_narrator_tasks_running_auto
    ON narrator_tasks (updated_at)
    WHERE status = 'running' AND run_auto = 1;
"""
