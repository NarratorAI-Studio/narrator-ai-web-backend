"""Unit tests for account.format helpers (mask_mobile + format_balance)."""

from __future__ import annotations

from decimal import Decimal

from account.format import format_balance, mask_mobile


# ---------- mask_mobile ----------


def test_mask_mobile_matches_account_me_example():
    # Example pinned by the implementation requirement: 11-digit CN mobile → first 3 + last 3,
    # middle replaced by 5 asterisks.
    assert mask_mobile("13900000222") == "139*****222"


def test_mask_mobile_generic_11_digits():
    assert mask_mobile("13912345678") == "139*****678"


def test_mask_mobile_none_returns_none():
    # Nullable column; JSON renders as `null`, not `""`.
    assert mask_mobile(None) is None


def test_mask_mobile_short_value_fully_masked():
    # < 7 chars: not enough room to keep 3 head + 3 tail without exposing
    # most of the value. Mask the whole thing rather than leak it raw —
    # the API boundary contract is "no raw mobile leaves backend"
    # (contract compatibility on regression coverage).
    assert mask_mobile("12345") == "*****"
    assert mask_mobile("1") == "*"
    assert mask_mobile("123456") == "******"  # exactly at the boundary


def test_mask_mobile_scales_with_length():
    # 13 chars → 3 + 7*'*' + 3.
    assert mask_mobile("1391234567890") == "139*******890"


# ---------- format_balance ----------


def test_format_balance_decimal_two_places():
    assert format_balance(Decimal("2340.02")) == "2340.02"


def test_format_balance_integer_renders_two_decimals():
    assert format_balance(Decimal("1000")) == "1000.00"


def test_format_balance_python_int():
    # create_user.py inserts via SQLAlchemy with Decimal but sqlite tests
    # may surface the value as int when there's no fractional part.
    assert format_balance(1000) == "1000.00"


def test_format_balance_float_no_precision_loss():
    # sqlite test fixtures often produce float; `str()` coercion in the
    # helper avoids `Decimal(2340.02)` decomposing into 2340.0199999…
    assert format_balance(2340.02) == "2340.02"


def test_format_balance_string_passthrough():
    assert format_balance("123.4") == "123.40"


def test_format_balance_none_returns_none():
    assert format_balance(None) is None
