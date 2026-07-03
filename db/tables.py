from __future__ import annotations

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    Numeric,
    SmallInteger,
    String,
    Table,
    UniqueConstraint,
)


metadata = MetaData()


fa_template_price = Table(
    "fa_template_price",
    metadata,
    Column("template_id", Integer, nullable=False),
    Column("combo_key", String, nullable=False),
    Column("hard_price", Numeric(18, 2), nullable=False),
    Column("text_chars", Integer),
    Column("text_lines", Integer),
    Column("pricing_rule_version", Integer, nullable=False),
    Column("is_current", Boolean, nullable=False),
    Column("source_sheet_id", String),
)


# Reseller end-user identity + balance source-of-truth.
# wallet_accounts.available_balance / frozen_balance will be refactored to
# follow users.balance_points in a subsequent update issue.
# `id` is a UNIQUE auto-incrementing identity column (added in regression coverage) that
# the API surfaces as `user_id`; `app_key` stays as PK to keep the regression coverage
# auth middleware lookup untouched.
users = Table(
    "users",
    metadata,
    Column("app_key", String, primary_key=True),
    Column("id", Integer, nullable=False, unique=True),
    Column("balance_points", Numeric(18, 2), nullable=False),
    Column("nickname", String),
    Column("mobile", String),
    Column("email", String),
    Column("company_name", String),
    Column("cloud_drive_quota_bytes", BigInteger, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    CheckConstraint("balance_points >= 0"),
)


wallet_accounts = Table(
    "wallet_accounts",
    metadata,
    Column("wallet_account_id", String, primary_key=True),
    Column("web_tenant_id", String, nullable=False),
    Column("web_user_id", String, nullable=False),
    Column("available_balance", Numeric(18, 2), nullable=False),
    Column("frozen_balance", Numeric(18, 2), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    UniqueConstraint("web_tenant_id", "web_user_id"),
    CheckConstraint("available_balance >= 0"),
    CheckConstraint("frozen_balance >= 0"),
)


wallet_quotes = Table(
    "wallet_quotes",
    metadata,
    Column("quote_id", String, primary_key=True),
    Column("status", String, nullable=False),
    Column("web_tenant_id", String, nullable=False),
    Column("web_user_id", String, nullable=False),
    Column("web_order_id", String, nullable=False),
    Column("template_id", Integer, nullable=False),
    Column("combo_key", String, nullable=False),
    Column("hard_price", Numeric(18, 2), nullable=False),
    Column("amount_points", Numeric(18, 2), nullable=False),
    Column("pricing_rule_version", Integer, nullable=False),
    Column("expires_at", DateTime(timezone=True), nullable=False),
    Column("pricing_metadata", JSON, nullable=False),
    Column("correlation", JSON, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    UniqueConstraint("web_order_id"),
    CheckConstraint("status IN ('ACTIVE', 'CONSUMED', 'EXPIRED')"),
    CheckConstraint("hard_price > 0"),
    CheckConstraint("amount_points > 0"),
)
Index(
    "wallet_quotes_owner_order_idx",
    wallet_quotes.c.web_tenant_id,
    wallet_quotes.c.web_user_id,
    wallet_quotes.c.web_order_id,
)


wallet_transactions = Table(
    "wallet_transactions",
    metadata,
    Column("wallet_transaction_id", String, primary_key=True),
    Column("state", String, nullable=False),
    Column("quote_id", String, ForeignKey("wallet_quotes.quote_id"), nullable=False),
    Column(
        "wallet_account_id",
        String,
        ForeignKey("wallet_accounts.wallet_account_id"),
        nullable=False,
    ),
    Column("web_tenant_id", String, nullable=False),
    Column("web_user_id", String, nullable=False),
    Column("web_order_id", String, nullable=False),
    Column("amount_points", Numeric(18, 2), nullable=False),
    Column("pricing_rule_version", Integer, nullable=False),
    Column("frozen_at", DateTime(timezone=True)),
    Column("confirmed_at", DateTime(timezone=True)),
    Column("refunded_at", DateTime(timezone=True)),
    Column("refund_reason_code", String),
    Column("refund_reason_message", String),
    Column("correlation", JSON, nullable=False),
    Column("refund_correlation", JSON, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    UniqueConstraint("web_order_id"),
    CheckConstraint("state IN ('FROZEN', 'CONFIRMED', 'REFUNDED')"),
    CheckConstraint("amount_points > 0"),
)
Index(
    "wallet_transactions_owner_order_idx",
    wallet_transactions.c.web_tenant_id,
    wallet_transactions.c.web_user_id,
    wallet_transactions.c.web_order_id,
)


wallet_ledger_entries = Table(
    "wallet_ledger_entries",
    metadata,
    Column("ledger_entry_id", String, primary_key=True),
    Column(
        "wallet_transaction_id",
        String,
        ForeignKey("wallet_transactions.wallet_transaction_id"),
        nullable=False,
    ),
    Column(
        "wallet_account_id",
        String,
        ForeignKey("wallet_accounts.wallet_account_id"),
        nullable=False,
    ),
    Column("entry_type", String, nullable=False),
    Column("amount_points", Numeric(18, 2), nullable=False),
    Column("balance_available_after", Numeric(18, 2), nullable=False),
    Column("balance_frozen_after", Numeric(18, 2), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    CheckConstraint("entry_type IN ('FREEZE', 'CONFIRM', 'REFUND')"),
    CheckConstraint("amount_points > 0"),
    CheckConstraint("balance_available_after >= 0"),
    CheckConstraint("balance_frozen_after >= 0"),
)
Index("wallet_ledger_transaction_idx", wallet_ledger_entries.c.wallet_transaction_id)


wallet_idempotency_records = Table(
    "wallet_idempotency_records",
    metadata,
    Column("operation", String, primary_key=True),
    Column("idempotency_key", String, primary_key=True),
    Column("request_hash", String, nullable=False),
    Column("response_status", Integer, nullable=False),
    Column("response_body", JSON, nullable=False),
    Column("web_tenant_id", String, nullable=False),
    Column("web_user_id", String, nullable=False),
    Column("web_order_id", String, nullable=False),
    Column("wallet_transaction_id", String),
    Column("first_seen_at", DateTime(timezone=True), nullable=False),
    Column("last_replay_at", DateTime(timezone=True), nullable=False),
    CheckConstraint("response_status >= 100 AND response_status <= 599"),
)
Index(
    "wallet_idempotency_owner_order_idx",
    wallet_idempotency_records.c.web_tenant_id,
    wallet_idempotency_records.c.web_user_id,
    wallet_idempotency_records.c.web_order_id,
)
Index(
    "wallet_idempotency_first_seen_idx",
    wallet_idempotency_records.c.first_seen_at,
)


# Tenant-backed replacement for the legacy MySQL
# `narrator_master_tasks` table . The `data` column holds the full
# NarratorMasterTask body as a JSON blob; hot columns are platformed out
# for query / tenant scoping. `user_id` FK to `users.id` is the tenant
# boundary; `app_key` is denormalized for log / debug.
narrator_tasks = Table(
    "narrator_tasks",
    metadata,
    Column("narrator_task_id", String, primary_key=True),
    Column("user_id", Integer, ForeignKey("users.id"), nullable=False),
    Column("app_key", String, nullable=False),
    Column("status", String, nullable=False),
    Column("current_step", String),
    Column("data", JSON, nullable=False),
    # Hot-column copy of the JSONB `data->>'run_auto'` field. Migration
    # 20260611_0001 added it so the orchestrator scan can hit a partial
    # index instead of casting JSONB on every tick. store.py writes both
    # values in lockstep; the blob still carries the field for web
    # backward compat during the transition window.
    Column("run_auto", SmallInteger, nullable=False, server_default="0"),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)
Index(
    "narrator_tasks_owner_status_idx",
    narrator_tasks.c.user_id,
    narrator_tasks.c.status,
    narrator_tasks.c.updated_at.desc(),
)


# Hard-price v2 catalog.
# Three tables for the tier schema described in
# docs/pricing/v2/catalog-tier-contract.md.
# v1's fa_template_price (above) stays in place for HP-XX quote/pin
# compatibility — v2 lives in new tables so the migration is additive.

pricing_template_v2 = Table(
    "pricing_template_v2",
    metadata,
    Column("template_id", String, primary_key=True),
    Column("template_family_id", String, nullable=True),
    Column("tier_multiplier", Numeric(6, 4), nullable=False),
    Column("enabled", Boolean, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    # The implementation requirement — upstream identity (xy-code, human name, narrator
    # learning model id). Nullable; populated by the seeder.
    Column("code", String, nullable=True),
    Column("name", String, nullable=True),
    Column("learning_model_id", String, nullable=True),
    # The implementation requirement — upstream-sourced video duration (seconds). Used by
    # the 3-min tiered pricing formula. Nullable; populated by the
    # refresh script.
    Column("video_duration_seconds", Integer, nullable=True),
    CheckConstraint("tier_multiplier > 0"),
)


pricing_catalog_v2_family = Table(
    "pricing_catalog_v2_family",
    metadata,
    Column("template_family_id", String, primary_key=True),
    Column("tier_code", String, primary_key=True),
    Column("effective_version", Integer, primary_key=True),
    Column("product_line", String, nullable=False),
    Column("mode", String, nullable=True),
    Column("quality", String, nullable=True),
    Column("flash_pro_axis", String, nullable=False),
    Column("manual_price", Integer, nullable=False),
    Column("pro_surcharge_display", Integer, nullable=True),
    Column("system_reference_price", Integer, nullable=False),
    Column("currency_unit", String, nullable=False),
    Column("raw_rate", Numeric(10, 5), nullable=False),
    Column("final_rate", Numeric(6, 2), nullable=False),
    Column("rounding_rule_version", String, nullable=False),
    Column("manual_override_warning", Boolean, nullable=False),
    Column("enabled", Boolean, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    Column("updated_by", String, nullable=False),
    CheckConstraint("flash_pro_axis IN ('required', 'optional')"),
    CheckConstraint(
        "flash_pro_axis = 'required' OR (mode IS NULL AND quality IS NULL)",
        name="flash_pro_axis_optional_nullifies_mode_quality",
    ),
    CheckConstraint("manual_price >= 0"),
    CheckConstraint("pro_surcharge_display IS NULL OR pro_surcharge_display >= 0"),
    CheckConstraint("system_reference_price >= 0"),
    CheckConstraint("effective_version > 0"),
)
# Partial index on Postgres (lookup only the latest enabled row for a
# (family, tier_code)); SQLite ignores `postgresql_where` and creates
# a plain index. Either way the lookup pattern works — Postgres just
# does it faster.
Index(
    "pricing_catalog_v2_family_current_idx",
    pricing_catalog_v2_family.c.template_family_id,
    pricing_catalog_v2_family.c.tier_code,
    pricing_catalog_v2_family.c.effective_version.desc(),
    postgresql_where=pricing_catalog_v2_family.c.enabled.is_(True),
)


pricing_catalog_v2_entry = Table(
    "pricing_catalog_v2_entry",
    metadata,
    # UUID-as-string so the same column type works on SQLite (tests) and
    # Postgres (prod). Application code generates uuid4 hex; we don't
    # rely on pgcrypto's gen_random_uuid().
    Column("catalog_entry_id", String, primary_key=True),
    Column("template_id", String, nullable=False),
    Column("tier_code", String, nullable=False),
    Column("product_line", String, nullable=False),
    Column("mode", String, nullable=True),
    Column("quality", String, nullable=True),
    Column("flash_pro_axis", String, nullable=False),
    Column("manual_price", Integer, nullable=False),
    Column("pro_surcharge_display", Integer, nullable=True),
    Column("system_reference_price", Integer, nullable=False),
    Column("currency_unit", String, nullable=False),
    Column("raw_rate", Numeric(10, 5), nullable=False),
    Column("final_rate", Numeric(6, 2), nullable=False),
    Column("rounding_rule_version", String, nullable=False),
    Column("manual_override_warning", Boolean, nullable=False),
    Column("enabled", Boolean, nullable=False),
    Column("effective_version", Integer, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    Column("updated_by", String, nullable=False),
    UniqueConstraint("template_id", "tier_code", "effective_version"),
    CheckConstraint("flash_pro_axis IN ('required', 'optional')"),
    CheckConstraint(
        "flash_pro_axis = 'required' OR (mode IS NULL AND quality IS NULL)",
        name="flash_pro_axis_optional_nullifies_mode_quality",
    ),
    CheckConstraint("manual_price >= 0"),
    CheckConstraint("pro_surcharge_display IS NULL OR pro_surcharge_display >= 0"),
    CheckConstraint("system_reference_price >= 0"),
    CheckConstraint("effective_version > 0"),
)
Index(
    "pricing_catalog_v2_entry_current_idx",
    pricing_catalog_v2_entry.c.template_id,
    pricing_catalog_v2_entry.c.tier_code,
    pricing_catalog_v2_entry.c.effective_version.desc(),
    postgresql_where=pricing_catalog_v2_entry.c.enabled.is_(True),
)


# Hard-price v2 quote + snapshot (regression coverage / narrator-ai-Web API contract).
# Quote = every reported price. Snapshot = immutable per-order record
# written on master-task commit. narrator_tasks gets a nullable
# snapshot_id FK so v2 master-task rows can join to their snapshot;
# v1 master-task rows keep snapshot_id NULL and behave unchanged.

pricing_quotes_v2 = Table(
    "pricing_quotes_v2",
    metadata,
    Column("quote_id", String, primary_key=True),
    Column("pricing_rule_version", String, nullable=False),
    Column("price_source", String, nullable=False),
    Column("template_id", String, nullable=True),
    # Canonical upstream xy-code (e.g. "xy0178"); added in regression coverage so the
    # quote audit trail records the identifier the caller actually used,
    # not just the derived numeric template_id (which is NOT unique
    # against narrator's CSV.id space). Nullable for legacy rows
    # written before the column existed.
    Column("code", String, nullable=True),
    Column("custom_template_id", String, nullable=True),
    Column("combo_key", String, nullable=False),
    Column("pro_upgrade", Boolean, nullable=False),
    Column("starting_price", Integer, nullable=True),
    Column("final_charge_price", Integer, nullable=False),
    Column("flash_total", Integer, nullable=False),
    Column("pro_total", Integer, nullable=False),
    Column("pro_upgrade_delta", Integer, nullable=False),
    Column("pricing_minutes", Numeric(10, 4), nullable=False),
    Column("valid_line_count", Integer, nullable=True),
    Column("srt_file_hash", String, nullable=True),
    # The cloud-drive file_id the SRT was fetched from at quote time.
    # Bound at commit (§6.2) so a caller can't quote one SRT and
    # commit a different one.
    Column("custom_srt_file_id", String, nullable=True),
    Column("system_reference_price", Integer, nullable=False),
    # Stored as JSON-encoded TEXT for SQLite portability + replayable
    # audit. The store layer (de)serializes on read/write.
    Column("breakdown", String, nullable=False),
    Column("currency_unit", String, nullable=False),
    Column("expires_at", DateTime(timezone=True), nullable=False),
    Column("committed_at", DateTime(timezone=True), nullable=True),
    Column("web_user_id", Integer, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    CheckConstraint(
        "price_source IN ('manual_catalog_price', 'system_calculated_price')"
    ),
    CheckConstraint("final_charge_price >= 0"),
    CheckConstraint("flash_total >= 0"),
    CheckConstraint("pro_total >= 0"),
    CheckConstraint("pro_upgrade_delta >= 0"),
    CheckConstraint("system_reference_price >= 0"),
    CheckConstraint(
        "(template_id IS NOT NULL) <> (custom_template_id IS NOT NULL)",
        name="exactly_one_template_id",
    ),
)
Index(
    "pricing_quotes_v2_user_idx",
    pricing_quotes_v2.c.web_user_id,
    pricing_quotes_v2.c.created_at.desc(),
)


pricing_snapshots_v2 = Table(
    "pricing_snapshots_v2",
    metadata,
    Column("snapshot_id", String, primary_key=True),
    Column(
        "quote_id",
        String,
        ForeignKey("pricing_quotes_v2.quote_id"),
        nullable=False,
        unique=True,
    ),
    Column("pricing_rule_version", String, nullable=False),
    Column("combo_key", String, nullable=False),
    Column("price_source", String, nullable=False),
    Column("template_id", String, nullable=True),
    # Mirrors pricing_quotes_v2.code (the implementation requirement). Copied from the
    # bound quote at snapshot time so master-task commit binding can
    # match by code (preferred) and fall back to template_id only when
    # both quote and request body have no code.
    Column("code", String, nullable=True),
    Column("custom_template_id", String, nullable=True),
    Column("template_duration", Numeric(10, 4), nullable=True),
    Column("pricing_minutes", Numeric(10, 4), nullable=False),
    Column("valid_line_count", Integer, nullable=True),
    Column("srt_file_hash", String, nullable=True),
    Column("system_reference_price", Integer, nullable=False),
    Column("manual_catalog_price", Integer, nullable=True),
    Column("system_calculated_price", Integer, nullable=True),
    Column("final_charge_price", Integer, nullable=False),
    Column("breakdown", String, nullable=False),
    Column("currency_unit", String, nullable=False),
    Column("committed_at", DateTime(timezone=True), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    # v1 refund placeholders (regression coverage §7.1) — schema reserves the
    # columns so v1.x partial-by-subflow automation lands without a
    # second migration.
    Column("refund_policy", String, nullable=False),
    Column("refund_status", String, nullable=False),
    Column("subflow_status", String, nullable=False),
    Column("web_user_id", Integer, nullable=False),
    CheckConstraint("final_charge_price >= 0"),
    CheckConstraint(
        "refund_policy IN ('manual', 'all', 'partial_by_subflow')"
    ),
)
Index(
    "pricing_snapshots_v2_user_idx",
    pricing_snapshots_v2.c.web_user_id,
    pricing_snapshots_v2.c.committed_at.desc(),
)


# narrator_tasks.snapshot_id is added via the same migration. The
# ALTER TABLE runs after pricing_snapshots_v2 is created so the FK is
# valid. SQLAlchemy model below mirrors the new column.
narrator_tasks.append_column(
    Column(
        "snapshot_id",
        String,
        ForeignKey("pricing_snapshots_v2.snapshot_id"),
        nullable=True,
    )
)


user_cloud_files = Table(
    "user_cloud_files",
    metadata,
    Column("id", BigInteger, primary_key=True),
    Column("reservation_id", String, nullable=False, unique=True),
    Column("user_id", Integer, ForeignKey("users.id"), nullable=False),
    Column("app_key", String, nullable=False),
    Column("file_id", String, unique=True),
    Column("object_key", String),
    Column("file_name", String, nullable=False),
    Column("suffix", String, nullable=False),
    Column("category", Integer, nullable=False),
    Column("file_size", BigInteger, nullable=False),
    Column("content_type", String),
    Column("source", String, nullable=False),
    Column("status", String, nullable=False),
    Column("upstream_status", Integer),
    Column("progress", Integer, nullable=False),
    Column("srt_file_hash", String),
    Column("upstream_payload", JSON, nullable=False),
    # Upstream upload identifier (e.g. `baidu-<hash>`) for link transfers.
    # Set on the parent reservation by the async submission worker and
    # denormalized onto each child row by the refresh fan-out .
    Column("upload_id", String),
    # Child rows produced by the refresh fan-out reference their parent
    # reservation here. NULL on parents and on local uploads.
    Column("parent_reservation_id", String),
    # Marks a parent reservation whose children have all reached
    # terminal upstream status. Once set, the GET handler hides the
    # parent and the refresh loop stops polling.
    Column("settled_at", DateTime(timezone=True)),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    Column("completed_at", DateTime(timezone=True)),
    Column("deleted_at", DateTime(timezone=True)),
    CheckConstraint("file_size >= 0"),
    CheckConstraint("progress >= 0 AND progress <= 100"),
    CheckConstraint("source IN ('local_upload', 'transfer')"),
    CheckConstraint(
        "status IN ('reserved', 'completed', 'failed', 'delete_pending', 'deleted', 'transfer_pending', 'transfer_running', 'transfer_completed')"
    ),
)
Index(
    "user_cloud_files_owner_status_idx",
    user_cloud_files.c.user_id,
    user_cloud_files.c.status,
    user_cloud_files.c.updated_at.desc(),
)
Index(
    "user_cloud_files_owner_file_idx",
    user_cloud_files.c.user_id,
    user_cloud_files.c.file_id,
)
Index(
    "user_cloud_files_unsettled_upload_idx",
    user_cloud_files.c.user_id,
    user_cloud_files.c.upload_id,
    postgresql_where=(
        user_cloud_files.c.upload_id.is_not(None)
        & user_cloud_files.c.parent_reservation_id.is_(None)
        & user_cloud_files.c.settled_at.is_(None)
    ),
)
