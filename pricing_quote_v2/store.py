"""CRUD helpers for pricing_quotes_v2 and pricing_snapshots_v2 .

The route layer owns the connection + transaction. Helpers here are
plain inserts / lookups; all business logic lives in `service.py`.
"""

from __future__ import annotations

import json
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Optional

from sqlalchemy import insert, select
from sqlalchemy.engine import Connection
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from db.tables import (
    narrator_tasks,
    pricing_quotes_v2,
    pricing_snapshots_v2,
)

from .errors import (
    QuoteNotFound,
    QuotePersistenceError,
    SnapshotQuoteCollision,
)


@dataclass(frozen=True)
class PricingQuote:
    quote_id: str
    pricing_rule_version: str
    price_source: str
    template_id: Optional[str]
    # Canonical upstream xy-code (e.g. "xy0178"); None for legacy rows
    # written before the implementation requirement and for custom-template quotes.
    code: Optional[str]
    custom_template_id: Optional[str]
    combo_key: str
    pro_upgrade: bool
    starting_price: Optional[int]
    final_charge_price: int
    flash_total: int
    pro_total: int
    pro_upgrade_delta: int
    pricing_minutes: Decimal
    valid_line_count: Optional[int]
    srt_file_hash: Optional[str]
    custom_srt_file_id: Optional[str]
    system_reference_price: int
    breakdown: list[dict[str, Any]]
    currency_unit: str
    expires_at: datetime
    committed_at: Optional[datetime]
    web_user_id: int
    created_at: datetime


@dataclass(frozen=True)
class PricingSnapshot:
    snapshot_id: str
    quote_id: str
    pricing_rule_version: str
    combo_key: str
    price_source: str
    template_id: Optional[str]
    # Copied from the bound quote at snapshot time (the implementation requirement).
    code: Optional[str]
    custom_template_id: Optional[str]
    template_duration: Optional[Decimal]
    pricing_minutes: Decimal
    valid_line_count: Optional[int]
    srt_file_hash: Optional[str]
    system_reference_price: int
    manual_catalog_price: Optional[int]
    system_calculated_price: Optional[int]
    final_charge_price: int
    breakdown: list[dict[str, Any]]
    currency_unit: str
    committed_at: datetime
    refund_policy: str
    refund_status: str
    subflow_status: list[dict[str, Any]]
    web_user_id: int
    created_at: datetime


def new_quote_id() -> str:
    """Format `Q-<UTC-yyyy-mm-dd>-<10 hex>`. Human-eyeballable in logs."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return f"Q-{today}-{secrets.token_hex(5)}"


def new_snapshot_id() -> str:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return f"S-{today}-{secrets.token_hex(5)}"


def insert_quote(conn: Connection, *, row: dict[str, Any]) -> str:
    """Write a freshly-built quote row. The route layer assigns
    `quote_id` (via `new_quote_id()`) and stamps timestamps; this
    helper just performs the INSERT and bubbles SQLAlchemyError as
    QuotePersistenceError.
    """
    try:
        # `breakdown` is a list[dict] in the dataclass world but a JSON
        # string on disk for SQLite portability + audit re-replay.
        stored = dict(row)
        if isinstance(stored.get("breakdown"), list):
            stored["breakdown"] = json.dumps(stored["breakdown"])
        conn.execute(insert(pricing_quotes_v2).values(**stored))
    except SQLAlchemyError as error:
        raise QuotePersistenceError(
            "Failed to insert quote row.",
            details={"error_class": error.__class__.__name__},
        ) from error
    return row["quote_id"]


def get_quote(conn: Connection, quote_id: str) -> PricingQuote:
    """Lookup by `quote_id`. Raises QuoteNotFound on miss,
    QuotePersistenceError on DB failure."""
    try:
        stmt = select(pricing_quotes_v2).where(
            pricing_quotes_v2.c.quote_id == quote_id
        )
        row = conn.execute(stmt).mappings().first()
    except SQLAlchemyError as error:
        raise QuotePersistenceError(
            "Failed to read quote row.",
            details={"error_class": error.__class__.__name__},
        ) from error
    if row is None:
        raise QuoteNotFound(quote_id)
    breakdown_raw = row["breakdown"]
    if isinstance(breakdown_raw, str):
        breakdown = json.loads(breakdown_raw)
    elif isinstance(breakdown_raw, list):
        breakdown = breakdown_raw
    else:
        breakdown = []
    return PricingQuote(
        quote_id=row["quote_id"],
        pricing_rule_version=row["pricing_rule_version"],
        price_source=row["price_source"],
        template_id=row["template_id"],
        code=row["code"],
        custom_template_id=row["custom_template_id"],
        combo_key=row["combo_key"],
        pro_upgrade=bool(row["pro_upgrade"]),
        starting_price=row["starting_price"],
        final_charge_price=int(row["final_charge_price"]),
        flash_total=int(row["flash_total"]),
        pro_total=int(row["pro_total"]),
        pro_upgrade_delta=int(row["pro_upgrade_delta"]),
        pricing_minutes=_to_decimal(row["pricing_minutes"]),
        valid_line_count=row["valid_line_count"],
        srt_file_hash=row["srt_file_hash"],
        custom_srt_file_id=row["custom_srt_file_id"],
        system_reference_price=int(row["system_reference_price"]),
        breakdown=breakdown,
        currency_unit=row["currency_unit"],
        expires_at=_aware_utc(row["expires_at"]),
        committed_at=_aware_utc(row["committed_at"]),
        web_user_id=int(row["web_user_id"]),
        created_at=_aware_utc(row["created_at"]),
    )


def insert_snapshot(conn: Connection, *, row: dict[str, Any]) -> str:
    """Write a snapshot row inside the caller's transaction.

    Raises `SnapshotQuoteCollision` when the insert hits the
    `pricing_snapshots_v2.quote_id` UNIQUE constraint — the service
    layer uses this signal to recover from a concurrent-commit race
    without surfacing it as a 503 to the caller.
    """
    try:
        stored = dict(row)
        if isinstance(stored.get("breakdown"), list):
            stored["breakdown"] = json.dumps(stored["breakdown"])
        if isinstance(stored.get("subflow_status"), list):
            stored["subflow_status"] = json.dumps(stored["subflow_status"])
        conn.execute(insert(pricing_snapshots_v2).values(**stored))
    except IntegrityError as error:
        raise SnapshotQuoteCollision(quote_id=row["quote_id"]) from error
    except SQLAlchemyError as error:
        raise QuotePersistenceError(
            "Failed to insert snapshot row.",
            details={"error_class": error.__class__.__name__},
        ) from error
    return row["snapshot_id"]


def get_snapshot_id_by_quote(
    conn: Connection, quote_id: str
) -> Optional[tuple[str, Optional[str]]]:
    """For idempotency: return `(snapshot_id, linked_narrator_task_id)`
    for a previously-committed quote, or `None` when no snapshot
    exists yet. The narrator_task_id is the row from
    `narrator_tasks.snapshot_id` (may be NULL if the FK wasn't yet
    written when the prior commit failed mid-transaction)."""
    try:
        snap_row = conn.execute(
            select(pricing_snapshots_v2.c.snapshot_id).where(
                pricing_snapshots_v2.c.quote_id == quote_id
            )
        ).first()
        if snap_row is None:
            return None
        snapshot_id = snap_row[0]
        task_row = conn.execute(
            select(narrator_tasks.c.narrator_task_id).where(
                narrator_tasks.c.snapshot_id == snapshot_id
            )
        ).first()
        linked_task_id = task_row[0] if task_row else None
        return snapshot_id, linked_task_id
    except SQLAlchemyError as error:
        raise QuotePersistenceError(
            "Failed to look up snapshot by quote_id.",
            details={"error_class": error.__class__.__name__},
        ) from error


def attach_snapshot_to_master_task(
    conn: Connection, *, narrator_task_id: str, snapshot_id: str
) -> None:
    """Set narrator_tasks.snapshot_id for a row that was just committed
    via the master-task flow. Caller-managed transaction."""
    try:
        conn.execute(
            narrator_tasks.update()
            .where(narrator_tasks.c.narrator_task_id == narrator_task_id)
            .values(snapshot_id=snapshot_id)
        )
    except SQLAlchemyError as error:
        raise QuotePersistenceError(
            "Failed to attach snapshot to master task.",
            details={"error_class": error.__class__.__name__},
        ) from error


def mark_quote_committed(
    conn: Connection, *, quote_id: str, committed_at: datetime
) -> None:
    """Stamp committed_at on the quote row so audit can join
    committed quotes vs un-committed ones."""
    try:
        conn.execute(
            pricing_quotes_v2.update()
            .where(pricing_quotes_v2.c.quote_id == quote_id)
            .values(committed_at=committed_at)
        )
    except SQLAlchemyError as error:
        raise QuotePersistenceError(
            "Failed to stamp committed_at on quote.",
            details={"error_class": error.__class__.__name__},
        ) from error


def _to_decimal(value: Any) -> Decimal:
    if value is None:
        return Decimal(0)
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def _aware_utc(value: Any) -> Optional[datetime]:
    """Normalize a datetime read from the DB to UTC-aware. SQLite drops
    tzinfo on roundtrip; PostgreSQL `TIMESTAMP WITH TIME ZONE` already
    returns aware. This makes both engines consistent so the §6.1
    lock-timing arithmetic doesn't blow up under SQLite tests."""
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value
    # SQLite may return TEXT for TIMESTAMP columns when CURRENT_TIMESTAMP
    # was used as a default. Best-effort parse via fromisoformat.
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed
