from __future__ import annotations


def metric_payload(**overrides):
    payload = {
        "order_id": "order_123",
        "template_id": "tpl_456",
        "final_script_minutes": "12.34567",
        "original_template_minutes": "10",
        "modified_script_minutes": "12.5",
        "delta_minutes": "2.5",
        "modification_ratio": "0.25",
        "created_at": "2026-05-20T01:02:03Z",
    }
    payload.update(overrides)
    return payload


def test_hard_price_metric_sink_accepts_and_normalizes_payload(monkeypatch):
    import server

    written = []
    monkeypatch.setenv("HARD_PRICE_METRIC_SLS_LOGSTORE", "hp-metrics-staging")
    monkeypatch.setattr(
        server, "write_hard_price_metric_to_sls", lambda payload: written.append(payload)
    )

    client = server.app.test_client()
    response = client.post("/api/metrics/hard-price", json=metric_payload())

    assert response.status_code == 202
    data = response.get_json()["data"]
    assert data["accepted"] is True
    assert data["telemetry_status"] == "accepted"
    assert data["sls_logstore"] == "hp-metrics-staging"
    assert written == [
        {
            "order_id": "order_123",
            "template_id": "tpl_456",
            "final_script_minutes": "12.3457",
            "original_template_minutes": "10.0000",
            "modified_script_minutes": "12.5000",
            "delta_minutes": "2.5000",
            "modification_ratio": "0.2500",
            "created_at": "2026-05-20T01:02:03Z",
            "schema_version": "hard_price_order_modification_metric_v1",
            "sls_logstore": "hp-metrics-staging",
        }
    ]


def test_hard_price_metric_sink_allows_negative_ratio_and_rejects_bad_payload():
    import server

    client = server.app.test_client()

    ok = client.post(
        "/api/metrics/hard-price",
        json=metric_payload(modification_ratio="-0.125"),
    )
    assert ok.status_code == 202

    bad = client.post(
        "/api/metrics/hard-price",
        json=metric_payload(original_template_minutes="-1"),
    )
    assert bad.status_code == 400
    assert bad.get_json()["error"]["code"] == "HARD_PRICE_METRIC_BAD_REQUEST"

    missing = client.post(
        "/api/metrics/hard-price",
        json={"order_id": "order_123"},
    )
    assert missing.status_code == 400
    assert "template_id" in missing.get_json()["error"]["details"]["missing_fields"]


def test_hard_price_metric_sink_degrades_without_blocking(monkeypatch):
    import server

    def fail(_payload):
        raise RuntimeError("sls unavailable")

    monkeypatch.setattr(server, "write_hard_price_metric_to_sls", fail)
    client = server.app.test_client()

    response = client.post("/api/metrics/hard-price", json=metric_payload())

    assert response.status_code == 202
    data = response.get_json()["data"]
    assert data["accepted"] is True
    assert data["telemetry_status"] == "degraded"
    assert data["error_code"] == "HARD_PRICE_METRIC_SINK_FAILED"
