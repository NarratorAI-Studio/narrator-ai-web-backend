from datetime import datetime, timezone
from pathlib import Path
import sys

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from sqlalchemy.dialects import postgresql  # noqa: E402

from finance import (  # noqa: E402
    FINANCE_RECONCILIATION_COLUMNS,
    build_reconciliation_query,
    month_bounds,
    render_reconciliation_csv,
    summarize_reconciliation,
)
from finance.reconciliation import normalize_reconciliation_row  # noqa: E402


def test_reconciliation_query_compiles_against_wallet_tables():
    start, end = month_bounds("2026-05")
    compiled = str(
        build_reconciliation_query(start, end).compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )

    assert "wallet_quotes" in compiled
    assert "wallet_transactions" in compiled
    assert "wallet_ledger_entries" in compiled
    assert "wallet_transactions.created_at >= '2026-05-01" in compiled
    assert "wallet_transactions.created_at < '2026-06-01" in compiled
    assert "WITH period_wallet_transactions AS" in compiled


@pytest.mark.parametrize("month", ["2026-5", "202605", "2026-00", "2026-13"])
def test_month_bounds_rejects_invalid_months(month):
    with pytest.raises(ValueError, match="month must use YYYY-MM"):
        month_bounds(month)


def test_reconciliation_csv_and_summary_cover_finance_close_rows():
    start, end = month_bounds("2026-05")
    row = normalize_reconciliation_row(
        {
            "web_tenant_id": "toc",
            "web_user_id": "web_user_123",
            "web_order_id": "wo_123",
            "quote_id": "wq_123",
            "wallet_transaction_id": "wtx_123",
            "state": "CONFIRMED",
            "template_id": 42,
            "combo_key": "original_narration_flash",
            "pricing_rule_version": 7,
            "quoted_hard_price_points": "84.50",
            "transaction_amount_points": "84.50",
            "freeze_ledger_points": "84.50",
            "confirm_ledger_points": "84.50",
            "refund_ledger_points": "0.00",
            "frozen_at": datetime(2026, 5, 12, 8, 1, tzinfo=timezone.utc),
            "confirmed_at": datetime(2026, 5, 12, 8, 3, tzinfo=timezone.utc),
            "refunded_at": None,
            "quote_created_at": datetime(2026, 5, 12, 8, 0, tzinfo=timezone.utc),
            "transaction_updated_at": datetime(2026, 5, 12, 8, 3, tzinfo=timezone.utc),
            "correlation": {
                "web_master_task_id": "wmt_123",
                "api_task_id": "api_task_456",
                "api_request_id": "req_789",
            },
            "refund_reason_code": None,
            "refund_reason_message": None,
            "ledger_entry_count": 2,
        },
        start,
        end,
    )

    assert list(row.keys()) == FINANCE_RECONCILIATION_COLUMNS
    assert row["risk_grade"] == "OK"
    assert row["risk_reasons"] == "BALANCED"
    assert row["net_confirmed_points"] == "84.50"
    csv_text = render_reconciliation_csv([row])
    assert "web_order_id" in csv_text
    assert "wo_123" in csv_text
    assert summarize_reconciliation([row]) == {
        "rows": "1",
        "ok_rows": "1",
        "p0_rows": "0",
        "p1_rows": "0",
        "confirmed_points": "84.50",
        "refunded_points": "0.00",
        "open_frozen_points": "0.00",
    }


def test_reconciliation_risk_grades_amount_mismatch_and_open_frozen():
    start, end = month_bounds("2026-05")
    mismatch = normalize_reconciliation_row(
        {
            "web_tenant_id": "toc",
            "web_user_id": "web_user_123",
            "web_order_id": "wo_bad",
            "quote_id": "wq_bad",
            "wallet_transaction_id": "wtx_bad",
            "state": "CONFIRMED",
            "template_id": 42,
            "combo_key": "original_narration_flash",
            "pricing_rule_version": 7,
            "quoted_hard_price_points": "84.50",
            "transaction_amount_points": "80.00",
            "freeze_ledger_points": "80.00",
            "confirm_ledger_points": "79.00",
            "refund_ledger_points": "0.00",
            "frozen_at": datetime(2026, 5, 12, 8, 1, tzinfo=timezone.utc),
            "confirmed_at": datetime(2026, 5, 12, 8, 3, tzinfo=timezone.utc),
            "refunded_at": None,
            "quote_created_at": datetime(2026, 5, 12, 8, 0, tzinfo=timezone.utc),
            "transaction_updated_at": datetime(2026, 5, 12, 8, 3, tzinfo=timezone.utc),
            "correlation": {},
            "refund_reason_code": None,
            "refund_reason_message": None,
            "ledger_entry_count": 2,
        },
        start,
        end,
    )
    frozen = normalize_reconciliation_row(
        {
            "web_tenant_id": "toc",
            "web_user_id": "web_user_123",
            "web_order_id": "wo_frozen",
            "quote_id": "wq_frozen",
            "wallet_transaction_id": "wtx_frozen",
            "state": "FROZEN",
            "template_id": 42,
            "combo_key": "original_narration_flash",
            "pricing_rule_version": 7,
            "quoted_hard_price_points": "84.50",
            "transaction_amount_points": "84.50",
            "freeze_ledger_points": "84.50",
            "confirm_ledger_points": "0.00",
            "refund_ledger_points": "0.00",
            "frozen_at": datetime(2026, 5, 30, 8, 1, tzinfo=timezone.utc),
            "confirmed_at": None,
            "refunded_at": None,
            "quote_created_at": datetime(2026, 5, 30, 8, 0, tzinfo=timezone.utc),
            "transaction_updated_at": datetime(2026, 5, 30, 8, 1, tzinfo=timezone.utc),
            "correlation": {"web_master_task_id": "wmt_123"},
            "refund_reason_code": None,
            "refund_reason_message": None,
            "ledger_entry_count": 1,
        },
        start,
        end,
    )

    assert mismatch["risk_grade"] == "P0"
    assert "QUOTE_TRANSACTION_AMOUNT_MISMATCH" in mismatch["risk_reasons"]
    assert "TRANSACTION_LEDGER_AMOUNT_MISMATCH" in mismatch["risk_reasons"]
    assert frozen["risk_grade"] == "P1"
    assert frozen["risk_reasons"] == "FROZEN_OPEN_AT_PERIOD_CLOSE"


def test_reconciliation_counts_confirmed_overbill_refunds_as_net_revenue():
    start, end = month_bounds("2026-05")
    row = normalize_reconciliation_row(
        {
            "web_tenant_id": "toc",
            "web_user_id": "web_user_123",
            "web_order_id": "wo_overbill",
            "quote_id": "wq_overbill",
            "wallet_transaction_id": "wtx_overbill",
            "state": "CONFIRMED",
            "template_id": 42,
            "combo_key": "original_narration_flash",
            "pricing_rule_version": 7,
            "quoted_hard_price_points": "84.50",
            "transaction_amount_points": "84.50",
            "freeze_ledger_points": "84.50",
            "confirm_ledger_points": "84.50",
            "refund_ledger_points": "10.00",
            "frozen_at": datetime(2026, 5, 12, 8, 1, tzinfo=timezone.utc),
            "confirmed_at": datetime(2026, 5, 12, 8, 3, tzinfo=timezone.utc),
            "refunded_at": datetime(2026, 5, 12, 8, 5, tzinfo=timezone.utc),
            "quote_created_at": datetime(2026, 5, 12, 8, 0, tzinfo=timezone.utc),
            "transaction_updated_at": datetime(2026, 5, 12, 8, 5, tzinfo=timezone.utc),
            "correlation": {
                "web_master_task_id": "wmt_123",
                "api_task_id": "api_task_456",
                "api_request_id": "req_789",
            },
            "refund_reason_code": "OVERBILLED_HARD_PRICE",
            "refund_reason_message": "Finance audit found a template price overbill.",
            "ledger_entry_count": 3,
        },
        start,
        end,
    )

    assert row["risk_grade"] == "OK"
    assert row["net_confirmed_points"] == "74.50"
    assert summarize_reconciliation([row])["confirmed_points"] == "74.50"
    assert summarize_reconciliation([row])["refunded_points"] == "10.00"
