"""Tests for the 3-minute tiered pricing redesign .

Two layers, kept in this single file so the AC scenarios are easy to
audit against the issue:

1. Pure-function tests for `tiered_subtotal` and
   `compute_tier_system_reference_price` — no DB / no SRT plumbing.
   These cover the AC duration boundaries (2.8 / 3 / 4 / 5 / 10
   minutes) across all five manual-catalog tier_codes, plus the
   custom-template branch's wenan/leading-subflow recipes.

2. Refresh script (`scripts.refresh_v2_system_reference_price`)
   parsing helpers and `plan_refresh` against an in-memory SQLite
   catalog. Verifies that historical snapshots are NOT touched and
   that DB-vs-CSV diff reporting matches the contract from the script
   docstring.
"""

from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

from pricing_catalog_v2.schema import ALL_SCHEMA_SQL as CATALOG_SCHEMA_SQL
from pricing_quote_v2.subflows import (
    FIXED_TRAILING_SUBFLOWS,
    LEADING_SUBFLOWS_BY_COMBO,
    MANUAL_CATALOG_TIER_RECIPES,
    WENAN_COMBO_UNIT_PRICES,
    compute_tier_system_reference_price,
    tiered_subtotal,
)


sqlite3.register_adapter(Decimal, str)


# ── pure: tiered_subtotal ──────────────────────────────────────────────────


def test_tiered_subtotal_flat_under_three_minutes():
    # Flat rates degenerate to rate × minutes when minutes <= 3.
    # narration_flash = (2, 2).
    assert tiered_subtotal(2, 2, Decimal("2.0")) == Decimal("4.0")
    assert tiered_subtotal(2, 2, Decimal("2.8")) == Decimal("5.6")


def test_tiered_subtotal_at_boundary_three_minutes():
    # At minutes == 3, the after-3 term contributes 0 even when rates differ.
    assert tiered_subtotal(10, 6, Decimal(3)) == Decimal(30)
    assert tiered_subtotal(7, 4, Decimal(3)) == Decimal(21)


def test_tiered_subtotal_splits_past_three_minutes():
    # 5 min, clip_data (10, 6) → 3×10 + 2×6 = 42
    assert tiered_subtotal(10, 6, Decimal(5)) == Decimal(42)
    # 5 min, video_composing (7, 4) → 3×7 + 2×4 = 29
    assert tiered_subtotal(7, 4, Decimal(5)) == Decimal(29)


def test_tiered_subtotal_long_video_amortizes_first_three():
    # 10 min, clip_data → 3×10 + 7×6 = 72
    assert tiered_subtotal(10, 6, Decimal(10)) == Decimal(72)
    # Same rate-flat subflow scales linearly past 3 min.
    assert tiered_subtotal(2, 2, Decimal(10)) == Decimal(20)


def test_tiered_subtotal_negative_minutes_clamped():
    # Defensive: a negative duration is treated as zero so a downstream
    # caller can't invert the math by passing a bogus value.
    assert tiered_subtotal(10, 6, Decimal("-1")) == Decimal(0)


# ── pure: compute_tier_system_reference_price ──────────────────────────────


# (tier_code, minutes, expected_int_total). Cross-checked against the
# §"规格汇总价应自然得到" table at the issue:
#   - 原创纯解说 + Flash:                3min=19, 5min=12×min(post)+3min start
#   - 原声混剪 + Flash:                  3min=22
#   - 二创文案 已有模板 (derivative):    3min=32, beyond-3min=25/min after
# The recipe excludes popular_learning for derivative (existing-template),
# matching the issue's "二创文案，已有模板" line.
@pytest.mark.parametrize(
    ("tier_code", "minutes", "expected"),
    [
        # Issue table row: 原创纯解说 + Flash, 19 per minute under 3min.
        # At exact 3min boundary: 19×3 = 57 (no after-3 component).
        ("original_narration_flash", Decimal(3), 57),
        # 2.8 min: 2*2.8 + 10*2.8 + 7*2.8 = 5.6 + 28 + 19.6 = 53.2 → ceil 54
        # Wait — that's the 3-subflow sum across (2,2)+(10,6)+(7,4) at 2.8m
        # under-3min portion. Recompute: each subflow's first_3=2.8 → 2*2.8+10*2.8+7*2.8 = 53.2 → 54.
        ("original_narration_flash", Decimal("2.8"), 54),
        # 4 min: 2*4 + (10*3+6*1) + (7*3+4*1) = 8 + 36 + 25 = 69
        ("original_narration_flash", Decimal(4), 69),
        # 5 min: 2*5 + (10*3+6*2) + (7*3+4*2) = 10 + 42 + 29 = 81
        ("original_narration_flash", Decimal(5), 81),
        # Issue table row: 原创纯解说 + Pro 3min=23, 6+30+21 = 57? wait check.
        # (6,6)+(10,6)+(7,4) at 3min: 18+30+21 = 69. The issue's "3分钟内单价"
        # table column is /minute (3min worth costs 23×3=69). So total at 3min
        # = 23 × 3 minutes = 69 ✓.
        ("original_narration_pro", Decimal(3), 69),
        # mix/remix mapping: tier_code original_mix_flash → wenan original_remix_flash (5,5)
        # 3min: 15+30+21 = 66 ; "原声混剪 + Flash 3min/min=22" → 22×3=66 ✓.
        ("original_mix_flash", Decimal(3), 66),
        # mix/remix Pro: (17,17)+(10,6)+(7,4) at 3min = 51+30+21 = 102 ; 34×3=102 ✓
        ("original_mix_pro", Decimal(3), 102),
        # derivative (existing-template 二创, NO popular_learning):
        # (15,15)+(10,6)+(7,4) at 3min = 45+30+21 = 96 ; 32×3=96 ✓
        ("derivative", Decimal(3), 96),
        # derivative at 10 min: 15*10 + (3*10+7*6) + (3*7+7*4) = 150 + 72 + 49 = 271
        # check via /min table: 32 first 3 min, 25/min after → 96 + 7×25 = 96+175 = 271 ✓
        ("derivative", Decimal(10), 271),
    ],
)
def test_compute_tier_system_reference_price_matches_issue_table(
    tier_code: str, minutes: Decimal, expected: int
) -> None:
    assert (
        compute_tier_system_reference_price(tier_code, minutes) == expected
    ), f"{tier_code} @ {minutes}min"


def test_manual_catalog_tier_recipes_omit_template_learning():
    # Issue: "已有模板二创不计入 template_learning". The derivative recipe
    # must NOT carry popular_learning, even though the custom-template
    # secondary_creation branch DOES.
    for tier_code, recipe in MANUAL_CATALOG_TIER_RECIPES.items():
        subflow_keys = [row[0] for row in recipe]
        assert (
            "popular_learning" not in subflow_keys
        ), f"manual-catalog recipe for {tier_code!r} must not include popular_learning"


def test_custom_template_secondary_creation_includes_popular_learning():
    # Mirror check on the custom-template side — the leading-subflow
    # registry is what distinguishes the two branches.
    leading = LEADING_SUBFLOWS_BY_COMBO["secondary_creation"]
    assert leading[0][0] == "popular_learning"


def test_wenan_rates_match_issue_table():
    # Sanity check the constants in subflows.py against the issue's
    # §"当前确认的子流程阶梯单价" — flat tiered for all wenan steps.
    assert WENAN_COMBO_UNIT_PRICES["original_narration_flash"] == (2, 2)
    assert WENAN_COMBO_UNIT_PRICES["original_narration_pro"] == (6, 6)
    assert WENAN_COMBO_UNIT_PRICES["original_remix_flash"] == (5, 5)
    assert WENAN_COMBO_UNIT_PRICES["original_remix_pro"] == (17, 17)
    assert WENAN_COMBO_UNIT_PRICES["secondary_creation"] == (15, 15)


def test_trailing_rates_match_issue_table():
    rates = {key: tup for key, _, tup in FIXED_TRAILING_SUBFLOWS}
    assert rates["clip_data"] == (10, 6)
    assert rates["video_composing"] == (7, 4)


# ── refresh script: parsing helpers ────────────────────────────────────────


def test_parse_mm_ss_to_seconds_happy_paths():
    from scripts.refresh_v2_system_reference_price import parse_mm_ss_to_seconds

    assert parse_mm_ss_to_seconds("2:10") == 130
    assert parse_mm_ss_to_seconds("0:30") == 30
    assert parse_mm_ss_to_seconds("9:40") == 580
    assert parse_mm_ss_to_seconds("12:00") == 720


def test_parse_mm_ss_to_seconds_rejects_malformed():
    from scripts.refresh_v2_system_reference_price import parse_mm_ss_to_seconds

    assert parse_mm_ss_to_seconds("") is None
    assert parse_mm_ss_to_seconds("NULL") is None
    assert parse_mm_ss_to_seconds("2:60") is None  # seconds must be 0..59
    assert parse_mm_ss_to_seconds("abc") is None
    assert parse_mm_ss_to_seconds("1:2:3") is None
    assert parse_mm_ss_to_seconds("-1:00") is None


def test_load_csv_durations_skips_null_code_and_null_time(tmp_path: Path):
    from scripts.refresh_v2_system_reference_price import load_csv_durations

    csv_path = tmp_path / "baokuan.csv"
    csv_path.write_text(
        "id,code,time\n"
        "1,NULL,NULL\n"
        "2,xy0046,2:10\n"
        "3,xy0001,1:00\n"
        "4,not-an-xy-code,3:00\n"
        "5,xy0042,NULL\n",
        encoding="utf-8",
    )
    durations, skipped = load_csv_durations(csv_path)
    assert durations == {"46": 130, "1": 60}
    reasons = {ident: reason for ident, reason in skipped}
    assert reasons["NULL"] == "code is NULL/empty"
    assert "not-an-xy-code" in reasons
    assert reasons["xy0042"] == "time NULL or unparseable"


# ── refresh script: plan_refresh against in-memory SQLite ─────────────────


@pytest.fixture()
def catalog_engine():
    eng = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}
    )
    with eng.begin() as conn:
        for sql in CATALOG_SCHEMA_SQL:
            for stmt in sql.strip().split(";"):
                stmt = stmt.strip()
                if stmt:
                    conn.execute(text(stmt))
    yield eng
    eng.dispose()


def _now_iso() -> datetime:
    return datetime.now(timezone.utc)


def _seed_template_row(engine, *, template_id: str, video_duration_seconds=None):
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO pricing_template_v2 ("
                "template_id, tier_multiplier, enabled, created_at, updated_at, "
                "video_duration_seconds) VALUES "
                "(:tid, 1.0, 1, :now, :now, :vds)"
            ),
            {"tid": template_id, "now": _now_iso(), "vds": video_duration_seconds},
        )


def _seed_catalog_tier(
    engine,
    *,
    template_id: str,
    tier_code: str,
    manual_price: int,
    system_reference_price: int,
    effective_version: int = 1,
    product_line: str = "original",
    mode: str | None = "narration",
    quality: str | None = "flash",
    flash_pro_axis: str = "required",
):
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO pricing_catalog_v2_entry ("
                "catalog_entry_id, template_id, tier_code, effective_version, "
                "product_line, mode, quality, flash_pro_axis, "
                "manual_price, pro_surcharge_display, system_reference_price, "
                "currency_unit, raw_rate, final_rate, rounding_rule_version, "
                "manual_override_warning, enabled, "
                "created_at, updated_at, updated_by) VALUES ("
                ":id, :tid, :tc, :ver, :pl, :mode, :q, :axis, "
                ":mp, NULL, :srp, 'web_point', 1, 1, "
                "'v2.0-round-half-up', 0, 1, :now, :now, 'seed')"
            ),
            {
                "id": str(uuid.uuid4()),
                "tid": template_id,
                "tc": tier_code,
                "ver": effective_version,
                "pl": product_line,
                "mode": mode,
                "q": quality,
                "axis": flash_pro_axis,
                "mp": manual_price,
                "srp": system_reference_price,
                "now": _now_iso(),
            },
        )


def test_plan_refresh_updates_duration_and_inserts_new_tier_rows(catalog_engine):
    from scripts.refresh_v2_system_reference_price import plan_refresh

    _seed_template_row(catalog_engine, template_id="46", video_duration_seconds=None)
    _seed_catalog_tier(
        catalog_engine,
        template_id="46",
        tier_code="original_narration_flash",
        manual_price=295,
        system_reference_price=295,
    )

    csv_durations = {"46": 180}  # 3 min
    with catalog_engine.connect() as conn:
        duration_updates, tier_inserts, csv_only, warnings = plan_refresh(
            conn, csv_durations, limit=None
        )

    assert len(duration_updates) == 1
    assert duration_updates[0]["template_id"] == "46"
    assert duration_updates[0]["video_duration_seconds"] == 180

    assert len(tier_inserts) == 1
    new = tier_inserts[0]
    assert new["tier_code"] == "original_narration_flash"
    # 3 min × 3-min-tier formula:
    #   narration_flash 2×3 + clip_data 10×3 + video_composing 7×3 = 57
    assert new["system_reference_price"] == 57
    # Q1=B (subsequent update): manual_price ALSO tracks the formula now.
    # The old 295 value (from `seed_from_v1`) is overwritten.
    assert new["manual_price"] == 57
    # New row supersedes effective_version = 1.
    assert new["effective_version"] == 2
    assert csv_only == []
    # No spurious warnings on the single in-CSV template.
    assert all(t != "46" for t, _ in warnings)


def test_plan_refresh_skips_when_both_prices_already_current(catalog_engine):
    """Re-running on the same CSV produces no new rows when BOTH the
    stored system_reference_price AND manual_price already match the
    formula output (Q1=B)."""
    from scripts.refresh_v2_system_reference_price import plan_refresh

    _seed_template_row(catalog_engine, template_id="47", video_duration_seconds=180)
    _seed_catalog_tier(
        catalog_engine,
        template_id="47",
        tier_code="original_narration_flash",
        manual_price=57,  # already the formula answer
        system_reference_price=57,
    )

    with catalog_engine.connect() as conn:
        duration_updates, tier_inserts, csv_only, _ = plan_refresh(
            conn, {"47": 180}, limit=None
        )
    assert duration_updates == []
    assert tier_inserts == []
    assert csv_only == []


def test_plan_refresh_inserts_when_only_manual_price_is_stale(catalog_engine):
    """Q1=B: a row whose system_reference_price is already current but
    whose manual_price still carries a legacy value (the previous PR's
    behavior) must still produce a new effective_version so the two
    columns realign."""
    from scripts.refresh_v2_system_reference_price import plan_refresh

    _seed_template_row(catalog_engine, template_id="48", video_duration_seconds=180)
    _seed_catalog_tier(
        catalog_engine,
        template_id="48",
        tier_code="original_narration_flash",
        manual_price=295,  # stale legacy catalog price
        system_reference_price=57,  # already current
    )

    with catalog_engine.connect() as conn:
        _, tier_inserts, _, _ = plan_refresh(
            conn, {"48": 180}, limit=None
        )
    assert len(tier_inserts) == 1
    assert tier_inserts[0]["manual_price"] == 57
    assert tier_inserts[0]["system_reference_price"] == 57


def test_plan_refresh_reports_csv_only_and_db_only(catalog_engine):
    """CSV templates not in DB get listed for operators; DB templates with no
    CSV coverage are preserved + flagged."""
    from scripts.refresh_v2_system_reference_price import plan_refresh

    _seed_template_row(catalog_engine, template_id="50", video_duration_seconds=None)
    _seed_catalog_tier(
        catalog_engine,
        template_id="50",
        tier_code="original_narration_flash",
        manual_price=100,
        system_reference_price=100,
    )

    csv_durations = {"50": 180, "999": 240}
    with catalog_engine.connect() as conn:
        _, _, csv_only, warnings = plan_refresh(
            conn, csv_durations, limit=None
        )
    assert csv_only == ["999"]
    # template "50" is fully handled, so it must not appear in warnings.
    assert all(t != "50" for t, _ in warnings)


def test_plan_refresh_warns_on_unknown_tier_code_but_preserves_row(catalog_engine):
    """A tier_code outside the recipe map is preserved (no INSERT) so
    historical / experimental tiers don't get clobbered to 0."""
    from scripts.refresh_v2_system_reference_price import plan_refresh

    _seed_template_row(catalog_engine, template_id="51", video_duration_seconds=180)
    _seed_catalog_tier(
        catalog_engine,
        template_id="51",
        tier_code="experimental_tier_zzz",
        manual_price=42,
        system_reference_price=42,
        product_line="experimental",
        mode=None,
        quality=None,
        flash_pro_axis="optional",
    )

    with catalog_engine.connect() as conn:
        _, tier_inserts, _, warnings = plan_refresh(
            conn, {"51": 180}, limit=None
        )
    assert tier_inserts == []
    assert any(
        t == "51" and "experimental_tier_zzz" in reason
        for t, reason in warnings
    )


# ── refresh script: --seed-missing pass ────────────────────────────────────


def test_plan_seeds_creates_template_and_5_tier_rows():
    """--seed-missing pass: a CSV-only template gets one
    pricing_template_v2 row + 5 catalog rows (one per tier_code in
    `MANUAL_CATALOG_TIER_RECIPES`), all with manual_price =
    system_reference_price = formula output. Pro tiers carry the
    derived `pro_surcharge_display`."""
    from scripts.refresh_v2_system_reference_price import plan_seeds

    csv_rows = {
        "15": {
            "seconds": 848,  # xy0015 14:08
            "code": "xy0015",
            "name": "n/a",
            "learning_model_id": None,
        }
    }
    template_seeds, tier_seeds = plan_seeds(csv_rows, db_template_ids=set())

    assert len(template_seeds) == 1
    t = template_seeds[0]
    assert t["template_id"] == "15"
    assert t["code"] == "xy0015"
    assert t["video_duration_seconds"] == 848

    # 5 tier rows.
    assert len(tier_seeds) == 5
    by_code = {row["tier_code"]: row for row in tier_seeds}
    expected = set([
        "original_narration_flash",
        "original_narration_pro",
        "original_mix_flash",
        "original_mix_pro",
        "derivative",
    ])
    assert set(by_code) == expected

    # 848 s = 14.1333 min: 3 min first-tier + 11.1333 min after-3 tier.
    # narration_flash: 3×2 + 11.1333×2 = 28.2666 → ceil 29
    # clip_data:       3×10 + 11.1333×6 = 30 + 66.8 = 96.8 → as part of total
    # video_composing: 3×7 + 11.1333×4 = 21 + 44.5333 = 65.5333
    # Total: 29ish + ceil arithmetic — let the assertions match the
    # formula directly via compute_tier_system_reference_price.
    from pricing_quote_v2.subflows import compute_tier_system_reference_price
    from decimal import Decimal as D

    minutes = D(848) / D(60)
    for tier_code in expected:
        expected_price = compute_tier_system_reference_price(tier_code, minutes)
        row = by_code[tier_code]
        assert row["manual_price"] == expected_price
        assert row["system_reference_price"] == expected_price
        assert row["effective_version"] == 1
        # Axis-optional derivative keeps NULL surcharge, both Pros derive.
        if tier_code in {"original_narration_pro", "original_mix_pro"}:
            flash_code = (
                "original_narration_flash"
                if tier_code == "original_narration_pro"
                else "original_mix_flash"
            )
            flash_price = compute_tier_system_reference_price(flash_code, minutes)
            assert row["pro_surcharge_display"] == expected_price - flash_price
        else:
            assert row["pro_surcharge_display"] is None


def test_plan_seeds_returns_empty_when_no_csv_only_templates():
    """Templates already present in DB don't get re-seeded."""
    from scripts.refresh_v2_system_reference_price import plan_seeds

    csv_rows = {
        "15": {
            "seconds": 848,
            "code": "xy0015",
            "name": "n/a",
            "learning_model_id": None,
        }
    }
    template_seeds, tier_seeds = plan_seeds(csv_rows, db_template_ids={"15"})
    assert template_seeds == []
    assert tier_seeds == []
