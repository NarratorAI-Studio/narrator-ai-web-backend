"""Subflow pricing table — 3-minute tiered rates (the implementation requirement).

Every subflow now carries a `(rate_first_3min, rate_after_3min)` pair.
The per-subflow subtotal is:

    min(minutes, 3) × rate_first_3min + max(minutes - 3, 0) × rate_after_3min

where `minutes` is a Decimal (template duration is kept as a fractional
value; only the final per-quote total is rounded — see requirement).

Each subflow's settlement is summed; the final charge is
`math.ceil(sum)`. The shape depends on `combo_key`:

    [popular_learning] →  <wenan combo>  →  clip_data  →  video_composing

`popular_learning` (爆款学习, image's `template_learning`) is billed
only for `secondary_creation` AND only on the custom-template branch —
the existing-template branch's derivative tier intentionally omits it
(see the implementation requirement §"二创文案，已有模板"). The wenan step is one of five
`combo_key`-selected variants (four Flash/Pro pairs plus the
axis-optional `secondary_creation`).

Most rates are flat across both tiers — the 3-min split only bites on
`clip_data` (10 → 6) and `video_composing` (7 → 4). These constants
live in code so quote computation stays deterministic across deployments.

Manual-catalog quotes (template_id branch) still surface a flat
`system_reference_price` from the catalog row; the refresh script
`scripts/refresh_v2_system_reference_price.py` pre-computes that price
using the same recipes below.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Final


# (rate_first_3min, rate_after_3min). Wenan steps are flat-tiered today;
# only clip_data and video_composing differ across the 3-min boundary.
WENAN_COMBO_UNIT_PRICES: Final[dict[str, tuple[int, int]]] = {
    "original_narration_flash": (2, 2),
    "original_narration_pro": (6, 6),
    "original_remix_flash": (5, 5),
    "original_remix_pro": (17, 17),
    "secondary_creation": (15, 15),
}

WENAN_COMBO_LABELS: Final[dict[str, str]] = {
    "original_narration_flash": "原创文案 / 纯解说 / Flash",
    "original_narration_pro": "原创文案 / 纯解说 / Pro",
    "original_remix_flash": "原创文案 / 原声混剪 / Flash",
    "original_remix_pro": "原创文案 / 原声混剪 / Pro",
    "secondary_creation": "二创文案",
}

# Combos with no Flash/Pro axis. `_validate_combo_key` must NOT enforce
# the `_flash`/`_pro` suffix rule for these.
AXIS_OPTIONAL_COMBOS: Final[frozenset[str]] = frozenset({"secondary_creation"})


# Per-combo leading subflows inserted BEFORE the wenan step (custom-
# template branch only). Combos not listed here have no leading step
# (their breakdown starts with the wenan row). Order within a tuple is
# preserved in the emitted breakdown.
LEADING_SUBFLOWS_BY_COMBO: Final[
    dict[str, tuple[tuple[str, str, tuple[int, int]], ...]]
] = {
    # (subflow_key, display_label, (rate_first_3min, rate_after_3min))
    "secondary_creation": (("popular_learning", "爆款学习", (13, 13)),),
}

# Trailing steps appended AFTER the wenan step for every combo. These
# are the two subflows whose rate drops past the 3-min mark — the
# rate-tier carries the API-minimum-spend amortization (regression coverage §rationale).
FIXED_TRAILING_SUBFLOWS: Final[tuple[tuple[str, str, tuple[int, int]], ...]] = (
    ("clip_data", "剪辑脚本", (10, 6)),
    ("video_composing", "视频合成", (7, 4)),
)


# Existing-template (manual-catalog) branch recipe per catalog `tier_code`
# (the implementation requirement). The refresh script uses this map to recompute
# `system_reference_price` per tier. Two key differences from the
# custom-template branch:
#
#   1. `derivative` (existing-template 二创) does NOT include
#      `popular_learning` — image's "二创文案，已有模板 32/25"
#      decomposes as 15 + 10 + 7 / 15 + 6 + 4 only.
#   2. The catalog uses tier_code `original_mix_*` for the family the
#      runtime calls `original_remix_*` (the v1 catalog seed renamed it;
#      see `_MANUAL_CATALOG_COMBO_ALIASES`). The mapping below is keyed
#      by tier_code, so the refresh script can look it up directly.
#
# Each entry is a tuple of (subflow_key, display_label, (rate_first, rate_after)).
MANUAL_CATALOG_TIER_RECIPES: Final[
    dict[str, tuple[tuple[str, str, tuple[int, int]], ...]]
] = {
    "original_narration_flash": (
        ("original_narration_flash", WENAN_COMBO_LABELS["original_narration_flash"], WENAN_COMBO_UNIT_PRICES["original_narration_flash"]),
        *FIXED_TRAILING_SUBFLOWS,
    ),
    "original_narration_pro": (
        ("original_narration_pro", WENAN_COMBO_LABELS["original_narration_pro"], WENAN_COMBO_UNIT_PRICES["original_narration_pro"]),
        *FIXED_TRAILING_SUBFLOWS,
    ),
    "original_mix_flash": (
        ("original_remix_flash", WENAN_COMBO_LABELS["original_remix_flash"], WENAN_COMBO_UNIT_PRICES["original_remix_flash"]),
        *FIXED_TRAILING_SUBFLOWS,
    ),
    "original_mix_pro": (
        ("original_remix_pro", WENAN_COMBO_LABELS["original_remix_pro"], WENAN_COMBO_UNIT_PRICES["original_remix_pro"]),
        *FIXED_TRAILING_SUBFLOWS,
    ),
    "derivative": (
        # No leading popular_learning — that's the custom-template-only
        # extra step (regression coverage "二创文案，已有模板" decomposition).
        ("secondary_creation", WENAN_COMBO_LABELS["secondary_creation"], WENAN_COMBO_UNIT_PRICES["secondary_creation"]),
        *FIXED_TRAILING_SUBFLOWS,
    ),
}


def tiered_subtotal(
    rate_first_3min: int, rate_after_3min: int, minutes: Decimal
) -> Decimal:
    """Compute one subflow's subtotal under the 3-minute tier formula.

        subtotal = min(minutes, 3) × rate_first
                 + max(minutes - 3, 0) × rate_after

    Keeps the result as `Decimal` so the caller can sum across subflows
    without intermediate rounding — the final ceil happens at the
    `final_charge_price` boundary, not per-row.
    """
    if minutes < Decimal(0):
        # Defensive: SRT parse never returns negative line counts, but a
        # downstream caller shouldn't be able to invert the math by
        # passing one in.
        minutes = Decimal(0)
    cap = Decimal(3)
    first = min(minutes, cap)
    after = max(minutes - cap, Decimal(0))
    return first * Decimal(rate_first_3min) + after * Decimal(rate_after_3min)


def compute_tier_system_reference_price(
    tier_code: str, duration_minutes: Decimal
) -> int:
    """Recompute the existing-template `system_reference_price` for one
    catalog tier under the 3-min tier formula. Sums each subflow in the
    recipe and rounds the total up at the aggregate boundary.

    Raises KeyError if `tier_code` has no recipe — refresh script callers
    should pre-filter to `MANUAL_CATALOG_TIER_RECIPES.keys()`.
    """
    import math as _math

    recipe = MANUAL_CATALOG_TIER_RECIPES[tier_code]
    total = sum(
        (
            tiered_subtotal(rate_first, rate_after, duration_minutes)
            for _, _, (rate_first, rate_after) in recipe
        ),
        Decimal(0),
    )
    return _math.ceil(total)


__all__ = [
    "AXIS_OPTIONAL_COMBOS",
    "FIXED_TRAILING_SUBFLOWS",
    "LEADING_SUBFLOWS_BY_COMBO",
    "MANUAL_CATALOG_TIER_RECIPES",
    "WENAN_COMBO_LABELS",
    "WENAN_COMBO_UNIT_PRICES",
    "compute_tier_system_reference_price",
    "tiered_subtotal",
]
