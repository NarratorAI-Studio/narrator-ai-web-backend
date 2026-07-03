"""Tests for the v2 catalog persistence + read endpoint .

Strategy: bind SQLAlchemy metadata to an in-memory SQLite engine and
exercise the real store + route. SQLite ignores `postgresql_where`
partial-index hints so the WHERE-enabled lookup works on plain index;
the read path is correct on both engines.

Coverage:
- Inheritance resolution: template-only / family-only / both (template
  wins) / neither (404).
- `tier_multiplier` arithmetic (rounding_rule_version `v2.0-round-half-up`).
- `flash_pro_axis` CHECK constraint via the family table.
- `effective_version` highest-wins ordering (older enabled rows stay
  visible to snapshot lookback but the route surfaces only the latest).
- Auth: missing Bearer → 401, unknown X-Web-App-Key → 401.
- 404 → `CATALOG_TIER_MISSING` envelope.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

from pricing_catalog_v2.schema import ALL_SCHEMA_SQL


sqlite3.register_adapter(Decimal, str)


AUTH_TOKEN = "test-bff-token"
WEB_APP_KEY = "grid_AbCdEfGhIjKlMnOpQrStUv"
AUTH_HEADERS = {
    "Authorization": f"Bearer {AUTH_TOKEN}",
    "X-Web-App-Key": WEB_APP_KEY,
}

NOW = datetime(2026, 5, 29, 8, 0, 0, tzinfo=timezone.utc)


SQLITE_USERS_SCHEMA = """
CREATE TABLE users (
    app_key TEXT PRIMARY KEY,
    id INTEGER UNIQUE,
    balance_points NUMERIC NOT NULL DEFAULT 1000 CHECK (balance_points >= 0),
    nickname TEXT,
    mobile TEXT,
    email TEXT,
    company_name TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""


def _create_in_memory_engine():
    """Build the test engine using the SAME raw DDL the production
    Alembic migration runs (`ALL_SCHEMA_SQL`). Previously the fixture
    used `orm_metadata.create_all()` which only exercised the SQLAlchemy
    model's CHECK helpers, not the production DDL — drift in
    `pricing_catalog_v2/schema.py` could pass tests but fail at
    migration time. The users table stays on its own SQLite-compatible
    DDL because it is outside this test scope.
    """
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}
    )
    with engine.begin() as conn:
        conn.execute(text(SQLITE_USERS_SCHEMA))
        conn.execute(
            text(
                "INSERT INTO users (app_key, id, balance_points) "
                "VALUES (:k, 1, 1000)"
            ),
            {"k": WEB_APP_KEY},
        )
        # Production DDL — each ALL_SCHEMA_SQL element carries one or
        # more `;`-separated statements (CREATE TABLE + CREATE INDEX).
        for sql in ALL_SCHEMA_SQL:
            for stmt in sql.strip().split(";"):
                stmt = stmt.strip()
                if stmt:
                    conn.execute(text(stmt))
    return engine


@pytest.fixture()
def engine():
    eng = _create_in_memory_engine()
    yield eng
    eng.dispose()


@pytest.fixture()
def client(engine, monkeypatch):
    import server

    monkeypatch.setattr(server, "get_db_engine", lambda: engine)
    monkeypatch.setattr(server, "get_db_core_connection", lambda: engine.connect())
    monkeypatch.setenv("PRICING_BFF_AUTH_TOKEN", AUTH_TOKEN)
    return server.app.test_client()


# ── seeding helpers ──────────────────────────────────────────────────────────


def _insert_template(
    engine,
    *,
    template_id,
    family_id=None,
    multiplier="1.0",
    code=None,
    name=None,
    learning_model_id=None,
):
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO pricing_template_v2 "
                "(template_id, template_family_id, tier_multiplier, enabled, "
                " created_at, updated_at, code, name, learning_model_id) "
                "VALUES (:tid, :fid, :mult, 1, :now, :now, :code, :name, :lmid)"
            ),
            {
                "tid": template_id,
                "fid": family_id,
                "mult": multiplier,
                "now": NOW,
                "code": code,
                "name": name,
                "lmid": learning_model_id,
            },
        )


def _insert_template_entry(
    engine,
    *,
    catalog_entry_id,
    template_id,
    tier_code,
    manual_price,
    system_reference_price,
    flash_pro_axis="required",
    mode="narration",
    quality="flash",
    product_line="original",
    pro_surcharge_display=None,
    effective_version=1,
    enabled=True,
    manual_override_warning=False,
    rounding_rule_version="v2.0-round-half-up",
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
                ":id, :tid, :tc, :ver, :pl, :mode, :q, :axis, :mp, :psd, :srp, "
                "'web_point', 0.83333, 1, :rrv, :mow, :en, :now, :now, 'tester')"
            ),
            {
                "id": catalog_entry_id,
                "tid": template_id,
                "tc": tier_code,
                "ver": effective_version,
                "pl": product_line,
                "mode": mode,
                "q": quality,
                "axis": flash_pro_axis,
                "mp": manual_price,
                "psd": pro_surcharge_display,
                "srp": system_reference_price,
                "rrv": rounding_rule_version,
                "mow": 1 if manual_override_warning else 0,
                "en": 1 if enabled else 0,
                "now": NOW,
            },
        )


def _insert_family_entry(
    engine,
    *,
    family_id,
    tier_code,
    manual_price,
    system_reference_price,
    flash_pro_axis="required",
    mode="narration",
    quality="flash",
    product_line="original",
    pro_surcharge_display=None,
    effective_version=1,
    enabled=True,
    rounding_rule_version="v2.0-round-half-up",
):
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO pricing_catalog_v2_family ("
                "template_family_id, tier_code, effective_version, "
                "product_line, mode, quality, flash_pro_axis, "
                "manual_price, pro_surcharge_display, system_reference_price, "
                "currency_unit, raw_rate, final_rate, rounding_rule_version, "
                "manual_override_warning, enabled, "
                "created_at, updated_at, updated_by) VALUES ("
                ":fid, :tc, :ver, :pl, :mode, :q, :axis, :mp, :psd, :srp, "
                "'web_point', 0.83333, 1, :rrv, 0, :en, :now, :now, 'tester')"
            ),
            {
                "fid": family_id,
                "tc": tier_code,
                "ver": effective_version,
                "pl": product_line,
                "mode": mode,
                "q": quality,
                "axis": flash_pro_axis,
                "mp": manual_price,
                "psd": pro_surcharge_display,
                "srp": system_reference_price,
                "rrv": rounding_rule_version,
                "en": 1 if enabled else 0,
                "now": NOW,
            },
        )


# ── tests ────────────────────────────────────────────────────────────────────


def test_template_level_only_returns_those_tiers(client, engine):
    _insert_template(engine, template_id="T-001")
    _insert_template_entry(
        engine,
        catalog_entry_id="ce-001",
        template_id="T-001",
        tier_code="original_narration_flash",
        manual_price=800,
        system_reference_price=750,
    )

    resp = client.get("/pricing/catalog/T-001/tiers", headers=AUTH_HEADERS)
    assert resp.status_code == 200, resp.get_json()
    body = resp.get_json()
    assert body["success"] is True
    tiers = body["data"]["tiers"]
    assert len(tiers) == 1
    t = tiers[0]
    assert t["tier_code"] == "original_narration_flash"
    assert t["source"] == "template"
    assert t["catalog_entry_id"] == "ce-001"
    assert t["manual_price"] == 800
    assert t["manual_price_raw"] == 800


def test_family_fallback_with_multiplier(client, engine):
    """Family fallback scales manual_price, system_reference_price, AND
    raw_rate × multiplier with a SINGLE rounding into final_rate.
    Multiplier here (1.2) and family raw_rate (0.83333 — set in
    `_insert_family_entry`) jointly produce a value
    that double-rounding would corrupt: family.final_rate = 1 verbatim
    × 1.2 = 1.2 (still 1 if you don't round again), but the correct
    single-round of 0.83333 × 1.2 = 0.999996 → 1.0 (preserved sub-1
    precision per §5.1). This asserts raw_rate and final_rate scale
    through the inheritance path.
    """
    _insert_template(engine, template_id="T-002", family_id="TF-A", multiplier="1.2")
    _insert_family_entry(
        engine,
        family_id="TF-A",
        tier_code="original_narration_flash",
        manual_price=800,
        system_reference_price=750,
    )

    resp = client.get("/pricing/catalog/T-002/tiers", headers=AUTH_HEADERS)
    assert resp.status_code == 200, resp.get_json()
    tiers = resp.get_json()["data"]["tiers"]
    assert len(tiers) == 1
    t = tiers[0]
    assert t["source"] == "family"
    assert t["catalog_entry_id"] is None
    # 800 * 1.2 = 960
    assert t["manual_price"] == 960
    assert t["manual_price_raw"] == 800
    # 750 * 1.2 = 900
    assert t["system_reference_price"] == 900
    assert t["system_reference_price_raw"] == 750
    # raw_rate (0.83333 from the fixture) × multiplier (1.2) = 0.999996,
    # rounded ONCE under v2.0-round-half-up's sub-1 branch (1-dp
    # quantize): 1.0. Validates "single rounding" path.
    assert Decimal(t["raw_rate"]) == Decimal("0.999996")
    assert Decimal(t["final_rate"]) == Decimal("1.0")


def test_family_fallback_rate_scaling_single_round_vs_double_round(client, engine):
    """Construct a multiplier × raw_rate combination where naive
    double-rounding (round family.final_rate=1 verbatim × 1.5 = 1.5
    → round again → 2) drifts from the contract's single-round of
    raw_rate × multiplier (0.83333 × 1.5 = 1.249995 → round half-up
    to 1). The endpoint must surface the single-rounded value 1, not
    the double-rounded 2.
    """
    _insert_template(engine, template_id="T-002B", family_id="TF-A2", multiplier="1.5")
    _insert_family_entry(
        engine,
        family_id="TF-A2",
        tier_code="original_narration_flash",
        manual_price=800,
        system_reference_price=750,
    )

    resp = client.get("/pricing/catalog/T-002B/tiers", headers=AUTH_HEADERS)
    assert resp.status_code == 200
    t = resp.get_json()["data"]["tiers"][0]
    # 0.83333 × 1.5 = 1.249995, round half-up = 1 (single round path).
    # Double-round (round family final_rate=1 verbatim, then × 1.5 = 1.5,
    # then round again = 2) would WRONGLY give 2.
    assert Decimal(t["raw_rate"]) == Decimal("1.249995")
    assert Decimal(t["final_rate"]) == Decimal("1")


def test_template_wins_over_family(client, engine):
    _insert_template(engine, template_id="T-003", family_id="TF-B", multiplier="1.5")
    _insert_family_entry(
        engine,
        family_id="TF-B",
        tier_code="original_narration_flash",
        manual_price=500,
        system_reference_price=480,
    )
    _insert_template_entry(
        engine,
        catalog_entry_id="ce-003",
        template_id="T-003",
        tier_code="original_narration_flash",
        manual_price=850,
        system_reference_price=820,
    )

    resp = client.get("/pricing/catalog/T-003/tiers", headers=AUTH_HEADERS)
    assert resp.status_code == 200, resp.get_json()
    tiers = resp.get_json()["data"]["tiers"]
    assert len(tiers) == 1
    t = tiers[0]
    # Template-level wins; family entry is irrelevant.
    assert t["source"] == "template"
    assert t["manual_price"] == 850
    # Multiplier is NOT applied to template-level entries (it's a
    # family-level inheritance knob).
    assert t["manual_price_raw"] == 850


def test_mixed_template_and_family_per_tier(client, engine):
    """Template has its own Flash; Pro comes from family × multiplier.

    Family Pro has NO surcharge in this fixture so the read-side
    invariant check derives it from the effective Pro - Flash diff.
    Mixing template-level Flash with
    family-level Pro that DID carry an inherited surcharge would
    raise CATALOG_INHERITED_INVARIANT_VIOLATION — covered by
    `test_family_inheritance_invariant_violation_returns_422`.
    """
    _insert_template(engine, template_id="T-004", family_id="TF-C", multiplier="1.25")
    _insert_template_entry(
        engine,
        catalog_entry_id="ce-flash",
        template_id="T-004",
        tier_code="original_narration_flash",
        manual_price=820,
        system_reference_price=800,
    )
    _insert_family_entry(
        engine,
        family_id="TF-C",
        tier_code="original_narration_pro",
        quality="pro",
        manual_price=1000,
        pro_surcharge_display=None,  # derived on read from price diff
        system_reference_price=950,
    )

    resp = client.get("/pricing/catalog/T-004/tiers", headers=AUTH_HEADERS)
    assert resp.status_code == 200, resp.get_json()
    tiers = {t["tier_code"]: t for t in resp.get_json()["data"]["tiers"]}
    assert set(tiers) == {"original_narration_flash", "original_narration_pro"}
    assert tiers["original_narration_flash"]["source"] == "template"
    assert tiers["original_narration_flash"]["manual_price"] == 820
    # 1000 * 1.25 = 1250
    assert tiers["original_narration_pro"]["source"] == "family"
    assert tiers["original_narration_pro"]["manual_price"] == 1250
    # Derived from price diff: 1250 - 820 = 430
    assert tiers["original_narration_pro"]["pro_surcharge_display"] == 430


def test_neither_template_nor_family_returns_404(client, engine):
    _insert_template(engine, template_id="T-005", family_id="TF-D", multiplier="1.0")
    # No template-level entries AND no family-level entries.

    resp = client.get("/pricing/catalog/T-005/tiers", headers=AUTH_HEADERS)
    assert resp.status_code == 404
    body = resp.get_json()
    assert body["success"] is False
    assert body["error"]["code"] == "CATALOG_TIER_MISSING"
    assert body["error"]["details"] == {"template_id": "T-005"}


def test_unknown_template_returns_404(client):
    """Template row itself doesn't exist."""
    resp = client.get("/pricing/catalog/T-DOES-NOT-EXIST/tiers", headers=AUTH_HEADERS)
    assert resp.status_code == 404
    assert resp.get_json()["error"]["code"] == "CATALOG_TIER_MISSING"


def test_latest_effective_version_wins(client, engine):
    """Older enabled rows stay for snapshot lookback; the route surfaces
    only the latest enabled version per (template_id, tier_code)."""
    _insert_template(engine, template_id="T-006")
    _insert_template_entry(
        engine,
        catalog_entry_id="ce-old",
        template_id="T-006",
        tier_code="original_narration_flash",
        manual_price=600,
        system_reference_price=580,
        effective_version=1,
    )
    _insert_template_entry(
        engine,
        catalog_entry_id="ce-new",
        template_id="T-006",
        tier_code="original_narration_flash",
        manual_price=900,
        system_reference_price=850,
        effective_version=2,
    )

    resp = client.get("/pricing/catalog/T-006/tiers", headers=AUTH_HEADERS)
    assert resp.status_code == 200
    tiers = resp.get_json()["data"]["tiers"]
    assert len(tiers) == 1
    assert tiers[0]["effective_version"] == 2
    assert tiers[0]["manual_price"] == 900
    assert tiers[0]["catalog_entry_id"] == "ce-new"


def test_disabled_entries_are_skipped(client, engine):
    """An older `enabled=TRUE` row wins over a newer `enabled=FALSE` row."""
    _insert_template(engine, template_id="T-007")
    _insert_template_entry(
        engine,
        catalog_entry_id="ce-stable",
        template_id="T-007",
        tier_code="original_narration_flash",
        manual_price=700,
        system_reference_price=680,
        effective_version=1,
        enabled=True,
    )
    _insert_template_entry(
        engine,
        catalog_entry_id="ce-disabled",
        template_id="T-007",
        tier_code="original_narration_flash",
        manual_price=999,
        system_reference_price=999,
        effective_version=2,
        enabled=False,
    )

    resp = client.get("/pricing/catalog/T-007/tiers", headers=AUTH_HEADERS)
    assert resp.status_code == 200
    tiers = resp.get_json()["data"]["tiers"]
    assert len(tiers) == 1
    assert tiers[0]["catalog_entry_id"] == "ce-stable"
    assert tiers[0]["manual_price"] == 700


def test_flash_pro_axis_check_constraint(engine):
    """flash_pro_axis must be 'required' or 'optional' — DB rejects others."""
    import sqlalchemy.exc

    _insert_template(engine, template_id="T-008", family_id="TF-E")
    with pytest.raises(sqlalchemy.exc.IntegrityError):
        _insert_family_entry(
            engine,
            family_id="TF-E",
            tier_code="derivative",
            flash_pro_axis="bogus",
            mode=None,
            quality=None,
            manual_price=1200,
            system_reference_price=1100,
        )


def test_multiplier_rounds_half_up_for_decimal_outcome(client, engine):
    """800 * 1.15 = 920.0; rounded to 920 (no change). 800 * 1.005 = 804.0
    → 804 (still no rounding needed). 850 * 1.005 = 854.25 → 854 (round
    half-up to integer)."""
    _insert_template(engine, template_id="T-009", family_id="TF-F", multiplier="1.005")
    _insert_family_entry(
        engine,
        family_id="TF-F",
        tier_code="original_narration_flash",
        manual_price=850,
        system_reference_price=800,
    )

    resp = client.get("/pricing/catalog/T-009/tiers", headers=AUTH_HEADERS)
    assert resp.status_code == 200
    tiers = resp.get_json()["data"]["tiers"]
    # 850 * 1.005 = 854.25 → rounds to 854 (half-up applied to int).
    assert tiers[0]["manual_price"] == 854
    # 800 * 1.005 = 804.0 → 804.
    assert tiers[0]["system_reference_price"] == 804


def test_missing_bearer_returns_401(client, engine):
    _insert_template(engine, template_id="T-010")
    _insert_template_entry(
        engine,
        catalog_entry_id="ce-010",
        template_id="T-010",
        tier_code="original_narration_flash",
        manual_price=500,
        system_reference_price=480,
    )

    resp = client.get(
        "/pricing/catalog/T-010/tiers",
        headers={"X-Web-App-Key": WEB_APP_KEY},
    )
    assert resp.status_code == 401


def test_unknown_app_key_returns_401(client, engine):
    _insert_template(engine, template_id="T-011")
    _insert_template_entry(
        engine,
        catalog_entry_id="ce-011",
        template_id="T-011",
        tier_code="original_narration_flash",
        manual_price=500,
        system_reference_price=480,
    )

    resp = client.get(
        "/pricing/catalog/T-011/tiers",
        headers={
            "Authorization": f"Bearer {AUTH_TOKEN}",
            "X-Web-App-Key": "grid_UNKNOWN_KEY",
        },
    )
    assert resp.status_code == 401


def test_derivative_tier_with_optional_axis(client, engine):
    """Derivative tier carries flash_pro_axis='optional', mode/quality
    NULL. Round-trips intact through the read path."""
    _insert_template(engine, template_id="T-012")
    _insert_template_entry(
        engine,
        catalog_entry_id="ce-deriv",
        template_id="T-012",
        tier_code="derivative",
        product_line="derivative",
        mode=None,
        quality=None,
        flash_pro_axis="optional",
        manual_price=1200,
        system_reference_price=1100,
    )

    resp = client.get("/pricing/catalog/T-012/tiers", headers=AUTH_HEADERS)
    assert resp.status_code == 200
    tier = resp.get_json()["data"]["tiers"][0]
    assert tier["tier_code"] == "derivative"
    assert tier["flash_pro_axis"] == "optional"
    assert tier["mode"] is None
    assert tier["quality"] is None


# ── Optional axis CHECK constraint ────────────────────


def test_optional_axis_with_non_null_mode_or_quality_rejected(engine):
    """flash_pro_axis='optional' MUST come with mode IS NULL AND quality
    IS NULL — contract §3.1. Both family and template-entry DDL must
    enforce it."""
    import sqlalchemy.exc

    _insert_template(engine, template_id="T-013", family_id="TF-G")

    # FAMILY-side rejections.
    with pytest.raises(sqlalchemy.exc.IntegrityError):
        _insert_family_entry(
            engine,
            family_id="TF-G",
            tier_code="derivative",
            flash_pro_axis="optional",
            mode=None,
            quality="pro",
            manual_price=1200,
            system_reference_price=1100,
        )
    with pytest.raises(sqlalchemy.exc.IntegrityError):
        _insert_family_entry(
            engine,
            family_id="TF-G",
            tier_code="derivative",
            flash_pro_axis="optional",
            mode="narration",
            quality=None,
            manual_price=1200,
            system_reference_price=1100,
        )

    # TEMPLATE-entry-side rejections (mirror constraint must exist).
    with pytest.raises(sqlalchemy.exc.IntegrityError):
        _insert_template_entry(
            engine,
            catalog_entry_id="ce-axis-fail-quality",
            template_id="T-013",
            tier_code="derivative",
            flash_pro_axis="optional",
            mode=None,
            quality="pro",
            product_line="derivative",
            manual_price=1200,
            system_reference_price=1100,
        )
    with pytest.raises(sqlalchemy.exc.IntegrityError):
        _insert_template_entry(
            engine,
            catalog_entry_id="ce-axis-fail-mode",
            template_id="T-013",
            tier_code="derivative",
            flash_pro_axis="optional",
            mode="narration",
            quality=None,
            product_line="derivative",
            manual_price=1200,
            system_reference_price=1100,
        )


# ── Inherited-invariant validation ──────────────────────


def test_family_inheritance_derives_surcharge_from_price_diff(client, engine):
    """When family Flash (700) + family Pro surcharge (100) → family Pro
    (800), and the template's multiplier is 1.0, the read endpoint
    returns Pro surcharge derived from the Pro - Flash diff (100). No
    invariant violation."""
    _insert_template(engine, template_id="T-014", family_id="TF-H", multiplier="1.0")
    _insert_family_entry(
        engine,
        family_id="TF-H",
        tier_code="original_narration_flash",
        quality="flash",
        manual_price=700,
        system_reference_price=680,
    )
    _insert_family_entry(
        engine,
        family_id="TF-H",
        tier_code="original_narration_pro",
        quality="pro",
        manual_price=800,
        pro_surcharge_display=100,
        system_reference_price=780,
    )

    resp = client.get("/pricing/catalog/T-014/tiers", headers=AUTH_HEADERS)
    assert resp.status_code == 200
    tiers = {t["tier_code"]: t for t in resp.get_json()["data"]["tiers"]}
    assert tiers["original_narration_pro"]["pro_surcharge_display"] == 100
    assert (
        tiers["original_narration_pro"]["manual_price"]
        - tiers["original_narration_flash"]["manual_price"]
        == tiers["original_narration_pro"]["pro_surcharge_display"]
    )


def test_family_inheritance_silently_overwrites_surcharge_from_diff(
    client, engine
):
    """Per-tier inheritance scenario: template overrides
    Flash only, Pro inherits from family. Family Flash=700, Family
    Pro=800 with surcharge=100 (family-self-consistent). Template
    overrides Flash to 750. Effective Pro - Effective Flash = 50.
    The endpoint MUST silently overwrite Pro's surcharge to 50 and
    return 200 — NOT compare inherited surcharge (100, based on
    family Flash) against derived (50, based on template Flash) and
    raise. Family-row-internal §4.1 errors are caught at upsert path,
    not on read.
    """
    _insert_template(engine, template_id="T-015", family_id="TF-I", multiplier="1.0")
    _insert_family_entry(
        engine,
        family_id="TF-I",
        tier_code="original_narration_flash",
        quality="flash",
        manual_price=700,
        system_reference_price=680,
    )
    _insert_family_entry(
        engine,
        family_id="TF-I",
        tier_code="original_narration_pro",
        quality="pro",
        manual_price=800,
        pro_surcharge_display=100,  # consistent with family Flash 700 → Pro 800
        system_reference_price=780,
    )
    # Template overrides Flash to 750 (Pro stays family-inherited).
    _insert_template_entry(
        engine,
        catalog_entry_id="ce-T015-flash-override",
        template_id="T-015",
        tier_code="original_narration_flash",
        quality="flash",
        manual_price=750,
        system_reference_price=720,
    )

    resp = client.get("/pricing/catalog/T-015/tiers", headers=AUTH_HEADERS)
    assert resp.status_code == 200, resp.get_json()
    tiers = {t["tier_code"]: t for t in resp.get_json()["data"]["tiers"]}
    assert tiers["original_narration_flash"]["source"] == "template"
    assert tiers["original_narration_flash"]["manual_price"] == 750
    assert tiers["original_narration_pro"]["source"] == "family"
    assert tiers["original_narration_pro"]["manual_price"] == 800
    # Surcharge overwritten from effective price diff: 800 - 750 = 50.
    # Family-row stored surcharge (100, based on family Flash 700) is
    # IGNORED on read; it's not a "violation".
    assert tiers["original_narration_pro"]["pro_surcharge_display"] == 50


def test_template_pro_tier_does_not_trigger_inherited_invariant_check(client, engine):
    """Template-level Pro tier with a surcharge that does NOT match
    Pro - Flash MUST still return 200 — the upsert-side
    CATALOG_PRO_SURCHARGE_MISMATCH owns that case. This test guards
    against an over-eager validator extending the inherited-invariant
    check to template-level rows."""
    _insert_template(engine, template_id="T-016")
    _insert_template_entry(
        engine,
        catalog_entry_id="ce-flash-t",
        template_id="T-016",
        tier_code="original_narration_flash",
        quality="flash",
        manual_price=700,
        system_reference_price=680,
    )
    _insert_template_entry(
        engine,
        catalog_entry_id="ce-pro-t",
        template_id="T-016",
        tier_code="original_narration_pro",
        quality="pro",
        manual_price=900,
        pro_surcharge_display=150,  # not 200 (= 900 - 700)
        system_reference_price=850,
    )

    resp = client.get("/pricing/catalog/T-016/tiers", headers=AUTH_HEADERS)
    assert resp.status_code == 200
    tiers = {t["tier_code"]: t for t in resp.get_json()["data"]["tiers"]}
    # Surcharge stays as the operator typed it — no read-side derivation.
    assert tiers["original_narration_pro"]["pro_surcharge_display"] == 150


# ── Negative surcharge guard ───────────


def test_family_inheritance_negative_surcharge_returns_422(client, engine):
    """Family Pro (600) < Flash (700) at multiplier 1.0 → derived
    surcharge = -100. Contract requires non-negative surcharges; read
    MUST raise CATALOG_INHERITED_INVARIANT_VIOLATION rather than
    surface a negative value."""
    _insert_template(engine, template_id="T-NEG", family_id="TF-NEG", multiplier="1.0")
    _insert_family_entry(
        engine,
        family_id="TF-NEG",
        tier_code="original_narration_flash",
        quality="flash",
        manual_price=700,
        system_reference_price=680,
    )
    _insert_family_entry(
        engine,
        family_id="TF-NEG",
        tier_code="original_narration_pro",
        quality="pro",
        manual_price=600,  # cheaper than Flash → invalid
        pro_surcharge_display=None,
        system_reference_price=580,
    )

    resp = client.get("/pricing/catalog/T-NEG/tiers", headers=AUTH_HEADERS)
    assert resp.status_code == 422
    body = resp.get_json()
    assert body["error"]["code"] == "CATALOG_INHERITED_INVARIANT_VIOLATION"
    assert body["error"]["details"]["derived_surcharge"] == -100


# ── Public store API ──────────────────────────


def test_public_store_api_get_template_tiers_and_get_family_tiers(engine):
    """`get_template_tiers` and `get_family_tiers` are part of the
    documented module surface. Smoke test that both functions exist,
    return {tier_code: row} dicts, and
    surface the latest-enabled row when multiple versions exist."""
    from pricing_catalog_v2 import get_family_tiers, get_template_tiers

    _insert_template(engine, template_id="T-API", family_id="TF-API")
    _insert_template_entry(
        engine,
        catalog_entry_id="ce-api-old",
        template_id="T-API",
        tier_code="original_narration_flash",
        manual_price=500,
        system_reference_price=480,
        effective_version=1,
    )
    _insert_template_entry(
        engine,
        catalog_entry_id="ce-api-new",
        template_id="T-API",
        tier_code="original_narration_flash",
        manual_price=550,
        system_reference_price=520,
        effective_version=2,
    )
    _insert_family_entry(
        engine,
        family_id="TF-API",
        tier_code="derivative",
        flash_pro_axis="optional",
        mode=None,
        quality=None,
        product_line="derivative",
        manual_price=1000,
        system_reference_price=950,
    )

    with engine.connect() as conn:
        t_tiers = get_template_tiers(conn, "T-API")
        f_tiers = get_family_tiers(conn, "TF-API")

    assert set(t_tiers) == {"original_narration_flash"}
    # Latest enabled version wins.
    assert t_tiers["original_narration_flash"]["catalog_entry_id"] == "ce-api-new"
    assert t_tiers["original_narration_flash"]["manual_price"] == 550

    assert set(f_tiers) == {"derivative"}
    assert f_tiers["derivative"]["manual_price"] == 1000


# ── DB-down to 503 envelope ───────────


def test_db_connection_failure_returns_503(monkeypatch):
    """get_db_core_connection() raising SQLAlchemyError must map to the
    503 CATALOG_PERSISTENCE_ERROR envelope, not Flask's default 500.
    Uses a fresh client without the engine fixture so we control
    the connection path."""
    import server
    from sqlalchemy.exc import OperationalError

    monkeypatch.setenv("PRICING_BFF_AUTH_TOKEN", AUTH_TOKEN)
    monkeypatch.setattr(server, "get_db_engine", lambda: None)

    def boom():
        raise OperationalError("connect failed", {}, None)

    monkeypatch.setattr(server, "get_db_core_connection", boom)

    # Bypass require_web_user_auth (which would itself need a connection)
    # so the test targets the catalog handler's own connection acquisition.
    monkeypatch.setattr(
        server, "require_web_user_auth", lambda factory: None
    )

    old_testing = server.app.config.get("TESTING")
    old_propagate = server.app.config.get("PROPAGATE_EXCEPTIONS")
    server.app.config.update(TESTING=False, PROPAGATE_EXCEPTIONS=False)
    try:
        resp = server.app.test_client().get(
            "/pricing/catalog/T-WHATEVER/tiers", headers=AUTH_HEADERS
        )
    finally:
        server.app.config.update(
            TESTING=old_testing, PROPAGATE_EXCEPTIONS=old_propagate
        )

    assert resp.status_code == 503
    body = resp.get_json()
    assert body["success"] is False
    assert body["error"]["code"] == "CATALOG_PERSISTENCE_ERROR"
    assert body["error"]["retryable"] is True
    # security hardening (security hardening): the public envelope MUST NOT
    # leak exception-derived fields like `error_class`. Internal details
    # go to app.logger; the response carries an empty details dict and
    # a generic recoverable message.
    assert body["error"]["details"] == {}
    assert "OperationalError" not in body["error"]["message"]
    assert "SQLAlchemy" not in body["error"]["message"]


# ── Migration round-trip ───────────────────────────


def test_migration_upgrade_creates_all_three_tables(monkeypatch):
    """Execute ALL_SCHEMA_SQL against a fresh in-memory SQLite and
    verify the three tables (and their current-tier partial indexes)
    exist. Then execute DOWNGRADE_SQL and verify everything is gone.
    Catches DDL drift between the SQLAlchemy model in db/tables.py
    and the raw migration SQL."""
    from pricing_catalog_v2.schema import ALL_SCHEMA_SQL, DOWNGRADE_SQL

    eng = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}
    )

    # Upgrade. SQLAlchemy's `text(...).execute(...)` runs ONE SQL
    # statement per call on most DBAPI drivers, so split multi-statement
    # blocks (each ALL_SCHEMA_SQL element carries CREATE TABLE +
    # CREATE INDEX) on `;` and run each piece separately.
    def _split_and_exec(conn, sql_block):
        for stmt in sql_block.strip().split(";"):
            stmt = stmt.strip()
            if stmt:
                conn.execute(text(stmt))

    with eng.begin() as conn:
        for sql in ALL_SCHEMA_SQL:
            _split_and_exec(conn, sql)

    with eng.connect() as conn:
        tables = {
            row[0]
            for row in conn.execute(
                text("SELECT name FROM sqlite_master WHERE type = 'table'")
            ).fetchall()
        }
        assert "pricing_template_v2" in tables
        assert "pricing_catalog_v2_family" in tables
        assert "pricing_catalog_v2_entry" in tables
        idxs = {
            row[0]
            for row in conn.execute(
                text("SELECT name FROM sqlite_master WHERE type = 'index'")
            ).fetchall()
        }
        assert "pricing_catalog_v2_family_current_idx" in idxs
        assert "pricing_catalog_v2_entry_current_idx" in idxs

    # Downgrade. Each DOWNGRADE_SQL string is a single statement, but
    # keep the helper for symmetry / safety.
    with eng.begin() as conn:
        for sql in DOWNGRADE_SQL:
            _split_and_exec(conn, sql)

    with eng.connect() as conn:
        tables = {
            row[0]
            for row in conn.execute(
                text("SELECT name FROM sqlite_master WHERE type = 'table'")
            ).fetchall()
        }
        assert "pricing_template_v2" not in tables
        assert "pricing_catalog_v2_family" not in tables
        assert "pricing_catalog_v2_entry" not in tables

    eng.dispose()


def test_migration_module_invokes_schema_constants_in_order(monkeypatch):
    """Mock alembic op.execute and verify upgrade() / downgrade()
    invoke the documented SQL constants in the documented order. Guards
    against a future refactor that bypasses pricing_catalog_v2.schema."""
    from pricing_catalog_v2 import schema as v2_schema

    calls: list[str] = []

    class FakeOp:
        @staticmethod
        def execute(sql):
            calls.append(sql)

    # Re-import the migration module fresh with our fake op bound.
    migration_module_name = (
        "migrations.versions.20260529_0001_pricing_catalog_v2"
    )
    # Direct file load so the migration's `from alembic import op` resolves
    # to our fake.
    import sys
    from importlib.util import module_from_spec, spec_from_file_location

    repo_root = Path(__file__).resolve().parent.parent
    spec = spec_from_file_location(
        migration_module_name,
        repo_root / "migrations" / "versions" / "20260529_0001_pricing_catalog_v2.py",
    )
    module = module_from_spec(spec)
    sys.modules[migration_module_name] = module
    # Patch alembic.op with our fake on the module dict before exec.
    import alembic

    monkeypatch.setattr(alembic, "op", FakeOp)
    spec.loader.exec_module(module)

    calls.clear()
    module.upgrade()
    assert calls == list(v2_schema.ALL_SCHEMA_SQL), (
        "upgrade() must execute ALL_SCHEMA_SQL in declared order"
    )

    calls.clear()
    module.downgrade()
    assert calls == list(v2_schema.DOWNGRADE_SQL), (
        "downgrade() must execute DOWNGRADE_SQL in declared order"
    )


# ── regression coverage: PUT /pricing/catalog/<tid>/tiers + GET /history ────────────────────


def _tier_payload(
    tier_code,
    *,
    manual_price,
    system_reference_price,
    quality="flash",
    mode="narration",
    product_line="original",
    flash_pro_axis="required",
    pro_surcharge_display=None,
    raw_rate="0.83333",
    final_rate="1",
    rounding_rule_version="v2.0-round-half-up",
):
    payload = {
        "tier_code": tier_code,
        "product_line": product_line,
        "mode": mode,
        "quality": quality,
        "flash_pro_axis": flash_pro_axis,
        "manual_price": manual_price,
        "system_reference_price": system_reference_price,
        "raw_rate": raw_rate,
        "final_rate": final_rate,
        "rounding_rule_version": rounding_rule_version,
    }
    if pro_surcharge_display is not None:
        payload["pro_surcharge_display"] = pro_surcharge_display
    return payload


def test_upsert_5_tiers_atomic_happy_path(client, engine):
    """Atomic batch: PUT 5 tiers in one request, all written with
    server-assigned effective_version=1, manual_override_warning
    computed, response echoes the inserted rows."""
    _insert_template(engine, template_id="T-UPSERT-01")

    tiers = [
        _tier_payload("original_narration_flash", manual_price=800, system_reference_price=750),
        _tier_payload(
            "original_narration_pro",
            quality="pro",
            manual_price=1000,
            pro_surcharge_display=200,
            system_reference_price=900,
        ),
        _tier_payload(
            "original_mix_flash",
            mode="mix",
            manual_price=600,
            system_reference_price=550,
        ),
        _tier_payload(
            "original_mix_pro",
            mode="mix",
            quality="pro",
            manual_price=750,
            pro_surcharge_display=150,
            system_reference_price=680,
        ),
        _tier_payload(
            "derivative",
            product_line="derivative",
            mode=None,
            quality=None,
            flash_pro_axis="optional",
            manual_price=1200,
            system_reference_price=1100,
        ),
    ]

    resp = client.put(
        "/pricing/catalog/T-UPSERT-01/tiers",
        json={"tiers": tiers},
        headers=AUTH_HEADERS,
    )
    assert resp.status_code == 200, resp.get_json()
    data = resp.get_json()["data"]
    assert data["template_id"] == "T-UPSERT-01"
    assert len(data["tiers"]) == 5
    for t in data["tiers"]:
        assert t["effective_version"] == 1
        # updated_by serialized as string of users.id; the test fixture
        # seeds users.id=1.
        assert t["updated_by"] == "1"


def test_upsert_bumps_effective_version_on_each_save(client, engine):
    """A second PUT for an already-populated tier MUST allocate
    effective_version = max(existing) + 1, not collide on the unique
    (template_id, tier_code, effective_version) constraint."""
    _insert_template(engine, template_id="T-UPSERT-02")
    _insert_template_entry(
        engine,
        catalog_entry_id="ce-seed",
        template_id="T-UPSERT-02",
        tier_code="original_narration_flash",
        manual_price=700,
        system_reference_price=680,
        effective_version=1,
    )

    resp = client.put(
        "/pricing/catalog/T-UPSERT-02/tiers",
        json={
            "tiers": [
                _tier_payload(
                    "original_narration_flash",
                    manual_price=750,
                    system_reference_price=720,
                )
            ]
        },
        headers=AUTH_HEADERS,
    )
    assert resp.status_code == 200, resp.get_json()
    assert resp.get_json()["data"]["tiers"][0]["effective_version"] == 2


def test_upsert_partial_submission_validates_against_existing(client, engine):
    """Operator submits ONLY Pro (e.g. price tweak). Backend validates
    Pro >= existing-latest Flash for the same product_line+mode."""
    _insert_template(engine, template_id="T-UPSERT-03")
    _insert_template_entry(
        engine,
        catalog_entry_id="ce-flash-seed",
        template_id="T-UPSERT-03",
        tier_code="original_narration_flash",
        quality="flash",
        manual_price=900,
        system_reference_price=850,
    )

    resp = client.put(
        "/pricing/catalog/T-UPSERT-03/tiers",
        json={
            "tiers": [
                _tier_payload(
                    "original_narration_pro",
                    quality="pro",
                    manual_price=800,  # below existing Flash 900
                    pro_surcharge_display=-100,
                    system_reference_price=780,
                )
            ]
        },
        headers=AUTH_HEADERS,
    )
    # Pro < Flash should fire even though Flash is from existing rows
    # (not in submission). The CatalogValidationError on negative
    # pro_surcharge_display fires first.
    assert resp.status_code == 422
    code = resp.get_json()["error"]["code"]
    assert code in (
        "CATALOG_VALIDATION_ERROR",  # negative surcharge structural check
        "CATALOG_PRO_BELOW_FLASH",
    )


def test_upsert_pro_below_flash_within_submission(client, engine):
    """Both Flash + Pro submitted in same payload; Pro < Flash → 422."""
    _insert_template(engine, template_id="T-UPSERT-04")
    resp = client.put(
        "/pricing/catalog/T-UPSERT-04/tiers",
        json={
            "tiers": [
                _tier_payload(
                    "original_narration_flash", manual_price=800, system_reference_price=750
                ),
                _tier_payload(
                    "original_narration_pro",
                    quality="pro",
                    manual_price=750,  # 750 < 800
                    pro_surcharge_display=0,
                    system_reference_price=700,
                ),
            ]
        },
        headers=AUTH_HEADERS,
    )
    assert resp.status_code == 422
    body = resp.get_json()
    assert body["error"]["code"] == "CATALOG_PRO_BELOW_FLASH"
    assert body["error"]["details"]["flash_manual_price"] == 800
    assert body["error"]["details"]["pro_manual_price"] == 750


def test_upsert_pro_surcharge_invariant_violation(client, engine):
    """§4.1: pro.manual_price MUST equal flash.manual_price + surcharge."""
    _insert_template(engine, template_id="T-UPSERT-05")
    resp = client.put(
        "/pricing/catalog/T-UPSERT-05/tiers",
        json={
            "tiers": [
                _tier_payload("original_narration_flash", manual_price=800, system_reference_price=750),
                _tier_payload(
                    "original_narration_pro",
                    quality="pro",
                    manual_price=1100,
                    pro_surcharge_display=200,  # but 800+200 = 1000, not 1100
                    system_reference_price=1000,
                ),
            ]
        },
        headers=AUTH_HEADERS,
    )
    assert resp.status_code == 422
    body = resp.get_json()
    assert body["error"]["code"] == "CATALOG_PRO_SURCHARGE_MISMATCH"


def test_upsert_unknown_rounding_rule_version_rejected(client, engine):
    _insert_template(engine, template_id="T-UPSERT-06")
    resp = client.put(
        "/pricing/catalog/T-UPSERT-06/tiers",
        json={
            "tiers": [
                _tier_payload(
                    "original_narration_flash",
                    manual_price=800,
                    system_reference_price=750,
                    rounding_rule_version="v3.0-future-not-yet-registered",
                )
            ]
        },
        headers=AUTH_HEADERS,
    )
    assert resp.status_code == 422
    assert resp.get_json()["error"]["code"] == "CATALOG_ROUNDING_VERSION_UNKNOWN"


def test_upsert_unknown_template_returns_404(client):
    """Template not registered in pricing_template_v2 → 404."""
    resp = client.put(
        "/pricing/catalog/T-NEVER-REGISTERED/tiers",
        json={
            "tiers": [
                _tier_payload(
                    "original_narration_flash", manual_price=800, system_reference_price=750
                )
            ]
        },
        headers=AUTH_HEADERS,
    )
    assert resp.status_code == 404
    assert resp.get_json()["error"]["code"] == "CATALOG_TEMPLATE_NOT_FOUND"


def test_upsert_optional_axis_with_non_null_quality_rejected_in_route(client, engine):
    """Pre-DDL route-layer validation surfaces CATALOG_FLASH_PRO_AXIS_VIOLATION
    as 422 with a clean envelope, instead of letting the DDL CHECK
    surface as a 503 IntegrityError."""
    _insert_template(engine, template_id="T-UPSERT-07")
    resp = client.put(
        "/pricing/catalog/T-UPSERT-07/tiers",
        json={
            "tiers": [
                _tier_payload(
                    "derivative",
                    product_line="derivative",
                    flash_pro_axis="optional",
                    mode=None,
                    quality="pro",  # contract §3.1 violation
                    manual_price=1200,
                    system_reference_price=1100,
                )
            ]
        },
        headers=AUTH_HEADERS,
    )
    assert resp.status_code == 422
    assert resp.get_json()["error"]["code"] == "CATALOG_FLASH_PRO_AXIS_VIOLATION"


def test_upsert_missing_x_app_key_returns_401(client, engine):
    _insert_template(engine, template_id="T-UPSERT-08")
    resp = client.put(
        "/pricing/catalog/T-UPSERT-08/tiers",
        json={"tiers": [_tier_payload("original_narration_flash", manual_price=800, system_reference_price=750)]},
        headers={"Authorization": f"Bearer {AUTH_TOKEN}"},  # no X-Web-App-Key
    )
    assert resp.status_code == 401


def test_upsert_unknown_x_app_key_returns_401(client, engine):
    _insert_template(engine, template_id="T-UPSERT-09")
    resp = client.put(
        "/pricing/catalog/T-UPSERT-09/tiers",
        json={"tiers": [_tier_payload("original_narration_flash", manual_price=800, system_reference_price=750)]},
        headers={
            "Authorization": f"Bearer {AUTH_TOKEN}",
            "X-Web-App-Key": "grid_UNKNOWN",
        },
    )
    assert resp.status_code == 401


def test_upsert_malformed_body_returns_400(client, engine):
    _insert_template(engine, template_id="T-UPSERT-10")
    resp = client.put(
        "/pricing/catalog/T-UPSERT-10/tiers",
        data='{"tiers": [malformed',
        content_type="application/json",
        headers=AUTH_HEADERS,
    )
    assert resp.status_code == 400
    assert resp.get_json()["error"]["code"] == "BAD_REQUEST"


def test_upsert_atomic_rollback_on_validation_failure(client, engine):
    """If ANY tier in the batch fails validation, NONE are persisted."""
    _insert_template(engine, template_id="T-UPSERT-11")
    resp = client.put(
        "/pricing/catalog/T-UPSERT-11/tiers",
        json={
            "tiers": [
                _tier_payload("original_narration_flash", manual_price=800, system_reference_price=750),
                _tier_payload(
                    "original_narration_pro",
                    quality="pro",
                    manual_price=600,  # below 800 → Pro<Flash
                    pro_surcharge_display=0,
                    system_reference_price=580,
                ),
            ]
        },
        headers=AUTH_HEADERS,
    )
    assert resp.status_code == 422

    # Read back: no row should have been inserted for either tier.
    get_resp = client.get(
        "/pricing/catalog/T-UPSERT-11/tiers", headers=AUTH_HEADERS
    )
    # Returns 404 CATALOG_TIER_MISSING because nothing was written.
    assert get_resp.status_code == 404


def test_upsert_manual_override_warning_at_30pct_boundary(client, engine):
    """30% threshold is INCLUSIVE per contract §7. Verify boundary +
    just-under-boundary cases."""
    _insert_template(engine, template_id="T-UPSERT-12")

    # Exactly 30% over: manual=1300, reference=1000, ratio=0.30 → warning=True
    resp = client.put(
        "/pricing/catalog/T-UPSERT-12/tiers",
        json={
            "tiers": [
                _tier_payload(
                    "original_narration_flash",
                    manual_price=1300,
                    system_reference_price=1000,
                )
            ]
        },
        headers=AUTH_HEADERS,
    )
    assert resp.status_code == 200, resp.get_json()
    assert resp.get_json()["data"]["tiers"][0]["manual_override_warning"] is True

    # 29% over: ratio=0.29 → warning=False
    _insert_template(engine, template_id="T-UPSERT-13")
    resp = client.put(
        "/pricing/catalog/T-UPSERT-13/tiers",
        json={
            "tiers": [
                _tier_payload(
                    "original_narration_flash",
                    manual_price=1290,
                    system_reference_price=1000,
                )
            ]
        },
        headers=AUTH_HEADERS,
    )
    assert resp.status_code == 200
    assert resp.get_json()["data"]["tiers"][0]["manual_override_warning"] is False


def test_history_returns_all_versions_including_disabled(client, engine):
    """Disabled older rows must surface in history for audit."""
    _insert_template(engine, template_id="T-HIST-01")
    _insert_template_entry(
        engine,
        catalog_entry_id="ce-h1",
        template_id="T-HIST-01",
        tier_code="original_narration_flash",
        manual_price=600,
        system_reference_price=580,
        effective_version=1,
        enabled=False,  # archived
    )
    _insert_template_entry(
        engine,
        catalog_entry_id="ce-h2",
        template_id="T-HIST-01",
        tier_code="original_narration_flash",
        manual_price=700,
        system_reference_price=680,
        effective_version=2,
        enabled=True,
    )

    resp = client.get(
        "/pricing/catalog/T-HIST-01/history", headers=AUTH_HEADERS
    )
    assert resp.status_code == 200, resp.get_json()
    tiers = resp.get_json()["data"]["tiers"]
    history = tiers["original_narration_flash"]
    assert len(history) == 2
    # Newest first.
    assert history[0]["effective_version"] == 2
    assert history[0]["manual_price"] == 700
    assert history[0]["enabled"] is True
    assert history[1]["effective_version"] == 1
    assert history[1]["manual_price"] == 600
    assert history[1]["enabled"] is False


def test_history_unknown_template_returns_404(client):
    resp = client.get(
        "/pricing/catalog/T-NO-HISTORY/history", headers=AUTH_HEADERS
    )
    assert resp.status_code == 404
    assert resp.get_json()["error"]["code"] == "CATALOG_TIER_MISSING"


# ── regression coverage: upstream identity surfaced on GET + omitted-field defaults on PUT ──


def test_get_tiers_surfaces_template_identity(client, engine):
    """GET /pricing/catalog/<id>/tiers carries code / name /
    learning_model_id on the response `data` so the admin UI can show
    the template's human label next to the numeric template_id."""
    _insert_template(
        engine,
        template_id="T-IDENTITY-01",
        code="xy0046",
        name="热血动作-困兽之斗解说",
        learning_model_id="narrator-20250916152104-DYsban",
    )
    _insert_template_entry(
        engine,
        catalog_entry_id="ce-id-1",
        template_id="T-IDENTITY-01",
        tier_code="original_narration_flash",
        manual_price=70,
        system_reference_price=70,
    )

    resp = client.get("/pricing/catalog/T-IDENTITY-01/tiers", headers=AUTH_HEADERS)
    assert resp.status_code == 200, resp.get_json()
    data = resp.get_json()["data"]
    assert data["code"] == "xy0046"
    assert data["name"] == "热血动作-困兽之斗解说"
    assert data["learning_model_id"] == "narrator-20250916152104-DYsban"


def test_get_tiers_identity_null_for_unseeded_template(client, engine):
    """Template registered before the seeder ran: identity fields
    return null (frontend renders `-`). No 500 / no missing-key."""
    _insert_template(engine, template_id="T-IDENTITY-02")
    _insert_template_entry(
        engine,
        catalog_entry_id="ce-id-2",
        template_id="T-IDENTITY-02",
        tier_code="original_narration_flash",
        manual_price=70,
        system_reference_price=70,
    )

    resp = client.get("/pricing/catalog/T-IDENTITY-02/tiers", headers=AUTH_HEADERS)
    assert resp.status_code == 200
    data = resp.get_json()["data"]
    assert data["code"] is None
    assert data["name"] is None
    assert data["learning_model_id"] is None


def test_upsert_accepts_payload_without_rate_or_rule(client, engine):
    """Admin UI no longer sends raw_rate / final_rate /
    rounding_rule_version. The server defaults them to
    Decimal(manual_price) / v2.0-round-half-up so the schema (NOT
    NULL) stays satisfied without changing tier semantics."""
    _insert_template(engine, template_id="T-IDENTITY-03")

    payload = {
        "tier_code": "original_narration_flash",
        "product_line": "original",
        "mode": "narration",
        "quality": "flash",
        "flash_pro_axis": "required",
        "manual_price": 70,
        "system_reference_price": 70,
    }

    resp = client.put(
        "/pricing/catalog/T-IDENTITY-03/tiers",
        json={"tiers": [payload]},
        headers=AUTH_HEADERS,
    )
    assert resp.status_code == 200, resp.get_json()
    tier = resp.get_json()["data"]["tiers"][0]
    assert tier["raw_rate"] == "70"
    assert tier["final_rate"] == "70"
    assert tier["rounding_rule_version"] == "v2.0-round-half-up"


def test_upsert_still_accepts_legacy_payload_with_all_fields(client, engine):
    """Scripted upserts that still send the full envelope must keep
    working (no behavior regression)."""
    _insert_template(engine, template_id="T-IDENTITY-04")

    resp = client.put(
        "/pricing/catalog/T-IDENTITY-04/tiers",
        json={
            "tiers": [
                _tier_payload(
                    "original_narration_flash",
                    manual_price=80,
                    system_reference_price=75,
                    raw_rate="1.33333",
                    final_rate="1.33",
                )
            ]
        },
        headers=AUTH_HEADERS,
    )
    assert resp.status_code == 200, resp.get_json()
    tier = resp.get_json()["data"]["tiers"][0]
    assert tier["raw_rate"] == "1.33333"
    assert tier["final_rate"] == "1.33"


# ── regression coverage: server-side Pro pro_surcharge_display derivation ───────────────────


def test_upsert_derives_pro_surcharge_from_matching_flash_in_same_submission(
    client, engine
):
    """regression coverage — Pro tier omits pro_surcharge_display; matching Flash is in
    the same submission. Server derives surcharge = pro - flash and
    persists. Wire payload stays fully lean for the lean-admin client."""
    _insert_template(engine, template_id="T-301-01")

    flash = _tier_payload(
        "original_narration_flash",
        manual_price=70,
        system_reference_price=70,
    )
    pro = _tier_payload(
        "original_narration_pro",
        quality="pro",
        manual_price=78,
        system_reference_price=78,
    )
    # Lean envelope: Pro omits pro_surcharge_display entirely.
    assert "pro_surcharge_display" not in pro

    resp = client.put(
        "/pricing/catalog/T-301-01/tiers",
        json={"tiers": [flash, pro]},
        headers=AUTH_HEADERS,
    )
    assert resp.status_code == 200, resp.get_json()
    by_code = {t["tier_code"]: t for t in resp.get_json()["data"]["tiers"]}
    assert by_code["original_narration_pro"]["pro_surcharge_display"] == 8


def test_upsert_derives_pro_surcharge_from_existing_catalog_flash(client, engine):
    """regression coverage — Pro tier omits surcharge; Flash already exists in catalog
    from an earlier save. Server pulls Flash from existing rows, derives
    surcharge, persists Pro alone."""
    _insert_template(engine, template_id="T-301-02")
    _insert_template_entry(
        engine,
        catalog_entry_id="ce-flash-301",
        template_id="T-301-02",
        tier_code="original_narration_flash",
        manual_price=100,
        system_reference_price=100,
    )

    pro = _tier_payload(
        "original_narration_pro",
        quality="pro",
        manual_price=115,
        system_reference_price=115,
    )

    resp = client.put(
        "/pricing/catalog/T-301-02/tiers",
        json={"tiers": [pro]},
        headers=AUTH_HEADERS,
    )
    assert resp.status_code == 200, resp.get_json()
    tier = resp.get_json()["data"]["tiers"][0]
    assert tier["pro_surcharge_display"] == 15


def test_upsert_pro_without_matching_flash_returns_pro_needs_flash(client, engine):
    """regression coverage — Pro tier omits surcharge AND no Flash exists anywhere.
    Server returns 422 CATALOG_PRO_NEEDS_FLASH with the offending
    tier_code in details, distinct from CATALOG_PRO_SURCHARGE_MISMATCH."""
    _insert_template(engine, template_id="T-301-03")

    pro = _tier_payload(
        "original_narration_pro",
        quality="pro",
        manual_price=200,
        system_reference_price=200,
    )

    resp = client.put(
        "/pricing/catalog/T-301-03/tiers",
        json={"tiers": [pro]},
        headers=AUTH_HEADERS,
    )
    assert resp.status_code == 422, resp.get_json()
    err = resp.get_json()["error"]
    assert err["code"] == "CATALOG_PRO_NEEDS_FLASH"
    assert err["details"]["tier_code"] == "original_narration_pro"
    assert err["details"]["product_line"] == "original"
    assert err["details"]["mode"] == "narration"


def test_upsert_explicit_mismatched_surcharge_still_returns_surcharge_mismatch(
    client, engine
):
    """regression coverage — Caller explicitly supplies a mismatched
    pro_surcharge_display. Existing §4.1 invariant check still fires
    (CATALOG_PRO_SURCHARGE_MISMATCH); the new derivation path leaves
    supplied values untouched."""
    _insert_template(engine, template_id="T-301-04")

    flash = _tier_payload(
        "original_narration_flash",
        manual_price=70,
        system_reference_price=70,
    )
    pro = _tier_payload(
        "original_narration_pro",
        quality="pro",
        manual_price=80,
        system_reference_price=80,
        pro_surcharge_display=20,  # diff is 10, supplied is 20 — mismatch
    )

    resp = client.put(
        "/pricing/catalog/T-301-04/tiers",
        json={"tiers": [flash, pro]},
        headers=AUTH_HEADERS,
    )
    assert resp.status_code == 422, resp.get_json()
    assert resp.get_json()["error"]["code"] == "CATALOG_PRO_SURCHARGE_MISMATCH"
