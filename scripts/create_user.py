"""Create a reseller end-user: generate a fresh `grid_xxx` app_key,
insert a `users` row with the initial balance + optional profile fields,
print the key to stdout.

Usage:
    python scripts/create_user.py                 # default 1000 points, empty profile
    python scripts/create_user.py --balance 500   # custom initial balance
    python scripts/create_user.py --key grid_…    # insert pre-supplied key
    python scripts/create_user.py --nickname 'Demo User' --mobile 13912345678 \\
        --email user@example.com --company-name 'Example Inc'

Re-running with a `--key` already in the table is rejected (no overwrite,
no silent balance / profile drift).
"""

from __future__ import annotations

import argparse
import sys
from decimal import Decimal
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from users.admin import DEFAULT_BALANCE_POINTS, create_user  # noqa: E402
from users.schema import generate_app_key  # noqa: E402


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--balance",
        type=Decimal,
        default=DEFAULT_BALANCE_POINTS,
        help="Initial balance in points (default: 1000).",
    )
    parser.add_argument(
        "--key",
        type=str,
        default=None,
        help="Pre-supplied app_key (must match grid_<22 base62> format). "
        "Default: auto-generate.",
    )
    parser.add_argument("--nickname", type=str, default=None)
    parser.add_argument("--mobile", type=str, default=None)
    parser.add_argument("--email", type=str, default=None)
    parser.add_argument(
        "--company-name",
        dest="company_name",
        type=str,
        default=None,
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    app_key = args.key or generate_app_key()

    from server import get_db_engine  # late import; server.py owns engine

    engine = get_db_engine()
    create_user(
        engine,
        app_key,
        args.balance,
        nickname=args.nickname,
        mobile=args.mobile,
        email=args.email,
        company_name=args.company_name,
    )
    print(app_key)
    return 0


if __name__ == "__main__":
    sys.exit(main())
