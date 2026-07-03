"""Backfill fa_template_price rows from a seed dataset.

For each template in the seed, computes hard_price across the 5-combo matrix
(see pricing.hard_price_rules) and upserts into fa_template_price with
`is_current = true`. Idempotent: re-running yields no data drift.

Usage:
    python scripts/backfill_template_price.py            # apply seed
    python scripts/backfill_template_price.py --dry-run  # preview, no writes
    python scripts/backfill_template_price.py \\
        --seed-file scripts/seeds/template_price_seed.json \\
        --pricing-rule-version 1

When bumping `--pricing-rule-version` past the current row's version, rows
with the older version are marked `is_current = false` in the same transaction.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Iterable


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from sqlalchemy import text  # noqa: E402

from pricing.hard_price_rules import (  # noqa: E402
    COMBO_RULES,
    all_combo_keys,
    compute_hard_price,
)


DEFAULT_SEED_PATH = REPO_ROOT / "scripts" / "seeds" / "template_price_seed.json"
DEFAULT_PRICING_RULE_VERSION = 1


def load_seed(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as fp:
        return json.load(fp)


def expand_rows(
    templates: Iterable[dict], pricing_rule_version: int
) -> list[dict]:
    rows: list[dict] = []
    for template in templates:
        template_id = template["template_id"]
        text_chars = template["text_chars"]
        text_lines = template["text_lines"]
        source_sheet_id = template.get("source_sheet_id")
        for combo_key in all_combo_keys():
            hard_price = compute_hard_price(combo_key, text_chars, text_lines)
            rows.append(
                {
                    "template_id": template_id,
                    "combo_key": combo_key,
                    "hard_price": hard_price,
                    "text_chars": text_chars,
                    "text_lines": text_lines,
                    "pricing_rule_version": pricing_rule_version,
                    "is_current": True,
                    "source_sheet_id": source_sheet_id,
                }
            )
    return rows


def render_preview(rows: list[dict]) -> str:
    lines = [
        "template_id  combo_key                  hard_price  text_chars  text_lines  v   is_current",
        "-----------  -------------------------  ----------  ----------  ----------  --  ----------",
    ]
    for row in rows:
        lines.append(
            f"{row['template_id']:>11}  {row['combo_key']:<25}  "
            f"{row['hard_price']:>10}  {row['text_chars']:>10}  "
            f"{row['text_lines']:>10}  {row['pricing_rule_version']:<2}  "
            f"{str(row['is_current']).lower()}"
        )
    return "\n".join(lines)


UPSERT_SQL = text(
    """
    INSERT INTO fa_template_price (
        template_id,
        combo_key,
        hard_price,
        text_chars,
        text_lines,
        pricing_rule_version,
        is_current,
        source_sheet_id
    ) VALUES (
        :template_id,
        :combo_key,
        :hard_price,
        :text_chars,
        :text_lines,
        :pricing_rule_version,
        :is_current,
        :source_sheet_id
    )
    ON CONFLICT (template_id, combo_key, pricing_rule_version) DO UPDATE
        SET hard_price = EXCLUDED.hard_price,
            text_chars = EXCLUDED.text_chars,
            text_lines = EXCLUDED.text_lines,
            is_current = EXCLUDED.is_current,
            source_sheet_id = EXCLUDED.source_sheet_id
    """
)


# Mark ALL rows of OTHER versions for the same (template_id, combo_key) as
# non-current — both lower and higher. The version-downgrade guard
# (`CURRENT_MAX_VERSION_SQL` below) prevents accidental v1-after-v2 reruns
# from ever reaching this statement, so "supersede other" cannot demote a
# newer prod version.
SUPERSEDE_OTHER_VERSIONS_SQL = text(
    """
    UPDATE fa_template_price
       SET is_current = false
     WHERE template_id = :template_id
       AND combo_key = :combo_key
       AND pricing_rule_version <> :pricing_rule_version
       AND is_current = true
    """
)


CURRENT_MAX_VERSION_SQL = text(
    "SELECT COALESCE(MAX(pricing_rule_version), 0) AS max_version "
    "FROM fa_template_price"
)


class VersionDowngradeError(RuntimeError):
    """Raised when a backfill targets a pricing_rule_version older than the
    latest version already in the table — refused to prevent the
    accidental-v1-after-v2 footgun the regression coverage flagged."""


def _assert_version_not_downgrade(conn, requested_version: int) -> None:
    row = conn.execute(CURRENT_MAX_VERSION_SQL).mappings().first()
    current_max = int(row["max_version"]) if row else 0
    if requested_version < current_max:
        raise VersionDowngradeError(
            f"refusing to backfill pricing_rule_version={requested_version}: "
            f"a newer version ({current_max}) already exists. "
            f"Re-run with --pricing-rule-version >= {current_max}, or roll "
            f"back the newer version first."
        )


def apply_rows(rows: list[dict], requested_version: int) -> int:
    from server import get_db_engine  # noqa: E402

    engine = get_db_engine()
    with engine.begin() as conn:
        _assert_version_not_downgrade(conn, requested_version)
        for row in rows:
            conn.execute(SUPERSEDE_OTHER_VERSIONS_SQL, row)
            conn.execute(UPSERT_SQL, row)
    return len(rows)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Backfill fa_template_price from a seed dataset."
    )
    parser.add_argument(
        "--seed-file",
        type=Path,
        default=DEFAULT_SEED_PATH,
        help="Seed JSON file (default: scripts/seeds/template_price_seed.json)",
    )
    parser.add_argument(
        "--pricing-rule-version",
        type=int,
        default=DEFAULT_PRICING_RULE_VERSION,
        help="Pricing rule version to write (default: 1)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print computed rows without writing to the database.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    seed = load_seed(args.seed_file)
    templates = seed.get("templates", [])
    if not templates:
        print(f"seed file {args.seed_file} has no templates", file=sys.stderr)
        return 1

    rows = expand_rows(templates, args.pricing_rule_version)

    print(render_preview(rows))
    print()
    print(
        f"computed {len(rows)} row(s) across {len(templates)} template(s) "
        f"× {len(COMBO_RULES)} combo_key(s) "
        f"at pricing_rule_version={args.pricing_rule_version}"
    )

    if args.dry_run:
        print("dry-run: no rows written")
        return 0

    try:
        written = apply_rows(rows, args.pricing_rule_version)
    except VersionDowngradeError as error:
        print(f"backfill aborted: {error}", file=sys.stderr)
        return 2
    print(f"wrote {written} row(s) to fa_template_price")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
