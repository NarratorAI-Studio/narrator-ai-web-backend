from __future__ import annotations

import csv
from datetime import datetime, timezone
from decimal import Decimal
from io import StringIO
import re
from typing import Any, Iterable

from sqlalchemy import and_, case, func, literal, select

from db.tables import wallet_ledger_entries, wallet_quotes, wallet_transactions
from wallet.common import iso_z, money, money_str


FINANCE_RECONCILIATION_COLUMNS = [
    "period_start_utc",
    "period_end_utc",
    "web_tenant_id",
    "web_user_id",
    "web_order_id",
    "quote_id",
    "wallet_transaction_id",
    "state",
    "template_id",
    "combo_key",
    "pricing_rule_version",
    "quoted_hard_price_points",
    "transaction_amount_points",
    "freeze_ledger_points",
    "confirm_ledger_points",
    "refund_ledger_points",
    "net_confirmed_points",
    "delta_quote_vs_transaction_points",
    "delta_transaction_vs_ledger_points",
    "risk_grade",
    "risk_reasons",
    "frozen_at",
    "confirmed_at",
    "refunded_at",
    "quote_created_at",
    "transaction_updated_at",
    "web_master_task_id",
    "api_task_id",
    "api_request_id",
    "refund_reason_code",
    "refund_reason_message",
]


def month_bounds(month: str) -> tuple[datetime, datetime]:
    if not re.fullmatch(r"\d{4}-\d{2}", month):
        raise ValueError("month must use YYYY-MM with month 01-12")
    year_text, month_text = month.split("-", 1)
    year = int(year_text)
    month_number = int(month_text)
    if month_number < 1 or month_number > 12:
        raise ValueError("month must use YYYY-MM with month 01-12")
    start = datetime(year, month_number, 1, tzinfo=timezone.utc)
    if month_number == 12:
        end = datetime(year + 1, 1, 1, tzinfo=timezone.utc)
    else:
        end = datetime(year, month_number + 1, 1, tzinfo=timezone.utc)
    return start, end


def build_reconciliation_query(start: datetime, end: datetime):
    period_transactions = (
        select(wallet_transactions.c.wallet_transaction_id)
        .where(
            and_(
                wallet_transactions.c.created_at >= start,
                wallet_transactions.c.created_at < end,
            )
        )
        .cte("period_wallet_transactions")
    )
    freeze_amount = case(
        (
            wallet_ledger_entries.c.entry_type == "FREEZE",
            wallet_ledger_entries.c.amount_points,
        ),
        else_=literal(0),
    )
    confirm_amount = case(
        (
            wallet_ledger_entries.c.entry_type == "CONFIRM",
            wallet_ledger_entries.c.amount_points,
        ),
        else_=literal(0),
    )
    refund_amount = case(
        (
            wallet_ledger_entries.c.entry_type == "REFUND",
            wallet_ledger_entries.c.amount_points,
        ),
        else_=literal(0),
    )
    ledger_summary = (
        select(
            wallet_ledger_entries.c.wallet_transaction_id,
            func.sum(freeze_amount).label("freeze_ledger_points"),
            func.sum(confirm_amount).label("confirm_ledger_points"),
            func.sum(refund_amount).label("refund_ledger_points"),
            func.count().label("ledger_entry_count"),
        )
        .select_from(
            wallet_ledger_entries.join(
                period_transactions,
                wallet_ledger_entries.c.wallet_transaction_id
                == period_transactions.c.wallet_transaction_id,
            )
        )
        .group_by(wallet_ledger_entries.c.wallet_transaction_id)
        .subquery()
    )

    return (
        select(
            wallet_quotes.c.web_tenant_id,
            wallet_quotes.c.web_user_id,
            wallet_quotes.c.web_order_id,
            wallet_quotes.c.quote_id,
            wallet_transactions.c.wallet_transaction_id,
            wallet_transactions.c.state,
            wallet_quotes.c.template_id,
            wallet_quotes.c.combo_key,
            wallet_transactions.c.pricing_rule_version,
            wallet_quotes.c.hard_price.label("quoted_hard_price_points"),
            wallet_transactions.c.amount_points.label("transaction_amount_points"),
            func.coalesce(ledger_summary.c.freeze_ledger_points, 0).label(
                "freeze_ledger_points"
            ),
            func.coalesce(ledger_summary.c.confirm_ledger_points, 0).label(
                "confirm_ledger_points"
            ),
            func.coalesce(ledger_summary.c.refund_ledger_points, 0).label(
                "refund_ledger_points"
            ),
            wallet_transactions.c.frozen_at,
            wallet_transactions.c.confirmed_at,
            wallet_transactions.c.refunded_at,
            wallet_quotes.c.created_at.label("quote_created_at"),
            wallet_transactions.c.updated_at.label("transaction_updated_at"),
            wallet_transactions.c.correlation,
            wallet_transactions.c.refund_reason_code,
            wallet_transactions.c.refund_reason_message,
            func.coalesce(ledger_summary.c.ledger_entry_count, 0).label(
                "ledger_entry_count"
            ),
        )
        .select_from(
            wallet_transactions.join(
                wallet_quotes,
                wallet_transactions.c.quote_id == wallet_quotes.c.quote_id,
            ).outerjoin(
                ledger_summary,
                wallet_transactions.c.wallet_transaction_id
                == ledger_summary.c.wallet_transaction_id,
            )
        )
        .where(
            and_(
                wallet_transactions.c.created_at >= start,
                wallet_transactions.c.created_at < end,
            )
        )
        .order_by(
            wallet_transactions.c.created_at,
            wallet_transactions.c.web_order_id,
        )
    )


def normalize_reconciliation_row(
    row: dict[str, Any], start: datetime, end: datetime
) -> dict[str, str]:
    amount = money(row["transaction_amount_points"])
    quote_amount = money(row["quoted_hard_price_points"])
    confirm_amount = money(row["confirm_ledger_points"])
    refund_amount = money(row["refund_ledger_points"])
    if row["state"] == "CONFIRMED":
        net_confirmed = confirm_amount - refund_amount
        ledger_delta = amount - confirm_amount
    elif row["state"] == "REFUNDED":
        net_confirmed = Decimal("0.00")
        ledger_delta = amount - refund_amount
    else:
        net_confirmed = Decimal("0.00")
        ledger_delta = amount - money(row["freeze_ledger_points"])

    risk_grade, reasons = classify_reconciliation_risk(
        row=row,
        quote_delta=quote_amount - amount,
        ledger_delta=ledger_delta,
        period_end=end,
    )
    correlation = row.get("correlation") or {}
    return {
        "period_start_utc": iso_z(start),
        "period_end_utc": iso_z(end),
        "web_tenant_id": row["web_tenant_id"],
        "web_user_id": row["web_user_id"],
        "web_order_id": row["web_order_id"],
        "quote_id": row["quote_id"],
        "wallet_transaction_id": row["wallet_transaction_id"],
        "state": row["state"],
        "template_id": str(row["template_id"]),
        "combo_key": row["combo_key"],
        "pricing_rule_version": str(row["pricing_rule_version"]),
        "quoted_hard_price_points": money_str(quote_amount),
        "transaction_amount_points": money_str(amount),
        "freeze_ledger_points": money_str(row["freeze_ledger_points"]),
        "confirm_ledger_points": money_str(confirm_amount),
        "refund_ledger_points": money_str(refund_amount),
        "net_confirmed_points": money_str(net_confirmed),
        "delta_quote_vs_transaction_points": money_str(quote_amount - amount),
        "delta_transaction_vs_ledger_points": money_str(ledger_delta),
        "risk_grade": risk_grade,
        "risk_reasons": "|".join(reasons),
        "frozen_at": _date(row.get("frozen_at")),
        "confirmed_at": _date(row.get("confirmed_at")),
        "refunded_at": _date(row.get("refunded_at")),
        "quote_created_at": _date(row.get("quote_created_at")),
        "transaction_updated_at": _date(row.get("transaction_updated_at")),
        "web_master_task_id": str(correlation.get("web_master_task_id") or ""),
        "api_task_id": str(correlation.get("api_task_id") or ""),
        "api_request_id": str(correlation.get("api_request_id") or ""),
        "refund_reason_code": str(row.get("refund_reason_code") or ""),
        "refund_reason_message": str(row.get("refund_reason_message") or ""),
    }


def classify_reconciliation_risk(
    *,
    row: dict[str, Any],
    quote_delta: Decimal,
    ledger_delta: Decimal,
    period_end: datetime,
) -> tuple[str, list[str]]:
    reasons = []
    if quote_delta != Decimal("0.00"):
        reasons.append("QUOTE_TRANSACTION_AMOUNT_MISMATCH")
    if ledger_delta != Decimal("0.00"):
        reasons.append("TRANSACTION_LEDGER_AMOUNT_MISMATCH")
    if row["state"] == "CONFIRMED" and not row.get("confirmed_at"):
        reasons.append("CONFIRMED_WITHOUT_CONFIRMED_AT")
    if row["state"] == "CONFIRMED" and not (row.get("correlation") or {}).get(
        "api_task_id"
    ):
        reasons.append("CONFIRMED_WITHOUT_API_TASK_ID")
    if row["state"] == "REFUNDED" and not row.get("refunded_at"):
        reasons.append("REFUNDED_WITHOUT_REFUNDED_AT")
    if row["state"] == "REFUNDED" and not row.get("refund_reason_code"):
        reasons.append("REFUNDED_WITHOUT_REASON_CODE")
    if (
        row["state"] == "FROZEN"
        and row.get("frozen_at")
        and row["frozen_at"] < period_end
    ):
        reasons.append("FROZEN_OPEN_AT_PERIOD_CLOSE")
    if row.get("ledger_entry_count") == 0:
        reasons.append("MISSING_LEDGER_ENTRIES")

    if any("AMOUNT_MISMATCH" in reason for reason in reasons):
        return "P0", reasons
    if reasons:
        return "P1", reasons
    return "OK", ["BALANCED"]


def render_reconciliation_csv(rows: Iterable[dict[str, str]]) -> str:
    output = StringIO()
    writer = csv.DictWriter(output, fieldnames=FINANCE_RECONCILIATION_COLUMNS)
    writer.writeheader()
    for row in rows:
        writer.writerow(row)
    return output.getvalue()


def summarize_reconciliation(rows: Iterable[dict[str, str]]) -> dict[str, str]:
    total_rows = 0
    totals = {
        "OK": 0,
        "P0": 0,
        "P1": 0,
        "confirmed_points": Decimal("0.00"),
        "refunded_points": Decimal("0.00"),
        "open_frozen_points": Decimal("0.00"),
    }
    for row in rows:
        total_rows += 1
        totals[row["risk_grade"]] += 1
        if row["state"] == "CONFIRMED":
            totals["confirmed_points"] += money(row["net_confirmed_points"])
            totals["refunded_points"] += money(row["refund_ledger_points"])
        elif row["state"] == "REFUNDED":
            totals["refunded_points"] += money(row["refund_ledger_points"])
        elif row["state"] == "FROZEN":
            totals["open_frozen_points"] += money(row["freeze_ledger_points"])
    return {
        "rows": str(total_rows),
        "ok_rows": str(totals["OK"]),
        "p0_rows": str(totals["P0"]),
        "p1_rows": str(totals["P1"]),
        "confirmed_points": money_str(totals["confirmed_points"]),
        "refunded_points": money_str(totals["refunded_points"]),
        "open_frozen_points": money_str(totals["open_frozen_points"]),
    }


def _date(value: Any) -> str:
    if not value:
        return ""
    return iso_z(value)
