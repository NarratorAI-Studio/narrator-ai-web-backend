from __future__ import annotations

from prometheus_client import Counter


WALLET_DOUBLE_BILLING_BLOCKED = Counter(
    "wallet_double_billing_blocked_total",
    "Wallet template price confirmations blocked because legacy billing evidence exists.",
)
