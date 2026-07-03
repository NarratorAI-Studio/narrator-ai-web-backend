"""Save-side validation for template price v2 catalog upserts .

All invariants enforced here are defined in
`docs/pricing/v2/catalog-tier-contract.md` §3-§7. Validations run
against the MERGED set of `(submitted tiers ∪ latest-enabled tiers
not in submission)` so an operator can update only one quality
(e.g. just Pro) and have it validated against the existing other
quality.

The route layer composes these helpers into a single transaction
so all 5 tiers (or however many are submitted) commit atomically.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from .errors import (
    CatalogFlashProAxisViolation,
    CatalogProBelowFlash,
    CatalogProSurchargeMismatch,
    CatalogRoundingVersionUnknown,
    CatalogValidationError,
)


# Whitelist of rounding rule versions the server will accept on
# upsert. Adding a new version is a deliberate deploy-time decision —
# silently accepting unknown versions would let historical audit join
# on a rounding rule we can't replay.
ALLOWED_ROUNDING_RULE_VERSIONS: frozenset[str] = frozenset(
    {
        "v2.0-round-half-up",
        # `v2.0-backfill-from-manual` per contract §10 — only used by
        # the one-shot v1→v2 backfill script; accepting it via the
        # admin upsert path would let operators paper over real prices
        # as "backfilled" and bypass margin audit. Whitelisted in the
        # backfill script's own SQL, not here.
    }
)


# Required fields on every tier payload. Optional ones (e.g.
# `pro_surcharge_display`) are conditionally validated per tier.
_REQUIRED_FIELDS: tuple[str, ...] = (
    "tier_code",
    "product_line",
    "flash_pro_axis",
    "manual_price",
    "system_reference_price",
    "raw_rate",
    "final_rate",
    "rounding_rule_version",
)


def validate_tier_payload_shape(payload: dict[str, Any]) -> None:
    """Per-tier structural validation. Raises CatalogValidationError on
    missing required fields, wrong types, or out-of-range values. Does
    NOT cross-check Flash vs Pro — that's `validate_invariants`.
    """
    missing = [f for f in _REQUIRED_FIELDS if f not in payload]
    if missing:
        raise CatalogValidationError(
            f"Tier payload missing required fields: {missing}.",
            details={"missing": missing, "tier_code": payload.get("tier_code")},
        )

    axis = payload["flash_pro_axis"]
    if axis not in ("required", "optional"):
        raise CatalogValidationError(
            f"flash_pro_axis must be 'required' or 'optional', got {axis!r}.",
            details={"flash_pro_axis": axis, "tier_code": payload.get("tier_code")},
        )

    # axis=optional ⇒ mode + quality must be null (contract §3.1).
    # Pre-validate in route to surface CATALOG_FLASH_PRO_AXIS_VIOLATION
    # cleanly; the DDL CHECK also enforces but its IntegrityError
    # would surface as 503 via the persistence-error path.
    if axis == "optional":
        if payload.get("mode") is not None or payload.get("quality") is not None:
            raise CatalogFlashProAxisViolation(
                "flash_pro_axis='optional' requires mode and quality to be null.",
                details={
                    "tier_code": payload.get("tier_code"),
                    "mode": payload.get("mode"),
                    "quality": payload.get("quality"),
                },
            )

    if not isinstance(payload["manual_price"], int) or payload["manual_price"] < 0:
        raise CatalogValidationError(
            "manual_price must be a non-negative integer.",
            details={
                "tier_code": payload.get("tier_code"),
                "manual_price": payload.get("manual_price"),
            },
        )

    if not isinstance(payload["system_reference_price"], int) or (
        payload["system_reference_price"] < 0
    ):
        raise CatalogValidationError(
            "system_reference_price must be a non-negative integer.",
            details={
                "tier_code": payload.get("tier_code"),
                "system_reference_price": payload.get("system_reference_price"),
            },
        )

    if payload.get("pro_surcharge_display") is not None:
        if (
            not isinstance(payload["pro_surcharge_display"], int)
            or payload["pro_surcharge_display"] < 0
        ):
            raise CatalogValidationError(
                "pro_surcharge_display must be a non-negative integer when present.",
                details={
                    "tier_code": payload.get("tier_code"),
                    "pro_surcharge_display": payload.get("pro_surcharge_display"),
                },
            )


def validate_rounding_rule_version(version: str, *, tier_code: str) -> None:
    """Reject unknown rounding_rule_version values. See module docstring."""
    if version not in ALLOWED_ROUNDING_RULE_VERSIONS:
        raise CatalogRoundingVersionUnknown(
            f"rounding_rule_version {version!r} is not on the server-side whitelist.",
            details={
                "tier_code": tier_code,
                "rounding_rule_version": version,
                "allowed": sorted(ALLOWED_ROUNDING_RULE_VERSIONS),
            },
        )


def validate_invariants(merged_tiers: list[dict[str, Any]]) -> None:
    """Cross-tier invariants on the merged Flash + Pro set:

    §4.1: `pro.manual_price == flash.manual_price + pro.pro_surcharge_display`
    §4.2: `pro.manual_price >= flash.manual_price`

    Per `(product_line, mode)` pair. Tiers with no matching Flash/Pro
    counterpart in the merged set are skipped (e.g. derivative with
    flash_pro_axis='optional').
    """
    by_pair_flash: dict[tuple[str, str | None], dict[str, Any]] = {}
    by_pair_pro: dict[tuple[str, str | None], dict[str, Any]] = {}
    for t in merged_tiers:
        key = (t["product_line"], t.get("mode"))
        if t.get("quality") == "flash":
            by_pair_flash[key] = t
        elif t.get("quality") == "pro":
            by_pair_pro[key] = t

    for key, pro in by_pair_pro.items():
        flash = by_pair_flash.get(key)
        if flash is None:
            # Pro without matching Flash in the merged set — can't
            # validate the invariant. Skip rather than 422; the
            # operator may be doing a partial save with the matching
            # Flash already in the catalog under different naming.
            # (Defense-in-depth: realistically the merge step ensures
            # both will be present when both have rows.)
            continue
        if pro["manual_price"] < flash["manual_price"]:
            raise CatalogProBelowFlash(
                "Pro tier manual_price must be >= the same template's Flash tier price.",
                details={
                    "product_line": key[0],
                    "mode": key[1],
                    "flash_tier_code": flash["tier_code"],
                    "flash_manual_price": flash["manual_price"],
                    "pro_tier_code": pro["tier_code"],
                    "pro_manual_price": pro["manual_price"],
                },
            )
        surcharge = pro.get("pro_surcharge_display")
        if surcharge is None:
            raise CatalogProSurchargeMismatch(
                "Pro tier must declare pro_surcharge_display.",
                details={
                    "tier_code": pro["tier_code"],
                    "product_line": key[0],
                    "mode": key[1],
                },
            )
        expected_pro = flash["manual_price"] + surcharge
        if pro["manual_price"] != expected_pro:
            raise CatalogProSurchargeMismatch(
                "Pro full price must equal Flash full price plus pro_surcharge_display.",
                details={
                    "product_line": key[0],
                    "mode": key[1],
                    "flash_manual_price": flash["manual_price"],
                    "pro_surcharge_display": surcharge,
                    "expected_pro_manual_price": expected_pro,
                    "actual_pro_manual_price": pro["manual_price"],
                },
            )


def compute_manual_override_warning(
    manual_price: int, system_reference_price: int
) -> bool:
    """Per regression coverage §7: True when `|manual - reference| / reference >= 0.30`.

    Guarded against divide-by-zero — when reference is 0, any non-zero
    manual is 100% off and counted as a warning. Reference == 0 AND
    manual == 0 is treated as not-warned.
    """
    if system_reference_price == 0:
        return manual_price != 0
    ratio = abs(Decimal(manual_price) - Decimal(system_reference_price)) / Decimal(
        system_reference_price
    )
    return ratio >= Decimal("0.30")


def merge_for_invariant_check(
    submitted: list[dict[str, Any]],
    existing_latest: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """Combine the submitted payload with the latest-enabled existing
    tiers for `tier_code`s the payload doesn't touch. Submitted always
    wins. Returns a flat list of tier dicts suitable for
    `validate_invariants`.
    """
    submitted_codes = {t["tier_code"] for t in submitted}
    merged = list(submitted)
    for tier_code, existing in existing_latest.items():
        if tier_code in submitted_codes:
            continue
        merged.append(
            {
                "tier_code": existing["tier_code"],
                "product_line": existing["product_line"],
                "mode": existing.get("mode"),
                "quality": existing.get("quality"),
                "flash_pro_axis": existing["flash_pro_axis"],
                "manual_price": int(existing["manual_price"]),
                "pro_surcharge_display": (
                    int(existing["pro_surcharge_display"])
                    if existing.get("pro_surcharge_display") is not None
                    else None
                ),
                "system_reference_price": int(existing["system_reference_price"]),
            }
        )
    return merged
