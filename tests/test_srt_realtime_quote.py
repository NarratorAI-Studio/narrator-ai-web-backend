"""Tests for SRT realtime pricing quote (Backend API contract).

Covers product requirement §定价方案 Plan ③ (原创文案 + 自定义) and Plan ④ (二创文案 +
自定义), plus the 4 documented error classes from
docs/srt-realtime-pricing-api.md.
"""

from __future__ import annotations

from decimal import Decimal

import pytest


SAMPLE_SRT = """1
00:00:00,000 --> 00:00:02,500
大家好，欢迎来到第一集。

2
00:00:02,500 --> 00:00:05,000
今天我们来讲一个故事。

3
00:00:05,000 --> 00:00:07,500
故事的主角是一只小猫。
"""


# ---------- parser ----------


def test_parse_srt_counts_cues_not_newlines():
    from pricing.srt_realtime import parse_srt

    text_chars, text_lines = parse_srt(SAMPLE_SRT)

    assert text_lines == 3
    assert text_chars == sum(
        len(s)
        for s in ["大家好，欢迎来到第一集。", "今天我们来讲一个故事。", "故事的主角是一只小猫。"]
    )


def test_parse_srt_normalizes_crlf():
    from pricing.srt_realtime import parse_srt

    crlf = SAMPLE_SRT.replace("\n", "\r\n")

    assert parse_srt(crlf) == parse_srt(SAMPLE_SRT)


def test_parse_srt_rejects_empty_payload():
    from pricing.srt_realtime import SrtRealtimeError, parse_srt

    with pytest.raises(SrtRealtimeError) as exc:
        parse_srt("")
    assert exc.value.code == "SRT_INVALID"
    assert exc.value.http_status == 400


def test_parse_srt_rejects_payload_with_no_cues():
    from pricing.srt_realtime import SrtRealtimeError, parse_srt

    with pytest.raises(SrtRealtimeError) as exc:
        parse_srt("not an srt file just one line")
    assert exc.value.code == "SRT_INVALID"


def test_parse_srt_rejects_non_numeric_index():
    """regression coverage/regression coverage: malformed cue index must be rejected."""
    from pricing.srt_realtime import SrtRealtimeError, parse_srt

    malformed = "abc\n00:00:00,000 --> 00:00:01,000\ntext\n"

    with pytest.raises(SrtRealtimeError) as exc:
        parse_srt(malformed)
    assert exc.value.code == "SRT_INVALID"
    assert exc.value.details["field"] == "index"
    assert exc.value.details["cue_position"] == 1


def test_parse_srt_rejects_malformed_timestamp():
    """regression coverage/regression coverage: malformed timestamp must be rejected."""
    from pricing.srt_realtime import SrtRealtimeError, parse_srt

    malformed = "1\nnot-a-timestamp\ntext\n"

    with pytest.raises(SrtRealtimeError) as exc:
        parse_srt(malformed)
    assert exc.value.code == "SRT_INVALID"
    assert exc.value.details["field"] == "timestamp"


def test_parse_srt_rejects_cue_with_empty_dialogue():
    """A cue with only index + timestamp (no dialogue) is malformed."""
    from pricing.srt_realtime import SrtRealtimeError, parse_srt

    malformed = "1\n00:00:00,000 --> 00:00:01,000\n"  # truncated

    with pytest.raises(SrtRealtimeError) as exc:
        parse_srt(malformed)
    assert exc.value.code == "SRT_INVALID"


def test_parse_srt_accepts_period_separator_in_timestamp():
    """Some SRT exporters use `.` instead of `,` for milliseconds — common
    variant accepted by major players. Parser tolerates both."""
    from pricing.srt_realtime import parse_srt

    payload = "1\n00:00:00.000 --> 00:00:01.000\ntext\n"
    text_chars, text_lines = parse_srt(payload)
    assert text_lines == 1
    assert text_chars == 4


# ---------- formula ----------


@pytest.mark.parametrize(
    "combo_key,text_chars,text_lines,expected",
    [
        # Plan ③ canonical example from product requirement: 2800 chars + 52 lines + Flash = 50.00
        ("original_narration_flash", 2800, 52, Decimal("50.00")),
        # Plan ④ canonical example from product requirement: 52 lines + secondary_creation = 120.00
        ("secondary_creation", 1, 52, Decimal("120.00")),
        # Same numerator across other Plan ③ modes
        ("original_narration_pro", 2800, 52, Decimal("78.00")),
        ("original_remix_flash", 2800, 52, Decimal("69.60")),
        ("original_remix_pro", 2800, 52, Decimal("148.00")),
    ],
)
def test_compute_srt_realtime_quote_matches_prd_examples(
    combo_key, text_chars, text_lines, expected
):
    from pricing.srt_realtime import compute_srt_realtime_quote

    quote = compute_srt_realtime_quote(
        combo_key,
        srt_metrics={"text_chars": text_chars, "text_lines": text_lines},
    )

    assert quote["combo_key"] == combo_key
    assert quote["estimated_points"] == expected
    assert quote["srt_metrics"]["text_chars"] == text_chars
    assert quote["srt_metrics"]["text_lines"] == text_lines


def test_secondary_creation_breakdown_includes_trend_learning_line_item():
    """Plan ④ has 4 line items; Plan ③ has 3."""
    from pricing.srt_realtime import compute_srt_realtime_quote

    quote = compute_srt_realtime_quote(
        "secondary_creation", srt_metrics={"text_chars": 1, "text_lines": 52}
    )

    items = [item["item"] for item in quote["breakdown"]]
    assert items == [
        "trend_learning",
        "generate_text",
        "clip_script",
        "video_synthesize",
    ]


def test_plan3_breakdown_omits_trend_learning():
    from pricing.srt_realtime import compute_srt_realtime_quote

    quote = compute_srt_realtime_quote(
        "original_narration_flash",
        srt_metrics={"text_chars": 2800, "text_lines": 52},
    )

    items = [item["item"] for item in quote["breakdown"]]
    assert items == ["generate_text", "clip_script", "video_synthesize"]


def test_compute_quote_rejects_unsupported_combo_key():
    from pricing.srt_realtime import SrtRealtimeError, compute_srt_realtime_quote

    with pytest.raises(SrtRealtimeError) as exc:
        compute_srt_realtime_quote(
            "legacy_advanced",
            srt_metrics={"text_chars": 100, "text_lines": 5},
        )
    assert exc.value.code == "SRT_UNSUPPORTED_MODE"
    assert exc.value.http_status == 400
    assert "allowed" in exc.value.details


def test_compute_quote_accepts_raw_srt_payload():
    from pricing.srt_realtime import compute_srt_realtime_quote

    quote = compute_srt_realtime_quote(
        "original_narration_flash", srt_payload=SAMPLE_SRT
    )

    assert quote["srt_metrics"]["text_lines"] == 3
    assert quote["estimated_points"] > 0


def test_compute_quote_requires_either_payload_or_metrics():
    from pricing.srt_realtime import SrtRealtimeError, compute_srt_realtime_quote

    with pytest.raises(SrtRealtimeError) as exc:
        compute_srt_realtime_quote("original_narration_flash")
    assert exc.value.code == "SRT_INVALID"


def test_compute_quote_rejects_negative_metrics():
    from pricing.srt_realtime import SrtRealtimeError, compute_srt_realtime_quote

    with pytest.raises(SrtRealtimeError) as exc:
        compute_srt_realtime_quote(
            "original_narration_flash",
            srt_metrics={"text_chars": 0, "text_lines": 5},
        )
    assert exc.value.code == "SRT_INVALID"
    assert exc.value.details["field"] == "srt_metrics.text_chars"


def test_compute_quote_round_trips_correlation_id_and_version():
    from pricing.srt_realtime import compute_srt_realtime_quote

    quote = compute_srt_realtime_quote(
        "secondary_creation",
        srt_metrics={"text_chars": 1, "text_lines": 25},
        pricing_rule_version=7,
        correlation_id="browser-request-xyz",
    )

    assert quote["pricing_rule_version"] == 7
    assert quote["correlation_id"] == "browser-request-xyz"


# ---------- endpoint ----------


@pytest.fixture()
def srt_client():
    import server

    return server.app.test_client()


def test_endpoint_returns_200_for_canonical_plan_3_example(srt_client):
    response = srt_client.post(
        "/pricing/srt-realtime-quote",
        json={
            "combo_key": "original_narration_flash",
            "srt_metrics": {"text_chars": 2800, "text_lines": 52},
            "correlation_id": "trace-001",
        },
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["success"] is True
    data = payload["data"]
    assert data["combo_key"] == "original_narration_flash"
    assert Decimal(str(data["estimated_points"])) == Decimal("50.00")
    assert data["srt_metrics"] == {
        "text_chars": 2800,
        "text_lines": 52,
        "billing_minutes": 3,
    }
    assert data["correlation_id"] == "trace-001"


def test_endpoint_returns_400_for_srt_invalid(srt_client):
    response = srt_client.post(
        "/pricing/srt-realtime-quote",
        json={"combo_key": "secondary_creation", "srt_payload": ""},
    )

    assert response.status_code == 400
    payload = response.get_json()
    assert payload["success"] is False
    assert payload["error"]["code"] == "SRT_INVALID"


def test_endpoint_returns_400_for_srt_unsupported_mode(srt_client):
    response = srt_client.post(
        "/pricing/srt-realtime-quote",
        json={
            "combo_key": "legacy_advanced",
            "srt_metrics": {"text_chars": 100, "text_lines": 5},
        },
    )

    assert response.status_code == 400
    payload = response.get_json()
    assert payload["error"]["code"] == "SRT_UNSUPPORTED_MODE"
    assert "allowed" in payload["error"]["details"]


def test_endpoint_returns_400_for_non_json_body(srt_client):
    response = srt_client.post(
        "/pricing/srt-realtime-quote",
        data="not-json",
        content_type="text/plain",
    )

    assert response.status_code == 400
    payload = response.get_json()
    assert payload["error"]["code"] == "BAD_REQUEST"


def test_endpoint_returns_400_for_malformed_srt_cue(srt_client):
    """regression coverage: malformed cue at endpoint surface returns SRT_INVALID."""
    response = srt_client.post(
        "/pricing/srt-realtime-quote",
        json={
            "combo_key": "original_narration_flash",
            "srt_payload": "1\nnot-a-timestamp\ntext\n",
        },
    )

    assert response.status_code == 400
    payload = response.get_json()
    assert payload["error"]["code"] == "SRT_INVALID"
    assert payload["error"]["details"]["field"] == "timestamp"


def test_endpoint_returns_503_for_invalid_pricing_rule_version_env(
    monkeypatch, srt_client
):
    """regression coverage: invalid PRICING_RULE_VERSION → 503 JSON envelope,
    not an undocumented Flask 500."""
    monkeypatch.setenv("PRICING_RULE_VERSION", "not-an-int")

    response = srt_client.post(
        "/pricing/srt-realtime-quote",
        json={
            "combo_key": "secondary_creation",
            "srt_metrics": {"text_chars": 1, "text_lines": 25},
        },
    )

    assert response.status_code == 503
    payload = response.get_json()
    assert payload["success"] is False
    assert payload["error"]["retryable"] is True


def test_endpoint_returns_503_on_unexpected_error(monkeypatch, srt_client):
    import server

    def explode(*args, **kwargs):
        raise RuntimeError("upstream pricing service stalled")

    monkeypatch.setattr(server, "compute_srt_realtime_quote", explode)

    response = srt_client.post(
        "/pricing/srt-realtime-quote",
        json={
            "combo_key": "original_narration_flash",
            "srt_metrics": {"text_chars": 100, "text_lines": 5},
        },
    )

    assert response.status_code == 503
    payload = response.get_json()
    assert payload["error"]["code"] == "PRICING_SERVICE_UNAVAILABLE"
    assert payload["error"]["retryable"] is True


def test_endpoint_uses_env_pricing_rule_version(monkeypatch, srt_client):
    monkeypatch.setenv("PRICING_RULE_VERSION", "5")

    response = srt_client.post(
        "/pricing/srt-realtime-quote",
        json={
            "combo_key": "secondary_creation",
            "srt_metrics": {"text_chars": 1, "text_lines": 25},
        },
    )

    assert response.status_code == 200
    assert response.get_json()["data"]["pricing_rule_version"] == 5
