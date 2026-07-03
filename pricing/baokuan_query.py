"""SQL helpers for the upstream movie-baokuan endpoint.

`GET /pricing/movie-baokuan` receives external xy-codes from upstream and
returns only templates with a complete v2 catalog. The join key is:

    upstream item.code ("xy0046") -> pricing_template_v2.template_id ("46")

The v1 `fa_template_price` table is intentionally not used here anymore.
"""

from __future__ import annotations

from collections.abc import Iterable
from decimal import Decimal
from typing import Any

from sqlalchemy import bindparam, select, true

from db.tables import pricing_catalog_v2_entry, pricing_template_v2


REQUIRED_TIER_CODES = (
    "derivative",
    "original_mix_flash",
    "original_mix_pro",
    "original_narration_flash",
    "original_narration_pro",
)


def template_id_from_baokuan_code(code: object) -> str | None:
    """Convert upstream xy-code to the v2 template id string.

    Examples:
      - "xy0046" -> "46"
      - "xy1" -> "1"

    Null, malformed, and non-positive ids do not join.
    """
    if not isinstance(code, str):
        return None
    normalized = code.strip().lower()
    if not normalized.startswith("xy"):
        return None
    suffix = normalized[2:]
    if not suffix.isdigit():
        return None
    template_num = int(suffix)
    if template_num <= 0:
        return None
    return str(template_num)


def select_v2_catalog_tiers_by_template_ids(template_ids: Iterable[str]):
    """Build a SELECT for latest enabled v2 catalog rows.

    The statement may return multiple historical rows per
    (template_id, tier_code); `group_v2_catalog_tiers_by_template_id` keeps the
    highest `effective_version` because this query is ordered DESC.
    """
    template_id_list = [tid for tid in template_ids if tid]
    return (
        select(
            pricing_catalog_v2_entry.c.template_id,
            pricing_catalog_v2_entry.c.tier_code,
            pricing_catalog_v2_entry.c.product_line,
            pricing_catalog_v2_entry.c.mode,
            pricing_catalog_v2_entry.c.quality,
            pricing_catalog_v2_entry.c.flash_pro_axis,
            pricing_catalog_v2_entry.c.manual_price,
            pricing_catalog_v2_entry.c.pro_surcharge_display,
            pricing_catalog_v2_entry.c.system_reference_price,
            pricing_catalog_v2_entry.c.currency_unit,
            pricing_catalog_v2_entry.c.raw_rate,
            pricing_catalog_v2_entry.c.final_rate,
            pricing_catalog_v2_entry.c.rounding_rule_version,
            pricing_catalog_v2_entry.c.manual_override_warning,
            pricing_catalog_v2_entry.c.effective_version,
        )
        .select_from(
            pricing_catalog_v2_entry.join(
                pricing_template_v2,
                pricing_template_v2.c.template_id
                == pricing_catalog_v2_entry.c.template_id,
            )
        )
        .where(pricing_template_v2.c.enabled.is_(true()))
        .where(pricing_catalog_v2_entry.c.enabled.is_(true()))
        .where(
            pricing_catalog_v2_entry.c.template_id.in_(
                bindparam("template_ids", value=template_id_list, expanding=True)
            )
        )
        .order_by(
            pricing_catalog_v2_entry.c.template_id,
            pricing_catalog_v2_entry.c.tier_code,
            pricing_catalog_v2_entry.c.effective_version.desc(),
        )
    )


def group_v2_catalog_tiers_by_template_id(
    rows: Iterable[dict],
) -> dict[str, dict[str, Any]]:
    """Collapse long-form v2 catalog rows into complete tier maps.

    Incomplete templates are omitted entirely, matching the v2 cutover
    contract for `/pricing/movie-baokuan`.
    """
    grouped: dict[str, dict[str, Any]] = {}
    for row in rows:
        template_id = str(row["template_id"])
        tier_code = row["tier_code"]
        if tier_code not in REQUIRED_TIER_CODES:
            continue
        entry = grouped.setdefault(template_id, {"tiers": {}})
        # First-seen wins because SELECT orders effective_version DESC.
        if tier_code in entry["tiers"]:
            continue
        entry["tiers"][tier_code] = _serialize_tier(row)

    complete: dict[str, dict[str, Any]] = {}
    required = set(REQUIRED_TIER_CODES)
    for template_id, entry in grouped.items():
        tiers = entry["tiers"]
        if set(tiers) != required:
            continue
        versions = sorted({tier["pricing_rule_version"] for tier in tiers.values()})
        complete[template_id] = {
            "tiers": tiers,
            "pricing_rule_version": versions[0] if len(versions) == 1 else versions,
        }
    return complete


def _serialize_tier(row: dict) -> dict[str, Any]:
    return {
        "tier_code": row["tier_code"],
        "product_line": row["product_line"],
        "mode": row["mode"],
        "quality": row["quality"],
        "flash_pro_axis": row["flash_pro_axis"],
        "manual_price": int(row["manual_price"]),
        "pro_surcharge_display": (
            int(row["pro_surcharge_display"])
            if row["pro_surcharge_display"] is not None
            else None
        ),
        "system_reference_price": int(row["system_reference_price"]),
        "currency_unit": row["currency_unit"],
        "raw_rate": _to_decimal(row["raw_rate"]),
        "final_rate": _to_decimal(row["final_rate"]),
        "pricing_rule_version": row["rounding_rule_version"],
        "manual_override_warning": bool(row["manual_override_warning"]),
        "effective_version": int(row["effective_version"]),
    }


def _to_decimal(value: Any) -> Decimal:
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))
