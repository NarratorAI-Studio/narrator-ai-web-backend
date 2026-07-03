"""Tests for template price v2 quote + snapshot persistence .

Strategy mirrors `test_pricing_catalog_v2.py`: an in-memory SQLite
engine seeded with the production DDL from
`pricing_catalog_v2.schema.ALL_SCHEMA_SQL` and
`pricing_quote_v2.schema.PRICING_QUOTES_V2_SCHEMA_SQL` /
`PRICING_SNAPSHOTS_V2_SCHEMA_SQL`. We bypass the ALTER TABLE part of
`NARRATOR_TASKS_SNAPSHOT_LINK_SQL` by creating `narrator_tasks` with
`snapshot_id` baked in.

Coverage focuses on the frozen contract codes from
`docs/pricing/v2/quote-snapshot-contract.md` §5 / §6:
  - 402 WALLET_INSUFFICIENT_BALANCE
  - 404 CATALOG_TIER_MISSING / QUOTE_NOT_FOUND
  - 409 QUOTE_PARAMETERS_CHANGED / QUOTE_PRICE_DRIFTED
  - 410 QUOTE_EXPIRED
  - 422 QUOTE_COMBO_KEY_INVALID / CUSTOM_SRT_HASH_MISSING /
        QUOTE_BODY_PRICE_FORBIDDEN

Plus the happy paths (manual + custom) and the §6.1 lock-timing
windows (< 60s reuse / 60s-TTL silent re-quote / ≥ TTL expired).
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, text

from pricing_catalog_v2.schema import ALL_SCHEMA_SQL as CATALOG_SCHEMA_SQL
from pricing_quote_v2 import (
    QuoteBodyPriceForbidden,
    QuoteComboKeyInvalid,
    QuoteExpired,
    QuoteNotFound,
    QuoteParametersChanged,
    QuotePriceDrifted,
    QuoteTemplateCodeInvalid,
    WalletInsufficientBalance,
    commit_master_task_snapshot,
    generate_quote,
)
from pricing_quote_v2.schema import (
    PRICING_QUOTES_V2_SCHEMA_SQL,
    PRICING_SNAPSHOTS_V2_SCHEMA_SQL,
)


sqlite3.register_adapter(Decimal, str)


AUTH_TOKEN = "test-bff-token"
WEB_APP_KEY = "grid_AbCdEfGhIjKlMnOpQrStUv"
AUTH_HEADERS = {
    "Authorization": f"Bearer {AUTH_TOKEN}",
    "X-Web-App-Key": WEB_APP_KEY,
}

NOW = datetime(2026, 5, 30, 8, 0, 0, tzinfo=timezone.utc)

TEMPLATE_ID = "tpl_test_001"
FAMILY_ID = "fam_test"


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


# Narrator-tasks DDL with snapshot_id baked in — the production migration
# does ALTER TABLE to add the column; tests collapse it into the
# original CREATE so we don't have to run two migrations in order.
SQLITE_NARRATOR_TASKS_SCHEMA = """
CREATE TABLE narrator_tasks (
    narrator_task_id TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL,
    app_key TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    current_step TEXT,
    data TEXT NOT NULL,
    snapshot_id TEXT,
    run_auto SMALLINT NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


# user_cloud_files schema — needed because the /pricing/quote custom branch
# resolves `custom_srt_file_id` against this table for ownership before
# downloading from cloud-drive upstream. Mirrors the SQLite version in
# tests/test_cloud_drive.py.
SQLITE_USER_CLOUD_FILES_SCHEMA = """
CREATE TABLE user_cloud_files (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    reservation_id TEXT NOT NULL UNIQUE,
    user_id INTEGER NOT NULL REFERENCES users(id),
    app_key TEXT NOT NULL,
    file_id TEXT UNIQUE,
    object_key TEXT,
    file_name TEXT NOT NULL,
    suffix TEXT NOT NULL DEFAULT '',
    category INTEGER NOT NULL DEFAULT 0,
    file_size INTEGER NOT NULL DEFAULT 0 CHECK (file_size >= 0),
    content_type TEXT,
    source TEXT NOT NULL,
    status TEXT NOT NULL CHECK (
        status IN (
            'reserved', 'completed', 'failed', 'delete_pending', 'deleted',
            'transfer_pending', 'transfer_running', 'transfer_completed'
        )
    ),
    upstream_status INTEGER,
    progress INTEGER NOT NULL DEFAULT 0 CHECK (progress >= 0 AND progress <= 100),
    srt_file_hash TEXT,
    upstream_payload TEXT NOT NULL DEFAULT '{}',
    upload_id TEXT,
    parent_reservation_id TEXT,
    settled_at TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    completed_at TEXT,
    deleted_at TEXT
);
"""


def _exec_each(conn, sql_blob: str) -> None:
    """Split a `;`-separated DDL blob into single statements (matches
    the pattern used in test_pricing_catalog_v2)."""
    for stmt in sql_blob.strip().split(";"):
        stmt = stmt.strip()
        if stmt:
            conn.execute(text(stmt))


def _create_engine(balance_points: int = 1000):
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}
    )
    with engine.begin() as conn:
        conn.execute(text(SQLITE_USERS_SCHEMA))
        conn.execute(
            text(
                "INSERT INTO users (app_key, id, balance_points) "
                "VALUES (:k, 1, :b)"
            ),
            {"k": WEB_APP_KEY, "b": balance_points},
        )
        conn.execute(text(SQLITE_NARRATOR_TASKS_SCHEMA))
        conn.execute(text(SQLITE_USER_CLOUD_FILES_SCHEMA))
        for sql in CATALOG_SCHEMA_SQL:
            _exec_each(conn, sql)
        _exec_each(conn, PRICING_QUOTES_V2_SCHEMA_SQL)
        _exec_each(conn, PRICING_SNAPSHOTS_V2_SCHEMA_SQL)
    return engine


def _seed_template(
    engine,
    *,
    template_id=TEMPLATE_ID,
    family_id=None,
    multiplier="1.0",
    video_duration_seconds: int | None = 60,
):
    """Seed a `pricing_template_v2` row. Defaults to a 60-second
    (1 minute) duration so subsequent update tiered manual-catalog
    pricing has a deterministic baseline — pass an explicit value to
    exercise boundary cases. Pass `None` to test the
    `MANUAL_CATALOG_DURATION_MISSING` fail-closed branch.
    """
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO pricing_template_v2 ("
                "template_id, template_family_id, tier_multiplier, enabled, "
                "created_at, updated_at, video_duration_seconds) "
                "VALUES (:tid, :fid, :mult, 1, :now, :now, :vds)"
            ),
            {
                "tid": template_id,
                "fid": family_id,
                "mult": multiplier,
                "now": NOW,
                "vds": video_duration_seconds,
            },
        )


def _seed_template_tier(
    engine,
    *,
    template_id=TEMPLATE_ID,
    tier_code,
    manual_price,
    system_reference_price=None,
    flash_pro_axis="required",
    mode="narration",
    quality="flash",
    product_line="original",
    catalog_entry_id=None,
    effective_version=1,
    raw_rate="1",
    final_rate="1",
):
    """Insert one catalog tier row. Defaults mirror the contract's
    Flash/Pro narration pair so existing combo_keys validate. Optional-
    axis tiers (e.g. `derivative`) must pass mode=None, quality=None to
    satisfy `flash_pro_axis_optional_nullifies_mode_quality`."""
    if system_reference_price is None:
        system_reference_price = manual_price
    if catalog_entry_id is None:
        catalog_entry_id = f"ce_{tier_code}_{effective_version}"
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
                ":id, :tid, :tc, :ver, :pl, :mode, :q, :axis, :mp, NULL, :srp, "
                "'web_point', :rr, :fr, 'v2.0-round-half-up', 0, 1, :now, :now, 'tester')"
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
                "srp": system_reference_price,
                "rr": raw_rate,
                "fr": final_rate,
                "now": NOW,
            },
        )


def _seed_cloud_file(
    engine,
    *,
    file_id: str = "cf_srt_001",
    user_id: int = 1,
    app_key: str = WEB_APP_KEY,
    status: str = "completed",
    file_size: int = 200,
    file_name: str = "subtitles.srt",
    suffix: str = ".srt",
    reservation_id: str | None = None,
) -> None:
    """Insert one user_cloud_files row so the custom-SRT flow's
    `cd_get_file(user_id, file_id)` ownership check can succeed."""
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO user_cloud_files ("
                "reservation_id, user_id, app_key, file_id, file_name, "
                "suffix, category, file_size, source, status, progress, "
                "upstream_payload, created_at, updated_at, completed_at) VALUES ("
                ":rid, :uid, :ak, :fid, :fn, :sfx, 0, :sz, 'local_upload', "
                ":st, 100, '{}', :now, :now, :now)"
            ),
            {
                "rid": reservation_id or f"res_{file_id}",
                "uid": user_id,
                "ak": app_key,
                "fid": file_id,
                "fn": file_name,
                "sfx": suffix,
                "sz": file_size,
                "st": status,
                "now": NOW,
            },
        )


# ── fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture()
def engine():
    eng = _create_engine()
    _seed_template(eng)
    _seed_template_tier(
        eng, tier_code="original_narration_flash", manual_price=100, quality="flash"
    )
    _seed_template_tier(
        eng, tier_code="original_narration_pro", manual_price=140, quality="pro"
    )
    yield eng
    eng.dispose()


@pytest.fixture()
def low_balance_engine():
    # 10 web_point < the default 60s template's tiered Flash price (19),
    # so the wallet preflight raises WalletInsufficientBalance.
    eng = _create_engine(balance_points=10)
    _seed_template(eng)
    _seed_template_tier(
        eng, tier_code="original_narration_flash", manual_price=100, quality="flash"
    )
    _seed_template_tier(
        eng, tier_code="original_narration_pro", manual_price=140, quality="pro"
    )
    yield eng
    eng.dispose()


@pytest.fixture()
def client(engine, monkeypatch):
    import server

    monkeypatch.setattr(server, "get_db_engine", lambda: engine)
    monkeypatch.setattr(server, "get_db_core_connection", lambda: engine.connect())
    monkeypatch.setenv("PRICING_BFF_AUTH_TOKEN", AUTH_TOKEN)
    return server.app.test_client()


@pytest.fixture()
def low_balance_client(low_balance_engine, monkeypatch):
    import server

    monkeypatch.setattr(server, "get_db_engine", lambda: low_balance_engine)
    monkeypatch.setattr(
        server, "get_db_core_connection", lambda: low_balance_engine.connect()
    )
    monkeypatch.setenv("PRICING_BFF_AUTH_TOKEN", AUTH_TOKEN)
    return server.app.test_client()


# ── unit: generate_quote ────────────────────────────────────────────────────


def test_generate_quote_manual_branch_fails_closed_without_duration():
    """The implementation requirement subsequent update: a template with NULL
    `video_duration_seconds` must raise 422
    `MANUAL_CATALOG_DURATION_MISSING` rather than fall back to a stale
    catalog manual_price. Operators backfills via the refresh script's
    `--seed-missing` path before the template can quote.
    """
    from pricing_quote_v2 import QuoteManualCatalogDurationMissing

    eng = _create_engine()
    _seed_template(eng, video_duration_seconds=None)  # explicitly missing
    _seed_template_tier(
        eng, tier_code="original_narration_flash", manual_price=100, quality="flash"
    )
    _seed_template_tier(
        eng, tier_code="original_narration_pro", manual_price=140, quality="pro"
    )
    try:
        with eng.connect() as conn:
            with pytest.raises(QuoteManualCatalogDurationMissing) as excinfo:
                generate_quote(
                    conn,
                    request_body={
                        "template_id": TEMPLATE_ID,
                        "combo_key": "original_narration_flash",
                    },
                    user_id=1,
                    now=NOW,
                )
        assert excinfo.value.code == "MANUAL_CATALOG_DURATION_MISSING"
        assert excinfo.value.http_status == 422
        assert excinfo.value.details["template_id"] == TEMPLATE_ID
    finally:
        eng.dispose()


@pytest.mark.parametrize(
    ("video_seconds", "combo_key", "expected"),
    [
        # Boundary AC table from the implementation requirement:
        # original_narration_flash recipe = (2+10+7) per minute under 3min,
        # (2+6+4) per minute after 3min.
        (60, "original_narration_flash", 19),    # 1 min × 19
        (180, "original_narration_flash", 57),   # 3 min × 19 (boundary)
        (240, "original_narration_flash", 69),   # 3 × 19 + 1 × 12
        (300, "original_narration_flash", 81),   # 3 × 19 + 2 × 12
        (600, "original_narration_flash", 141),  # 3 × 19 + 7 × 12
        # Pro recipe: (6+10+7) under, (6+6+4) after.
        (180, "original_narration_pro", 69),     # 23 × 3
        # Mix Flash recipe = (5+10+7) under, (5+6+4) after.
        (300, "original_remix_flash", 96),       # 22×3 + 15×2 = 66+30 = 96
    ],
)
def test_generate_quote_manual_branch_tiered_formula_boundaries(
    video_seconds: int, combo_key: str, expected: int
):
    """The implementation requirement AC: cover 2.8 / 3 / 4 / 5 / 10-minute boundaries on the
    existing-template branch. All charge prices follow the same formula
    as the custom-template branch (the previous PR's
    `compute_tier_system_reference_price` is the single source of
    truth)."""
    eng = _create_engine()
    _seed_template(eng, video_duration_seconds=video_seconds)
    _seed_template_tier(
        eng, tier_code="original_narration_flash", manual_price=999, quality="flash"
    )
    _seed_template_tier(
        eng, tier_code="original_narration_pro", manual_price=999, quality="pro"
    )
    if combo_key == "original_remix_flash":
        _seed_template_tier(
            eng,
            tier_code="original_mix_flash",
            manual_price=999,
            quality="flash",
            mode="mix",
        )
        _seed_template_tier(
            eng,
            tier_code="original_mix_pro",
            manual_price=999,
            quality="pro",
            mode="mix",
        )
    try:
        with eng.connect() as conn:
            quote = generate_quote(
                conn,
                request_body={
                    "template_id": TEMPLATE_ID,
                    "combo_key": combo_key,
                    "pro_upgrade": combo_key.endswith("_pro"),
                },
                user_id=1,
                now=NOW,
            )
            conn.commit()
        # manual_price=999 in seed is intentionally inflated — assert the
        # formula wins, not the stale catalog value.
        assert quote.final_charge_price == expected
        assert quote.system_reference_price == expected
    finally:
        eng.dispose()


def test_generate_quote_manual_derivative_omits_popular_learning():
    """The implementation requirement "二创文案，已有模板": existing-template `derivative`
    tier uses recipe (15+10+7) — NOT (13+15+10+7). popular_learning
    (template_learning, image's name) is only on the custom-template
    branch. The custom-template counterpart uses 45/min at 3min in;
    existing-template stops at 32/min.
    """
    eng = _create_engine()
    # 3-minute exact, so under-3 portion only:
    #   existing-template: 3 × (15 + 10 + 7) = 96
    #   custom-template (for reference, not exercised here): 3 × (13+15+10+7) = 135
    _seed_template(eng, template_id="177", video_duration_seconds=180)
    _seed_template_tier(
        eng,
        template_id="177",
        tier_code="derivative",
        manual_price=999,
        flash_pro_axis="optional",
        product_line="derivative",
        mode=None,
        quality=None,
    )
    try:
        with eng.connect() as conn:
            quote = generate_quote(
                conn,
                request_body={
                    "template_id": "177",
                    "combo_key": "secondary_creation",
                    "pro_upgrade": False,
                },
                user_id=1,
                now=NOW,
            )
            conn.commit()
        assert quote.final_charge_price == 96
        # 3 rows, NOT 4 — the popular_learning step is omitted for
        # existing templates per the issue table decomposition.
        keys = [item["subflow_key"] for item in quote.breakdown]
        assert "popular_learning" not in keys
        assert keys == ["secondary_creation", "clip_data", "video_composing"]
    finally:
        eng.dispose()


def test_generate_quote_manual_flash(engine):
    # Default seed: 60s = 1 min, all under 3min. Flash recipe is
    # original_narration_flash wenan(2,2) + clip_data(10,6) + video_composing(7,4):
    #   1 × (2 + 10 + 7) = 19   (under-3min portion only)
    # Pro recipe swaps wenan rate to (6,6): 1 × (6 + 10 + 7) = 23.
    with engine.connect() as conn:
        quote = generate_quote(
            conn,
            request_body={
                "template_id": TEMPLATE_ID,
                "combo_key": "original_narration_flash",
                "pro_upgrade": False,
            },
            user_id=1,
            now=NOW,
        )
        conn.commit()
    assert quote.price_source == "manual_catalog_price"
    assert quote.final_charge_price == 19
    assert quote.flash_total == 19
    assert quote.pro_total == 23
    assert quote.pro_upgrade_delta == 4
    assert quote.starting_price == 19
    assert quote.template_id == TEMPLATE_ID
    assert quote.combo_key == "original_narration_flash"
    # 3-row tiered breakdown: wenan + clip_data + video_composing.
    keys = [item["subflow_key"] for item in quote.breakdown]
    assert keys == ["original_narration_flash", "clip_data", "video_composing"]
    assert sum(int(item["subtotal"]) for item in quote.breakdown) == 19
    # SQLite roundtrip drops tzinfo, so compare via the naive UTC form.
    expires_naive = quote.expires_at.replace(tzinfo=None) if quote.expires_at.tzinfo else quote.expires_at
    assert expires_naive > NOW.replace(tzinfo=None)


def test_resolve_template_identity_coerces_int_to_str():
    """Regression for regression coverage.

    JSON callers commonly send `template_id` as an integer
    (`{"template_id": 308}`). `pricing_template_v2.template_id` and
    `pricing_quotes_v2.template_id` / `custom_template_id` are TEXT
    columns; Postgres rejects `text = integer` with `UndefinedFunction`
    → SQLAlchemyError → QuotePersistenceError on prod. SQLite is lax
    about cross-type equality so end-to-end SQLite tests can't catch
    this — assert the coercion at the input boundary directly.
    """
    from pricing_quote_v2.service import _resolve_template_identity

    _, template_id, custom_template_id = _resolve_template_identity(
        {"template_id": 308, "combo_key": "original_narration_flash"}
    )
    assert template_id == "308"
    assert isinstance(template_id, str)
    assert custom_template_id is None

    _, template_id, custom_template_id = _resolve_template_identity(
        {"custom_template_id": 42, "combo_key": "secondary_creation"}
    )
    assert template_id is None
    assert custom_template_id == "42"
    assert isinstance(custom_template_id, str)

    # str inputs pass through unchanged.
    _, template_id, _ = _resolve_template_identity(
        {"template_id": "308", "combo_key": "original_narration_flash"}
    )
    assert template_id == "308"

    # bool is a subclass of int but never valid here — leave untouched so
    # downstream validation can reject; do NOT coerce True → "True".
    _, template_id, _ = _resolve_template_identity(
        {"template_id": True, "combo_key": "original_narration_flash"}
    )
    assert template_id is True


def test_generate_quote_manual_pro_upgrade(engine):
    # Default 60s seed, Pro recipe: 1 × (6 + 10 + 7) = 23.
    # pro_upgrade_delta vs Flash recipe (1 × 19) is 4.
    with engine.connect() as conn:
        quote = generate_quote(
            conn,
            request_body={
                "template_id": TEMPLATE_ID,
                "combo_key": "original_narration_pro",
                "pro_upgrade": True,
            },
            user_id=1,
            now=NOW,
        )
        conn.commit()
    assert quote.final_charge_price == 23
    assert quote.flash_total == 19
    assert quote.pro_total == 23
    assert quote.pro_upgrade_delta == 4
    # Pro breakdown emits the Pro recipe's 3 subflows directly — under
    # regression coverage there is no separate "pro_upgrade" delta row; the Pro
    # premium lives entirely in the wenan rate difference.
    keys = [item["subflow_key"] for item in quote.breakdown]
    assert keys == ["original_narration_pro", "clip_data", "video_composing"]
    assert sum(int(i["subtotal"]) for i in quote.breakdown) == 23


def test_generate_quote_manual_accepts_remix_alias_for_mix_catalog(engine):
    """Web sends `original_remix_*`; the seeded manual catalog may use
    `original_mix_*`. Quote should still resolve by alias while storing
    the submitted public combo_key."""
    _seed_template(engine, template_id="178")
    _seed_template_tier(
        engine,
        template_id="178",
        tier_code="original_mix_flash",
        manual_price=27,
        quality="flash",
    )
    _seed_template_tier(
        engine,
        template_id="178",
        tier_code="original_mix_pro",
        manual_price=54,
        quality="pro",
    )

    with engine.connect() as conn:
        quote = generate_quote(
            conn,
            request_body={
                "code": "xy0178",
                "template_id": "303",
                "combo_key": "original_remix_flash",
                "pro_upgrade": False,
            },
            user_id=1,
            now=NOW,
        )
        conn.commit()

    assert quote.template_id == "178"
    assert quote.code == "xy0178"
    assert quote.combo_key == "original_remix_flash"
    # Default 60s seed, remix Flash recipe: 1 × (5 + 10 + 7) = 22.
    # Remix Pro recipe: 1 × (17 + 10 + 7) = 34. Delta = 12.
    assert quote.final_charge_price == 22
    assert quote.flash_total == 22
    assert quote.pro_total == 34
    assert quote.pro_upgrade_delta == 12


def test_generate_quote_manual_accepts_secondary_creation_alias_for_derivative(
    engine,
):
    """Web sends `secondary_creation`; the seeded v2 catalog uses
    `derivative` (v1->v2 rename in seed_pricing_catalog_v2_from_v1.py).
    Quote should resolve by alias while persisting the public key."""
    _seed_template(engine, template_id="177")
    _seed_template_tier(
        engine,
        template_id="177",
        tier_code="derivative",
        manual_price=135,
        flash_pro_axis="optional",
        product_line="derivative",
        mode=None,
        quality=None,
    )

    with engine.connect() as conn:
        quote = generate_quote(
            conn,
            request_body={
                "code": "xy0177",
                "template_id": "302",
                "combo_key": "secondary_creation",
                "pro_upgrade": False,
            },
            user_id=1,
            now=NOW,
        )
        conn.commit()

    assert quote.template_id == "177"
    assert quote.code == "xy0177"
    assert quote.combo_key == "secondary_creation"
    # Default 60s, derivative recipe (no popular_learning, per requirement
    # "二创文案，已有模板" decomposition): 1 × (15 + 10 + 7) = 32.
    # Axis-optional → flash_total == pro_total, delta = 0.
    assert quote.final_charge_price == 32
    assert quote.flash_total == 32
    assert quote.pro_total == 32
    assert quote.pro_upgrade_delta == 0


def test_generate_quote_custom_template(engine, monkeypatch):
    # Client passes only `custom_srt_file_id`; backend resolves
    # ownership, downloads bytes, hashes + parses server-side.
    _seed_cloud_file(engine, file_id="cf_srt_001", user_id=1, file_size=200)

    # 50-line SRT fixture; the 5a parser sees 50 valid text lines.
    fake_lines = [
        f"{i}\n00:00:{i:02d},000 --> 00:00:{i+1:02d},000\nLine {i}\n"
        for i in range(1, 51)
    ]
    srt_bytes = "\n".join(fake_lines).encode("utf-8")

    from pricing_quote_v2 import custom_srt

    monkeypatch.setattr(
        custom_srt,
        "_call_cloud_drive_upstream",
        lambda *_a, **_k: {"data": {"download_url": "https://example.invalid/srt"}},
    )
    monkeypatch.setattr(custom_srt, "_download_bytes", lambda _url: srt_bytes)

    with engine.connect() as conn:
        quote = generate_quote(
            conn,
            request_body={
                "custom_template_id": "ct_user_001",
                "combo_key": "original_narration_flash",
                "custom_srt_file_id": "cf_srt_001",
            },
            user_id=1,
            now=NOW,
        )
        conn.commit()
    import hashlib

    expected_hash = hashlib.sha256(srt_bytes).hexdigest()
    # 50 lines → 50/25 = 2.0 minutes (under 3, no after-3 surcharge).
    # original_narration_flash skips popular_learning , so the
    # breakdown is 3 rows under the regression coverage 3-min tier formula:
    #   original_narration_flash 2×2 + clip_data 10×2 + video_composing 7×2
    # = 4 + 20 + 14 = 38
    assert quote.price_source == "system_calculated_price"
    assert quote.final_charge_price == 38
    assert quote.pricing_minutes == Decimal(2)
    assert quote.valid_line_count == 50
    assert quote.srt_file_hash == expected_hash
    assert quote.custom_template_id == "ct_user_001"
    keys = [item["subflow_key"] for item in quote.breakdown]
    assert keys == [
        "original_narration_flash",
        "clip_data",
        "video_composing",
    ]
    subtotals = {item["subflow_key"]: item["subtotal"] for item in quote.breakdown}
    assert subtotals == {
        "original_narration_flash": 4,
        "clip_data": 20,
        "video_composing": 14,
    }
    assert sum(subtotals.values()) == quote.final_charge_price
    # regression coverage — each subflow row now carries the explicit tiered rates so
    # the breakdown is replay-auditable.
    for row in quote.breakdown:
        assert "rate_first_3_minutes" in row
        assert "rate_after_3_minutes" in row


def _stub_srt_download(monkeypatch, srt_bytes: bytes) -> None:
    """Patch the cloud-drive upstream + bytes downloader used by the
    custom-SRT flow. Shared across the custom-template breakdown
    tests below."""
    from pricing_quote_v2 import custom_srt

    monkeypatch.setattr(
        custom_srt,
        "_call_cloud_drive_upstream",
        lambda *_a, **_k: {"data": {"download_url": "https://example.invalid/srt"}},
    )
    monkeypatch.setattr(custom_srt, "_download_bytes", lambda _url: srt_bytes)


def test_generate_quote_custom_template_remix_flash_75_lines(engine, monkeypatch):
    """`combo_key=original_remix_flash`, 75 valid SRT lines → exactly 3
    minutes (boundary case under regression coverage's 3-min tier formula). The
    `min(m,3) × first_rate + max(m-3,0) × after_rate` math degenerates
    to flat first-rate × minutes at the boundary:
        remix-flash 5×3 + clip_data 10×3 + video_composing 7×3
        = 15 + 30 + 21 = 66 web_point.
    """
    _seed_cloud_file(engine, file_id="cf_srt_remix", user_id=1, file_size=600)
    fake_lines = [
        f"{i}\n00:00:{i:02d},000 --> 00:00:{i+1:02d},000\nLine {i}\n"
        for i in range(1, 76)
    ]
    _stub_srt_download(monkeypatch, "\n".join(fake_lines).encode("utf-8"))

    with engine.connect() as conn:
        quote = generate_quote(
            conn,
            request_body={
                "custom_template_id": "ct_user_002",
                "combo_key": "original_remix_flash",
                "custom_srt_file_id": "cf_srt_remix",
            },
            user_id=1,
            now=NOW,
        )
        conn.commit()
    assert quote.valid_line_count == 75
    assert quote.pricing_minutes == Decimal(3)
    assert quote.final_charge_price == 66
    assert [item["subflow_key"] for item in quote.breakdown] == [
        "original_remix_flash",
        "clip_data",
        "video_composing",
    ]
    assert [item["subtotal"] for item in quote.breakdown] == [15, 30, 21]


def test_generate_quote_custom_template_secondary_creation_accepts_either_pro_flag(
    engine, monkeypatch
):
    """`secondary_creation` is axis-optional (no Flash/Pro variant), so
    it must validate regardless of the `pro_upgrade` flag — same
    treatment as `flash_pro_axis='optional'` in the manual catalog."""
    _seed_cloud_file(engine, file_id="cf_srt_secondary", user_id=1, file_size=200)
    fake_lines = [
        f"{i}\n00:00:{i:02d},000 --> 00:00:{i+1:02d},000\nLine {i}\n"
        for i in range(1, 26)
    ]
    _stub_srt_download(monkeypatch, "\n".join(fake_lines).encode("utf-8"))

    # 25 lines → 1.0 minute (under 3, no after-3 surcharge).
    # Custom-template 二创 keeps popular_learning (image's
    # "二创文案，自定义模板 45/38"):
    #   popular_learning 13×1 + secondary_creation 15×1
    #   + clip_data 10×1 + video_composing 7×1 = 45 web_point.
    for pro_upgrade in (False, True):
        with engine.connect() as conn:
            quote = generate_quote(
                conn,
                request_body={
                    "custom_template_id": "ct_user_003",
                    "combo_key": "secondary_creation",
                    "custom_srt_file_id": "cf_srt_secondary",
                    "pro_upgrade": pro_upgrade,
                },
                user_id=1,
                now=NOW,
            )
            conn.commit()
        assert quote.combo_key == "secondary_creation"
        assert quote.final_charge_price == 45
        wenan_row = quote.breakdown[1]
        assert wenan_row["subflow_key"] == "secondary_creation"
        assert wenan_row["unit_price"] == 15


def test_generate_quote_custom_template_unknown_combo_key_fails_closed(
    engine, monkeypatch
):
    """No catalog fallback for custom templates: an unknown combo_key
    must raise `QuoteComboKeyInvalid` instead of silently producing a
    1 web_point/minute quote (the pre-redesign bug class)."""
    _seed_cloud_file(engine, file_id="cf_srt_unknown", user_id=1, file_size=200)
    fake_lines = [
        f"{i}\n00:00:{i:02d},000 --> 00:00:{i+1:02d},000\nLine {i}\n"
        for i in range(1, 26)
    ]
    _stub_srt_download(monkeypatch, "\n".join(fake_lines).encode("utf-8"))

    with engine.connect() as conn:
        with pytest.raises(QuoteComboKeyInvalid):
            generate_quote(
                conn,
                request_body={
                    "custom_template_id": "ct_user_004",
                    "combo_key": "derivative",  # not in WENAN_COMBO_UNIT_PRICES
                    "custom_srt_file_id": "cf_srt_unknown",
                },
                user_id=1,
                now=NOW,
            )


def test_generate_quote_combo_key_invalid_for_pro_flag(engine):
    with engine.connect() as conn:
        with pytest.raises(QuoteComboKeyInvalid):
            generate_quote(
                conn,
                request_body={
                    "template_id": TEMPLATE_ID,
                    "combo_key": "original_narration_flash",
                    "pro_upgrade": True,
                },
                user_id=1,
                now=NOW,
            )


def test_generate_quote_template_xor_violation(engine):
    with engine.connect() as conn:
        with pytest.raises(QuoteComboKeyInvalid):
            generate_quote(
                conn,
                request_body={
                    "template_id": TEMPLATE_ID,
                    "custom_template_id": "ct_x",
                    "combo_key": "original_narration_flash",
                },
                user_id=1,
                now=NOW,
            )


def test_generate_quote_custom_srt_file_id_missing(engine):
    from pricing_quote_v2 import CustomSrtFileIdMissing

    with engine.connect() as conn:
        with pytest.raises(CustomSrtFileIdMissing):
            generate_quote(
                conn,
                request_body={
                    "custom_template_id": "ct_user_001",
                    "combo_key": "original_narration_flash",
                    # No custom_srt_file_id — old fields no longer accepted.
                },
                user_id=1,
                now=NOW,
            )


def test_generate_quote_custom_srt_file_not_found(engine, monkeypatch):
    # No matching row in user_cloud_files → CustomSrtFileNotFound (404).
    # No upstream call should happen.
    from pricing_quote_v2 import CustomSrtFileNotFound
    from pricing_quote_v2 import custom_srt

    upstream_calls: list = []
    monkeypatch.setattr(
        custom_srt,
        "_call_cloud_drive_upstream",
        lambda *a, **k: upstream_calls.append((a, k)) or {"data": {"url": "x"}},
    )
    monkeypatch.setattr(custom_srt, "_download_bytes", lambda _url: b"")

    with engine.connect() as conn:
        with pytest.raises(CustomSrtFileNotFound):
            generate_quote(
                conn,
                request_body={
                    "custom_template_id": "ct_user_001",
                    "combo_key": "original_narration_flash",
                    "custom_srt_file_id": "cf_does_not_exist",
                },
                user_id=1,
                now=NOW,
            )
    assert upstream_calls == [], "upstream must not be called when ownership check fails"


def test_generate_quote_custom_srt_wrong_owner_returns_not_found(engine, monkeypatch):
    # File owned by a different user_id — same envelope as "doesn't
    # exist" to avoid existence-leak across tenants.
    from pricing_quote_v2 import CustomSrtFileNotFound
    from pricing_quote_v2 import custom_srt

    # Seed user 2 so the foreign key on user_cloud_files.user_id resolves.
    with engine.begin() as conn:
        conn.execute(
            text("INSERT INTO users (app_key, id, balance_points) VALUES ('grid_other', 2, 1000)")
        )
    _seed_cloud_file(
        engine,
        file_id="cf_other_user",
        user_id=2,
        app_key="grid_other",
        reservation_id="res_other",
    )

    monkeypatch.setattr(
        custom_srt,
        "_call_cloud_drive_upstream",
        lambda *_a, **_k: {"data": {"url": "x"}},
    )
    monkeypatch.setattr(custom_srt, "_download_bytes", lambda _url: b"")

    with engine.connect() as conn:
        with pytest.raises(CustomSrtFileNotFound):
            generate_quote(
                conn,
                request_body={
                    "custom_template_id": "ct_user_001",
                    "combo_key": "original_narration_flash",
                    "custom_srt_file_id": "cf_other_user",
                },
                # Requesting user is user 1 — does not own cf_other_user.
                user_id=1,
                now=NOW,
            )


def test_generate_quote_custom_srt_too_large_via_metadata(engine, monkeypatch):
    # If user_cloud_files.file_size already declares > MAX_SRT_BYTES we
    # short-circuit before the upstream call.
    from pricing_quote_v2 import CustomSrtFileTooLarge
    from pricing_quote_v2 import custom_srt

    _seed_cloud_file(
        engine,
        file_id="cf_huge",
        user_id=1,
        file_size=custom_srt.MAX_SRT_BYTES + 1,
    )
    upstream_calls: list = []
    monkeypatch.setattr(
        custom_srt,
        "_call_cloud_drive_upstream",
        lambda *a, **k: upstream_calls.append((a, k)) or {"data": {"url": "x"}},
    )
    monkeypatch.setattr(custom_srt, "_download_bytes", lambda _url: b"")

    with engine.connect() as conn:
        with pytest.raises(CustomSrtFileTooLarge):
            generate_quote(
                conn,
                request_body={
                    "custom_template_id": "ct_user_001",
                    "combo_key": "original_narration_flash",
                    "custom_srt_file_id": "cf_huge",
                },
                user_id=1,
                now=NOW,
            )
    assert upstream_calls == [], "upstream must not be called when metadata size already exceeds cap"


def test_generate_quote_custom_srt_download_failure(engine, monkeypatch):
    # Cloud-drive upstream presigned-URL call fails → CustomSrtDownloadFailed.
    from cloud_drive.upstream import UpstreamCloudDriveError
    from pricing_quote_v2 import CustomSrtDownloadFailed
    from pricing_quote_v2 import custom_srt

    _seed_cloud_file(engine, file_id="cf_unreachable", user_id=1, file_size=200)

    def _boom(*_a, **_k):
        raise UpstreamCloudDriveError(503, "UPSTREAM_TIMEOUT", "boom", retryable=True)

    monkeypatch.setattr(custom_srt, "_call_cloud_drive_upstream", _boom)
    monkeypatch.setattr(custom_srt, "_download_bytes", lambda _url: b"")

    with engine.connect() as conn:
        with pytest.raises(CustomSrtDownloadFailed):
            generate_quote(
                conn,
                request_body={
                    "custom_template_id": "ct_user_001",
                    "combo_key": "original_narration_flash",
                    "custom_srt_file_id": "cf_unreachable",
                },
                user_id=1,
                now=NOW,
            )


def test_generate_quote_custom_srt_empty(engine, monkeypatch):
    # SRT has structure but no actual subtitle text lines → CustomSrtEmpty.
    from pricing_quote_v2 import CustomSrtEmpty
    from pricing_quote_v2 import custom_srt

    _seed_cloud_file(engine, file_id="cf_empty_srt", user_id=1, file_size=50)
    only_structural = b"1\n00:00:00,000 --> 00:00:01,000\n\n"

    monkeypatch.setattr(
        custom_srt,
        "_call_cloud_drive_upstream",
        lambda *_a, **_k: {"data": {"download_url": "https://example.invalid/srt"}},
    )
    monkeypatch.setattr(custom_srt, "_download_bytes", lambda _url: only_structural)

    with engine.connect() as conn:
        with pytest.raises(CustomSrtEmpty):
            generate_quote(
                conn,
                request_body={
                    "custom_template_id": "ct_user_001",
                    "combo_key": "original_narration_flash",
                    "custom_srt_file_id": "cf_empty_srt",
                },
                user_id=1,
                now=NOW,
            )


def test_commit_rejects_swapped_custom_srt_file_id(engine, monkeypatch):
    # review attack closure: caller quotes with cheap file_id A, then
    # tries to commit with different (presumably larger) file_id B.
    # §6.2 bound check must reject before snapshot writes.
    _seed_cloud_file(engine, file_id="cf_cheap_A", user_id=1, file_size=200)
    _seed_cloud_file(
        engine,
        file_id="cf_expensive_B",
        user_id=1,
        file_size=200,
        reservation_id="res_cf_expensive_B",
    )

    from pricing_quote_v2 import custom_srt

    # Both files parse to 5 lines for this test's purposes; the bound
    # check is on file_id identity, not parse output, so the count is
    # immaterial.
    fake_lines = [
        f"{i}\n00:00:{i:02d},000 --> 00:00:{i+1:02d},000\nLine {i}\n"
        for i in range(1, 6)
    ]
    srt_bytes = "\n".join(fake_lines).encode("utf-8")
    monkeypatch.setattr(
        custom_srt,
        "_call_cloud_drive_upstream",
        lambda *_a, **_k: {"data": {"download_url": "https://example.invalid/srt"}},
    )
    monkeypatch.setattr(custom_srt, "_download_bytes", lambda _url: srt_bytes)

    with engine.connect() as conn:
        quote = generate_quote(
            conn,
            request_body={
                "custom_template_id": "ct_user_001",
                "combo_key": "original_narration_flash",
                "custom_srt_file_id": "cf_cheap_A",
            },
            user_id=1,
            now=NOW,
        )
        conn.commit()
    assert quote.custom_srt_file_id == "cf_cheap_A"

    _insert_master_task(engine, narrator_task_id="mt_swap", user_id=1)
    with engine.connect() as conn:
        with pytest.raises(QuoteParametersChanged) as excinfo:
            commit_master_task_snapshot(
                conn,
                quote_id=quote.quote_id,
                master_task_id="mt_swap",
                request_body={
                    "custom_template_id": "ct_user_001",
                    "combo_key": "original_narration_flash",
                    # Attacker swaps to a different file_id.
                    "custom_srt_file_id": "cf_expensive_B",
                },
                user_id=1,
                now=NOW + timedelta(seconds=5),
            )
    details = excinfo.value.details
    assert details["expected"]["custom_srt_file_id"] == "cf_cheap_A"
    assert details["submitted"]["custom_srt_file_id"] == "cf_expensive_B"


def test_generate_quote_custom_srt_mid_stream_too_large(engine, monkeypatch):
    # Metadata says small, but the actual downloaded bytes exceed cap
    # (e.g. malicious / misreported upload). Defense in depth.
    from pricing_quote_v2 import CustomSrtFileTooLarge
    from pricing_quote_v2 import custom_srt

    _seed_cloud_file(engine, file_id="cf_lying", user_id=1, file_size=100)

    monkeypatch.setattr(
        custom_srt,
        "_call_cloud_drive_upstream",
        lambda *_a, **_k: {"data": {"download_url": "https://example.invalid/srt"}},
    )
    monkeypatch.setattr(
        custom_srt,
        "_download_bytes",
        lambda _url: b"x" * (custom_srt.MAX_SRT_BYTES + 100),
    )

    with engine.connect() as conn:
        with pytest.raises(CustomSrtFileTooLarge):
            generate_quote(
                conn,
                request_body={
                    "custom_template_id": "ct_user_001",
                    "combo_key": "original_narration_flash",
                    "custom_srt_file_id": "cf_lying",
                },
                user_id=1,
                now=NOW,
            )


def test_generate_quote_wallet_insufficient(low_balance_engine):
    with low_balance_engine.connect() as conn:
        with pytest.raises(WalletInsufficientBalance) as excinfo:
            generate_quote(
                conn,
                request_body={
                    "template_id": TEMPLATE_ID,
                    "combo_key": "original_narration_flash",
                },
                user_id=1,
                now=NOW,
            )
    details = excinfo.value.details
    assert details["required"] == 19
    assert details["available"] == 10
    assert details["shortfall"] == 9


# ── unit: commit_master_task_snapshot ───────────────────────────────────────


def _insert_master_task(engine, *, narrator_task_id: str, user_id: int = 1):
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO narrator_tasks ("
                "narrator_task_id, user_id, app_key, status, data, "
                "created_at, updated_at) VALUES ("
                ":id, :uid, :ak, 'pending', '{}', :now, :now)"
            ),
            {
                "id": narrator_task_id,
                "uid": user_id,
                "ak": WEB_APP_KEY,
                "now": NOW,
            },
        )


def _read_master_task_snapshot_id(engine, narrator_task_id: str):
    with engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT snapshot_id FROM narrator_tasks "
                "WHERE narrator_task_id = :id"
            ),
            {"id": narrator_task_id},
        ).first()
    return row[0] if row else None


def _insert_legacy_custom_quote(engine, *, quote_id: str = "Q-legacy-custom"):
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO pricing_quotes_v2 ("
                "quote_id, pricing_rule_version, price_source, template_id, "
                "custom_template_id, combo_key, pro_upgrade, starting_price, "
                "final_charge_price, flash_total, pro_total, pro_upgrade_delta, "
                "pricing_minutes, valid_line_count, srt_file_hash, custom_srt_file_id, "
                "system_reference_price, breakdown, currency_unit, expires_at, "
                "committed_at, web_user_id, created_at) VALUES ("
                ":quote_id, 'catalog-v2', 'system_calculated_price', NULL, "
                "'ct_legacy', 'derivative', 0, NULL, "
                "1, 1, 1, 0, "
                "1, 1, 'legacy-client-hash', NULL, "
                "1, '[]', 'web_point', :expires_at, "
                "NULL, 1, :created_at)"
            ),
            {
                "quote_id": quote_id,
                "expires_at": NOW + timedelta(minutes=5),
                "created_at": NOW,
            },
        )
    return quote_id


def test_commit_persists_snapshot_and_links_master_task(engine):
    _insert_master_task(engine, narrator_task_id="mt_001")
    with engine.connect() as conn:
        quote = generate_quote(
            conn,
            request_body={
                "template_id": TEMPLATE_ID,
                "combo_key": "original_narration_flash",
            },
            user_id=1,
            now=NOW,
        )
        snapshot_id = commit_master_task_snapshot(
            conn,
            quote_id=quote.quote_id,
            master_task_id="mt_001",
            request_body={
                "template_id": TEMPLATE_ID,
                "combo_key": "original_narration_flash",
            },
            user_id=1,
            now=NOW + timedelta(seconds=10),
        )
        conn.commit()
    assert snapshot_id.startswith("S-")
    assert _read_master_task_snapshot_id(engine, "mt_001") == snapshot_id


def test_commit_rejects_forbidden_body_fields(engine):
    _insert_master_task(engine, narrator_task_id="mt_002")
    with engine.connect() as conn:
        quote = generate_quote(
            conn,
            request_body={
                "template_id": TEMPLATE_ID,
                "combo_key": "original_narration_flash",
            },
            user_id=1,
            now=NOW,
        )
        with pytest.raises(QuoteBodyPriceForbidden):
            commit_master_task_snapshot(
                conn,
                quote_id=quote.quote_id,
                master_task_id="mt_002",
                request_body={
                    "template_id": TEMPLATE_ID,
                    "combo_key": "original_narration_flash",
                    "final_charge_price": 1,
                },
                user_id=1,
                now=NOW + timedelta(seconds=5),
            )


def test_commit_quote_not_found(engine):
    with engine.connect() as conn:
        with pytest.raises(QuoteNotFound):
            commit_master_task_snapshot(
                conn,
                quote_id="Q-2026-05-30-deadbeef00",
                master_task_id="mt_xxx",
                request_body={"combo_key": "original_narration_flash"},
                user_id=1,
                now=NOW,
            )


def test_commit_parameters_changed(engine):
    _insert_master_task(engine, narrator_task_id="mt_003")
    with engine.connect() as conn:
        quote = generate_quote(
            conn,
            request_body={
                "template_id": TEMPLATE_ID,
                "combo_key": "original_narration_flash",
            },
            user_id=1,
            now=NOW,
        )
        with pytest.raises(QuoteParametersChanged):
            commit_master_task_snapshot(
                conn,
                quote_id=quote.quote_id,
                master_task_id="mt_003",
                request_body={
                    "template_id": TEMPLATE_ID,
                    # Different combo_key from the quote → parameters changed
                    "combo_key": "original_narration_pro",
                },
                user_id=1,
                now=NOW + timedelta(seconds=5),
            )


def test_commit_rejects_legacy_custom_quote_without_bound_srt_file(engine):
    _insert_master_task(engine, narrator_task_id="mt_legacy_custom")
    quote_id = _insert_legacy_custom_quote(engine)

    with engine.connect() as conn:
        with pytest.raises(QuoteParametersChanged) as excinfo:
            commit_master_task_snapshot(
                conn,
                quote_id=quote_id,
                master_task_id="mt_legacy_custom",
                request_body={
                    "custom_template_id": "ct_legacy",
                    "combo_key": "derivative",
                    # No custom_srt_file_id: this used to compare equal to
                    # the migrated quote's NULL and allow a stale low-price
                    # custom quote to commit.
                },
                user_id=1,
                now=NOW + timedelta(seconds=5),
            )

    assert excinfo.value.code == "QUOTE_PARAMETERS_CHANGED"
    assert excinfo.value.details["custom_template_id"] == "ct_legacy"
    assert excinfo.value.details["custom_srt_file_id"] is None
    assert _read_master_task_snapshot_id(engine, "mt_legacy_custom") is None


def test_commit_quote_expired(engine):
    _insert_master_task(engine, narrator_task_id="mt_004")
    with engine.connect() as conn:
        quote = generate_quote(
            conn,
            request_body={
                "template_id": TEMPLATE_ID,
                "combo_key": "original_narration_flash",
            },
            user_id=1,
            now=NOW,
        )
        # Default TTL is 5min; submit at +6 min → expired
        with pytest.raises(QuoteExpired):
            commit_master_task_snapshot(
                conn,
                quote_id=quote.quote_id,
                master_task_id="mt_004",
                request_body={
                    "template_id": TEMPLATE_ID,
                    "combo_key": "original_narration_flash",
                },
                user_id=1,
                now=NOW + timedelta(seconds=6 * 60),
            )


def test_commit_silent_requote_within_ttl_same_price_passes(engine):
    """Inside TTL but past 60s, a silent re-quote runs. If the price
    hasn't drifted, the commit succeeds (no exception)."""
    _insert_master_task(engine, narrator_task_id="mt_005")
    with engine.connect() as conn:
        quote = generate_quote(
            conn,
            request_body={
                "template_id": TEMPLATE_ID,
                "combo_key": "original_narration_flash",
            },
            user_id=1,
            now=NOW,
        )
        snapshot_id = commit_master_task_snapshot(
            conn,
            quote_id=quote.quote_id,
            master_task_id="mt_005",
            request_body={
                "template_id": TEMPLATE_ID,
                "combo_key": "original_narration_flash",
            },
            user_id=1,
            now=NOW + timedelta(seconds=90),
        )
        conn.commit()
    assert snapshot_id


def test_commit_price_drifted_after_catalog_update(engine):
    """Catalog input changes between quote and commit (past 60s) → §6.1
    silent re-quote raises QuotePriceDrifted. Under subsequent update the
    pricing driver for the existing-template branch is
    `pricing_template_v2.video_duration_seconds`; simulate operators bumping
    it (e.g. re-running the refresh script with a new CSV)."""
    _insert_master_task(engine, narrator_task_id="mt_006")
    with engine.connect() as conn:
        quote = generate_quote(
            conn,
            request_body={
                "template_id": TEMPLATE_ID,
                "combo_key": "original_narration_flash",
            },
            user_id=1,
            now=NOW,
        )
        conn.commit()

    # Bump the duration from 60s (price 19) to 240s (4 min, price 69):
    #   under-3min: 2×3 + 10×3 + 7×3 = 57
    #   after-3min: 2×1 + 6×1 + 4×1 = 12
    # → final 69, clearly drifted from 19.
    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE pricing_template_v2 SET video_duration_seconds = 240 "
                "WHERE template_id = :tid"
            ),
            {"tid": TEMPLATE_ID},
        )

    with engine.connect() as conn:
        with pytest.raises(QuotePriceDrifted):
            commit_master_task_snapshot(
                conn,
                quote_id=quote.quote_id,
                master_task_id="mt_006",
                request_body={
                    "template_id": TEMPLATE_ID,
                    "combo_key": "original_narration_flash",
                },
                user_id=1,
                now=NOW + timedelta(seconds=90),
            )


# ── HTTP: POST /pricing/quote ───────────────────────────────────────────────


def test_pricing_quote_route_returns_envelope(client):
    res = client.post(
        "/pricing/quote",
        headers={**AUTH_HEADERS, "Content-Type": "application/json"},
        json={
            "template_id": TEMPLATE_ID,
            "combo_key": "original_narration_flash",
        },
    )
    assert res.status_code == 200, res.get_json()
    body = res.get_json()
    assert body["success"] is True
    data = body["data"]
    assert data["quote_id"].startswith("Q-")
    # Default 60s → 1 min, Flash recipe 1 × (2+10+7) = 19.
    assert data["final_charge_price"] == 19
    assert data["flash_total"] == 19
    assert data["pro_total"] == 23
    assert data["pro_upgrade_delta"] == 4
    assert data["price_source"] == "manual_catalog_price"
    assert data["pricing_rule_version"] == "v2.0"
    assert "expires_at" in data
    assert data["currency_unit"] == "web_point"


def test_pricing_quote_route_requires_bearer(client):
    res = client.post(
        "/pricing/quote",
        headers={"X-Web-App-Key": WEB_APP_KEY, "Content-Type": "application/json"},
        json={"template_id": TEMPLATE_ID, "combo_key": "original_narration_flash"},
    )
    assert res.status_code == 401


def test_pricing_quote_route_returns_402_on_low_balance(low_balance_client):
    res = low_balance_client.post(
        "/pricing/quote",
        headers={**AUTH_HEADERS, "Content-Type": "application/json"},
        json={"template_id": TEMPLATE_ID, "combo_key": "original_narration_flash"},
    )
    assert res.status_code == 402
    body = res.get_json()
    assert body["success"] is False
    assert body["error"]["code"] == "WALLET_INSUFFICIENT_BALANCE"
    assert body["error"]["details"]["required"] == 19
    assert body["error"]["details"]["available"] == 10


def test_pricing_quote_route_returns_404_on_missing_catalog(client):
    res = client.post(
        "/pricing/quote",
        headers={**AUTH_HEADERS, "Content-Type": "application/json"},
        json={
            "template_id": "tpl_does_not_exist",
            "combo_key": "original_narration_flash",
        },
    )
    assert res.status_code == 404
    assert res.get_json()["error"]["code"] == "CATALOG_TIER_MISSING"


def test_pricing_quote_route_returns_422_on_combo_key_invalid(client):
    res = client.post(
        "/pricing/quote",
        headers={**AUTH_HEADERS, "Content-Type": "application/json"},
        json={
            "template_id": TEMPLATE_ID,
            "combo_key": "original_narration_flash",
            "pro_upgrade": True,
        },
    )
    assert res.status_code == 422
    assert res.get_json()["error"]["code"] == "QUOTE_COMBO_KEY_INVALID"


def test_pricing_quote_route_rejects_non_json(client):
    res = client.post(
        "/pricing/quote",
        headers={**AUTH_HEADERS, "Content-Type": "application/json"},
        data="not-json",
    )
    assert res.status_code == 400


# ── HTTP: POST /narrator/tasks with quote_id ────────────────────────────────


def test_narrator_tasks_create_attaches_snapshot_when_quote_id_supplied(client, engine):
    # Step 1 — quote
    quote_res = client.post(
        "/pricing/quote",
        headers={**AUTH_HEADERS, "Content-Type": "application/json"},
        json={
            "template_id": TEMPLATE_ID,
            "combo_key": "original_narration_flash",
        },
    )
    assert quote_res.status_code == 200, quote_res.get_json()
    quote_id = quote_res.get_json()["data"]["quote_id"]

    # Step 2 — create master task carrying the quote_id
    task_res = client.post(
        "/narrator/tasks",
        headers={"X-Web-App-Key": WEB_APP_KEY, "Content-Type": "application/json"},
        json={
            "narrator_type": "playlet",
            "template_id": TEMPLATE_ID,
            "combo_key": "original_narration_flash",
            "quote_id": quote_id,
        },
    )
    assert task_res.status_code == 200, task_res.get_json()
    data = task_res.get_json()["data"]
    assert data["snapshot_id"]
    assert data["snapshot_id"].startswith("S-")
    # Confirm the DB-side link survived the round-trip.
    assert (
        _read_master_task_snapshot_id(engine, data["narrator_task_id"])
        == data["snapshot_id"]
    )


def test_narrator_tasks_create_rejects_quote_with_forbidden_body_price(client):
    quote_res = client.post(
        "/pricing/quote",
        headers={**AUTH_HEADERS, "Content-Type": "application/json"},
        json={
            "template_id": TEMPLATE_ID,
            "combo_key": "original_narration_flash",
        },
    )
    quote_id = quote_res.get_json()["data"]["quote_id"]

    task_res = client.post(
        "/narrator/tasks",
        headers={"X-Web-App-Key": WEB_APP_KEY, "Content-Type": "application/json"},
        json={
            "narrator_type": "playlet",
            "template_id": TEMPLATE_ID,
            "combo_key": "original_narration_flash",
            "quote_id": quote_id,
            # §6 forbidden — backend trusts only the quote.
            "final_charge_price": 1,
        },
    )
    assert task_res.status_code == 422
    assert task_res.get_json()["error"]["code"] == "QUOTE_BODY_PRICE_FORBIDDEN"


# ── review fixes: tenant isolation, idempotency, type validation ──────────


def test_commit_rejects_cross_user_quote(engine):
    """Tenant isolation (security regression): a user must not be able to
    commit another user's quote. We surface a non-leaking 404
    (`QuoteNotFound`) so the existence of cross-tenant quote_ids isn't
    disclosed."""
    # Add a second user so we have a different user_id to commit as.
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO users (app_key, id, balance_points) "
                "VALUES ('grid_OtherUserXXXXXXXXXX', 2, 1000)"
            )
        )
    _insert_master_task(engine, narrator_task_id="mt_attacker", user_id=2)
    with engine.connect() as conn:
        # User 1 generates a quote.
        quote = generate_quote(
            conn,
            request_body={
                "template_id": TEMPLATE_ID,
                "combo_key": "original_narration_flash",
            },
            user_id=1,
            now=NOW,
        )
        # User 2 tries to commit user 1's quote → should look indistinguishable
        # from a missing quote.
        with pytest.raises(QuoteNotFound):
            commit_master_task_snapshot(
                conn,
                quote_id=quote.quote_id,
                master_task_id="mt_attacker",
                request_body={
                    "template_id": TEMPLATE_ID,
                    "combo_key": "original_narration_flash",
                },
                user_id=2,
                now=NOW + timedelta(seconds=5),
            )


def test_commit_is_idempotent_for_same_master_task(engine):
    """A retry of the same `(quote_id, master_task_id)` returns the
    existing snapshot_id instead of creating a duplicate."""
    _insert_master_task(engine, narrator_task_id="mt_idem")
    with engine.connect() as conn:
        quote = generate_quote(
            conn,
            request_body={
                "template_id": TEMPLATE_ID,
                "combo_key": "original_narration_flash",
            },
            user_id=1,
            now=NOW,
        )
        first = commit_master_task_snapshot(
            conn,
            quote_id=quote.quote_id,
            master_task_id="mt_idem",
            request_body={
                "template_id": TEMPLATE_ID,
                "combo_key": "original_narration_flash",
            },
            user_id=1,
            now=NOW + timedelta(seconds=5),
        )
        # Retry with the exact same parameters.
        second = commit_master_task_snapshot(
            conn,
            quote_id=quote.quote_id,
            master_task_id="mt_idem",
            request_body={
                "template_id": TEMPLATE_ID,
                "combo_key": "original_narration_flash",
            },
            user_id=1,
            now=NOW + timedelta(seconds=10),
        )
        conn.commit()
    assert first == second
    # And the DB only has one snapshot row for this quote.
    with engine.connect() as conn:
        count = conn.execute(
            text("SELECT COUNT(*) FROM pricing_snapshots_v2 WHERE quote_id = :q"),
            {"q": quote.quote_id},
        ).scalar()
    assert count == 1


def _read_balance(engine, *, user_id: int = 1) -> int:
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT balance_points FROM users WHERE id = :uid"),
            {"uid": user_id},
        ).first()
    return int(row[0]) if row and row[0] is not None else 0


def test_commit_deducts_balance_points(engine):
    """Successful commit must debit users.balance_points by
    final_charge_price in the same transaction as the snapshot
    insert."""
    starting = _read_balance(engine)
    _insert_master_task(engine, narrator_task_id="mt_debit")
    with engine.connect() as conn:
        quote = generate_quote(
            conn,
            request_body={
                "template_id": TEMPLATE_ID,
                "combo_key": "original_narration_flash",
            },
            user_id=1,
            now=NOW,
        )
        commit_master_task_snapshot(
            conn,
            quote_id=quote.quote_id,
            master_task_id="mt_debit",
            request_body={
                "template_id": TEMPLATE_ID,
                "combo_key": "original_narration_flash",
            },
            user_id=1,
            now=NOW + timedelta(seconds=5),
        )
        conn.commit()
    assert _read_balance(engine) == starting - quote.final_charge_price


def test_commit_raises_402_when_balance_falls_between_quote_and_commit(engine):
    """Quote-time preflight passes, but balance drops below
    final_charge_price before commit (e.g. a parallel order
    committed first). Commit must raise WalletInsufficientBalance
    AND roll back the snapshot insert so the quote stays
    uncommitted and the balance is unchanged."""
    _insert_master_task(engine, narrator_task_id="mt_drain")
    with engine.connect() as conn:
        quote = generate_quote(
            conn,
            request_body={
                "template_id": TEMPLATE_ID,
                "combo_key": "original_narration_flash",
            },
            user_id=1,
            now=NOW,
        )
        conn.commit()

    # Drain the balance below the quote price (19) between quote and
    # commit. 10 < 19 so the wallet check raises.
    with engine.begin() as conn:
        conn.execute(text("UPDATE users SET balance_points = 10 WHERE id = 1"))

    with engine.connect() as conn:
        with pytest.raises(WalletInsufficientBalance) as excinfo:
            commit_master_task_snapshot(
                conn,
                quote_id=quote.quote_id,
                master_task_id="mt_drain",
                request_body={
                    "template_id": TEMPLATE_ID,
                    "combo_key": "original_narration_flash",
                },
                user_id=1,
                now=NOW + timedelta(seconds=5),
            )
    assert excinfo.value.details["required"] == quote.final_charge_price
    assert excinfo.value.details["available"] == 10
    assert excinfo.value.details["shortfall"] == quote.final_charge_price - 10

    # Balance unchanged (debit rolled back together with snapshot).
    assert _read_balance(engine) == 10
    # Snapshot NOT persisted — quote can be re-tried after topup.
    with engine.connect() as conn:
        count = conn.execute(
            text(
                "SELECT COUNT(*) FROM pricing_snapshots_v2 WHERE quote_id = :q"
            ),
            {"q": quote.quote_id},
        ).scalar()
    assert count == 0
    # And the master task is NOT linked to a snapshot.
    assert _read_master_task_snapshot_id(engine, "mt_drain") is None


def test_commit_idempotent_retry_does_not_double_charge(engine):
    """A retry with the same (quote_id, master_task_id) must return
    the existing snapshot AND must NOT debit the wallet a second
    time."""
    starting = _read_balance(engine)
    _insert_master_task(engine, narrator_task_id="mt_idem_charge")
    with engine.connect() as conn:
        quote = generate_quote(
            conn,
            request_body={
                "template_id": TEMPLATE_ID,
                "combo_key": "original_narration_flash",
            },
            user_id=1,
            now=NOW,
        )
        commit_master_task_snapshot(
            conn,
            quote_id=quote.quote_id,
            master_task_id="mt_idem_charge",
            request_body={
                "template_id": TEMPLATE_ID,
                "combo_key": "original_narration_flash",
            },
            user_id=1,
            now=NOW + timedelta(seconds=5),
        )
        commit_master_task_snapshot(
            conn,
            quote_id=quote.quote_id,
            master_task_id="mt_idem_charge",
            request_body={
                "template_id": TEMPLATE_ID,
                "combo_key": "original_narration_flash",
            },
            user_id=1,
            now=NOW + timedelta(seconds=10),
        )
        conn.commit()
    # Charged once, not twice.
    assert _read_balance(engine) == starting - quote.final_charge_price


def test_commit_rejects_quote_already_bound_to_different_task(engine):
    """Same quote cannot be committed to two different master tasks."""
    from pricing_quote_v2 import QuoteAlreadyCommitted

    _insert_master_task(engine, narrator_task_id="mt_first")
    _insert_master_task(engine, narrator_task_id="mt_second")
    with engine.connect() as conn:
        quote = generate_quote(
            conn,
            request_body={
                "template_id": TEMPLATE_ID,
                "combo_key": "original_narration_flash",
            },
            user_id=1,
            now=NOW,
        )
        commit_master_task_snapshot(
            conn,
            quote_id=quote.quote_id,
            master_task_id="mt_first",
            request_body={
                "template_id": TEMPLATE_ID,
                "combo_key": "original_narration_flash",
            },
            user_id=1,
            now=NOW + timedelta(seconds=5),
        )
        with pytest.raises(QuoteAlreadyCommitted):
            commit_master_task_snapshot(
                conn,
                quote_id=quote.quote_id,
                master_task_id="mt_second",
                request_body={
                    "template_id": TEMPLATE_ID,
                    "combo_key": "original_narration_flash",
                },
                user_id=1,
                now=NOW + timedelta(seconds=10),
            )


def test_narrator_tasks_rejects_non_string_quote_id(client):
    """regression coverage: non-string quote_id must not silently fall back to
    the v1 (snapshot-less) path."""
    res = client.post(
        "/narrator/tasks",
        headers={"X-Web-App-Key": WEB_APP_KEY, "Content-Type": "application/json"},
        json={"narrator_type": "playlet", "quote_id": 12345},
    )
    assert res.status_code == 400
    body = res.get_json()
    assert body["success"] is False
    assert body["error"]["code"] == "BAD_REQUEST"
    assert "quote_id" in body["error"]["message"]


def test_narrator_tasks_rejects_empty_string_quote_id(client):
    res = client.post(
        "/narrator/tasks",
        headers={"X-Web-App-Key": WEB_APP_KEY, "Content-Type": "application/json"},
        json={"narrator_type": "playlet", "quote_id": ""},
    )
    assert res.status_code == 400
    assert res.get_json()["error"]["code"] == "BAD_REQUEST"


def test_narrator_tasks_create_without_quote_id_unchanged(client, engine):
    """Sanity: omitting `quote_id` keeps v1 behavior — no snapshot
    written, no `snapshot_id` in the response."""
    res = client.post(
        "/narrator/tasks",
        headers={"X-Web-App-Key": WEB_APP_KEY, "Content-Type": "application/json"},
        json={"narrator_type": "playlet"},
    )
    assert res.status_code == 200
    data = res.get_json()["data"]
    assert "snapshot_id" not in data or data.get("snapshot_id") is None
    assert _read_master_task_snapshot_id(engine, data["narrator_task_id"]) is None


# ── review fixes: Decimal rate, triple-before-idempotent, audit reference ──


def test_commit_rejects_triple_change_when_snapshot_already_exists(engine):
    """regression coverage: triple validation runs BEFORE the idempotency
    check. A retry with the same `(quote_id, master_task_id)` but a
    drifted combo_key must raise QUOTE_PARAMETERS_CHANGED rather than
    silently returning the existing snapshot."""
    _insert_master_task(engine, narrator_task_id="mt_triple")
    with engine.connect() as conn:
        quote = generate_quote(
            conn,
            request_body={
                "template_id": TEMPLATE_ID,
                "combo_key": "original_narration_flash",
            },
            user_id=1,
            now=NOW,
        )
        # First commit succeeds with the bound triple.
        first = commit_master_task_snapshot(
            conn,
            quote_id=quote.quote_id,
            master_task_id="mt_triple",
            request_body={
                "template_id": TEMPLATE_ID,
                "combo_key": "original_narration_flash",
            },
            user_id=1,
            now=NOW + timedelta(seconds=5),
        )
        conn.commit()
    # Retry with a drifted combo_key — even though a snapshot exists
    # for the same (quote_id, master_task_id), the body no longer
    # matches the bound quote so the contract demands a re-quote.
    with engine.connect() as conn:
        with pytest.raises(QuoteParametersChanged):
            commit_master_task_snapshot(
                conn,
                quote_id=quote.quote_id,
                master_task_id="mt_triple",
                request_body={
                    "template_id": TEMPLATE_ID,
                    # Drifted from the bound combo_key on the quote.
                    "combo_key": "original_narration_pro",
                },
                user_id=1,
                now=NOW + timedelta(seconds=10),
            )
    assert first  # first commit still valid


def test_snapshot_system_reference_price_collapses_to_formula_total():
    """regression coverage, updated by subsequent update: under the new tiered
    formula `system_reference_price` collapses to `final_charge_price`
    for the manual-catalog branch — both are the formula output, and
    catalog-stored manual_price / system_reference_price are no longer
    the price source. The snapshot row reflects this: all three of
    `system_reference_price`, `manual_catalog_price`, and
    `final_charge_price` carry the same formula value (not the stale
    catalog seed values)."""
    eng = _create_engine()
    _seed_template(eng)
    # Intentionally seed stale catalog values (manual_price=100,
    # system_reference_price=80) to prove the code ignores them in
    # favor of the formula. The refresh script can overwrite
    # these values; the test asserts the runtime is independent.
    _seed_template_tier(
        eng,
        tier_code="original_narration_flash",
        manual_price=100,
        system_reference_price=80,
        quality="flash",
    )
    _seed_template_tier(
        eng,
        tier_code="original_narration_pro",
        manual_price=140,
        system_reference_price=110,
        quality="pro",
    )
    _insert_master_task(eng, narrator_task_id="mt_ref", user_id=1)
    try:
        with eng.connect() as conn:
            quote = generate_quote(
                conn,
                request_body={
                    "template_id": TEMPLATE_ID,
                    "combo_key": "original_narration_flash",
                },
                user_id=1,
                now=NOW,
            )
            # Formula: 1 min × (2 + 10 + 7) = 19. Stale catalog values
            # of 100 / 80 are ignored.
            assert quote.final_charge_price == 19
            assert quote.system_reference_price == 19
            snapshot_id = commit_master_task_snapshot(
                conn,
                quote_id=quote.quote_id,
                master_task_id="mt_ref",
                request_body={
                    "template_id": TEMPLATE_ID,
                    "combo_key": "original_narration_flash",
                },
                user_id=1,
                now=NOW + timedelta(seconds=5),
            )
            row = conn.execute(
                text(
                    "SELECT system_reference_price, manual_catalog_price, "
                    "final_charge_price FROM pricing_snapshots_v2 "
                    "WHERE snapshot_id = :s"
                ),
                {"s": snapshot_id},
            ).first()
            conn.commit()
        assert row[0] == 19, "snapshot.system_reference_price = formula total"
        assert row[1] == 19, "snapshot.manual_catalog_price = formula total"
        assert row[2] == 19, "snapshot.final_charge_price = formula total"
    finally:
        eng.dispose()


def test_commit_recovers_from_snapshot_unique_race(engine, monkeypatch):
    """regression coverage: a concurrent commit for the same (quote_id,
    master_task_id) that passes `get_snapshot_id_by_quote` but races
    on `insert_snapshot`'s UNIQUE constraint must NOT surface as 503.
    Instead the loser re-reads and returns the winner's snapshot_id
    idempotently."""
    from pricing_quote_v2 import store as quote_store

    _insert_master_task(engine, narrator_task_id="mt_race")
    with engine.connect() as conn:
        quote = generate_quote(
            conn,
            request_body={
                "template_id": TEMPLATE_ID,
                "combo_key": "original_narration_flash",
            },
            user_id=1,
            now=NOW,
        )
        conn.commit()
    # Simulate the race window: idempotency check sees no snapshot
    # (concurrent commit hasn't reached our session yet) but by the
    # time we INSERT the winner's row has materialized.
    original_lookup = quote_store.get_snapshot_id_by_quote
    seen = {"first": True}

    def fake_lookup(conn, qid):
        if qid == quote.quote_id and seen["first"]:
            seen["first"] = False
            return None  # window before peer is visible
        return original_lookup(conn, qid)

    monkeypatch.setattr(
        "pricing_quote_v2.service.get_snapshot_id_by_quote", fake_lookup
    )
    # Pre-insert the winner's snapshot row + master-task link before
    # our `insert_snapshot` runs.
    winner_snapshot_id = "S-2026-05-30-aaaaaaaaaa"
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO pricing_snapshots_v2 ("
                "snapshot_id, quote_id, pricing_rule_version, combo_key, "
                "price_source, pricing_minutes, system_reference_price, "
                "final_charge_price, breakdown, currency_unit, "
                "committed_at, refund_policy, refund_status, subflow_status, "
                "web_user_id, created_at) VALUES ("
                ":sid, :qid, 'v2.0', 'original_narration_flash', "
                "'manual_catalog_price', 1, 100, 100, '[]', 'web_point', "
                ":now, 'manual', 'none', '[]', 1, :now)"
            ),
            {"sid": winner_snapshot_id, "qid": quote.quote_id, "now": NOW},
        )
        conn.execute(
            text("UPDATE narrator_tasks SET snapshot_id = :sid WHERE narrator_task_id = :tid"),
            {"sid": winner_snapshot_id, "tid": "mt_race"},
        )
    with engine.connect() as conn:
        recovered = commit_master_task_snapshot(
            conn,
            quote_id=quote.quote_id,
            master_task_id="mt_race",
            request_body={
                "template_id": TEMPLATE_ID,
                "combo_key": "original_narration_flash",
            },
            user_id=1,
            now=NOW + timedelta(seconds=5),
        )
        conn.commit()
    assert recovered == winner_snapshot_id, (
        "Race loser must idempotently return the winner's snapshot_id"
    )
    # Still exactly one snapshot row for the quote.
    with engine.connect() as conn:
        count = conn.execute(
            text("SELECT COUNT(*) FROM pricing_snapshots_v2 WHERE quote_id = :q"),
            {"q": quote.quote_id},
        ).scalar()
    assert count == 1


# ─── regression coverage: `code` as canonical template identifier ─────────────────────────


def _seed_template_178(engine):
    """xy0178 derives to template_id="178". Seed a complete tier set so
    `code: xy0178` quotes can resolve through the catalog.

    `catalog_entry_id` defaults to `ce_{tier_code}_{ver}`, so it
    collides with the fixture template TEMPLATE_ID's tiers — pass
    explicit ids to keep both templates seedable in the same engine.
    """
    _seed_template(engine, template_id="178")
    _seed_template_tier(
        engine,
        template_id="178",
        tier_code="original_narration_flash",
        manual_price=100,
        quality="flash",
        catalog_entry_id="ce_original_narration_flash_178",
    )
    _seed_template_tier(
        engine,
        template_id="178",
        tier_code="original_narration_pro",
        manual_price=140,
        quality="pro",
        catalog_entry_id="ce_original_narration_pro_178",
    )


def test_generate_quote_with_code_derives_template_id(engine):
    _seed_template_178(engine)
    with engine.connect() as conn:
        quote = generate_quote(
            conn,
            request_body={
                "code": "xy0178",
                "combo_key": "original_narration_flash",
                "pro_upgrade": False,
            },
            user_id=1,
            now=NOW,
        )
        conn.commit()
    assert quote.code == "xy0178"
    assert quote.template_id == "178"
    # Default 60s seed for template 178 → formula 1 × (2+10+7) = 19.
    assert quote.final_charge_price == 19


def test_generate_quote_with_both_code_and_template_id_prefers_code(engine):
    """Legacy `template_id` is treated as advisory — backend ignores
    it and derives the lookup key from `code`."""
    _seed_template_178(engine)
    with engine.connect() as conn:
        quote = generate_quote(
            conn,
            request_body={
                "code": "xy0178",
                "template_id": "303",  # narrator 主 ID — must NOT be looked up
                "combo_key": "original_narration_flash",
                "pro_upgrade": False,
            },
            user_id=1,
            now=NOW,
        )
        conn.commit()
    assert quote.code == "xy0178"
    assert quote.template_id == "178"


def test_generate_quote_malformed_code_raises_template_code_invalid(engine):
    with engine.connect() as conn:
        with pytest.raises(QuoteTemplateCodeInvalid):
            generate_quote(
                conn,
                request_body={
                    "code": "not-a-code",
                    "combo_key": "original_narration_flash",
                    "pro_upgrade": False,
                },
                user_id=1,
                now=NOW,
            )


def test_generate_quote_legacy_template_id_only_unchanged(engine):
    """Previous contract: only `template_id` → behavior unchanged,
    `code` column written as NULL."""
    with engine.connect() as conn:
        quote = generate_quote(
            conn,
            request_body={
                "template_id": TEMPLATE_ID,
                "combo_key": "original_narration_flash",
                "pro_upgrade": False,
            },
            user_id=1,
            now=NOW,
        )
        conn.commit()
    assert quote.code is None
    assert quote.template_id == TEMPLATE_ID


def test_commit_binding_matches_by_code_when_both_have_code(engine):
    _seed_template_178(engine)
    _insert_master_task(engine, narrator_task_id="mt_code_match")
    with engine.connect() as conn:
        quote = generate_quote(
            conn,
            request_body={
                "code": "xy0178",
                "combo_key": "original_narration_flash",
                "pro_upgrade": False,
            },
            user_id=1,
            now=NOW,
        )
        snapshot_id = commit_master_task_snapshot(
            conn,
            quote_id=quote.quote_id,
            master_task_id="mt_code_match",
            request_body={
                "code": "xy0178",
                "template_id": "303",  # narrator 主 ID — ignored under code match
                "combo_key": "original_narration_flash",
            },
            user_id=1,
            now=NOW + timedelta(seconds=10),
        )
        conn.commit()
    assert snapshot_id.startswith("S-")


def test_commit_binding_falls_back_to_template_id_for_legacy_quote(engine):
    """Legacy quote (code IS NULL) + new client sending code in body:
    binding must fall back to template_id so legacy quotes stay
    bindable."""
    _insert_master_task(engine, narrator_task_id="mt_legacy_quote")
    with engine.connect() as conn:
        quote = generate_quote(
            conn,
            request_body={
                "template_id": TEMPLATE_ID,
                "combo_key": "original_narration_flash",
                "pro_upgrade": False,
            },
            user_id=1,
            now=NOW,
        )
        assert quote.code is None
        snapshot_id = commit_master_task_snapshot(
            conn,
            quote_id=quote.quote_id,
            master_task_id="mt_legacy_quote",
            request_body={
                "code": "xy0178",            # body carries code; quote does not
                "template_id": TEMPLATE_ID,  # fallback path matches this
                "combo_key": "original_narration_flash",
            },
            user_id=1,
            now=NOW + timedelta(seconds=10),
        )
        conn.commit()
    assert snapshot_id.startswith("S-")


def test_commit_binding_falls_back_to_template_id_when_body_has_no_code(engine):
    """New-shape quote (code set) + legacy client (template_id only):
    binding falls back to template_id (the derived value matches
    quote.template_id)."""
    _seed_template_178(engine)
    _insert_master_task(engine, narrator_task_id="mt_legacy_client")
    with engine.connect() as conn:
        quote = generate_quote(
            conn,
            request_body={
                "code": "xy0178",
                "combo_key": "original_narration_flash",
                "pro_upgrade": False,
            },
            user_id=1,
            now=NOW,
        )
        snapshot_id = commit_master_task_snapshot(
            conn,
            quote_id=quote.quote_id,
            master_task_id="mt_legacy_client",
            request_body={
                "template_id": "178",  # legacy body, equals quote.template_id
                "combo_key": "original_narration_flash",
            },
            user_id=1,
            now=NOW + timedelta(seconds=10),
        )
        conn.commit()
    assert snapshot_id.startswith("S-")


def test_commit_binding_code_match_rejects_wrong_code(engine):
    """When both sides have code, a mismatched code raises
    QUOTE_PARAMETERS_CHANGED (not silent pass)."""
    _seed_template_178(engine)
    _insert_master_task(engine, narrator_task_id="mt_wrong_code")
    with engine.connect() as conn:
        quote = generate_quote(
            conn,
            request_body={
                "code": "xy0178",
                "combo_key": "original_narration_flash",
                "pro_upgrade": False,
            },
            user_id=1,
            now=NOW,
        )
        with pytest.raises(QuoteParametersChanged) as exc:
            commit_master_task_snapshot(
                conn,
                quote_id=quote.quote_id,
                master_task_id="mt_wrong_code",
                request_body={
                    "code": "xy0001",  # different code
                    "combo_key": "original_narration_flash",
                },
                user_id=1,
                now=NOW + timedelta(seconds=10),
            )
    assert exc.value.details["match_field"] == "code"
