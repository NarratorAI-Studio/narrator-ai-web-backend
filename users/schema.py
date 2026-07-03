"""Schema + key-issuance helpers for the reseller `users` table.

This table is the source-of-truth for end-user balance. The existing
`wallet_accounts.available_balance / frozen_balance` columns will be
refactored to follow this table in a separate issue — for now the two
balances co-exist and the wallet path is untouched.
"""

from __future__ import annotations

import re
import secrets
import string


USERS_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS users (
    app_key TEXT PRIMARY KEY,
    balance_points NUMERIC(18, 2) NOT NULL DEFAULT 1000 CHECK (balance_points >= 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""

KEY_PREFIX = "grid_"
KEY_RANDOM_LEN = 22
_KEY_ALPHABET = string.ascii_letters + string.digits  # base62
_KEY_PATTERN = re.compile(rf"^{re.escape(KEY_PREFIX)}[A-Za-z0-9]{{{KEY_RANDOM_LEN}}}$")


def generate_app_key() -> str:
    """Return a fresh `grid_<22 base62>` key. Uses `secrets` for entropy."""
    suffix = "".join(secrets.choice(_KEY_ALPHABET) for _ in range(KEY_RANDOM_LEN))
    return f"{KEY_PREFIX}{suffix}"


def is_valid_app_key(key: str) -> bool:
    """True if `key` matches the issued format. Does not check existence."""
    return bool(_KEY_PATTERN.fullmatch(key))
