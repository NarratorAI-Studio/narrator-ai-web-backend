"""SRT parser for template price v2 custom-template pricing.

Counts "valid subtitle text lines" in a raw SRT input per the rules
used by the /pricing/quote custom-template branch.

Distinct from `pricing.srt_realtime.parse_srt`:

- `srt_realtime.parse_srt` returns `(text_chars, cue_block_count)` and
  raises `SrtRealtimeError` on any malformed block. It is v1 pricing's
  strict cue-level parser.
- `srt_parser.count_valid_lines` returns the per-text-LINE count
  (a cue with two dialogue lines contributes 2, not 1) and is
  intentionally tolerant: malformed lines are simply not counted, never
  raised. This matches the v2 spec, which separates parsing from
  pricing-time error handling — the caller decides what to do when the
  count is unreasonable (e.g. 0).

The parser is line-by-line (not cue-block-aware) so:

- a missing sequence number or stray time axis doesn't drop the
  surrounding text from the count;
- repeated time axes are a non-issue (each appearance is skipped
  wherever it lands, and the text that follows each appearance is
  counted independently — explicitly required by the spec);
- the rule set reads as one predicate per row type.
"""

from __future__ import annotations

import re
import unicodedata


# SRT time axis: HH:MM:SS,mmm --> HH:MM:SS,mmm
# Accepts both ',' (standard SRT) and '.' (some tools emit this) as the
# millisecond separator. Surrounding whitespace tolerated.
_TIME_AXIS_RE = re.compile(
    r"^\s*\d{1,2}:\d{2}:\d{2}[,.]\d{1,3}\s*-->\s*\d{1,2}:\d{2}:\d{2}[,.]\d{1,3}\s*$"
)

# SRT sequence number: a single non-negative integer on its own line.
_SEQUENCE_RE = re.compile(r"^\s*\d+\s*$")

# "Looks like an SRT sequence number but has trailing content" — e.g.
# `"1 garbage"`, `"42 some text"`. The pure-digit prefix + at least one
# non-whitespace char after the gap means the line is ambiguously
# structural; per spec regression coverage this is malformed, not text.
_MALFORMED_SEQUENCE_RE = re.compile(r"^\s*\d+\s+\S")

# "Contains the SRT time-axis arrow `-->` but does not match the strict
# time-axis shape" — e.g. partial timestamps, arrow-only lines. Per
# spec regression coverage these are malformed, not text.
_TIME_ARROW = "-->"


def _is_punctuation_only(stripped: str) -> bool:
    """True iff every non-whitespace character in `stripped` is
    punctuation (Unicode general category starts with 'P': Pc, Pd, Pe,
    Pf, Pi, Po, Ps). Caller must pass an already-non-empty stripped
    string; we still guard against the all-whitespace edge case.

    Symbols (categories 'S*' like '★', '→', '♪', emoji) are NOT
    treated as punctuation here — they classify as text. The spec only
    excludes "纯标点行"; symbols are intentionally not in scope.
    """
    has_any = False
    for ch in stripped:
        if ch.isspace():
            continue
        has_any = True
        if not unicodedata.category(ch).startswith("P"):
            return False
    return has_any


def count_valid_lines(srt_text: str) -> int:
    """Return the number of valid subtitle text lines in `srt_text`.

    Rules (frozen in Web API contract § Confirmed Specifications):

    | Input row                                  | Counted? |
    |--------------------------------------------|----------|
    | Empty / whitespace-only                    |    no    |
    | SRT sequence number (bare integer)         |    no    |
    | Time axis (HH:MM:SS,mmm --> HH:MM:SS,mmm)  |    no    |
    | Punctuation-only                           |    no    |
    | Normal subtitle text                       |    yes   |
    | Ultra-short text (single char etc.)        |    yes   |
    | Repeated time axis                         | text after each occurrence counts (no dedup) |

    Malformed lines — those that look ambiguously structural but don't
    cleanly parse into sequence/time-axis — are NOT counted, per spec
    regression coverage ("Malformed line ❌"). Specifically:

    - ``"1 garbage"``, ``"42 some text"``: first token is a pure-digit
      stem suggesting SRT sequence, but trailing content means the
      writer's intent is unclear. Skipped.
    - ``"00:00 --> 00:01"``, ``"--> abc"``: contains the SRT time-axis
      arrow ``-->`` but fails the strict time-axis shape. Skipped.

    The trade-off: dialogue lines that genuinely begin with a digit
    followed by space (e.g. ``"2026 was a good year"``) will be
    classified as malformed and excluded. This matches the spec's
    strict interpretation and over-counting would inflate
    ``pricing_minutes`` (over-charge the user). The opposite trade
    (count anything that looks even vaguely text-like) would risk
    overbilling; strict-skip is the conservative side.

    Returns 0 for empty input or all-structural input. Never raises.
    Caller decides what an unreasonable count (e.g. 0) means for
    pricing.
    """
    count = 0
    for line in srt_text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if _TIME_AXIS_RE.match(line):
            continue
        if _SEQUENCE_RE.match(line):
            continue
        if _MALFORMED_SEQUENCE_RE.match(line):
            continue
        if _TIME_ARROW in stripped:
            # Contains the SRT arrow but didn't match the strict
            # time-axis regex above — malformed time axis.
            continue
        if _is_punctuation_only(stripped):
            continue
        count += 1
    return count
