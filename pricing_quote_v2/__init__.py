"""Hard-price v2 quote + snapshot persistence.

Implements the quote and snapshot surface defined in narrator-ai-web's
`docs/pricing/v2/quote-snapshot-contract.md`, including confirm-page
quote locking and custom SRT pricing.

Two tables (additive — v1's `wallet_quotes` stays for the HP-XX path):

- `pricing_quotes_v2` — every quote, regardless of commit status.
- `pricing_snapshots_v2` — immutable per-order price record written
  on master-task commit. v1 refund_policy / refund_status /
  subflow_status fields reserved for v1.x automation.

Plus `narrator_tasks.snapshot_id` (nullable FK) so v2 master-task
rows can be joined to their snapshot.
"""

from __future__ import annotations

from .errors import (
    CustomSrtDownloadFailed,
    CustomSrtEmpty,
    CustomSrtFileIdMissing,
    CustomSrtFileNotFound,
    CustomSrtFileTooLarge,
    QuoteAlreadyCommitted,
    QuoteBodyPriceForbidden,
    QuoteBreakdownMismatch,
    QuoteComboKeyInvalid,
    QuoteExpired,
    QuoteManualCatalogDurationMissing,
    QuoteNotFound,
    QuoteParametersChanged,
    QuotePersistenceError,
    QuotePriceDrifted,
    QuoteTemplateCodeInvalid,
    WalletInsufficientBalance,
)
from .service import (
    commit_master_task_snapshot,
    generate_quote,
)
from .store import (
    PricingQuote,
    PricingSnapshot,
    get_quote,
    insert_quote,
    insert_snapshot,
)


__all__ = [
    "CustomSrtDownloadFailed",
    "CustomSrtEmpty",
    "CustomSrtFileIdMissing",
    "CustomSrtFileNotFound",
    "CustomSrtFileTooLarge",
    "PricingQuote",
    "PricingSnapshot",
    "QuoteAlreadyCommitted",
    "QuoteBodyPriceForbidden",
    "QuoteBreakdownMismatch",
    "QuoteComboKeyInvalid",
    "QuoteExpired",
    "QuoteManualCatalogDurationMissing",
    "QuoteNotFound",
    "QuoteParametersChanged",
    "QuotePersistenceError",
    "QuotePriceDrifted",
    "QuoteTemplateCodeInvalid",
    "WalletInsufficientBalance",
    "commit_master_task_snapshot",
    "generate_quote",
    "get_quote",
    "insert_quote",
    "insert_snapshot",
]
