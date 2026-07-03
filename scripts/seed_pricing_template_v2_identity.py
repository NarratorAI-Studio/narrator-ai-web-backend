"""Seed pricing_template_v2.{code,name,learning_model_id} from res_movie_baokuan CSV.

The implementation requirement. The 3 columns were added by migration 20260616_0001 and
default to NULL on existing rows; this script fills them from the
operations CSV dump (the upstream `/v2/res/movie-baokuan` API does not
return `code` at runtime — see `project_movie_baokuan_upstream_code_field`).

Join key: CSV.code -> pricing_template_v2.template_id via
`template_id_from_baokuan_code` (e.g. xy0046 -> "46"). Rows whose code
fails to parse (NULL, malformed, the `自定义` placeholder) are skipped.

Idempotent: rerunning UPDATEs the same fields with the same values. Rows
absent from pricing_template_v2 (template never registered locally) are
counted as misses, not errors — operator can ignore or backfill.

Usage:
    python scripts/seed_pricing_template_v2_identity.py --csv PATH            # dry-run (default)
    python scripts/seed_pricing_template_v2_identity.py --csv PATH --apply    # write to DB
"""

from __future__ import annotations

import argparse
import csv
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from sqlalchemy import create_engine, text  # noqa: E402

from pricing.baokuan_query import template_id_from_baokuan_code  # noqa: E402


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv", type=Path, required=True, help="res_movie_baokuan CSV path.")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write to DB. Default is dry-run (preview matched / missing).",
    )
    return parser.parse_args(argv)


def load_csv(csv_path: Path) -> list[dict[str, str | None]]:
    """Return rows with parseable {template_id, code, name, learning_model_id}."""
    out: list[dict[str, str | None]] = []
    with csv_path.open(encoding="utf-8") as fp:
        for row in csv.DictReader(fp):
            code = (row.get("code") or "").strip()
            if not code or code.upper() == "NULL":
                continue
            template_id = template_id_from_baokuan_code(code)
            if template_id is None:
                continue
            out.append(
                {
                    "template_id": template_id,
                    "code": code,
                    "name": (row.get("name") or "").strip() or None,
                    "learning_model_id": (row.get("learning_model_id") or "").strip() or None,
                }
            )
    return out


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if not args.csv.is_file():
        print(f"CSV not found: {args.csv}", file=sys.stderr)
        return 2

    rows = load_csv(args.csv)
    print(f"Parsed {len(rows)} CSV rows with valid xy-codes.")

    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        print("DATABASE_URL must be set.", file=sys.stderr)
        return 2
    # SQLAlchemy 2.x rejects the legacy `postgres://` scheme.
    if dsn.startswith("postgres://"):
        dsn = "postgresql://" + dsn[len("postgres://") :]

    engine = create_engine(dsn)
    matched: list[str] = []
    missing: list[str] = []
    with engine.begin() as conn:
        for row in rows:
            existing = conn.execute(
                text("SELECT 1 FROM pricing_template_v2 WHERE template_id = :t"),
                {"t": row["template_id"]},
            ).first()
            if existing is None:
                missing.append(row["template_id"])
                continue
            matched.append(row["template_id"])
            if args.apply:
                conn.execute(
                    text(
                        "UPDATE pricing_template_v2 "
                        "SET code = :code, name = :name, "
                        "learning_model_id = :lmid, "
                        "updated_at = CURRENT_TIMESTAMP "
                        "WHERE template_id = :t"
                    ),
                    {
                        "code": row["code"],
                        "name": row["name"],
                        "lmid": row["learning_model_id"],
                        "t": row["template_id"],
                    },
                )

    verb = "UPDATED" if args.apply else "WOULD UPDATE"
    print(f"{verb}: {len(matched)} rows in pricing_template_v2.")
    print(f"Skipped (no matching template row): {len(missing)}.")
    if missing and len(missing) <= 20:
        print(f"  Missing template_ids: {sorted(missing, key=lambda x: int(x))}")
    if not args.apply:
        print("Dry run — pass --apply to commit.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
