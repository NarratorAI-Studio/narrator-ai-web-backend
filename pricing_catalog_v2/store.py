"""Read layer for the v2 catalog.

Resolves the effective tier list for a given template per the
catalog-tier-contract §6 inheritance rule:

    effective_tier(template_id, tier_code) =
        template-level catalog entry, if one exists; else
        family-level entry × template's tier_multiplier; else
        null (= price not configured)

Rounding of inherited prices uses the SAME rounding_rule_version
that the source family entry carries, so historical audit can
re-derive the effective price without rate drift.

All read functions take an open SQLAlchemy Connection. The route layer
owns the connection lifecycle.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Optional

from sqlalchemy import func, insert, select
from sqlalchemy.engine import Connection
from sqlalchemy.exc import SQLAlchemyError

from db.tables import (
    pricing_catalog_v2_entry,
    pricing_catalog_v2_family,
    pricing_template_v2,
)

from .errors import (
    CatalogInheritedInvariantViolation,
    CatalogPersistenceError,
    CatalogTemplateNotFound,
    CatalogTierMissing,
)
from .validators import (
    compute_manual_override_warning,
    merge_for_invariant_check,
    validate_invariants,
    validate_rounding_rule_version,
    validate_tier_payload_shape,
)


@dataclass(frozen=True)
class TemplateMetadata:
    """The per-template row in `pricing_template_v2`."""

    template_id: str
    template_family_id: Optional[str]
    tier_multiplier: Decimal
    enabled: bool
    # The implementation requirement: upstream identity. Nullable when the seeder hasn't
    # run yet or the template predates res_movie_baokuan.
    code: Optional[str]
    name: Optional[str]
    learning_model_id: Optional[str]
    # The implementation requirement — video duration (seconds) used by the 3-min tiered
    # system_reference_price formula. Nullable while operators backfill.
    video_duration_seconds: Optional[int]


@dataclass(frozen=True)
class EffectiveTier:
    """A single resolved tier entry, after family fallback + multiplier."""

    tier_code: str
    source: str  # "template" or "family"
    catalog_entry_id: Optional[str]  # set when source = "template"
    product_line: str
    mode: Optional[str]
    quality: Optional[str]
    flash_pro_axis: str
    manual_price: int  # effective (multiplier applied if source = family)
    manual_price_raw: int  # pre-multiplier; equals manual_price when source = template
    pro_surcharge_display: Optional[int]
    system_reference_price: int  # effective (multiplier applied if source = family)
    system_reference_price_raw: int  # pre-multiplier
    currency_unit: str
    raw_rate: Decimal
    final_rate: Decimal
    rounding_rule_version: str
    manual_override_warning: bool
    effective_version: int


# --- public read API ---


def get_template_tiers(
    conn: Connection, template_id: str
) -> dict[str, dict[str, Any]]:
    """Return {tier_code: row} for the latest enabled template-level
    catalog entries for `template_id`. Empty dict when none exist.

    Public surface for callers that need only template-level entries.
    Wraps `_load_current_template_entries`.
    """
    return _load_current_template_entries(conn, template_id)


def get_family_tiers(
    conn: Connection, family_id: str
) -> dict[str, dict[str, Any]]:
    """Return {tier_code: row} for the latest enabled family-level
    catalog entries for `family_id`. Empty dict when none exist.

    Public surface for callers that need only family-level entries.
    Wraps `_load_current_family_entries`.
    """
    return _load_current_family_entries(conn, family_id)


def get_template_metadata(
    conn: Connection, template_id: str
) -> Optional[TemplateMetadata]:
    """Return the `pricing_template_v2` row for this template, or None."""
    try:
        stmt = select(pricing_template_v2).where(
            pricing_template_v2.c.template_id == template_id
        )
        row = conn.execute(stmt).mappings().first()
    except SQLAlchemyError as error:
        raise CatalogPersistenceError(
            "Failed to read template metadata.",
            details={"error_class": error.__class__.__name__},
        ) from error
    if row is None:
        return None
    return TemplateMetadata(
        template_id=row["template_id"],
        template_family_id=row["template_family_id"],
        tier_multiplier=_to_decimal(row["tier_multiplier"]),
        enabled=bool(row["enabled"]),
        code=row["code"],
        name=row["name"],
        learning_model_id=row["learning_model_id"],
        video_duration_seconds=(
            int(row["video_duration_seconds"])
            if row.get("video_duration_seconds") is not None
            else None
        ),
    )


def resolve_effective_tiers(
    conn: Connection, template_id: str
) -> tuple[TemplateMetadata, list[EffectiveTier]]:
    """Return the metadata + the effective tier list for a template.

    Resolution order per contract §6:
      1. For each tier_code, prefer the latest enabled template-level entry.
      2. Otherwise fall back to the latest enabled family-level entry,
         scaled by the template's `tier_multiplier`.
      3. If neither exists for ANY tier, raise `CatalogTierMissing`.

    Inheritance is per-tier: a template can have some tier_codes from
    its template-level entries and others inherited from the family.

    The route layer maps `CatalogTierMissing` to HTTP 404 with
    `code: CATALOG_TIER_MISSING`; `CatalogPersistenceError` maps to 503.
    """
    metadata = get_template_metadata(conn, template_id)
    if metadata is None:
        raise CatalogTierMissing(template_id)

    template_rows = _load_current_template_entries(conn, template_id)
    family_rows = (
        _load_current_family_entries(conn, metadata.template_family_id)
        if metadata.template_family_id
        else {}
    )

    tier_codes = set(template_rows) | set(family_rows)
    if not tier_codes:
        raise CatalogTierMissing(template_id)

    multiplier = metadata.tier_multiplier
    tiers: list[EffectiveTier] = []
    for tier_code in sorted(tier_codes):
        if tier_code in template_rows:
            tiers.append(_template_row_to_tier(template_rows[tier_code]))
        else:
            tiers.append(
                _family_row_to_tier(family_rows[tier_code], multiplier=multiplier)
            )

    # Second pass: re-derive Pro surcharge for inherited Pro tiers from
    # the effective Pro/Flash price diff, then validate the §4.1
    # invariant. Independent scaling of `pro_surcharge_display` in the
    # family path drifts off the invariant under rounding. Template-level Pro tiers stay
    # as-typed; the operator-side `CATALOG_PRO_SURCHARGE_MISMATCH`
    # check at upsert covers that case.
    tiers = _validate_and_derive_inherited_surcharges(
        tiers, template_id=template_id
    )

    return metadata, tiers


# --- internal helpers ---


def _load_current_template_entries(
    conn: Connection, template_id: str
) -> dict[str, dict[str, Any]]:
    """Return {tier_code: latest enabled row} for template-level entries."""
    try:
        stmt = (
            select(pricing_catalog_v2_entry)
            .where(
                pricing_catalog_v2_entry.c.template_id == template_id,
                pricing_catalog_v2_entry.c.enabled.is_(True),
            )
            .order_by(pricing_catalog_v2_entry.c.effective_version.desc())
        )
        rows = conn.execute(stmt).mappings().all()
    except SQLAlchemyError as error:
        raise CatalogPersistenceError(
            "Failed to read template-level catalog entries.",
            details={"error_class": error.__class__.__name__},
        ) from error
    latest: dict[str, dict[str, Any]] = {}
    for row in rows:
        tier_code = row["tier_code"]
        # First-seen wins because the query is ordered DESC by version.
        if tier_code not in latest:
            latest[tier_code] = dict(row)
    return latest


def _load_current_family_entries(
    conn: Connection, family_id: str
) -> dict[str, dict[str, Any]]:
    """Return {tier_code: latest enabled row} for a family."""
    try:
        stmt = (
            select(pricing_catalog_v2_family)
            .where(
                pricing_catalog_v2_family.c.template_family_id == family_id,
                pricing_catalog_v2_family.c.enabled.is_(True),
            )
            .order_by(pricing_catalog_v2_family.c.effective_version.desc())
        )
        rows = conn.execute(stmt).mappings().all()
    except SQLAlchemyError as error:
        raise CatalogPersistenceError(
            "Failed to read family-level catalog entries.",
            details={"error_class": error.__class__.__name__},
        ) from error
    latest: dict[str, dict[str, Any]] = {}
    for row in rows:
        tier_code = row["tier_code"]
        if tier_code not in latest:
            latest[tier_code] = dict(row)
    return latest


def _template_row_to_tier(row: dict[str, Any]) -> EffectiveTier:
    manual_price = int(row["manual_price"])
    system_reference_price = int(row["system_reference_price"])
    return EffectiveTier(
        tier_code=row["tier_code"],
        source="template",
        catalog_entry_id=row["catalog_entry_id"],
        product_line=row["product_line"],
        mode=row["mode"],
        quality=row["quality"],
        flash_pro_axis=row["flash_pro_axis"],
        manual_price=manual_price,
        manual_price_raw=manual_price,
        pro_surcharge_display=(
            int(row["pro_surcharge_display"])
            if row["pro_surcharge_display"] is not None
            else None
        ),
        system_reference_price=system_reference_price,
        system_reference_price_raw=system_reference_price,
        currency_unit=row["currency_unit"],
        raw_rate=_to_decimal(row["raw_rate"]),
        final_rate=_to_decimal(row["final_rate"]),
        rounding_rule_version=row["rounding_rule_version"],
        manual_override_warning=bool(row["manual_override_warning"]),
        effective_version=int(row["effective_version"]),
    )


def _family_row_to_tier(row: dict[str, Any], *, multiplier: Decimal) -> EffectiveTier:
    """Scale a family row by `multiplier`.

    Note: `pro_surcharge_display` here is the **provisional** inherited
    value (independently scaled + rounded). It may drift from the
    `pro.manual_price - flash.manual_price` invariant after rounding;
    `_validate_and_derive_inherited_surcharges` runs a second pass over
    the full tier list to re-derive the correct surcharge (and fail
    loudly if the drift is real, not a rounding artifact). See
    the catalog inheritance contract.

    `raw_rate` and `final_rate` are also scaled by the multiplier with
    a SINGLE rounding step — rate is per-minute
    pricing; if a template is 1.25× the family's prices, the per-minute
    rate it advertises is also 1.25× the family's.
    """
    raw_manual = int(row["manual_price"])
    raw_reference = int(row["system_reference_price"])
    rrv = row["rounding_rule_version"]
    family_raw_rate = _to_decimal(row["raw_rate"])
    # Scale the unrounded rate first, then round ONCE. Avoids
    # the "rounded family rate × multiplier rounded again" double-round
    # that loses precision.
    scaled_raw_rate = family_raw_rate * multiplier
    scaled_final_rate = _apply_multiplier_to_decimal(scaled_raw_rate, rrv)
    return EffectiveTier(
        tier_code=row["tier_code"],
        source="family",
        catalog_entry_id=None,
        product_line=row["product_line"],
        mode=row["mode"],
        quality=row["quality"],
        flash_pro_axis=row["flash_pro_axis"],
        manual_price=_apply_multiplier(raw_manual, multiplier, rrv),
        manual_price_raw=raw_manual,
        pro_surcharge_display=(
            _apply_multiplier(int(row["pro_surcharge_display"]), multiplier, rrv)
            if row["pro_surcharge_display"] is not None
            else None
        ),
        system_reference_price=_apply_multiplier(raw_reference, multiplier, rrv),
        system_reference_price_raw=raw_reference,
        currency_unit=row["currency_unit"],
        raw_rate=scaled_raw_rate,
        final_rate=scaled_final_rate,
        rounding_rule_version=rrv,
        manual_override_warning=bool(row["manual_override_warning"]),
        effective_version=int(row["effective_version"]),
    )


def _validate_and_derive_inherited_surcharges(
    tiers: list[EffectiveTier], *, template_id: str
) -> list[EffectiveTier]:
    """For every inherited Pro tier with a matching Flash tier (same
    `product_line` + `mode`), re-derive `pro_surcharge_display` from
    the effective price diff and raise on invariant violation.

    Template-level Pro tiers are skipped — their surcharge was typed
    by the operator and validated at upsert via
    `CATALOG_PRO_SURCHARGE_MISMATCH`. Family-inherited Pro tiers can
    only fail this check on read, after rounding multiplies through.
    """
    flash_index: dict[tuple[str, str | None], EffectiveTier] = {}
    for t in tiers:
        if t.quality == "flash":
            flash_index[(t.product_line, t.mode)] = t

    corrected: list[EffectiveTier] = []
    for t in tiers:
        if t.quality != "pro" or t.source != "family":
            corrected.append(t)
            continue
        flash = flash_index.get((t.product_line, t.mode))
        if flash is None:
            # No matching Flash to compare against — keep inherited surcharge.
            corrected.append(t)
            continue
        # Per-tier inheritance is contract-supported: template
        # can override only Flash and let Pro fall back to family. In that
        # case the family-row's stored surcharge (based on family Flash)
        # MUST NOT be compared against the effective Pro/template-Flash
        # diff — they're from different scopes and disagreement is
        # expected, not a violation.
        # The correct behavior on read is to ALWAYS overwrite the
        # response surcharge with the effective price diff so callers
        # see a consistent Pro = Flash + surcharge for the rendered
        # tier list. Family-row-internal §4.1 errors are caught at the
        # family upsert path, not on read.
        derived = t.manual_price - flash.manual_price
        # The one read-side guard we DO keep: derived < 0 means
        # effective Pro is cheaper than effective Flash, which violates
        # the non-negative surcharge invariant AND would leak a negative
        # value to the response. Surface as
        # CATALOG_INHERITED_INVARIANT_VIOLATION so admin tooling can
        # flag the malformed inheritance.
        if derived < 0:
            raise CatalogInheritedInvariantViolation(
                template_id=template_id,
                product_line=t.product_line,
                mode=t.mode,
                flash_manual_price=flash.manual_price,
                pro_manual_price=t.manual_price,
                inherited_surcharge=t.pro_surcharge_display,
                derived_surcharge=derived,
            )
        corrected.append(
            EffectiveTier(
                tier_code=t.tier_code,
                source=t.source,
                catalog_entry_id=t.catalog_entry_id,
                product_line=t.product_line,
                mode=t.mode,
                quality=t.quality,
                flash_pro_axis=t.flash_pro_axis,
                manual_price=t.manual_price,
                manual_price_raw=t.manual_price_raw,
                pro_surcharge_display=derived,
                system_reference_price=t.system_reference_price,
                system_reference_price_raw=t.system_reference_price_raw,
                currency_unit=t.currency_unit,
                raw_rate=t.raw_rate,
                final_rate=t.final_rate,
                rounding_rule_version=t.rounding_rule_version,
                manual_override_warning=t.manual_override_warning,
                effective_version=t.effective_version,
            )
        )
    return corrected


def _apply_multiplier(
    value: int, multiplier: Decimal, rounding_rule_version: str
) -> int:
    """Scale an integer `value` by `multiplier` and round to an integer
    per the rule version. Used for prices (manual_price,
    system_reference_price, pro_surcharge_display) which are integer
    web_points.

    Unknown rule versions fall back to half-up because misbehaving
    silently is worse than a stale rounding choice; the alarm comes
    from the audit which scans `rounding_rule_version` per row.
    """
    scaled = Decimal(value) * multiplier
    # v2.0-round-half-up: ROUND_HALF_UP semantics on the integer.
    # Other rule versions can branch here when they're added.
    return int(scaled.quantize(Decimal("1"), rounding="ROUND_HALF_UP"))


def _apply_multiplier_to_decimal(
    scaled: Decimal, rounding_rule_version: str
) -> Decimal:
    """Round a pre-scaled Decimal `scaled` per the rule version.

    Used for `final_rate` which is allowed to carry one decimal place
    (per catalog contract §5.1: integer when rate >= 1, 1-dp when
    0 < rate < 1 to avoid collapsing into 0).
    """
    if scaled >= 1:
        return scaled.quantize(Decimal("1"), rounding="ROUND_HALF_UP")
    return scaled.quantize(Decimal("0.1"), rounding="ROUND_HALF_UP")


def _to_decimal(value: Any) -> Decimal:
    if isinstance(value, Decimal):
        return value
    if value is None:
        return Decimal(0)
    return Decimal(str(value))


# --- write API  ------------------------------------------------------


def _derive_pro_surcharge_in_place(
    submitted_tiers: list[dict[str, Any]], merged: list[dict[str, Any]]
) -> None:
    """The implementation requirement — for every submitted Pro tier missing
    `pro_surcharge_display`, derive it as `pro.manual_price -
    flash.manual_price` from the matching Flash tier in the merged view
    (current submission ∪ existing catalog), and write it back into
    BOTH `submitted_tiers` (so the INSERT carries the value) and
    `merged` (so `validate_invariants` sees a complete payload).

    If no matching Flash exists anywhere, raise `CatalogProNeedsFlash`
    so the caller gets a distinct 422 envelope instead of the
    confusing "pro_surcharge_display required" message that
    `validate_invariants` would emit.

    Submissions that already carry the field are left untouched —
    `validate_invariants` will still reject mismatches under
    `CATALOG_PRO_SURCHARGE_MISMATCH`.
    """
    from .errors import CatalogProNeedsFlash

    flash_by_pair: dict[tuple[str, str | None], dict[str, Any]] = {}
    for t in merged:
        if t.get("quality") == "flash":
            flash_by_pair[(t["product_line"], t.get("mode"))] = t

    # Index merged-view Pro entries so the same derivation flows into
    # both the submitted dict (for INSERT) and its merged counterpart
    # (for validate_invariants).
    merged_by_code = {t["tier_code"]: t for t in merged}

    for tier in submitted_tiers:
        if tier.get("quality") != "pro":
            continue
        if tier.get("pro_surcharge_display") is not None:
            continue
        key = (tier["product_line"], tier.get("mode"))
        flash = flash_by_pair.get(key)
        if flash is None:
            raise CatalogProNeedsFlash(
                "Cannot derive pro_surcharge_display: no matching Flash "
                "tier in the submission or existing catalog.",
                details={
                    "tier_code": tier["tier_code"],
                    "product_line": key[0],
                    "mode": key[1],
                },
            )
        derived = int(tier["manual_price"]) - int(flash["manual_price"])
        tier["pro_surcharge_display"] = derived
        merged_view = merged_by_code.get(tier["tier_code"])
        if merged_view is not None:
            merged_view["pro_surcharge_display"] = derived


def _apply_omitted_field_defaults(tier: dict[str, Any]) -> dict[str, Any]:
    """For regression coverage: when the admin UI omits raw_rate / final_rate /
    rounding_rule_version on a tier submission, fill them with the
    only values they have ever held in practice. Submissions that
    already carry the fields pass through unchanged.

    `raw_rate` and `final_rate` default to `Decimal(manual_price)` —
    for integer manual_prices the round-half-up rule is a no-op, so
    `final_rate == raw_rate == manual_price`. `rounding_rule_version`
    defaults to the supported whitelist value
    (`v2.0-round-half-up`); the explicit selector in the UI was
    redundant.

    If `manual_price` itself is missing, leave raw_rate / final_rate
    untouched — `validate_tier_payload_shape` will then raise the
    clearer "manual_price required" error rather than a confusing
    KeyError from this helper.
    """
    out = dict(tier)
    out.setdefault("rounding_rule_version", "v2.0-round-half-up")
    if "manual_price" in out:
        out.setdefault("raw_rate", Decimal(out["manual_price"]))
        out.setdefault("final_rate", Decimal(out["manual_price"]))
    return out


def upsert_tiers(
    conn: Connection,
    *,
    template_id: str,
    submitted_tiers: list[dict[str, Any]],
    updated_by: int,
) -> list[dict[str, Any]]:
    """Atomic batch upsert of submitted tiers for a template.

    Steps (all inside the caller's transaction):

    1. Verify `pricing_template_v2.template_id` exists → else
       `CatalogTemplateNotFound`.
    2. Per-tier structural validation (shape, types, range, axis
       pairing) → `CatalogValidationError` / `CatalogFlashProAxisViolation`.
    3. Rounding-rule-version whitelist → `CatalogRoundingVersionUnknown`.
    4. Load the latest enabled rows for tier_codes the payload does
       NOT touch; merge with submitted for invariant validation.
    5. Run §4.1 + §4.2 invariants on the merged set →
       `CatalogProBelowFlash` / `CatalogProSurchargeMismatch`.
    6. For each submitted tier, allocate `effective_version =
       max(existing for that tier) + 1`, compute
       `manual_override_warning`, set timestamps + `updated_by`,
       INSERT new row.
    7. Return the freshly inserted rows so the caller can echo them
       to the operator.

    The caller is responsible for the connection lifecycle and for
    committing the transaction on success.
    """
    if not submitted_tiers:
        from .errors import CatalogValidationError

        raise CatalogValidationError(
            "Submission must contain at least one tier.",
            details={"submitted_count": 0},
        )

    # The implementation requirement: admin UI now hides raw_rate / final_rate /
    # rounding_rule_version since they're operationally meaningless.
    # Fill server-side defaults BEFORE validation so the rest of the
    # pipeline still sees a complete payload. Submissions that DO
    # carry these fields (legacy or scripted) are passed through
    # unchanged.
    submitted_tiers = [_apply_omitted_field_defaults(t) for t in submitted_tiers]

    # (1) template must exist
    try:
        template_stmt = select(pricing_template_v2.c.template_id).where(
            pricing_template_v2.c.template_id == template_id
        )
        template_row = conn.execute(template_stmt).first()
    except SQLAlchemyError as error:
        raise CatalogPersistenceError(
            "Failed to verify template existence on upsert.",
            details={"error_class": error.__class__.__name__},
        ) from error
    if template_row is None:
        raise CatalogTemplateNotFound(template_id)

    # (2) per-tier structural validation
    for tier in submitted_tiers:
        validate_tier_payload_shape(tier)
        validate_rounding_rule_version(
            tier["rounding_rule_version"], tier_code=tier["tier_code"]
        )

    # (3+4) load existing latest-enabled tiers NOT in submission for
    # merge-time invariant check.
    existing_latest = _load_current_template_entries(conn, template_id)
    merged = merge_for_invariant_check(submitted_tiers, existing_latest)
    # The implementation requirement: for Pro tiers omitting pro_surcharge_display, derive
    # from the matching Flash in the merged view before invariant
    # validation. Mutates submitted_tiers + merged in place so the
    # downstream INSERT and the invariant check both see the derived
    # value. Raises CATALOG_PRO_NEEDS_FLASH if no Flash exists at all.
    _derive_pro_surcharge_in_place(submitted_tiers, merged)
    validate_invariants(merged)

    # (5) allocate effective_version + insert
    now = datetime.now(timezone.utc)
    inserted_rows: list[dict[str, Any]] = []
    try:
        # Bulk-lookup of max existing version per submitted tier_code,
        # one round-trip. SQLite + Postgres compatible.
        tier_codes = [t["tier_code"] for t in submitted_tiers]
        version_stmt = (
            select(
                pricing_catalog_v2_entry.c.tier_code,
                func.max(pricing_catalog_v2_entry.c.effective_version).label(
                    "max_version"
                ),
            )
            .where(
                pricing_catalog_v2_entry.c.template_id == template_id,
                pricing_catalog_v2_entry.c.tier_code.in_(tier_codes),
            )
            .group_by(pricing_catalog_v2_entry.c.tier_code)
        )
        version_rows = conn.execute(version_stmt).all()
        next_versions = {row.tier_code: int(row.max_version) + 1 for row in version_rows}

        for tier in submitted_tiers:
            tier_code = tier["tier_code"]
            new_version = next_versions.get(tier_code, 1)
            warning = compute_manual_override_warning(
                tier["manual_price"], tier["system_reference_price"]
            )
            row = {
                "catalog_entry_id": _new_catalog_entry_id(),
                "template_id": template_id,
                "tier_code": tier_code,
                "product_line": tier["product_line"],
                "mode": tier.get("mode"),
                "quality": tier.get("quality"),
                "flash_pro_axis": tier["flash_pro_axis"],
                "manual_price": tier["manual_price"],
                "pro_surcharge_display": tier.get("pro_surcharge_display"),
                "system_reference_price": tier["system_reference_price"],
                "currency_unit": tier.get("currency_unit", "web_point"),
                "raw_rate": _to_decimal(tier["raw_rate"]),
                "final_rate": _to_decimal(tier["final_rate"]),
                "rounding_rule_version": tier["rounding_rule_version"],
                "manual_override_warning": warning,
                "enabled": True,
                "effective_version": new_version,
                "created_at": now,
                "updated_at": now,
                "updated_by": str(updated_by),
            }
            conn.execute(insert(pricing_catalog_v2_entry).values(**row))
            inserted_rows.append(row)
    except SQLAlchemyError as error:
        raise CatalogPersistenceError(
            "Failed to write catalog upsert.",
            details={"error_class": error.__class__.__name__},
        ) from error

    return inserted_rows


def list_all_versions(
    conn: Connection, template_id: str
) -> dict[str, list[dict[str, Any]]]:
    """Return all versions of all tiers for `template_id`, including
    disabled rows, grouped by tier_code and ordered DESC by
    effective_version. Used by GET /pricing/catalog/{template_id}/history
    to render the admin audit trail (requirement).

    Empty dict when no rows exist; the route layer maps that to
    `CatalogTierMissing` so 404 still differentiates "no history" from
    "history empty".
    """
    try:
        stmt = (
            select(pricing_catalog_v2_entry)
            .where(pricing_catalog_v2_entry.c.template_id == template_id)
            .order_by(
                pricing_catalog_v2_entry.c.tier_code,
                pricing_catalog_v2_entry.c.effective_version.desc(),
            )
        )
        rows = conn.execute(stmt).mappings().all()
    except SQLAlchemyError as error:
        raise CatalogPersistenceError(
            "Failed to read catalog history.",
            details={"error_class": error.__class__.__name__},
        ) from error

    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        tier_code = row["tier_code"]
        grouped.setdefault(tier_code, []).append(dict(row))
    return grouped


def _new_catalog_entry_id() -> str:
    """UUID4 as plain hex string. Matches the TEXT primary key column."""
    return str(uuid.uuid4())
