"""SRT realtime pricing quote for custom-template orders (Backend API contract).

Implements product requirement §定价方案 Plan ③ (原创文案 + 自定义) and Plan ④ (二创文案 +
自定义). Shares the combo_key registry and 「行数 ÷ 25 向上取整」 rule with
`pricing.hard_price_rules` (Backend API contract) so any future combo addition is
single-source.
"""

from __future__ import annotations

import re
from decimal import Decimal
from typing import Any

from .hard_price_rules import (
    CLIP_SCRIPT_RATE_PER_MINUTE,
    COMBO_RULES,
    VIDEO_SYNTHESIZE_RATE_PER_MINUTE,
    billing_minutes,
)


_SRT_INDEX_RE = re.compile(r"^\d+$")
_SRT_TIMESTAMP_RE = re.compile(
    r"^\d{2}:\d{2}:\d{2}[,.]\d{1,3}"
    r"\s*-->\s*"
    r"\d{2}:\d{2}:\d{2}[,.]\d{1,3}"
    r"(?:\s+.*)?$"
)


TREND_LEARNING_RATE_PER_MINUTE = Decimal("13")
SECONDARY_CREATION_GENERATE_TEXT_RATE_PER_MINUTE = Decimal("15")


class SrtRealtimeError(Exception):
    """Domain error from SRT realtime pricing. `code` maps to the public
    error envelope; `http_status` selects the HTTP response status."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        http_status: int = 400,
        retryable: bool = False,
        details: dict[str, Any] | None = None,
    ):
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status
        self.retryable = retryable
        self.details = details or {}


def parse_srt(payload: str) -> tuple[int, int]:
    """Returns (text_chars, text_lines) from raw SRT content.

    Per product requirement §六.7 the count is over **cue blocks** (字幕行数), not raw
    newlines, and the legacy 字符÷15 (`calculate_text_lines`) method is
    forbidden. CRLF is normalized to LF.
    """
    if not isinstance(payload, str) or not payload.strip():
        raise SrtRealtimeError(
            "SRT_INVALID",
            "srt_payload is empty",
            details={"hint": "payload must contain at least one SRT cue"},
        )

    normalized = payload.replace("\r\n", "\n").replace("\r", "\n").strip()
    blocks = [block for block in normalized.split("\n\n") if block.strip()]

    text_lines = 0
    text_chars = 0
    for position, block in enumerate(blocks, start=1):
        lines = [line for line in block.split("\n") if line.strip()]
        if len(lines) < 3:
            raise SrtRealtimeError(
                "SRT_INVALID",
                f"SRT cue #{position} has fewer than 3 non-empty lines",
                details={
                    "cue_position": position,
                    "hint": "each cue requires <index>, <timestamp>, and at least one dialogue line",
                },
            )
        if not _SRT_INDEX_RE.match(lines[0].strip()):
            raise SrtRealtimeError(
                "SRT_INVALID",
                f"SRT cue #{position} has non-numeric index {lines[0]!r}",
                details={
                    "cue_position": position,
                    "field": "index",
                    "hint": "cue index must be a positive integer",
                },
            )
        if not _SRT_TIMESTAMP_RE.match(lines[1].strip()):
            raise SrtRealtimeError(
                "SRT_INVALID",
                f"SRT cue #{position} has malformed timestamp {lines[1]!r}",
                details={
                    "cue_position": position,
                    "field": "timestamp",
                    "hint": "timestamp must match HH:MM:SS,mmm --> HH:MM:SS,mmm",
                },
            )
        dialogue_lines = [line.strip() for line in lines[2:] if line.strip()]
        if not dialogue_lines:
            raise SrtRealtimeError(
                "SRT_INVALID",
                f"SRT cue #{position} has no dialogue",
                details={"cue_position": position, "field": "dialogue"},
            )
        text_lines += 1
        for line in dialogue_lines:
            text_chars += len(line)

    if text_lines == 0:
        raise SrtRealtimeError(
            "SRT_INVALID",
            "no valid SRT cues parsed",
            details={"hint": "expected `<index>\\n<timestamp>\\n<dialogue>` blocks"},
        )

    return text_chars, text_lines


def _validate_metrics(srt_metrics: Any) -> tuple[int, int]:
    if not isinstance(srt_metrics, dict):
        raise SrtRealtimeError(
            "SRT_INVALID",
            "srt_metrics must be an object",
            details={"hint": "expected {text_chars: int, text_lines: int}"},
        )
    text_chars = srt_metrics.get("text_chars")
    text_lines = srt_metrics.get("text_lines")
    if (
        not isinstance(text_chars, int)
        or isinstance(text_chars, bool)
        or text_chars <= 0
    ):
        raise SrtRealtimeError(
            "SRT_INVALID",
            "srt_metrics.text_chars must be a positive integer",
            details={"field": "srt_metrics.text_chars"},
        )
    if (
        not isinstance(text_lines, int)
        or isinstance(text_lines, bool)
        or text_lines <= 0
    ):
        raise SrtRealtimeError(
            "SRT_INVALID",
            "srt_metrics.text_lines must be a positive integer",
            details={"field": "srt_metrics.text_lines"},
        )
    return text_chars, text_lines


def _resolve_metrics(
    srt_payload: str | None, srt_metrics: dict[str, Any] | None
) -> tuple[int, int]:
    if srt_metrics is not None:
        return _validate_metrics(srt_metrics)
    if srt_payload is None:
        raise SrtRealtimeError(
            "SRT_INVALID",
            "either srt_payload or srt_metrics must be provided",
            details={"hint": "include srt_payload (raw SRT) or srt_metrics object"},
        )
    return parse_srt(srt_payload)


def _build_breakdown(
    combo_key: str, text_chars: int, minutes: int
) -> list[dict[str, Any]]:
    breakdown: list[dict[str, Any]] = []
    if combo_key == "secondary_creation":
        breakdown.append(
            {
                "item": "trend_learning",
                "points": (
                    Decimal(minutes) * TREND_LEARNING_RATE_PER_MINUTE
                ).quantize(Decimal("0.01")),
            }
        )
        breakdown.append(
            {
                "item": "generate_text",
                "points": (
                    Decimal(minutes) * SECONDARY_CREATION_GENERATE_TEXT_RATE_PER_MINUTE
                ).quantize(Decimal("0.01")),
            }
        )
    else:
        rule = COMBO_RULES[combo_key]
        rate: Decimal = rule["rate"]  # type: ignore[assignment]
        breakdown.append(
            {
                "item": "generate_text",
                "points": (
                    Decimal(text_chars) / Decimal(1000) * rate
                ).quantize(Decimal("0.01")),
            }
        )
    breakdown.append(
        {
            "item": "clip_script",
            "points": (
                Decimal(minutes) * CLIP_SCRIPT_RATE_PER_MINUTE
            ).quantize(Decimal("0.01")),
        }
    )
    breakdown.append(
        {
            "item": "video_synthesize",
            "points": (
                Decimal(minutes) * VIDEO_SYNTHESIZE_RATE_PER_MINUTE
            ).quantize(Decimal("0.01")),
        }
    )
    return breakdown


def compute_srt_realtime_quote(
    combo_key: Any,
    *,
    srt_payload: str | None = None,
    srt_metrics: dict[str, Any] | None = None,
    pricing_rule_version: int = 1,
    correlation_id: str | None = None,
) -> dict[str, Any]:
    if not isinstance(combo_key, str) or not combo_key:
        raise SrtRealtimeError(
            "BAD_REQUEST",
            "combo_key is required",
            details={"field": "combo_key"},
        )
    if combo_key not in COMBO_RULES:
        raise SrtRealtimeError(
            "SRT_UNSUPPORTED_MODE",
            f"combo_key '{combo_key}' is not supported",
            details={"combo_key": combo_key, "allowed": list(COMBO_RULES.keys())},
        )

    text_chars, text_lines = _resolve_metrics(srt_payload, srt_metrics)
    minutes = billing_minutes(text_lines)
    breakdown = _build_breakdown(combo_key, text_chars, minutes)
    estimated_points = sum(
        (item["points"] for item in breakdown), start=Decimal("0")
    ).quantize(Decimal("0.01"))

    return {
        "combo_key": combo_key,
        "estimated_points": estimated_points,
        "pricing_rule_version": pricing_rule_version,
        "srt_metrics": {
            "text_chars": text_chars,
            "text_lines": text_lines,
            "billing_minutes": minutes,
        },
        "breakdown": breakdown,
        "correlation_id": correlation_id,
    }
