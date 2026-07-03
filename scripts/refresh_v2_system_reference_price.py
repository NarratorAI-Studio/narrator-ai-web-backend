"""Refresh `system_reference_price` AND `manual_price` for every v2
catalog tier using the 3-minute tiered pricing formula .

Two-step pipeline, both run inside the same transaction per `--apply`:

1. **Backfill `pricing_template_v2.video_duration_seconds`** from the
   operators-provided `/v2/res/movie-baokuan` CSV dump. The `time` column is
   `MM:SS`; rows where it is NULL or `code` doesn't parse to a backend
   template_id are skipped (matches the seeder convention in
   `seed_pricing_catalog_v2_from_ops_csv.py`).

2. **Recompute `system_reference_price` AND `manual_price`** for each
   enabled template × tier_code using
   `subflows.compute_tier_system_reference_price`. Both columns get
   the new formula value — under the subsequent update (user direction:
   Q1=B) the manual_price is no longer a sale-price override; it now
   tracks the formula so historical admin tooling sees a consistent
   view. The refresh inserts NEW rows in `pricing_catalog_v2_entry`
   with `effective_version = max(existing) + 1`, leaving history
   intact (matches the catalog versioning contract from regression coverage).

`--seed-missing`: optional flag that ALSO creates pricing_template_v2 +
5 catalog rows for templates present in the CSV but missing entirely
from the DB. Without the flag, those templates are listed as
"CSV templates NOT in DB" and skipped (the previous behavior — kept
as the default so operators don't accidentally seed new templates
into prod without intent). The implementation requirement subsequent update explicitly seeded the
5 orphans (xy0015 / 38 / 42 / 44 / 83) this way; future missing
templates fail-closed on quote per the user's Q2 direction.

Idempotent in spirit: re-running on the same CSV with no rate-table
changes produces zero new rows. Run without `--apply` (the default)
to inspect the diff plan first; `--apply` writes.

DB-vs-CSV diff report (always printed):
- templates in CSV but missing from DB    → seeded (with --seed-missing) or listed
- templates in DB without CSV coverage    → preserved as-is, flagged
- templates whose tier table doesn't fit  → preserved, flagged
- templates whose new == old              → preserved (no INSERT)

Usage:
    python scripts/refresh_v2_system_reference_price.py --csv PATH
    python scripts/refresh_v2_system_reference_price.py --csv PATH --apply
    python scripts/refresh_v2_system_reference_price.py --csv PATH --apply --seed-missing
    python scripts/refresh_v2_system_reference_price.py --csv PATH --apply --limit 5
"""

from __future__ import annotations

import argparse
import csv
import os
import sys
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from sqlalchemy import create_engine, select, text  # noqa: E402

from db.tables import (  # noqa: E402
    pricing_catalog_v2_entry,
    pricing_template_v2,
)
from pricing.baokuan_query import template_id_from_baokuan_code  # noqa: E402
from pricing_quote_v2.subflows import (  # noqa: E402
    MANUAL_CATALOG_TIER_RECIPES,
    compute_tier_system_reference_price,
)


def parse_mm_ss_to_seconds(raw: str) -> int | None:
    """`'2:10'` → 130. Returns None on empty / NULL / malformed."""
    if not raw:
        return None
    cleaned = raw.strip().strip('"')
    if not cleaned or cleaned.upper() == "NULL":
        return None
    parts = cleaned.split(":")
    if len(parts) != 2:
        return None
    try:
        minutes = int(parts[0])
        seconds = int(parts[1])
    except ValueError:
        return None
    if minutes < 0 or seconds < 0 or seconds >= 60:
        return None
    return minutes * 60 + seconds


# Tier metadata for the seed path. Matches `TIER_META` in
# `seed_pricing_catalog_v2_from_ops_csv.py`; copied here so the script
# can run standalone.
SEED_TIER_META: dict[str, dict[str, object]] = {
    "original_narration_flash": {
        "product_line": "original", "mode": "narration",
        "quality": "flash", "flash_pro_axis": "required",
    },
    "original_narration_pro": {
        "product_line": "original", "mode": "narration",
        "quality": "pro", "flash_pro_axis": "required",
    },
    "original_mix_flash": {
        "product_line": "original", "mode": "mix",
        "quality": "flash", "flash_pro_axis": "required",
    },
    "original_mix_pro": {
        "product_line": "original", "mode": "mix",
        "quality": "pro", "flash_pro_axis": "required",
    },
    "derivative": {
        "product_line": "derivative", "mode": None,
        "quality": None, "flash_pro_axis": "optional",
    },
}

# Flash counterpart per Pro tier, for `pro_surcharge_display` derivation.
PRO_FLASH_PAIRS: dict[str, str] = {
    "original_narration_pro": "original_narration_flash",
    "original_mix_pro": "original_mix_flash",
}


def load_csv_rows(csv_path: Path) -> tuple[
    dict[str, dict[str, object]],
    list[tuple[str, str]],
]:
    """Return (`{template_id: {seconds, code, name, learning_model_id}}`,
    skipped). The full identity tuple is captured so `--seed-missing`
    can create `pricing_template_v2` rows without re-reading the CSV.

    `skipped` is a list of (code_or_id, reason) for rows the script
    dropped, for operators triage.
    """
    rows: dict[str, dict[str, object]] = {}
    skipped: list[tuple[str, str]] = []
    with csv_path.open(encoding="utf-8") as fp:
        reader = csv.DictReader(fp)
        if reader.fieldnames is None or "time" not in reader.fieldnames:
            raise SystemExit("CSV is missing the required `time` column.")
        for row in reader:
            code = (row.get("code") or "").strip()
            ident = code or row.get("id") or "<unknown>"
            if not code or code.upper() == "NULL":
                skipped.append((ident, "code is NULL/empty"))
                continue
            template_id = template_id_from_baokuan_code(code)
            if template_id is None:
                skipped.append((ident, f"code {code!r} does not parse to backend id"))
                continue
            seconds = parse_mm_ss_to_seconds(row.get("time") or "")
            if seconds is None:
                skipped.append((ident, "time NULL or unparseable"))
                continue
            rows[template_id] = {
                "seconds": seconds,
                "code": code,
                "name": (row.get("name") or "").strip() or None,
                "learning_model_id": (row.get("learning_model_id") or "").strip() or None,
            }
    return rows, skipped


def _durations_view(csv_rows: dict[str, dict[str, object]]) -> dict[str, int]:
    """Project the full CSV-row dict down to {template_id: seconds} for
    callers that only need the duration."""
    return {tid: int(row["seconds"]) for tid, row in csv_rows.items()}


# Back-compat alias for callers that want only the duration map (the
# original test surface from review).
def load_csv_durations(csv_path: Path) -> tuple[dict[str, int], list[tuple[str, str]]]:
    rows, skipped = load_csv_rows(csv_path)
    return _durations_view(rows), skipped


def plan_refresh(
    conn,
    csv_durations: dict[str, int],
    *,
    limit: int | None,
) -> tuple[list[dict], list[dict], list[str], list[tuple[str, str]]]:
    """Return (duration_updates, tier_inserts, csv_only_ids, db_warnings).

    - `duration_updates`: pricing_template_v2 rows whose
      video_duration_seconds will change (or be set).
    - `tier_inserts`: pricing_catalog_v2_entry rows to INSERT (one per
      changed tier, bumping effective_version).
    - `csv_only_ids`: CSV template_ids not present in DB.
    - `db_warnings`: (template_id, reason) for DB templates skipped or
      flagged for operators (missing CSV time, tier_code not in recipe map,
      etc.).
    """
    # Pull every enabled template's id + current duration in one shot.
    template_rows = (
        conn.execute(
            select(
                pricing_template_v2.c.template_id,
                pricing_template_v2.c.video_duration_seconds,
                pricing_template_v2.c.enabled,
            )
        )
        .mappings()
        .all()
    )
    db_template_ids = {row["template_id"] for row in template_rows}

    duration_updates: list[dict] = []
    tier_inserts: list[dict] = []
    db_warnings: list[tuple[str, str]] = []
    now = datetime.now(timezone.utc)

    processed = 0
    for row in template_rows:
        if limit is not None and processed >= limit:
            break
        template_id = row["template_id"]
        if not row["enabled"]:
            db_warnings.append((template_id, "template disabled, skipped"))
            continue
        csv_seconds = csv_durations.get(template_id)
        if csv_seconds is None:
            db_warnings.append((template_id, "no CSV time entry, preserved"))
            continue

        if row["video_duration_seconds"] != csv_seconds:
            duration_updates.append(
                {
                    "template_id": template_id,
                    "video_duration_seconds": csv_seconds,
                    "updated_at": now,
                }
            )

        duration_minutes = Decimal(csv_seconds) / Decimal(60)

        # Latest enabled tier row per tier_code for this template.
        tier_rows = (
            conn.execute(
                select(
                    pricing_catalog_v2_entry.c.tier_code,
                    pricing_catalog_v2_entry.c.manual_price,
                    pricing_catalog_v2_entry.c.pro_surcharge_display,
                    pricing_catalog_v2_entry.c.system_reference_price,
                    pricing_catalog_v2_entry.c.product_line,
                    pricing_catalog_v2_entry.c.mode,
                    pricing_catalog_v2_entry.c.quality,
                    pricing_catalog_v2_entry.c.flash_pro_axis,
                    pricing_catalog_v2_entry.c.currency_unit,
                    pricing_catalog_v2_entry.c.raw_rate,
                    pricing_catalog_v2_entry.c.final_rate,
                    pricing_catalog_v2_entry.c.rounding_rule_version,
                    pricing_catalog_v2_entry.c.effective_version,
                )
                .where(pricing_catalog_v2_entry.c.template_id == template_id)
                .where(pricing_catalog_v2_entry.c.enabled.is_(True))
                .order_by(
                    pricing_catalog_v2_entry.c.tier_code,
                    pricing_catalog_v2_entry.c.effective_version.desc(),
                )
            )
            .mappings()
            .all()
        )
        latest_per_tier: dict[str, dict] = {}
        for tier_row in tier_rows:
            tier_code = tier_row["tier_code"]
            if tier_code not in latest_per_tier:
                latest_per_tier[tier_code] = dict(tier_row)

        # Precompute the flash tier's total for this template's pro-
        # surcharge derivation. Both Pro tiers reference their flash
        # counterpart in `PRO_FLASH_PAIRS`.
        flash_totals: dict[str, int] = {}
        for tier_code in MANUAL_CATALOG_TIER_RECIPES:
            if tier_code.endswith("_flash") or tier_code == "derivative":
                flash_totals[tier_code] = compute_tier_system_reference_price(
                    tier_code, duration_minutes
                )

        for tier_code, tier_row in latest_per_tier.items():
            if tier_code not in MANUAL_CATALOG_TIER_RECIPES:
                db_warnings.append(
                    (template_id, f"tier_code {tier_code!r} has no recipe, preserved")
                )
                continue
            new_price = compute_tier_system_reference_price(
                tier_code, duration_minutes
            )
            # manual_price tracks the formula. Skip only when BOTH
            # columns already match — a row whose system_reference_price
            # is current but whose manual_price still carries a legacy
            # value still needs a new effective_version.
            if (
                new_price == int(tier_row["system_reference_price"])
                and new_price == int(tier_row["manual_price"])
            ):
                continue
            # Pro tiers: surcharge = pro_total - flash_total, derived
            # from the formula (the wenan-rate delta × minutes).
            # Axis-optional (derivative) keeps NULL.
            if tier_code in PRO_FLASH_PAIRS:
                flash_code = PRO_FLASH_PAIRS[tier_code]
                pro_surcharge: int | None = new_price - flash_totals[flash_code]
            else:
                pro_surcharge = None
            tier_inserts.append(
                {
                    "catalog_entry_id": str(uuid.uuid4()),
                    "template_id": template_id,
                    "tier_code": tier_code,
                    "product_line": tier_row["product_line"],
                    "mode": tier_row["mode"],
                    "quality": tier_row["quality"],
                    "flash_pro_axis": tier_row["flash_pro_axis"],
                    # Both columns get the formula value (Q1=B).
                    "manual_price": new_price,
                    "pro_surcharge_display": pro_surcharge,
                    "system_reference_price": new_price,
                    "currency_unit": tier_row["currency_unit"],
                    # raw_rate / final_rate stay as the legacy
                    # per-minute-rate shape on a 60-min baseline so
                    # admin UI's contract doesn't break. They are
                    # informational only — the tiered formula is the
                    # actual price source.
                    "raw_rate": (Decimal(new_price) / Decimal(60)).quantize(Decimal("0.00001")),
                    "final_rate": (Decimal(new_price) / Decimal(60)).quantize(Decimal("0.01")),
                    "rounding_rule_version": tier_row["rounding_rule_version"],
                    # manual_price now == system_reference_price by
                    # construction, so the override warning collapses
                    # to False. Kept for schema compat.
                    "manual_override_warning": False,
                    "enabled": True,
                    "effective_version": int(tier_row["effective_version"]) + 1,
                    "created_at": now,
                    "updated_at": now,
                    "updated_by": "refresh_v2_system_reference_price",
                }
            )
        processed += 1

    csv_only_ids = sorted(set(csv_durations) - db_template_ids)
    return duration_updates, tier_inserts, csv_only_ids, db_warnings


def plan_seeds(
    csv_rows: dict[str, dict[str, object]],
    db_template_ids: set[str],
) -> tuple[list[dict], list[dict]]:
    """Build (template_seed_rows, tier_seed_rows) for templates present
    in the CSV but missing from `pricing_template_v2`. Each tier_code
    in `MANUAL_CATALOG_TIER_RECIPES` gets a row with manual_price =
    system_reference_price = formula output, mirroring `plan_refresh`
    so the `--seed-missing` path leaves the same audit shape as the
    refresh path.
    """
    now = datetime.now(timezone.utc)
    template_seed_rows: list[dict] = []
    tier_seed_rows: list[dict] = []

    csv_only_ids = sorted(set(csv_rows) - db_template_ids)
    for template_id in csv_only_ids:
        csv_row = csv_rows[template_id]
        seconds = int(csv_row["seconds"])
        duration_minutes = Decimal(seconds) / Decimal(60)
        template_seed_rows.append(
            {
                "template_id": template_id,
                "code": csv_row["code"],
                "name": csv_row["name"],
                "learning_model_id": csv_row["learning_model_id"],
                "video_duration_seconds": seconds,
                "created_at": now,
                "updated_at": now,
            }
        )

        # Precompute Flash totals so Pro surcharge math doesn't recompute.
        flash_totals = {
            t: compute_tier_system_reference_price(t, duration_minutes)
            for t in MANUAL_CATALOG_TIER_RECIPES
            if t.endswith("_flash") or t == "derivative"
        }
        for tier_code, meta in SEED_TIER_META.items():
            new_price = compute_tier_system_reference_price(tier_code, duration_minutes)
            if tier_code in PRO_FLASH_PAIRS:
                flash_code = PRO_FLASH_PAIRS[tier_code]
                pro_surcharge: int | None = new_price - flash_totals[flash_code]
            else:
                pro_surcharge = None
            tier_seed_rows.append(
                {
                    "catalog_entry_id": str(uuid.uuid4()),
                    "template_id": template_id,
                    "tier_code": tier_code,
                    "product_line": meta["product_line"],
                    "mode": meta["mode"],
                    "quality": meta["quality"],
                    "flash_pro_axis": meta["flash_pro_axis"],
                    "manual_price": new_price,
                    "pro_surcharge_display": pro_surcharge,
                    "system_reference_price": new_price,
                    "currency_unit": "web_point",
                    "raw_rate": (Decimal(new_price) / Decimal(60)).quantize(Decimal("0.00001")),
                    "final_rate": (Decimal(new_price) / Decimal(60)).quantize(Decimal("0.01")),
                    "rounding_rule_version": "v2.0-round-half-up",
                    "manual_override_warning": False,
                    "enabled": True,
                    "effective_version": 1,
                    "created_at": now,
                    "updated_at": now,
                    "updated_by": "refresh_v2_system_reference_price.seed_missing",
                }
            )
    return template_seed_rows, tier_seed_rows


def apply_to_db(
    engine,
    duration_updates: list[dict],
    tier_inserts: list[dict],
    template_seeds: list[dict] | None = None,
    tier_seeds: list[dict] | None = None,
) -> tuple[int, int, int, int]:
    """Apply the full plan in one transaction. Returns
    (template_seeds_inserted, duration_updates_applied,
    catalog_inserts_applied, tier_seeds_inserted).
    """
    template_seeds = template_seeds or []
    tier_seeds = tier_seeds or []
    template_seeds_count = 0
    updated = 0
    inserted = 0
    tier_seeds_count = 0
    with engine.begin() as conn:
        # Seed new pricing_template_v2 rows first so the FK references
        # in tier_seeds resolve.
        for row in template_seeds:
            result = conn.execute(
                text(
                    "INSERT INTO pricing_template_v2 ("
                    "template_id, template_family_id, tier_multiplier, enabled, "
                    "code, name, learning_model_id, video_duration_seconds, "
                    "created_at, updated_at) VALUES ("
                    ":template_id, NULL, 1.0, TRUE, "
                    ":code, :name, :learning_model_id, :video_duration_seconds, "
                    ":created_at, :updated_at) "
                    "ON CONFLICT (template_id) DO NOTHING"
                ),
                row,
            )
            template_seeds_count += result.rowcount or 0
        for row in duration_updates:
            result = conn.execute(
                text(
                    "UPDATE pricing_template_v2 "
                    "SET video_duration_seconds = :video_duration_seconds, "
                    "    updated_at = :updated_at "
                    "WHERE template_id = :template_id"
                ),
                row,
            )
            updated += result.rowcount or 0
        for source_list, is_seed in ((tier_inserts, False), (tier_seeds, True)):
            for row in source_list:
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
                    row,
                )
                if is_seed:
                    tier_seeds_count += result.rowcount or 0
                else:
                    inserted += result.rowcount or 0
    return template_seeds_count, updated, inserted, tier_seeds_count


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--csv",
        required=True,
        help="Path to the operators dump of /v2/res/movie-baokuan (must contain `code` and `time` columns).",
    )
    parser.add_argument(
        "--apply", action="store_true", help="Write changes to DB (default: dry-run)."
    )
    parser.add_argument(
        "--limit", type=int, default=None, help="Only process first N templates."
    )
    parser.add_argument(
        "--seed-missing",
        action="store_true",
        help=(
            "Also create pricing_template_v2 + 5 catalog rows for "
            "templates present in CSV but missing from DB. Without this "
            "flag, those templates are listed and skipped."
        ),
    )
    parser.add_argument(
        "--db-url",
        default=os.environ.get(
            "DATABASE_URL",
            "postgresql://postgres@localhost:5432/narrator_ai_web_backend",
        ),
    )
    args = parser.parse_args()

    csv_path = Path(args.csv)
    if not csv_path.exists():
        print(f"ERROR: CSV not found: {csv_path}", file=sys.stderr)
        return 2

    csv_rows, skipped = load_csv_rows(csv_path)
    csv_durations = _durations_view(csv_rows)
    print(
        f"Loaded {len(csv_durations)} (code, time) entries from CSV."
        f" Skipped {len(skipped)} rows at parse time."
    )
    for ident, reason in skipped[:10]:
        print(f"  skip: {ident}: {reason}")
    if len(skipped) > 10:
        print(f"  ...({len(skipped) - 10} more)")

    engine = create_engine(args.db_url)
    with engine.connect() as conn:
        duration_updates, tier_inserts, csv_only_ids, db_warnings = plan_refresh(
            conn, csv_durations, limit=args.limit
        )
        # Stable read of every template_id in DB for the seed pass (so
        # the connection close after this block is fine).
        db_template_ids = {
            row["template_id"]
            for row in conn.execute(
                select(pricing_template_v2.c.template_id)
            ).mappings()
        }

    template_seeds: list[dict] = []
    tier_seeds: list[dict] = []
    if args.seed_missing:
        template_seeds, tier_seeds = plan_seeds(csv_rows, db_template_ids)

    print()
    print(
        f"Plan: update {len(duration_updates)} template durations, "
        f"insert {len(tier_inserts)} new catalog entries "
        f"(effective_version bumped, manual_price + system_reference_price "
        f"both = formula output, Q1=B)."
    )
    if args.seed_missing:
        print(
            f"Seed (--seed-missing): {len(template_seeds)} new templates "
            f"+ {len(tier_seeds)} new catalog entries."
        )

    if csv_only_ids and not args.seed_missing:
        print(
            f"\nCSV templates NOT in DB (--seed-missing to create): "
            f"{len(csv_only_ids)}"
        )
        for tid in csv_only_ids[:20]:
            print(f"  {tid}")
        if len(csv_only_ids) > 20:
            print(f"  ...({len(csv_only_ids) - 20} more)")

    if db_warnings:
        print(f"\nDB templates needing operators attention: {len(db_warnings)}")
        # De-dupe so a template with multiple tier warnings prints once
        # per (template_id, reason).
        seen: set[tuple[str, str]] = set()
        for template_id, reason in db_warnings:
            key = (template_id, reason)
            if key in seen:
                continue
            seen.add(key)
            print(f"  {template_id}: {reason}")
            if len(seen) >= 20:
                print("  ...(more truncated)")
                break

    if args.apply:
        seeded_templates, updated, inserted, seeded_tiers = apply_to_db(
            engine,
            duration_updates,
            tier_inserts,
            template_seeds,
            tier_seeds,
        )
        print(
            f"\nApplied: {seeded_templates} template seeds, "
            f"{updated} template-duration UPDATEs, "
            f"{inserted} catalog refresh INSERTs, "
            f"{seeded_tiers} catalog seed INSERTs."
        )
    else:
        if tier_inserts:
            sample = tier_inserts[:5]
            print("\nSample tier inserts (first 5):")
            for row in sample:
                print(
                    f"  template_id={row['template_id']:>4s} tier={row['tier_code']:30s} "
                    f"new_manual={row['manual_price']:>4d}  "
                    f"new_ref={row['system_reference_price']:>4d}"
                )
        if tier_seeds:
            sample = tier_seeds[:5]
            print("\nSample tier seeds (first 5):")
            for row in sample:
                print(
                    f"  template_id={row['template_id']:>4s} tier={row['tier_code']:30s} "
                    f"price={row['system_reference_price']:>4d}"
                )
        print("\nDRY-RUN. Re-run with --apply to write to DB.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
