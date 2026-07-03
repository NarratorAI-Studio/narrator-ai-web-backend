"""Hard-price computation rules for fa_template_price backfill and verification.

The formulas implement product requirement §定价方案 (public-example-doc, public example revision):
- Plan ① (原创文案 + 模板库): per-kchar rate × text_chars + duration-based clip/synth.
- Plan ② (二创文案 + 模板库): per-minute rate × billing minutes for all three line items.

`billing_minutes(text_lines)` uses the canonical 「行数 ÷ 25, 向上取整」 rule
from product requirement §五.5 and is shared by quote/pin  and SRT realtime quote .
"""

from __future__ import annotations

from decimal import Decimal
from math import ceil
from typing import Mapping


CLIP_SCRIPT_RATE_PER_MINUTE = Decimal("7")
VIDEO_SYNTHESIZE_RATE_PER_MINUTE = Decimal("5")
LINES_PER_MINUTE = 25


COMBO_RULES: Mapping[str, Mapping[str, object]] = {
    "original_narration_flash": {
        "mode": "per_kchar",
        "rate": Decimal("5"),
    },
    "original_narration_pro": {
        "mode": "per_kchar",
        "rate": Decimal("15"),
    },
    "original_remix_flash": {
        "mode": "per_kchar",
        "rate": Decimal("12"),
    },
    "original_remix_pro": {
        "mode": "per_kchar",
        "rate": Decimal("40"),
    },
    "secondary_creation": {
        "mode": "per_minute",
        "rate": Decimal("15"),
    },
}


def billing_minutes(text_lines: int) -> int:
    if text_lines <= 0:
        raise ValueError("text_lines must be a positive integer")
    return ceil(Decimal(text_lines) / Decimal(LINES_PER_MINUTE))


def compute_hard_price(combo_key: str, text_chars: int, text_lines: int) -> Decimal:
    if combo_key not in COMBO_RULES:
        raise ValueError(f"unknown combo_key: {combo_key}")
    if text_chars <= 0:
        raise ValueError("text_chars must be a positive integer")
    rule = COMBO_RULES[combo_key]
    minutes = billing_minutes(text_lines)
    rate: Decimal = rule["rate"]  # type: ignore[assignment]
    if rule["mode"] == "per_kchar":
        generate_text = Decimal(text_chars) / Decimal(1000) * rate
    else:
        generate_text = Decimal(minutes) * rate
    clip_script = Decimal(minutes) * CLIP_SCRIPT_RATE_PER_MINUTE
    video_synthesize = Decimal(minutes) * VIDEO_SYNTHESIZE_RATE_PER_MINUTE
    return (generate_text + clip_script + video_synthesize).quantize(Decimal("0.01"))


def all_combo_keys() -> tuple[str, ...]:
    return tuple(COMBO_RULES.keys())
