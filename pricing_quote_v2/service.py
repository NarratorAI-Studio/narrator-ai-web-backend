"""Quote generation + master-task snapshot commit .

Composes the catalog read (Step 1), business arithmetic (§4 / §8),
TTL / lock-timing rules (§6.1 / §6.2), and the wallet preflight
(§5 → 402) into the two top-level entry points exposed via the
route layer:

- `generate_quote(conn, request_body, user_id, now=...)` →
  PricingQuote (persisted, all fields ready to echo back to UI)
- `commit_master_task_snapshot(conn, quote_id, master_task_id,
  request_payload, now=...)` → PricingSnapshot (persisted, linked
  back to narrator_tasks via snapshot_id)
"""

from __future__ import annotations

import math
import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Optional

from sqlalchemy import select, update
from sqlalchemy.engine import Connection
from sqlalchemy.exc import SQLAlchemyError

from db.tables import users
from pricing.baokuan_query import template_id_from_baokuan_code
from pricing_catalog_v2 import (
    CatalogPersistenceError,
    CatalogTierMissing,
    resolve_effective_tiers,
)
from pricing_catalog_v2.store import get_template_metadata

from .custom_srt import fetch_and_parse_srt
from .subflows import (
    AXIS_OPTIONAL_COMBOS,
    FIXED_TRAILING_SUBFLOWS,
    LEADING_SUBFLOWS_BY_COMBO,
    MANUAL_CATALOG_TIER_RECIPES,
    WENAN_COMBO_LABELS,
    WENAN_COMBO_UNIT_PRICES,
    tiered_subtotal,
)
from .errors import (
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
    SnapshotQuoteCollision,
    WalletInsufficientBalance,
)
from .store import (
    PricingQuote,
    attach_snapshot_to_master_task,
    get_quote,
    get_snapshot_id_by_quote,
    insert_quote,
    insert_snapshot,
    mark_quote_committed,
    new_quote_id,
    new_snapshot_id,
)


PRICING_RULE_VERSION = "v2.0"

_MANUAL_CATALOG_COMBO_ALIASES: dict[str, tuple[str, ...]] = {
    # Web uses "remix" in the public flow; existing v2 catalog seed rows
    # use "mix". Accept both spellings while keeping the submitted key on
    # the quote/snapshot contract.
    "original_remix_flash": ("original_mix_flash",),
    "original_remix_pro": ("original_mix_pro",),
    "original_mix_flash": ("original_remix_flash",),
    "original_mix_pro": ("original_remix_pro",),
    # Web still submits the v1 public name `secondary_creation`; the v1->v2
    # seed (`scripts/seed_pricing_catalog_v2_from_v1.py`) renamed the tier
    # to `derivative`. Same one-directional public/catalog spelling gap.
    "secondary_creation": ("derivative",),
    "derivative": ("secondary_creation",),
}

# Contract §4.4 — default TTL 5 minutes.
_DEFAULT_TTL_SECONDS = 5 * 60


def _ttl_seconds() -> int:
    raw = os.environ.get("QUOTE_TTL_SECONDS")
    if not raw:
        return _DEFAULT_TTL_SECONDS
    try:
        v = int(raw)
        return v if v > 0 else _DEFAULT_TTL_SECONDS
    except ValueError:
        return _DEFAULT_TTL_SECONDS


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


# ─── quote generation ──────────────────────────────────────────────────────


def generate_quote(
    conn: Connection,
    *,
    request_body: dict[str, Any],
    user_id: int,
    now: Optional[datetime] = None,
) -> PricingQuote:
    """End-to-end §3-§5: validate request, compute quote, check
    balance, persist, return the dataclass.

    Caller (route) is responsible for the connection + commit.
    """
    when = now or _now_utc()

    code, template_id, custom_template_id = _resolve_template_identity(request_body)
    combo_key = _validate_combo_key(request_body, request_body.get("pro_upgrade", False))
    pro_upgrade = bool(request_body.get("pro_upgrade", False))

    if custom_template_id is not None:
        # Server-authoritative SRT parse — never trust client-provided
        # `custom_srt_valid_line_count` or `custom_srt_file_hash` as
        # pricing inputs. The
        # client only supplies a cloud-drive file_id it owns; the
        # backend downloads + hashes + parses.
        srt_metadata = fetch_and_parse_srt(
            conn,
            web_user_id=user_id,
            file_id=request_body.get("custom_srt_file_id"),
        )
        priced = _price_custom_template(
            conn,
            custom_template_id=custom_template_id,
            combo_key=combo_key,
            valid_line_count=srt_metadata["valid_line_count"],
        )
        priced["srt_file_hash"] = srt_metadata["srt_file_hash"]
        priced["valid_line_count"] = srt_metadata["valid_line_count"]
    else:
        priced = _price_manual_catalog(
            conn,
            template_id=template_id,
            combo_key=combo_key,
            pro_upgrade=pro_upgrade,
        )
        priced["srt_file_hash"] = None
        priced["valid_line_count"] = None

    # Server-side §4.3 invariant: sum(breakdown.subtotal) ==
    # final_charge_price. Bug-catcher, not a user-facing case.
    breakdown_total = sum(int(item["subtotal"]) for item in priced["breakdown"])
    if breakdown_total != priced["final_charge_price"]:
        raise QuoteBreakdownMismatch(
            expected_total=priced["final_charge_price"],
            breakdown_total=breakdown_total,
        )

    # Wallet preflight at quote time (§5) so UI gets the "余额不足"
    # signal in one roundtrip.
    balance = _read_wallet_balance(conn, user_id)
    if balance < priced["final_charge_price"]:
        raise WalletInsufficientBalance(
            "Wallet balance is below the required final charge price.",
            details={
                "required": priced["final_charge_price"],
                "available": balance,
                "shortfall": priced["final_charge_price"] - balance,
                "currency_unit": "web_point",
            },
        )

    quote_id = new_quote_id()
    expires_at = when + timedelta(seconds=_ttl_seconds())
    row = {
        "quote_id": quote_id,
        "pricing_rule_version": PRICING_RULE_VERSION,
        "price_source": priced["price_source"],
        "template_id": template_id,
        "code": code,
        "custom_template_id": custom_template_id,
        "combo_key": combo_key,
        "pro_upgrade": pro_upgrade,
        "starting_price": priced.get("starting_price"),
        "final_charge_price": priced["final_charge_price"],
        "flash_total": priced["flash_total"],
        "pro_total": priced["pro_total"],
        "pro_upgrade_delta": priced["pro_upgrade_delta"],
        "pricing_minutes": priced["pricing_minutes"],
        "valid_line_count": priced["valid_line_count"],
        "srt_file_hash": priced["srt_file_hash"],
        # Bound at commit (§6.2) so a caller can't quote one SRT and
        # commit a different one. NULL for manual
        # catalog quotes where there is no SRT.
        "custom_srt_file_id": (
            request_body.get("custom_srt_file_id") if custom_template_id is not None else None
        ),
        "system_reference_price": priced["system_reference_price"],
        "breakdown": priced["breakdown"],
        "currency_unit": "web_point",
        "expires_at": expires_at,
        "committed_at": None,
        "web_user_id": user_id,
        "created_at": when,
    }
    insert_quote(conn, row=row)
    return get_quote(conn, quote_id)


def _coerce_text_id(value: Any) -> Any:
    """JSON callers often send the numeric template id as an int.
    `pricing_template_v2.template_id` and `pricing_quotes_v2.template_id`
    / `custom_template_id` are TEXT columns; Postgres rejects
    `text = integer` comparisons . SQLite is lax so unit tests
    didn't catch this. Coerce int → str at the input boundary so
    downstream SQL binds the right type. bool is a subclass of int
    but never a valid id — leave it untouched so validation rejects
    it downstream.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return str(value)
    return value


def _resolve_template_identity(
    body: dict[str, Any],
) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """Resolve the request's template identity into a (code, effective
    template_id, custom_template_id) triple.

    Code precedence rule : the upstream xy-`code` is the canonical
    cross-system identifier; the local `template_id` is only the
    numeric suffix used as a catalog FK. When the caller supplies
    `code`, derive the effective `template_id` for catalog lookup via
    `template_id_from_baokuan_code` and surface a distinct
    `QUOTE_TEMPLATE_CODE_INVALID` error for malformed input so the UI
    can tell that case apart from `CATALOG_TIER_MISSING` (which means
    the code parsed but the catalog row is absent).

    Legacy callers that send only `template_id` still work — the
    helper passes it through unchanged so the existing tests / on-disk
    quotes keep behaving identically.

    Exactly-one constraint applies to {(code OR template_id)} XOR
    custom_template_id (mirrors the v1 `_validate_template_ids` rule,
    just widened to accept code as a template-side signal).
    """
    code = body.get("code")
    template_id = _coerce_text_id(body.get("template_id"))
    custom_template_id = _coerce_text_id(body.get("custom_template_id"))

    has_template_input = code is not None or template_id is not None
    has_custom = custom_template_id is not None
    if has_template_input == has_custom:
        raise QuoteComboKeyInvalid(
            "Exactly one of (code or template_id) or custom_template_id is required.",
            details={
                "code": code,
                "template_id": template_id,
                "custom_template_id": custom_template_id,
            },
        )

    if code is not None:
        derived = template_id_from_baokuan_code(code)
        if derived is None:
            raise QuoteTemplateCodeInvalid(
                f"`code` {code!r} is not a valid xy-code.",
                details={"code": code},
            )
        effective_template_id = derived
    else:
        effective_template_id = template_id

    return code, effective_template_id, custom_template_id


def _validate_combo_key(body: dict[str, Any], pro_upgrade: bool) -> str:
    combo_key = body.get("combo_key")
    if not isinstance(combo_key, str) or not combo_key:
        raise QuoteComboKeyInvalid(
            "combo_key is required.",
            details={"combo_key": combo_key},
        )
    # Axis-optional combos (e.g. `secondary_creation`) carry no
    # Flash/Pro suffix and are valid for either pro_upgrade value —
    # mirrors `flash_pro_axis='optional'` in the manual catalog.
    if combo_key in AXIS_OPTIONAL_COMBOS:
        return combo_key
    expected_quality = "pro" if pro_upgrade else "flash"
    if expected_quality == "pro" and combo_key.endswith("_flash"):
        raise QuoteComboKeyInvalid(
            "pro_upgrade=true requires a Pro combo_key.",
            details={"combo_key": combo_key, "expected_quality": expected_quality},
        )
    if expected_quality == "flash" and combo_key.endswith("_pro"):
        raise QuoteComboKeyInvalid(
            "pro_upgrade=false requires a Flash (or non-Pro) combo_key.",
            details={"combo_key": combo_key, "expected_quality": expected_quality},
        )
    return combo_key


def _price_manual_catalog(
    conn: Connection,
    *,
    template_id: str,
    combo_key: str,
    pro_upgrade: bool,
) -> dict[str, Any]:
    """Existing-template branch under the subsequent update:

    `final_charge_price` and `breakdown` BOTH derive from the 3-minute
    tier formula in `subflows.MANUAL_CATALOG_TIER_RECIPES`. Duration is
    sourced from `pricing_template_v2.video_duration_seconds` (seeded
    from upstream `/v2/res/movie-baokuan` `time`). The catalog tier rows
    are still read (for `catalog_entry_id` audit) but their stored
    `manual_price` is no longer the price source — by user direction
    on regression coverage the existing-template charge and the display must follow
    the same tiered rules as custom-template quotes, with
    `manual_price` overwritten in DB to match the formula output so
    historical admin tooling sees a consistent view.

    `final_charge_price = compute(recipe of chosen tier)`. For
    Flash/Pro axis tiers, `flash_total` and `pro_total` come from the
    two paired recipes (the wenan rate differs, the
    clip_data + video_composing tail is shared), and
    `pro_upgrade_delta = compute(pro_recipe) - compute(flash_recipe)`.
    For axis-optional (derivative) tiers, `flash_total = pro_total =
    compute(recipe)` and `delta = 0`.

    Raises `QuoteManualCatalogDurationMissing` (422) if the template
    has no `video_duration_seconds`; seed a duration
    (refresh script `--seed-missing` or admin tooling) before the
    template can quote.
    """
    try:
        metadata, tiers = resolve_effective_tiers(conn, template_id)
    except (CatalogPersistenceError, CatalogTierMissing):
        raise

    if metadata.video_duration_seconds is None:
        raise QuoteManualCatalogDurationMissing(
            "Template has no video_duration_seconds; cannot apply the "
            "3-minute tier formula. Operators must seed the duration before "
            "the template can quote.",
            details={"template_id": template_id},
        )

    by_code = {t.tier_code: t for t in tiers}
    catalog_combo_key = _resolve_manual_catalog_combo_key(combo_key, by_code)
    if catalog_combo_key is None:
        raise QuoteComboKeyInvalid(
            f"combo_key {combo_key!r} has no catalog entry for template {template_id!r}.",
            details={"template_id": template_id, "combo_key": combo_key},
        )

    chosen = by_code[catalog_combo_key]
    duration_minutes = Decimal(metadata.video_duration_seconds) / Decimal(60)

    # Pair-find the counterpart tier code for the Flash/Pro axis. Axis-
    # optional tiers (derivative) collapse: flash_total = pro_total =
    # compute(recipe), pro_upgrade_delta = 0.
    if chosen.flash_pro_axis == "optional":
        flash_code = pro_code = catalog_combo_key
    elif chosen.quality == "pro":
        flash_code = catalog_combo_key[: -len("_pro")] + "_flash"
        pro_code = catalog_combo_key
    else:
        flash_code = catalog_combo_key
        pro_code = catalog_combo_key[: -len("_flash")] + "_pro"

    if (
        flash_code not in MANUAL_CATALOG_TIER_RECIPES
        or pro_code not in MANUAL_CATALOG_TIER_RECIPES
    ):
        # Catalog has a tier_code we don't know how to price (e.g. an
        # experimental tier the recipe map hasn't been updated for).
        raise QuoteComboKeyInvalid(
            f"combo_key {combo_key!r} has no manual-catalog tier recipe.",
            details={
                "template_id": template_id,
                "flash_code": flash_code,
                "pro_code": pro_code,
            },
        )

    flash_total = _compute_recipe_total(flash_code, duration_minutes)
    pro_total = _compute_recipe_total(pro_code, duration_minutes)
    pro_upgrade_delta = pro_total - flash_total
    final_charge_price = pro_total if pro_upgrade else flash_total
    starting_price = flash_total

    # `system_reference_price` is now exactly `final_charge_price` —
    # the formula IS the reference. Kept as a distinct field so the
    # snapshot schema  doesn't need a column drop, and so the
    # admin UI's "audit baseline" view keeps the same key shape.
    system_reference_price = final_charge_price

    # Build the breakdown from the recipe of the chosen tier — same
    # 3-row shape as the custom-template branch, with the
    # `rate_first_3_minutes` / `rate_after_3_minutes` fields for
    # replay audit. The integer-subtotal quantization helper from the
    # custom branch enforces the §4.3 sum-invariant.
    chosen_code = pro_code if pro_upgrade else flash_code
    breakdown = _build_tiered_breakdown(chosen_code, duration_minutes, final_charge_price)

    return {
        "price_source": "manual_catalog_price",
        "final_charge_price": final_charge_price,
        "flash_total": flash_total,
        "pro_total": pro_total,
        "pro_upgrade_delta": pro_upgrade_delta,
        "pricing_minutes": duration_minutes,
        "starting_price": starting_price,
        "system_reference_price": system_reference_price,
        "breakdown": breakdown,
    }


def _compute_recipe_total(tier_code: str, minutes: Decimal) -> int:
    """Sum every subflow's tiered subtotal in a manual-catalog tier
    recipe and ceil the result. Same math as
    `subflows.compute_tier_system_reference_price`, but lives here to
    avoid pulling math into `subflows.py` at module load."""
    recipe = MANUAL_CATALOG_TIER_RECIPES[tier_code]
    total = sum(
        (
            tiered_subtotal(rate_first, rate_after, minutes)
            for _, _, (rate_first, rate_after) in recipe
        ),
        Decimal(0),
    )
    return math.ceil(total)


def _build_tiered_breakdown(
    tier_code: str, minutes: Decimal, total: int
) -> list[dict[str, Any]]:
    """Emit the 3-row tiered breakdown for a manual-catalog tier, in
    the same shape the custom-template branch produces. Integer-share
    quantization enforces `sum(row.subtotal) == total` (§4.3).
    """
    recipe = MANUAL_CATALOG_TIER_RECIPES[tier_code]
    breakdown: list[dict[str, Any]] = []
    for subflow_key, display_label, (rate_first, rate_after) in recipe:
        subtotal_dec = tiered_subtotal(rate_first, rate_after, minutes)
        breakdown.append(
            {
                "subflow_key": subflow_key,
                "display_label": display_label,
                "pricing_minutes": float(minutes),
                # Legacy single-rate field for back-compat readers;
                # the tiered fields below are authoritative.
                "unit_price": rate_first,
                "rate_first_3_minutes": rate_first,
                "rate_after_3_minutes": rate_after,
                "subtotal": float(subtotal_dec),
            }
        )
    _quantize_breakdown_subtotals_to_int(breakdown, total)
    return breakdown


def _resolve_manual_catalog_combo_key(
    combo_key: str, by_code: dict[str, Any]
) -> Optional[str]:
    if combo_key in by_code:
        return combo_key
    for alias in _MANUAL_CATALOG_COMBO_ALIASES.get(combo_key, ()):
        if alias in by_code:
            return alias
    return None


def _price_custom_template(
    conn: Connection,
    *,
    custom_template_id: str,
    combo_key: str,
    valid_line_count: int,
) -> dict[str, Any]:
    """Custom-template branch (§8, updated by regression coverage): `pricing_minutes =
    valid_line_count / 25` as a Decimal (NO ceiling on minutes — only
    the final per-quote total is rounded up). Each subflow's subtotal
    uses the 3-minute tier formula from `tiered_subtotal`:

        [popular_learning]  →  <wenan by combo_key>  →  clip_data  →  video_composing

    `popular_learning` is included only when the combo has a leading
    entry in `LEADING_SUBFLOWS_BY_COMBO` (today that means just
    `secondary_creation`; original_narration / original_remix variants
    skip it for this branch). `final_charge_price = ceil(sum(subtotals))`.
    Fails closed if `combo_key` has no wenan entry — there is no
    longer a catalog-tier fallback for custom templates, since a
    missing tier used to silently produce a 1 web_point/minute quote
    because no authoritative price can be computed.

    `conn` is unused today but kept in the signature for symmetry with
    `_price_manual_catalog` and the drift-check caller.
    """
    del conn  # custom-template pricing is fully constant-table-driven

    if combo_key not in WENAN_COMBO_UNIT_PRICES:
        raise QuoteComboKeyInvalid(
            f"combo_key {combo_key!r} has no wenan unit price for custom-template quotes.",
            details={
                "combo_key": combo_key,
                "custom_template_id": custom_template_id,
            },
        )

    minutes = Decimal(valid_line_count) / Decimal(25)

    breakdown: list[dict[str, Any]] = []
    decimal_subtotals: list[Decimal] = []

    def _push(
        subflow_key: str,
        display_label: str,
        rates: tuple[int, int],
    ) -> None:
        rate_first, rate_after = rates
        subtotal_dec = tiered_subtotal(rate_first, rate_after, minutes)
        decimal_subtotals.append(subtotal_dec)
        breakdown.append(
            {
                "subflow_key": subflow_key,
                "display_label": display_label,
                "pricing_minutes": float(minutes),
                # `unit_price` kept as the legacy single-rate field for
                # back-compat with existing breakdown readers (requirement
                # "服务端日志或 snapshot 数据可查"). The new tiered
                # fields are the authoritative inputs.
                "unit_price": rate_first,
                "rate_first_3_minutes": rate_first,
                "rate_after_3_minutes": rate_after,
                "subtotal": float(subtotal_dec),
            }
        )

    for subflow_key, display_label, rates in LEADING_SUBFLOWS_BY_COMBO.get(
        combo_key, ()
    ):
        _push(subflow_key, display_label, rates)

    _push(combo_key, WENAN_COMBO_LABELS[combo_key], WENAN_COMBO_UNIT_PRICES[combo_key])

    for subflow_key, display_label, rates in FIXED_TRAILING_SUBFLOWS:
        _push(subflow_key, display_label, rates)

    # §352: ceil only at the per-quote total — subflow subtotals stay
    # fractional so a 2.8-minute quote rounds once at the boundary, not
    # once per row.
    final_charge_price = math.ceil(sum(decimal_subtotals, Decimal(0)))
    # The §4.3 invariant `sum(breakdown.subtotal) == final_charge_price`
    # is enforced in integer space — back-fill each row's `subtotal` to
    # an integer share of the rounded total. The rounding remainder
    # (final - sum(floor(s))) is added to the last row so the sum
    # matches exactly without distorting any single subflow.
    _quantize_breakdown_subtotals_to_int(breakdown, final_charge_price)

    return {
        "price_source": "system_calculated_price",
        "final_charge_price": final_charge_price,
        "flash_total": final_charge_price,
        "pro_total": final_charge_price,
        "pro_upgrade_delta": 0,
        "pricing_minutes": minutes,
        # Custom templates have no "起步价" baseline — the 4-step
        # composition IS the price, and there is no separate Flash
        # baseline to fall back to for the Pro-upgrade badge.
        "starting_price": None,
        # Custom-template price IS the system_calculated reference —
        # no separate human-set manual price exists.
        "system_reference_price": final_charge_price,
        "breakdown": breakdown,
    }


def _quantize_breakdown_subtotals_to_int(
    breakdown: list[dict[str, Any]], total: int
) -> None:
    """Convert each row's Decimal `subtotal` (currently a float) into an
    integer share so the §4.3 invariant `sum(row.subtotal) == total`
    holds in integer space. Floors every row except the last, which
    absorbs the rounding remainder. Trivially correct when `total`
    equals the un-rounded sum already.
    """
    if not breakdown:
        return
    floored = [int(Decimal(str(row["subtotal"]))) for row in breakdown]
    # `int(Decimal)` truncates toward zero — fine here because every
    # subtotal is non-negative.
    remainder = total - sum(floored)
    for idx, row in enumerate(breakdown):
        row["subtotal"] = floored[idx]
    breakdown[-1]["subtotal"] += remainder


def _charge_wallet(conn: Connection, *, user_id: int, amount: int) -> None:
    """Atomic debit of `users.balance_points` at master-task commit
    time. Issues an UPDATE with `balance_points >= :amount` in the
    WHERE so a concurrent debit cannot push the balance negative;
    rowcount==0 means the balance fell between quote-time preflight
    and commit (other order committed first), and the contract calls
    for a 402 with the freshest available figure.

    Runs in the caller's transaction — if a later step in
    `commit_master_task_snapshot` fails, the debit rolls back
    together with the snapshot insert.
    """
    if amount <= 0:
        # Free orders should never reach commit (§4 invariant), but
        # if final_charge_price ever ends up at 0 we still need to
        # not issue a no-op UPDATE that returns rowcount==0 and
        # misfires the insufficient-balance branch.
        return
    try:
        result = conn.execute(
            update(users)
            .where(users.c.id == user_id)
            .where(users.c.balance_points >= amount)
            .values(balance_points=users.c.balance_points - amount)
        )
    except SQLAlchemyError as error:
        raise QuotePersistenceError(
            "Failed to debit wallet at commit.",
            details={"error_class": error.__class__.__name__},
        ) from error
    if result.rowcount == 1:
        return
    current = _read_wallet_balance(conn, user_id)
    raise WalletInsufficientBalance(
        "Wallet balance fell below the required charge between "
        "quote and commit.",
        details={
            "required": amount,
            "available": current,
            "shortfall": max(amount - current, 0),
            "currency_unit": "web_point",
        },
    )


def _read_wallet_balance(conn: Connection, user_id: int) -> int:
    """Read `users.balance_points` for the wallet preflight. Stored
    as NUMERIC; coerce to int for the integer-web_point world.
    """
    try:
        stmt = select(users.c.balance_points).where(users.c.id == user_id)
        row = conn.execute(stmt).first()
    except SQLAlchemyError as error:
        raise QuotePersistenceError(
            "Failed to read wallet balance.",
            details={"error_class": error.__class__.__name__},
        ) from error
    if row is None:
        return 0
    val = row[0]
    if val is None:
        return 0
    try:
        return int(val)
    except (TypeError, ValueError):
        return int(Decimal(str(val)))


# ─── master-task commit ─────────────────────────────────────────────────────


_FORBIDDEN_BODY_PRICE_FIELDS = ("total", "breakdown", "final_charge_price")


def commit_master_task_snapshot(
    conn: Connection,
    *,
    quote_id: str,
    master_task_id: str,
    request_body: dict[str, Any],
    user_id: int,
    now: Optional[datetime] = None,
) -> str:
    """§6 commit logic. Returns the snapshot_id.

    Steps:
      1. Reject client-supplied total/breakdown/final_charge_price.
      2. Load the quote.
      3. Reject if the quote belongs to a different user (non-leaking
         404 — same as a truly missing quote).
      4. Validate parameter triple (combo_key + template_id_or_custom +
         srt_file_hash) matches the bound quote — runs BEFORE
         idempotency so a retry whose body drifted from the bound
         quote always raises `QUOTE_PARAMETERS_CHANGED` instead of
         silently returning an old snapshot.
      5. Idempotency: if the quote already has a snapshot, return that
         snapshot_id when it links to the same master task. If it
         links to a different task, reject as QUOTE_ALREADY_COMMITTED.
      6. Lock timing per §6.1: < 60s reuse; 60s-TTL silent re-quote;
         ≥ TTL → QuoteExpired (410).
      7. Insert snapshot row, link to master task, stamp committed_at
         on quote.

    `pricing_snapshots_v2.quote_id` carries a UNIQUE constraint so the
    DB enforces 1:1 even if a concurrent commit races past the
    application-level idempotency check. The insert is wrapped in a
    race-recovery clause so the loser of a concurrent (quote_id,
    same-task) commit returns the winner's snapshot_id instead of
    surfacing the UNIQUE collision as a 503.
    """
    when = now or _now_utc()

    for forbidden in _FORBIDDEN_BODY_PRICE_FIELDS:
        if forbidden in request_body:
            raise QuoteBodyPriceForbidden(
                f"Request body must not contain {forbidden!r}. "
                "Backend only trusts quote_id.",
                details={"forbidden_field": forbidden},
            )

    quote = get_quote(conn, quote_id)

    # Tenant isolation: a quote belongs to the user who generated it.
    # Surface a non-leaking 404 (same envelope as a missing quote) so
    # we don't disclose which quote_ids exist across tenants.
    if quote.web_user_id != user_id:
        raise QuoteNotFound(quote_id)

    if quote.custom_template_id is not None and not quote.custom_srt_file_id:
        raise QuoteParametersChanged(
            "Legacy custom-template quote is not bound to a custom_srt_file_id. "
            "Re-quote required.",
            details={
                "quote_id": quote_id,
                "custom_template_id": quote.custom_template_id,
                "custom_srt_file_id": quote.custom_srt_file_id,
            },
        )

    # §6.2 parameter quadruple — gated BEFORE the idempotency check so
    # a retry whose body drifted from the bound quote (different
    # combo_key / template / SRT file) always re-quotes rather than
    # silently returning the existing snapshot.
    #
    # `custom_srt_file_id` replaces the v1 `srt_file_hash` bound field
    # Cloud-drive file_ids are immutable once
    # `completed`/`transfer_completed`, so binding the file_id is
    # equivalent to binding the hash but doesn't require the client
    # to forward (and the server to trust) a hash value. A caller
    # who supplies a different file_id at commit gets
    # QUOTE_PARAMETERS_CHANGED — preventing the "quote SRT-A,
    # commit SRT-B" parameter-drift path.
    #
    # Template identity match : prefer `code` when both the
    # stored quote and the request body carry one — `code` is the
    # canonical cross-system identifier and `template_id` is just its
    # derived numeric suffix. Fall back to `template_id` whenever
    # either side is missing `code`, so legacy quotes (written before
    # the column existed) and legacy clients (template_id only) keep
    # binding correctly.
    submitted_code = request_body.get("code")
    use_code_match = quote.code is not None and submitted_code is not None
    if use_code_match:
        submitted_template_key = submitted_code
        bound_template_key = quote.code
        template_field_name = "code"
    else:
        submitted_template_key = request_body.get("template_id")
        bound_template_key = quote.template_id
        template_field_name = "template_id"
    submitted_triple = (
        request_body.get("combo_key"),
        submitted_template_key,
        request_body.get("custom_template_id"),
        request_body.get("custom_srt_file_id"),
    )
    bound_triple = (
        quote.combo_key,
        bound_template_key,
        quote.custom_template_id,
        quote.custom_srt_file_id,
    )
    if submitted_triple != bound_triple:
        raise QuoteParametersChanged(
            "Quote does not match the submitted parameters. Re-quote required.",
            details={
                "quote_id": quote_id,
                "match_field": template_field_name,
                "expected": {
                    "combo_key": bound_triple[0],
                    template_field_name: bound_triple[1],
                    "custom_template_id": bound_triple[2],
                    "custom_srt_file_id": bound_triple[3],
                },
                "submitted": {
                    "combo_key": submitted_triple[0],
                    template_field_name: submitted_triple[1],
                    "custom_template_id": submitted_triple[2],
                    "custom_srt_file_id": submitted_triple[3],
                },
            },
        )

    # Idempotency: same quote → same snapshot. If the snapshot already
    # exists, return it iff it links to the same master task. The
    # UNIQUE constraint on pricing_snapshots_v2.quote_id prevents two
    # rows from ever existing for the same quote.
    existing = get_snapshot_id_by_quote(conn, quote_id)
    if existing is not None:
        existing_snapshot_id, linked_task_id = existing
        if linked_task_id == master_task_id:
            return existing_snapshot_id
        raise QuoteAlreadyCommitted(
            "Quote already committed to a different master task.",
            details={
                "quote_id": quote_id,
                "linked_narrator_task_id": linked_task_id,
            },
        )

    # §6.1 lock timing
    if when >= quote.expires_at:
        raise QuoteExpired(
            "This quote has expired. Please re-quote and re-confirm.",
            details={
                "quote_id": quote_id,
                "expires_at": quote.expires_at.isoformat(),
                "submit_at": when.isoformat(),
            },
        )

    elapsed = (when - quote.created_at).total_seconds()
    if elapsed >= 60:
        # Silent re-quote inside TTL — only proceed if price unchanged.
        # For v1 we compare via a freshly computed quote (no persist).
        fresh = _recompute_for_drift_check(
            conn,
            quote=quote,
        )
        if fresh["final_charge_price"] != quote.final_charge_price:
            raise QuotePriceDrifted(
                "Price drifted between confirm-page open and submit.",
                details={
                    "quote_id": quote_id,
                    "previous_final_charge_price": quote.final_charge_price,
                    "current_final_charge_price": fresh["final_charge_price"],
                },
            )

    # Persist snapshot + link.
    snapshot_id = new_snapshot_id()
    snapshot_row = {
        "snapshot_id": snapshot_id,
        "quote_id": quote.quote_id,
        "pricing_rule_version": quote.pricing_rule_version,
        "combo_key": quote.combo_key,
        "price_source": quote.price_source,
        "template_id": quote.template_id,
        "code": quote.code,
        "custom_template_id": quote.custom_template_id,
        # regression coverage: snapshot the template duration in minutes for audit.
        # Custom template: `valid_line_count / 25` (the same Decimal the
        # quote used). Manual catalog: look up
        # `pricing_template_v2.video_duration_seconds` and convert.
        # NULL when the source is missing — admin can backfill operators data
        # without breaking commit.
        "template_duration": _resolve_snapshot_template_duration(conn, quote),
        "pricing_minutes": quote.pricing_minutes,
        "valid_line_count": quote.valid_line_count,
        "srt_file_hash": quote.srt_file_hash,
        # Audit reference price comes from the catalog tier the quote
        # locked onto, NOT the manual catalog price. Cost-mapping joins on
        # this column to detect manual-override deviations.
        "system_reference_price": quote.system_reference_price,
        "manual_catalog_price": (
            quote.final_charge_price
            if quote.price_source == "manual_catalog_price"
            else None
        ),
        "system_calculated_price": (
            quote.final_charge_price
            if quote.price_source == "system_calculated_price"
            else None
        ),
        "final_charge_price": quote.final_charge_price,
        "breakdown": quote.breakdown,
        "currency_unit": quote.currency_unit,
        "committed_at": when,
        "refund_policy": "manual",
        "refund_status": "none",
        "subflow_status": [
            {"subflow_key": item["subflow_key"], "status": "pending"}
            for item in quote.breakdown
        ],
        "web_user_id": user_id,
        "created_at": when,
    }
    try:
        insert_snapshot(conn, row=snapshot_row)
    except SnapshotQuoteCollision:
        # A concurrent commit for the same quote_id beat us to the
        # UNIQUE constraint. Re-read the snapshot and apply the same
        # idempotency rule as the fast path: same master_task_id →
        # return the winner's snapshot_id; different task → 409.
        recovered = get_snapshot_id_by_quote(conn, quote.quote_id)
        if recovered is None:
            # Either the winner rolled back between INSERT and our
            # SELECT, or there's an unrelated UNIQUE collision. Surface
            # a 503 — the caller can safely retry.
            raise QuotePersistenceError(
                "Snapshot insert collided but no row was found on retry.",
                details={},
            )
        recovered_snapshot_id, linked_task_id = recovered
        if linked_task_id == master_task_id:
            return recovered_snapshot_id
        raise QuoteAlreadyCommitted(
            "Quote already committed to a different master task.",
            details={
                "quote_id": quote.quote_id,
                "linked_narrator_task_id": linked_task_id,
            },
        )
    # Debit the wallet only on the snapshot-insert winner path.
    # Race losers + idempotent retries return earlier via
    # `get_snapshot_id_by_quote` and therefore do not re-charge.
    # On insufficient balance, the raise rolls back the snapshot
    # insert too (same transaction), leaving the quote uncommitted
    # for a topup-and-retry.
    _charge_wallet(conn, user_id=user_id, amount=quote.final_charge_price)
    attach_snapshot_to_master_task(
        conn, narrator_task_id=master_task_id, snapshot_id=snapshot_id
    )
    mark_quote_committed(conn, quote_id=quote.quote_id, committed_at=when)
    return snapshot_id


def _resolve_snapshot_template_duration(
    conn: Connection, quote: PricingQuote
) -> Optional[Decimal]:
    """Return the per-quote template duration in minutes for snapshot
    audit (requirement "snapshot 数据可查"). Custom-template quotes derive
    it from `valid_line_count / 25` — the same Decimal that drove the
    quote total. Manual-catalog quotes read the upstream-sourced
    `pricing_template_v2.video_duration_seconds` (populated by
    `scripts/refresh_v2_system_reference_price.py`); returns None when
    operators hasn't backfilled yet so a missing duration never blocks commit.
    """
    if quote.custom_template_id is not None:
        if quote.valid_line_count is None:
            return None
        return Decimal(quote.valid_line_count) / Decimal(25)
    if quote.template_id is None:
        return None
    try:
        metadata = get_template_metadata(conn, quote.template_id)
    except CatalogPersistenceError:
        # Snapshot duration is audit-only — never let a metadata read
        # failure block a commit; the catalog price path has already
        # succeeded by this point in `commit_master_task_snapshot`.
        return None
    if metadata is None or metadata.video_duration_seconds is None:
        return None
    return Decimal(metadata.video_duration_seconds) / Decimal(60)


def _recompute_for_drift_check(
    conn: Connection, *, quote: PricingQuote
) -> dict[str, Any]:
    """Re-run the pricing arithmetic against the live catalog without
    persisting. Used by the §6.1 silent re-quote to detect price
    drift between confirm-page open and submit."""
    if quote.custom_template_id is not None:
        return _price_custom_template(
            conn,
            custom_template_id=quote.custom_template_id,
            combo_key=quote.combo_key,
            valid_line_count=quote.valid_line_count or 0,
        )
    return _price_manual_catalog(
        conn,
        template_id=quote.template_id or "",
        combo_key=quote.combo_key,
        pro_upgrade=quote.pro_upgrade,
    )
