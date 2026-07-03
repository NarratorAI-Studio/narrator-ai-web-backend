"""Generate the monthly template price finance reconciliation CSV."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from finance import (  # noqa: E402
    build_reconciliation_query,
    month_bounds,
    render_reconciliation_csv,
    summarize_reconciliation,
)
from finance.reconciliation import normalize_reconciliation_row  # noqa: E402
from server import get_db_core_connection  # noqa: E402


def main() -> None:
    args = parse_args()
    start, end = month_bounds(args.month)
    conn = get_db_core_connection()
    try:
        result = conn.execute(build_reconciliation_query(start, end))
        rows = [
            normalize_reconciliation_row(dict(row), start, end)
            for row in result.mappings()
        ]
    finally:
        conn.close()

    csv_text = render_reconciliation_csv(rows)
    if args.output:
        output_path = Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(csv_text, encoding="utf-8")
    else:
        sys.stdout.write(csv_text)

    summary = summarize_reconciliation(rows)
    if args.summary_output:
        summary_path = Path(args.summary_output)
        summary_path.parent.mkdir(parents=True, exist_ok=True)
        summary_path.write_text(
            json.dumps(summary, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
    if args.fail_on_p0 and int(summary["p0_rows"]) > 0:
        raise SystemExit(2)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate finance reconciliation for wallet orders."
    )
    parser.add_argument(
        "--month",
        required=True,
        help="UTC accounting month in YYYY-MM form, for example 2026-05.",
    )
    parser.add_argument("--output", help="CSV destination. Defaults to stdout.")
    parser.add_argument("--summary-output", help="Optional JSON summary destination.")
    parser.add_argument(
        "--fail-on-p0",
        action="store_true",
        help="Exit 2 when amount mismatch rows exist.",
    )
    return parser.parse_args()


if __name__ == "__main__":
    main()
