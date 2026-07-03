"""Seed pricing_catalog_v2 (template + entries) from v1 fa_template_price.

For each template that appears in BOTH:
  - res_movie_baokuan CSV (the upstream template metadata dump), and
  - local fa_template_price (the v1 template price table with 5-combo matrix)

writes:
  - one pricing_template_v2 row, and
  - five pricing_catalog_v2_entry rows (one per tier_code).

The template_id namespace is the BACKEND id (== fa_template_price.template_id
== int(CSV.code[2:])), NOT the narrator main id (CSV.id). This keeps v2 in
the same id space as v1, so a future v1 -> v2 quote path can join cleanly.

Idempotent: re-runs are safe via ON CONFLICT DO NOTHING on both tables.
This means re-running will NOT overwrite admin edits that bumped
effective_version > 1.

Usage:
    python scripts/seed_pricing_catalog_v2_from_v1.py --csv PATH            # dry-run (default)
    python scripts/seed_pricing_catalog_v2_from_v1.py --csv PATH --apply    # write to DB
    python scripts/seed_pricing_catalog_v2_from_v1.py --csv PATH --limit 5  # only first 5 templates
    python scripts/seed_pricing_catalog_v2_from_v1.py --csv PATH --sql-out FILE  # dump SQL to file
"""

from __future__ import annotations

import argparse
import csv
import os
import sys
import uuid
from datetime import datetime, timezone
from decimal import Decimal, ROUND_CEILING
from pathlib import Path
from typing import Iterable

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from sqlalchemy import create_engine, text  # noqa: E402


# v1 combo_key -> v2 tier_code (note: v1 "remix" renamed to v2 "mix"; "secondary_creation" renamed to "derivative")
COMBO_TO_TIER = {
    "original_narration_flash": "original_narration_flash",
    "original_narration_pro": "original_narration_pro",
    "original_remix_flash": "original_mix_flash",
    "original_remix_pro": "original_mix_pro",
    "secondary_creation": "derivative",
}

# Tier metadata per docs/pricing/v2/catalog-tier-contract.md section 3.
# `derivative` uses flash_pro_axis=optional and MUST have mode/quality NULL
# (CheckConstraint flash_pro_axis_optional_nullifies_mode_quality).
TIER_META = {
    "original_narration_flash": {"product_line": "original", "mode": "narration", "quality": "flash", "flash_pro_axis": "required"},
    "original_narration_pro":   {"product_line": "original", "mode": "narration", "quality": "pro",   "flash_pro_axis": "required"},
    "original_mix_flash":       {"product_line": "original", "mode": "mix",       "quality": "flash", "flash_pro_axis": "required"},
    "original_mix_pro":         {"product_line": "original", "mode": "mix",       "quality": "pro",   "flash_pro_axis": "required"},
    "derivative":               {"product_line": "derivative", "mode": None, "quality": None, "flash_pro_axis": "optional"},
}


def parse_backend_id(code: str) -> int | None:
    """Extract numeric backend id from CSV code (e.g. 'xy0046' -> 46)."""
    if not code or not code.startswith("xy"):
        return None
    digits = code[2:].lstrip("0") or "0"
    try:
        return int(digits)
    except ValueError:
        return None


def load_csv_templates(csv_path: Path) -> dict[int, dict]:
    """Return {backend_id: {csv_id, code, name}} for non-NULL rows."""
    templates: dict[int, dict] = {}
    with csv_path.open(encoding="utf-8") as fp:
        for row in csv.DictReader(fp):
            if row["code"] in ("NULL", "", None):
                continue
            if row["name"] == "自定义":
                continue
            backend_id = parse_backend_id(row["code"])
            if backend_id is None:
                continue
            templates[backend_id] = {
                "csv_id": int(row["id"]),
                "code": row["code"],
                "name": row["name"],
            }
    return templates


def fetch_v1_prices(engine, backend_ids: Iterable[int]) -> dict[int, dict[str, Decimal]]:
    """Return {backend_id: {combo_key: hard_price_decimal}} for is_current rows."""
    ids = sorted(set(backend_ids))
    if not ids:
        return {}
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT template_id, combo_key, hard_price "
                "FROM fa_template_price "
                "WHERE template_id = ANY(:ids) AND is_current = TRUE "
                "ORDER BY template_id, combo_key"
            ),
            {"ids": ids},
        ).all()
    out: dict[int, dict[str, Decimal]] = {}
    for tid, ck, hp in rows:
        out.setdefault(int(tid), {})[ck] = Decimal(hp)
    return out


def to_int_ceil(d: Decimal | int | float) -> int:
    """Decimal -> int via upward rounding."""
    return int(Decimal(d).to_integral_value(rounding=ROUND_CEILING))


def build_rows(
    csv_templates: dict[int, dict],
    v1_prices: dict[int, dict[str, Decimal]],
    *,
    now_iso: str,
    updated_by: str,
) -> tuple[list[dict], list[dict], list[int], list[int]]:
    """Return (template_rows, entry_rows, csv_only_ids, v1_only_ids)."""
    template_rows: list[dict] = []
    entry_rows: list[dict] = []
    csv_only: list[int] = []
    v1_only = sorted(set(v1_prices) - set(csv_templates))

    for backend_id in sorted(csv_templates):
        if backend_id not in v1_prices:
            csv_only.append(backend_id)
            continue
        combos = v1_prices[backend_id]
        if not all(ck in combos for ck in COMBO_TO_TIER):
            # incomplete v1 row set — skip rather than seed partial
            csv_only.append(backend_id)
            continue

        template_id = str(backend_id)

        template_rows.append({
            "template_id": template_id,
            "tier_multiplier": "1.0000",
            "created_at": now_iso,
            "updated_at": now_iso,
        })

        flash_narration = to_int_ceil(combos["original_narration_flash"])
        flash_mix = to_int_ceil(combos["original_remix_flash"])

        for v1_combo, tier_code in COMBO_TO_TIER.items():
            tier_meta = TIER_META[tier_code]
            hp = combos[v1_combo]
            manual_price = to_int_ceil(hp)
            system_reference_price = manual_price  # |manual-ref|/ref = 0, no override warning

            pro_surcharge_display: int | None = None
            if tier_code == "original_narration_pro":
                pro_surcharge_display = manual_price - flash_narration
            elif tier_code == "original_mix_pro":
                pro_surcharge_display = manual_price - flash_mix

            # raw_rate/final_rate placeholders derived from manual_price.
            # Catalog section 5 does NOT require these to satisfy any equation
            # vs system_reference_price (pricing_minutes is not stored here);
            # operator can refine via admin UX without affecting price math.
            rate_value = (Decimal(manual_price) / Decimal(60)).quantize(Decimal("0.00001"))
            final_rate_value = (Decimal(manual_price) / Decimal(60)).quantize(Decimal("0.01"))

            entry_rows.append({
                "catalog_entry_id": str(uuid.uuid4()),
                "template_id": template_id,
                "tier_code": tier_code,
                "product_line": tier_meta["product_line"],
                "mode": tier_meta["mode"],
                "quality": tier_meta["quality"],
                "flash_pro_axis": tier_meta["flash_pro_axis"],
                "manual_price": manual_price,
                "pro_surcharge_display": pro_surcharge_display,
                "system_reference_price": system_reference_price,
                "currency_unit": "web_point",
                "raw_rate": str(rate_value),
                "final_rate": str(final_rate_value),
                "rounding_rule_version": "v2.0-round-half-up",
                "manual_override_warning": False,
                "enabled": True,
                "effective_version": 1,
                "created_at": now_iso,
                "updated_at": now_iso,
                "updated_by": updated_by,
            })

    return template_rows, entry_rows, csv_only, v1_only


def render_sql(template_rows: list[dict], entry_rows: list[dict]) -> str:
    """Render as a single transactional SQL script (idempotent via ON CONFLICT)."""
    lines = ["BEGIN;", ""]

    for r in template_rows:
        lines.append(
            "INSERT INTO pricing_template_v2 "
            "(template_id, template_family_id, tier_multiplier, enabled, created_at, updated_at) "
            f"VALUES ('{r['template_id']}', NULL, {r['tier_multiplier']}, TRUE, "
            f"'{r['created_at']}', '{r['updated_at']}') "
            "ON CONFLICT (template_id) DO NOTHING;"
        )
    lines.append("")

    for r in entry_rows:
        mode_sql = f"'{r['mode']}'" if r["mode"] is not None else "NULL"
        quality_sql = f"'{r['quality']}'" if r["quality"] is not None else "NULL"
        surcharge_sql = (
            str(r["pro_surcharge_display"])
            if r["pro_surcharge_display"] is not None
            else "NULL"
        )
        lines.append(
            "INSERT INTO pricing_catalog_v2_entry ("
            "catalog_entry_id, template_id, tier_code, product_line, mode, quality, "
            "flash_pro_axis, manual_price, pro_surcharge_display, system_reference_price, "
            "currency_unit, raw_rate, final_rate, rounding_rule_version, "
            "manual_override_warning, enabled, effective_version, "
            "created_at, updated_at, updated_by) VALUES ("
            f"'{r['catalog_entry_id']}', '{r['template_id']}', '{r['tier_code']}', "
            f"'{r['product_line']}', {mode_sql}, {quality_sql}, "
            f"'{r['flash_pro_axis']}', {r['manual_price']}, {surcharge_sql}, {r['system_reference_price']}, "
            f"'{r['currency_unit']}', {r['raw_rate']}, {r['final_rate']}, '{r['rounding_rule_version']}', "
            f"{str(r['manual_override_warning']).upper()}, {str(r['enabled']).upper()}, {r['effective_version']}, "
            f"'{r['created_at']}', '{r['updated_at']}', '{r['updated_by']}') "
            "ON CONFLICT (template_id, tier_code, effective_version) DO NOTHING;"
        )

    lines.append("")
    lines.append("COMMIT;")
    return "\n".join(lines)


def apply_to_db(engine, template_rows: list[dict], entry_rows: list[dict]) -> tuple[int, int]:
    """Apply via parameterized SQLAlchemy (safer than raw SQL string interpolation)."""
    inserted_templates = 0
    inserted_entries = 0
    with engine.begin() as conn:
        for r in template_rows:
            result = conn.execute(
                text(
                    "INSERT INTO pricing_template_v2 "
                    "(template_id, template_family_id, tier_multiplier, enabled, created_at, updated_at) "
                    "VALUES (:template_id, NULL, :tier_multiplier, TRUE, :created_at, :updated_at) "
                    "ON CONFLICT (template_id) DO NOTHING"
                ),
                r,
            )
            inserted_templates += result.rowcount or 0
        for r in entry_rows:
            result = conn.execute(
                text(
                    "INSERT INTO pricing_catalog_v2_entry ("
                    "catalog_entry_id, template_id, tier_code, product_line, mode, quality, "
                    "flash_pro_axis, manual_price, pro_surcharge_display, system_reference_price, "
                    "currency_unit, raw_rate, final_rate, rounding_rule_version, "
                    "manual_override_warning, enabled, effective_version, "
                    "created_at, updated_at, updated_by) VALUES ("
                    ":catalog_entry_id, :template_id, :tier_code, :product_line, :mode, :quality, "
                    ":flash_pro_axis, :manual_price, :pro_surcharge_display, :system_reference_price, "
                    ":currency_unit, :raw_rate, :final_rate, :rounding_rule_version, "
                    ":manual_override_warning, :enabled, :effective_version, "
                    ":created_at, :updated_at, :updated_by) "
                    "ON CONFLICT (template_id, tier_code, effective_version) DO NOTHING"
                ),
                r,
            )
            inserted_entries += result.rowcount or 0
    return inserted_templates, inserted_entries


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv", required=True, help="Path to res_movie_baokuan.csv")
    parser.add_argument("--apply", action="store_true", help="Apply to DB (default: dry-run)")
    parser.add_argument("--limit", type=int, default=None, help="Only process first N templates")
    parser.add_argument("--sql-out", help="Write generated SQL to file")
    parser.add_argument(
        "--db-url",
        default=os.environ.get("DATABASE_URL", "postgresql://postgres@localhost:5432/narrator_ai_web_backend"),
        help="DB connection string (default: $DATABASE_URL or local)",
    )
    parser.add_argument("--updated-by", default="seed_from_v1", help="updated_by column value")
    args = parser.parse_args()

    csv_path = Path(args.csv)
    if not csv_path.exists():
        print(f"ERROR: CSV not found: {csv_path}", file=sys.stderr)
        return 2

    csv_templates = load_csv_templates(csv_path)
    print(f"Loaded {len(csv_templates)} templates from CSV (non-NULL code, non-自定义).")

    engine = create_engine(args.db_url)
    v1_prices = fetch_v1_prices(engine, csv_templates.keys())
    print(f"Loaded {len(v1_prices)} v1 fa_template_price templates from DB.")

    now_iso = datetime.now(timezone.utc).isoformat()

    if args.limit is not None:
        csv_templates = dict(list(csv_templates.items())[: args.limit])

    template_rows, entry_rows, csv_only, v1_only = build_rows(
        csv_templates, v1_prices, now_iso=now_iso, updated_by=args.updated_by
    )

    print()
    print(f"Will seed: {len(template_rows)} templates, {len(entry_rows)} entries")
    print(f"CSV-only (no v1 price match, skipped): {len(csv_only)}")
    if csv_only:
        print(f"  backend_ids: {csv_only[:20]}{'...' if len(csv_only) > 20 else ''}")
    print(f"v1-only (in fa_template_price but not in CSV): {len(v1_only)}")
    if v1_only:
        print(f"  backend_ids: {v1_only[:20]}{'...' if len(v1_only) > 20 else ''}")

    sql = render_sql(template_rows, entry_rows)

    if args.sql_out:
        Path(args.sql_out).write_text(sql, encoding="utf-8")
        print(f"\nSQL written to {args.sql_out}")

    if args.apply:
        inserted_t, inserted_e = apply_to_db(engine, template_rows, entry_rows)
        print(f"\nApplied: {inserted_t} new templates, {inserted_e} new entries "
              f"({len(template_rows) - inserted_t} templates / {len(entry_rows) - inserted_e} entries already existed, ON CONFLICT DO NOTHING).")
    else:
        print("\n--- SQL preview (first 5 lines) ---")
        for line in sql.split("\n")[:5]:
            print(line)
        print("--- ... ---")
        print("\nDRY-RUN. Re-run with --apply to write to DB.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
