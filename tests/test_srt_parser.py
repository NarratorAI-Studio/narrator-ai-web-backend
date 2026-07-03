"""Tests for ``pricing.srt_parser.count_valid_lines``.

Each test covers one of the rules frozen in Web API contract
§ Confirmed Specifications. The parser is intentionally tolerant
(never raises) so all edge cases assert on the returned count.
"""

from __future__ import annotations

from pricing.srt_parser import count_valid_lines


def test_empty_input_returns_zero():
    assert count_valid_lines("") == 0


def test_whitespace_only_lines_not_counted():
    srt = "\n   \n\t\n   \t  \n"
    assert count_valid_lines(srt) == 0


def test_sequence_numbers_not_counted():
    srt = "1\n2\n3\n42\n"
    assert count_valid_lines(srt) == 0


def test_time_axis_lines_not_counted():
    # Standard comma separator + the dot variant some tools emit.
    srt = (
        "00:00:00,000 --> 00:00:01,000\n"
        "00:00:01.500 --> 00:00:02.000\n"
        "01:23:45,678 --> 01:23:46,789\n"
    )
    assert count_valid_lines(srt) == 0


def test_punctuation_only_lines_not_counted():
    # ASCII + CJK punctuation; both are Unicode 'P*' categories.
    srt = "...\n!!!\n。。。\n？！\n（）\n"
    assert count_valid_lines(srt) == 0


def test_single_punctuation_char_not_counted():
    # Single '.' is punctuation-only, not ultra-short text.
    assert count_valid_lines(".\n") == 0
    assert count_valid_lines("。\n") == 0


def test_normal_text_lines_counted():
    srt = "Hello world\nGoodbye world\n你好世界\n"
    assert count_valid_lines(srt) == 3


def test_ultra_short_text_lines_counted():
    # Single non-punctuation char (Latin + CJK).
    srt = "a\nb\n啊\n好\n"
    assert count_valid_lines(srt) == 4


def test_text_with_trailing_punctuation_counted_as_text():
    # First non-whitespace non-punctuation char makes the line text.
    srt = "Wow!\n好。\n"
    assert count_valid_lines(srt) == 2


def test_repeated_time_axis_does_not_dedup_text():
    # Same time axis appears twice; the text of each block counts.
    srt = (
        "1\n"
        "00:00:00,000 --> 00:00:01,000\n"
        "First text\n"
        "\n"
        "2\n"
        "00:00:00,000 --> 00:00:01,000\n"
        "Second text\n"
    )
    assert count_valid_lines(srt) == 2


def test_happy_path_multi_block_fixture():
    # 3 cue blocks; lines 4 (in block 1: 2, block 2: 1, block 3: 2).
    srt = (
        "1\n"
        "00:00:00,000 --> 00:00:02,000\n"
        "Hello, world!\n"
        "Hope this is useful.\n"
        "\n"
        "2\n"
        "00:00:02,500 --> 00:00:04,000\n"
        "Second subtitle.\n"
        "\n"
        "3\n"
        "00:00:04,500 --> 00:00:06,000\n"
        "Third subtitle\n"
        "spans two lines.\n"
    )
    assert count_valid_lines(srt) == 5


def test_malformed_sequence_lines_not_counted():
    # "Looks like an SRT sequence number but has trailing content" is
    # ambiguously structural per spec regression coverage → Malformed ❌. Only the
    # clean text line counts.
    srt = (
        "1 garbage\n"  # malformed sequence: digit + space + content
        "42 some text\n"  # malformed sequence
        "abc def\n"  # plain text → counts
    )
    assert count_valid_lines(srt) == 1


def test_malformed_time_axis_lines_not_counted():
    # Contains the SRT arrow "-->" but doesn't match the strict
    # time-axis shape → Malformed ❌.
    srt = (
        "00:00 --> 00:01\n"  # partial timestamps
        "--> abc\n"  # arrow-only-ish
        "real subtitle line\n"  # text → counts
    )
    assert count_valid_lines(srt) == 1


def test_dialogue_beginning_with_digit_then_space_is_skipped_per_strict_spec():
    # Documented trade-off: a real dialogue line like "2026 was a
    # good year" will be classified as malformed-sequence and skipped.
    # If product later relaxes the rule, this test will surface that
    # change in CI. Per spec regression coverage strict interpretation, the
    # conservative behavior (skip) is correct because over-counting
    # would over-charge the user.
    assert count_valid_lines("2026 was a good year\n") == 0
    # Dialogue that has a digit-prefixed FIRST WORD but no following
    # token (e.g. "2026!") is single-token, so MALFORMED_SEQUENCE_RE
    # does not match; this counts as text.
    assert count_valid_lines("2026!\n") == 1
    # Dialogue with digit but not as first token counts.
    assert count_valid_lines("It's 2026\n") == 1


def test_mixed_line_endings():
    # CRLF + LF + bare CR — splitlines() handles them all.
    srt = (
        "1\r\n00:00:00,000 --> 00:00:01,000\r\nText A\r\n\r\n"
        "2\n00:00:02,000 --> 00:00:03,000\nText B\n"
    )
    assert count_valid_lines(srt) == 2


def test_unicode_text_counted():
    srt = "Café\nαβγ\n日本語\nРусский\nمرحبا\n"
    assert count_valid_lines(srt) == 5


def test_symbol_lines_classified_as_text():
    # Music indicators (♪), arrows (→), stars (★), emoji are Unicode
    # 'S*' categories — NOT punctuation. Per current rule scope they
    # classify as text. If product later decides "符号行 also skip",
    # this test will surface the change.
    srt = "♪\n→\n★★★\n"
    assert count_valid_lines(srt) == 3


def test_decimal_millisecond_separator_in_time_axis_skipped():
    # Existing pricing.srt_realtime uses both '.' and ',' for the ms
    # separator; we match that to stay tolerant of upstream tooling.
    srt = (
        "1\n"
        "00:00:00.000 --> 00:00:01.000\n"
        "Real text\n"
    )
    assert count_valid_lines(srt) == 1
