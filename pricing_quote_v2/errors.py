"""Exceptions raised by the quote / snapshot layer .

Each subclass maps 1:1 to a frozen `code` in `quote-snapshot-contract.md`
§5 / §6. The route layer maps them to the documented HTTP status.
"""

from __future__ import annotations


class QuotePersistenceError(Exception):
    """DB unavailable / schema mismatch on the quote layer. Maps to 503
    `QUOTE_PERSISTENCE_ERROR` with `details: {}` (public-safe per
    security hardening from security hardening)."""

    def __init__(self, message: str, *, details: dict | None = None):
        super().__init__(message)
        self.message = message
        self.details = details or {}


class QuoteNotFound(Exception):
    """`quote_id` not in `pricing_quotes_v2`. Maps to 404."""

    def __init__(self, quote_id: str):
        super().__init__(f"Quote {quote_id!r} not found.")
        self.quote_id = quote_id


class QuoteValidationError(Exception):
    """Base class for §5 / §6 validation failures with structured
    `code:` and public details."""

    code: str = "QUOTE_VALIDATION_ERROR"
    http_status: int = 422

    def __init__(self, message: str, *, details: dict | None = None):
        super().__init__(message)
        self.message = message
        self.details = details or {}


class QuoteComboKeyInvalid(QuoteValidationError):
    """`combo_key` doesn't match the server-side swap result from
    `pro_upgrade` (contract §3)."""

    code = "QUOTE_COMBO_KEY_INVALID"


class QuoteTemplateCodeInvalid(QuoteValidationError):
    """Request body's `code` field is present but does not parse as a
    canonical xy-code (e.g. `"xy0178"`). Added in regression coverage — surface a
    distinct error code so the UI can tell "malformed code" apart from
    "catalog missing for a real code" (the latter is
    `CATALOG_TIER_MISSING`, 404)."""

    code = "QUOTE_TEMPLATE_CODE_INVALID"
    http_status = 400


class CustomSrtFileIdMissing(QuoteValidationError):
    """`custom_template_id` set but no `custom_srt_file_id` —
    server needs a cloud-drive pointer to fetch + parse + hash the
    SRT authoritatively. Replaces the previous
    `CustomSrtHashMissing` (which trusted client-provided hash;
    that contract was removed as a security fix)."""

    code = "CUSTOM_SRT_FILE_ID_MISSING"
    http_status = 400


class CustomSrtFileNotFound(QuoteValidationError):
    """`custom_srt_file_id` doesn't resolve to a cloud-drive file
    owned by the requesting `web_user_id`, or the file is not in a
    completed state. Returns the same envelope for "doesn't exist"
    and "owned by someone else" to avoid existence-leak (security review
    tenant-isolation pattern from security hardening)."""

    code = "CUSTOM_SRT_FILE_NOT_FOUND"
    http_status = 404


class CustomSrtFileTooLarge(QuoteValidationError):
    """SRT exceeds the configured byte cap (currently 2 MB —
    typical SRT is < 50 KB; the cap protects Flask workers from
    OOM if a caller uploads a non-SRT file by mistake)."""

    code = "CUSTOM_SRT_FILE_TOO_LARGE"
    http_status = 413


class CustomSrtDownloadFailed(QuoteValidationError):
    """Backend could not fetch the SRT bytes from cloud-drive
    upstream (network error / timeout / upstream 5xx). The caller
    should retry; the issue is on the server side."""

    code = "CUSTOM_SRT_DOWNLOAD_FAILED"
    http_status = 503


class CustomSrtEmpty(QuoteValidationError):
    """Server-side parse of the SRT produced 0 valid lines. Pricing
    `ceil(0/25) = 0` would yield a free order, so we reject early
    with a clear signal back to the caller (the SRT itself is
    malformed / empty, not a backend problem)."""

    code = "CUSTOM_SRT_EMPTY"
    http_status = 422


class QuoteManualCatalogDurationMissing(QuoteValidationError):
    """`pricing_template_v2.video_duration_seconds` is NULL for the
    requested template. Added by the subsequent update that moved the
    existing-template branch onto the 3-min tiered formula: without a
    duration, the formula has no input. Seed the duration (via
    `scripts/refresh_v2_system_reference_price.py --seed-missing`)
    before the template can quote."""

    code = "MANUAL_CATALOG_DURATION_MISSING"
    http_status = 422


class WalletInsufficientBalance(QuoteValidationError):
    """Wallet balance < final_charge_price at quote time. Maps to 402
    instead of waiting until master-task commit."""

    code = "WALLET_INSUFFICIENT_BALANCE"
    http_status = 402


class QuoteBreakdownMismatch(Exception):
    """Server-side invariant: sum(breakdown.subtotal) MUST equal
    final_charge_price. If we ever return one that doesn't, this
    fires server-side and surfaces as 500 (NOT user-facing).
    Catching the bug early is the whole point."""

    def __init__(self, *, expected_total: int, breakdown_total: int):
        super().__init__(
            f"Breakdown subtotals sum to {breakdown_total} but final_charge_price is {expected_total}."
        )
        self.expected_total = expected_total
        self.breakdown_total = breakdown_total


class QuoteBodyPriceForbidden(QuoteValidationError):
    """Master-task commit body contained `total` / `breakdown` /
    `final_charge_price`. Backend only trusts `quote_id` (§6)."""

    code = "QUOTE_BODY_PRICE_FORBIDDEN"


class QuoteExpired(QuoteValidationError):
    """`t_submit >= expires_at`. Confirm page must re-quote and
    re-confirm with the user (§6.1)."""

    code = "QUOTE_EXPIRED"
    http_status = 410


class QuoteParametersChanged(QuoteValidationError):
    """`(combo_key, template_id_or_custom_template_id, srt_file_hash)`
    drifted from the quote's bound triple (§6.2). Confirm page must
    re-quote."""

    code = "QUOTE_PARAMETERS_CHANGED"
    http_status = 409


class QuotePriceDrifted(QuoteValidationError):
    """Within TTL but past 60s, the silent re-quote computed a
    different `final_charge_price` than the original. Confirm page
    must show the new price to the user (§6.1)."""

    code = "QUOTE_PRICE_DRIFTED"
    http_status = 409


class QuoteAlreadyCommitted(QuoteValidationError):
    """The quote already has a committed snapshot bound to a different
    `narrator_task_id`. Prevents a retry from corrupting an existing
    snapshot — one quote commits to at most one master task."""

    code = "QUOTE_ALREADY_COMMITTED"
    http_status = 409


class SnapshotQuoteCollision(Exception):
    """Internal signal: an `insert_snapshot` raced with a concurrent
    commit and hit the `pricing_snapshots_v2.quote_id` UNIQUE
    constraint. Caught and handled inside `commit_master_task_snapshot`
    (re-read + idempotent return or `QuoteAlreadyCommitted`). Never
    surfaces to the route layer."""

    def __init__(self, *, quote_id: str):
        super().__init__(f"Snapshot for quote {quote_id!r} already exists (race).")
        self.quote_id = quote_id
