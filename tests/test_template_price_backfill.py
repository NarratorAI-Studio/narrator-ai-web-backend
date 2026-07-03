"""Tests for fa_template_price migration, schema, backfill rules, script, and
end-to-end coverage of POST /pricing/hard-price.

Coverage:
- Alembic migration applies fa_template_price schema.
- Backfill script computes hard_price via the 5-combo matrix.
- POST /pricing/hard-price returns 200 + non-empty price for every seeded
  (template_id, combo_key), using sqlite as a portable substitute for Postgres.
"""

from __future__ import annotations

import json
import runpy
import sqlite3
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text


# Production binds Decimal via psycopg2 natively; sqlite3 stdlib module needs
# an explicit adapter to round-trip Decimal values.
sqlite3.register_adapter(Decimal, str)


REPO_ROOT = Path(__file__).resolve().parents[1]
# Tests run against the synthetic fixture. It pins
# `original_narration_flash -> 84.50` via text_chars=2500/text_lines=130 under
# a reserved template_id range (>= 99000).
FIXTURE_PATH = REPO_ROOT / "tests" / "fixtures" / "template_price_fixture.json"
# Public sample seed used by a shape test that catches corruption or drift before
# it breaks local backfill examples.
SEED_PATH = REPO_ROOT / "scripts" / "seeds" / "template_price_seed.json"


# ---------- formula unit tests ----------


def test_compute_hard_price_matches_canonical_example():
    """Canonical example: template lines=130, chars=2500, Flash -> 84.50."""
    from pricing.hard_price_rules import compute_hard_price

    price = compute_hard_price("original_narration_flash", 2500, 130)

    assert price == Decimal("84.50")


@pytest.mark.parametrize(
    "combo_key,text_chars,text_lines,expected",
    [
        ("original_narration_flash", 2500, 130, Decimal("84.50")),
        ("original_narration_pro", 2500, 130, Decimal("109.50")),
        ("original_remix_flash", 2500, 130, Decimal("102.00")),
        ("original_remix_pro", 2500, 130, Decimal("172.00")),
        ("secondary_creation", 2500, 130, Decimal("162.00")),
        ("secondary_creation", 0, 52, Decimal("81.00")),
        ("original_narration_flash", 2800, 52, Decimal("50.00")),
    ],
)
def test_compute_hard_price_covers_full_combo_matrix(
    combo_key, text_chars, text_lines, expected
):
    """Verify 5-combo matrix + Plan ② example (81) + Plan ③ realtime (50)."""
    from pricing.hard_price_rules import compute_hard_price

    if combo_key == "secondary_creation" and text_chars == 0:
        text_chars = 1  # avoid validation
    assert compute_hard_price(combo_key, text_chars, text_lines) == expected


def test_compute_hard_price_rejects_unknown_combo_key():
    from pricing.hard_price_rules import compute_hard_price

    with pytest.raises(ValueError, match="unknown combo_key"):
        compute_hard_price("legacy_v0", 1000, 50)


def test_billing_minutes_rounds_up_per_public_rule():
    from pricing.hard_price_rules import billing_minutes

    assert billing_minutes(25) == 1
    assert billing_minutes(26) == 2
    assert billing_minutes(130) == 6
    assert billing_minutes(52) == 3
    assert billing_minutes(76) == 4


# ---------- schema & migration tests ----------


def test_template_price_schema_sql_defines_required_constraints():
    from pricing.schema import TEMPLATE_PRICE_SCHEMA_SQL

    schema = TEMPLATE_PRICE_SCHEMA_SQL
    assert "CREATE TABLE IF NOT EXISTS fa_template_price" in schema
    assert "PRIMARY KEY (template_id, combo_key, pricing_rule_version)" in schema
    assert "CHECK (hard_price > 0)" in schema
    assert "CHECK (pricing_rule_version > 0)" in schema
    assert "fa_template_price_current_idx" in schema
    assert "(template_id, combo_key, is_current)" in schema


def test_alembic_template_price_migration_uses_shared_schema_sql():
    from pricing.schema import TEMPLATE_PRICE_SCHEMA_SQL

    migration_path = (
        REPO_ROOT
        / "migrations"
        / "versions"
        / "20260518_0001_template_price.py"
    )
    module = runpy.run_path(str(migration_path))

    class Op:
        def __init__(self):
            self.executed: list[str] = []

        def execute(self, sql):
            self.executed.append(sql)

    op = Op()
    module["upgrade"].__globals__["op"] = op
    module["upgrade"]()

    assert module["revision"] == "20260518_0001"
    assert module["down_revision"] == "20260511_0001"
    assert op.executed == [TEMPLATE_PRICE_SCHEMA_SQL]


def test_alembic_template_price_downgrade_drops_table_and_index():
    migration_path = (
        REPO_ROOT
        / "migrations"
        / "versions"
        / "20260518_0001_template_price.py"
    )
    module = runpy.run_path(str(migration_path))

    class Op:
        def __init__(self):
            self.executed: list[str] = []

        def execute(self, sql):
            self.executed.append(sql)

    op = Op()
    module["downgrade"].__globals__["op"] = op
    module["downgrade"]()

    joined = "\n".join(op.executed)
    assert "DROP INDEX IF EXISTS fa_template_price_current_unique" in joined
    assert "DROP INDEX IF EXISTS fa_template_price_current_idx" in joined
    assert "DROP TABLE IF EXISTS fa_template_price" in joined


# ---------- end-to-end: sqlite + backfill + /pricing/hard-price ----------


SQLITE_SCHEMA = """
CREATE TABLE IF NOT EXISTS fa_template_price (
    template_id INTEGER NOT NULL,
    combo_key TEXT NOT NULL,
    hard_price NUMERIC(18, 2) NOT NULL CHECK (hard_price > 0),
    text_chars INTEGER,
    text_lines INTEGER,
    pricing_rule_version INTEGER NOT NULL CHECK (pricing_rule_version > 0),
    is_current INTEGER NOT NULL,
    source_sheet_id TEXT,
    PRIMARY KEY (template_id, combo_key, pricing_rule_version)
);
CREATE INDEX IF NOT EXISTS fa_template_price_current_idx
    ON fa_template_price (template_id, combo_key, is_current);
CREATE UNIQUE INDEX IF NOT EXISTS fa_template_price_current_unique
    ON fa_template_price (template_id, combo_key)
    WHERE is_current;
CREATE INDEX IF NOT EXISTS fa_template_price_source_sheet_id_idx
    ON fa_template_price (source_sheet_id);
"""


@pytest.fixture()
def sqlite_engine_with_schema():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
    )
    with engine.begin() as conn:
        for statement in SQLITE_SCHEMA.strip().split(";"):
            stripped = statement.strip()
            if stripped:
                conn.execute(text(stripped))
    yield engine
    engine.dispose()


def _run_backfill(engine, monkeypatch, *, dry_run=False, version=1):
    """Invoke scripts.backfill_template_price.main with engine override."""
    import server
    import scripts.backfill_template_price as backfill

    monkeypatch.setattr(server, "get_db_engine", lambda: engine)

    argv = ["--seed-file", str(FIXTURE_PATH), "--pricing-rule-version", str(version)]
    if dry_run:
        argv.append("--dry-run")
    return backfill.main(argv)


def test_backfill_dry_run_does_not_write_rows(
    sqlite_engine_with_schema, monkeypatch, capsys
):
    engine = sqlite_engine_with_schema

    rc = _run_backfill(engine, monkeypatch, dry_run=True)

    assert rc == 0
    output = capsys.readouterr().out
    assert "dry-run: no rows written" in output
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT count(*) AS cnt FROM fa_template_price")
        ).mappings().first()
    assert row["cnt"] == 0


def test_backfill_writes_seed_rows_and_is_idempotent(
    sqlite_engine_with_schema, monkeypatch
):
    engine = sqlite_engine_with_schema

    first_rc = _run_backfill(engine, monkeypatch)
    assert first_rc == 0

    with engine.connect() as conn:
        first_rows = conn.execute(
            text(
                "SELECT template_id, combo_key, hard_price, is_current, source_sheet_id "
                "FROM fa_template_price ORDER BY template_id, combo_key"
            )
        ).mappings().all()

    with FIXTURE_PATH.open(encoding="utf-8") as fp:
        seed = json.load(fp)
    expected_count = len(seed["templates"]) * 5
    assert len(first_rows) == expected_count

    fixture_flash = [
        r
        for r in first_rows
        if r["template_id"] == 99042 and r["combo_key"] == "original_narration_flash"
    ]
    assert len(fixture_flash) == 1
    assert Decimal(str(fixture_flash[0]["hard_price"])) == Decimal("84.50")
    assert fixture_flash[0]["source_sheet_id"] == "sample-99042"

    second_rc = _run_backfill(engine, monkeypatch)
    assert second_rc == 0
    with engine.connect() as conn:
        second_count = conn.execute(
            text("SELECT count(*) AS cnt FROM fa_template_price")
        ).mappings().first()["cnt"]
    assert second_count == expected_count


def test_pricing_hard_price_endpoint_returns_seeded_coverage(
    sqlite_engine_with_schema, monkeypatch
):
    """End-to-end: backfill into sqlite, then call /pricing/hard-price for
    every seeded (template_id, combo_key) — must return 200 + non-empty price."""
    import server
    from pricing.hard_price_rules import all_combo_keys

    engine = sqlite_engine_with_schema

    monkeypatch.setattr(server, "get_db_engine", lambda: engine)
    monkeypatch.setattr(server, "get_db_core_connection", lambda: engine.connect())

    rc = _run_backfill(engine, monkeypatch)
    assert rc == 0

    with FIXTURE_PATH.open(encoding="utf-8") as fp:
        seed = json.load(fp)

    client = server.app.test_client()

    coverage_report: list[dict] = []
    for template in seed["templates"]:
        template_id = template["template_id"]
        for combo_key in all_combo_keys():
            response = client.post(
                "/pricing/hard-price",
                json={"template_id": template_id, "combo_key": combo_key},
            )
            assert response.status_code == 200, (
                f"{template_id}/{combo_key} returned {response.status_code}: "
                f"{response.get_data(as_text=True)}"
            )
            payload = response.get_json()
            assert payload["success"] is True
            data = payload["data"]
            assert data["template_id"] == template_id
            assert data["combo_key"] == combo_key
            assert Decimal(str(data["hard_price"])) > 0
            assert data["pricing_rule_version"] == 1
            coverage_report.append(
                {
                    "template_id": template_id,
                    "combo_key": combo_key,
                    "hard_price": str(data["hard_price"]),
                }
            )

    # Canonical synthetic fixture.
    canonical = next(
        item
        for item in coverage_report
        if item["template_id"] == 99042 and item["combo_key"] == "original_narration_flash"
    )
    assert Decimal(canonical["hard_price"]) == Decimal("84.50")


def test_pricing_hard_price_endpoint_returns_404_for_unknown_combo(
    sqlite_engine_with_schema, monkeypatch
):
    import server

    engine = sqlite_engine_with_schema
    monkeypatch.setattr(server, "get_db_engine", lambda: engine)
    monkeypatch.setattr(server, "get_db_core_connection", lambda: engine.connect())

    _run_backfill(engine, monkeypatch)
    client = server.app.test_client()

    response = client.post(
        "/pricing/hard-price",
        json={"template_id": 99042, "combo_key": "no_such_combo"},
    )
    assert response.status_code == 404


def test_supersede_older_versions_marks_old_rows_not_current(
    sqlite_engine_with_schema, monkeypatch
):
    """Bumping pricing_rule_version flips older rows' is_current to false."""
    engine = sqlite_engine_with_schema

    _run_backfill(engine, monkeypatch, version=1)
    _run_backfill(engine, monkeypatch, version=2)

    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT pricing_rule_version, is_current "
                "FROM fa_template_price "
                "WHERE template_id = 99042 AND combo_key = 'original_narration_flash' "
                "ORDER BY pricing_rule_version"
            )
        ).mappings().all()

    assert len(rows) == 2
    assert rows[0]["pricing_rule_version"] == 1
    assert rows[0]["is_current"] in (0, False)
    assert rows[1]["pricing_rule_version"] == 2
    assert rows[1]["is_current"] in (1, True)


def test_backfill_refuses_version_downgrade(
    sqlite_engine_with_schema, monkeypatch, capsys
):
    """Re-running v1 after v2 is current must be rejected.

    The backfill must not silently demote v2 by upserting v1 back to
    is_current=true.
    """
    engine = sqlite_engine_with_schema

    assert _run_backfill(engine, monkeypatch, version=2) == 0
    assert _run_backfill(engine, monkeypatch, version=1) == 2

    err = capsys.readouterr().err
    assert "refusing to backfill" in err
    assert "newer version (2)" in err

    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT pricing_rule_version, is_current "
                "FROM fa_template_price "
                "WHERE template_id = 99042 AND combo_key = 'original_narration_flash'"
            )
        ).mappings().all()

    assert len(rows) == 1, "v1 must not have been inserted on downgrade attempt"
    assert rows[0]["pricing_rule_version"] == 2
    assert rows[0]["is_current"] in (1, True)


def test_partial_unique_index_blocks_concurrent_current_rows(
    sqlite_engine_with_schema,
):
    """DB-level guard rejects two current rows for the same template/combo."""
    from sqlalchemy.exc import IntegrityError

    engine = sqlite_engine_with_schema

    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO fa_template_price "
                "(template_id, combo_key, hard_price, pricing_rule_version, is_current) "
                "VALUES (1, 'secondary_creation', 27, 1, 1)"
            )
        )

    with pytest.raises(IntegrityError):
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO fa_template_price "
                    "(template_id, combo_key, hard_price, pricing_rule_version, is_current) "
                    "VALUES (1, 'secondary_creation', 27, 2, 1)"
                )
            )


def test_schema_sql_declares_partial_unique_index():
    from pricing.schema import TEMPLATE_PRICE_SCHEMA_SQL

    assert "fa_template_price_current_unique" in TEMPLATE_PRICE_SCHEMA_SQL
    assert "WHERE is_current" in TEMPLATE_PRICE_SCHEMA_SQL


def test_public_seed_has_consistent_shape():
    """Guard against silent corruption or column drift of the public sample seed.

    The fixture-based tests above can pass even if `template_price_seed.json`
    becomes malformed because they read FIXTURE_PATH, not SEED_PATH. This test
    loads the public sample seed and asserts every row is usable by
    `scripts.backfill_template_price.expand_rows()`.
    """
    from scripts.backfill_template_price import expand_rows

    with SEED_PATH.open(encoding="utf-8") as fp:
        seed = json.load(fp)

    templates = seed.get("templates", [])

    ids = [t["template_id"] for t in templates]
    assert all(isinstance(tid, int) and tid > 0 for tid in ids), (
        "every template_id must be a positive int"
    )
    assert len(set(ids)) == len(ids), "template_id must be unique across the seed"
    assert len(templates) == 3

    for t in templates:
        assert isinstance(t["text_chars"], int) and t["text_chars"] > 0, (
            f"template_id={t['template_id']}: text_chars must be a positive int, "
            f"got {t.get('text_chars')!r}"
        )
        assert isinstance(t["text_lines"], int) and t["text_lines"] > 0, (
            f"template_id={t['template_id']}: text_lines must be a positive int, "
            f"got {t.get('text_lines')!r}"
        )

    rows = expand_rows(templates, pricing_rule_version=1)
    assert len(rows) == 15, (
        f"expand_rows produced {len(rows)} rows; expected 3 x 5 = 15"
    )


# ---------- source_sheet_id column ----------


def test_template_price_schema_sql_declares_source_sheet_id_column():
    """Schema exposes source_sheet_id and an index for external catalog IDs."""
    from pricing.schema import TEMPLATE_PRICE_SCHEMA_SQL

    schema = TEMPLATE_PRICE_SCHEMA_SQL
    assert "source_sheet_id TEXT" in schema
    assert "fa_template_price_source_sheet_id_idx" in schema
    assert "ON fa_template_price (source_sheet_id)" in schema


def test_alembic_source_sheet_id_migration_alters_and_indexes():
    migration_path = (
        REPO_ROOT
        / "migrations"
        / "versions"
        / "20260525_0001_template_price_source_sheet_id.py"
    )
    module = runpy.run_path(str(migration_path))

    class Op:
        def __init__(self):
            self.executed: list[str] = []

        def execute(self, sql):
            self.executed.append(sql)

    op = Op()
    module["upgrade"].__globals__["op"] = op
    module["upgrade"]()

    assert module["revision"] == "20260525_0001"
    assert module["down_revision"] == "20260518_0001"
    joined = "\n".join(op.executed)
    assert "ALTER TABLE fa_template_price ADD COLUMN" in joined
    assert "source_sheet_id TEXT" in joined
    assert "CREATE INDEX IF NOT EXISTS fa_template_price_source_sheet_id_idx" in joined

    op_down = Op()
    module["downgrade"].__globals__["op"] = op_down
    module["downgrade"]()
    joined_down = "\n".join(op_down.executed)
    assert "DROP INDEX IF EXISTS fa_template_price_source_sheet_id_idx" in joined_down
    assert "DROP COLUMN IF EXISTS source_sheet_id" in joined_down


def test_backfill_persists_source_sheet_id_from_seed(
    sqlite_engine_with_schema, monkeypatch
):
    """Re-running backfill after the seed gains source_sheet_id must populate
    the new column. The fixture pins template_id=99042 -> 'sample-99042'."""
    engine = sqlite_engine_with_schema
    assert _run_backfill(engine, monkeypatch) == 0

    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT DISTINCT template_id, source_sheet_id "
                "FROM fa_template_price"
            )
        ).mappings().all()

    by_id = {r["template_id"]: r["source_sheet_id"] for r in rows}
    assert by_id[99042] == "sample-99042"


def test_public_seed_has_source_sheet_id_for_every_template():
    """Every public sample template must carry source_sheet_id."""
    with SEED_PATH.open(encoding="utf-8") as fp:
        seed = json.load(fp)
    missing = [
        t for t in seed["templates"]
        if not t.get("source_sheet_id")
    ]
    assert not missing, (
        f"{len(missing)} template(s) in public sample seed missing source_sheet_id; "
        f"sample template_ids: {[t['template_id'] for t in missing[:5]]}"
    )
