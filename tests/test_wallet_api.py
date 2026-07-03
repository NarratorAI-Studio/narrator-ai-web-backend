from datetime import datetime, timedelta, timezone

import pytest


FIXED_NOW = datetime(2026, 5, 12, 8, 0, 0, tzinfo=timezone.utc)
AUTH_HEADERS = {"Authorization": "Bearer test-wallet-token"}


@pytest.fixture()
def wallet_client(monkeypatch):
    import server
    from wallet import InMemoryWalletStore, WalletService

    store = InMemoryWalletStore(now=lambda: FIXED_NOW)
    store.upsert_price(
        template_id=42,
        combo_key="original_narration_flash",
        hard_price="84.50",
        pricing_rule_version=7,
        text_chars=2500,
        text_lines=130,
    )
    store.set_balance("toc", "web_user_123", "100.00")
    store.set_balance("toc", "low_balance_user", "10.00")

    service = WalletService(store=store, now=lambda: FIXED_NOW, quote_ttl_seconds=900)
    monkeypatch.setenv("WALLET_BFF_AUTH_TOKEN", "test-wallet-token")
    monkeypatch.setattr(server, "_wallet_service", service)

    yield server.app.test_client(), store

    monkeypatch.setattr(server, "_wallet_service", None)


def wallet_headers(idempotency_key):
    return {**AUTH_HEADERS, "Idempotency-Key": idempotency_key}


def quote_request(web_order_id="wo_123", web_user_id="web_user_123", template_id=42):
    return {
        "web_tenant_id": "toc",
        "web_user_id": web_user_id,
        "web_order_id": web_order_id,
        "template_id": template_id,
        "combo_key": "original_narration_flash",
        "client_correlation_id": "browser-request-1",
    }


def freeze_request(quote_id, web_order_id="wo_123", web_user_id="web_user_123"):
    return {
        "quote_id": quote_id,
        "web_tenant_id": "toc",
        "web_user_id": web_user_id,
        "web_order_id": web_order_id,
        "correlation": {"web_master_task_id": "wmt_123"},
    }


def confirm_request(wallet_transaction_id):
    return {
        "wallet_transaction_id": wallet_transaction_id,
        "web_tenant_id": "toc",
        "web_user_id": "web_user_123",
        "web_order_id": "wo_123",
        "correlation": {
            "web_master_task_id": "wmt_123",
            "api_task_id": "api_task_456",
            "api_request_id": "req_789",
            "api_correlation_id": "corr_abc",
        },
    }


def confirm_request_with_legacy_evidence(wallet_transaction_id):
    body = confirm_request(wallet_transaction_id)
    body["correlation"]["legacy_consume_budget_evidence"] = {
        "source": "fastagent",
        "api_task_id": "api_task_456",
        "consume_budget_request_id": "legacy_req_001",
        "consumed_points": "84.50",
    }
    return body


def refund_request(wallet_transaction_id):
    return {
        "wallet_transaction_id": wallet_transaction_id,
        "web_tenant_id": "toc",
        "web_user_id": "web_user_123",
        "web_order_id": "wo_123",
        "reason_code": "TASK_CREATION_FAILED",
        "reason_message": "Downstream task creation failed before any task was accepted.",
        "correlation": {
            "web_master_task_id": "wmt_123",
            "api_request_id": "req_789",
            "api_error_code": "VALIDATION_FAILED",
            "reconciliation_status": "NO_TASK_CREATED",
        },
    }


def overbill_refund_request(wallet_transaction_id, refund_amount_points="10.00"):
    return {
        "wallet_transaction_id": wallet_transaction_id,
        "web_tenant_id": "toc",
        "web_user_id": "web_user_123",
        "web_order_id": "wo_123",
        "refund_amount_points": refund_amount_points,
        "reason_code": "OVERBILLED_HARD_PRICE",
        "reason_message": "Finance audit found a template price overbill.",
        "evidence": {
            "source": "finance_reconciliation",
            "operator_id": "finance-operators",
            "evidence_url": "https://example.com/issues/214",
        },
    }


def create_quote(client, web_order_id="wo_123", web_user_id="web_user_123"):
    response = client.post(
        "/wallet/quotes",
        json=quote_request(web_order_id=web_order_id, web_user_id=web_user_id),
        headers=wallet_headers(f"quote:{web_order_id}:42:original_narration_flash:v1"),
    )
    assert response.status_code == 201
    return response.get_json()["data"]


def freeze_quote(client, quote_id, web_order_id="wo_123", web_user_id="web_user_123"):
    response = client.post(
        "/wallet/freezes",
        json=freeze_request(
            quote_id, web_order_id=web_order_id, web_user_id=web_user_id
        ),
        headers=wallet_headers(f"freeze:{web_order_id}:{quote_id}:v1"),
    )
    assert response.status_code == 201
    return response.get_json()["data"]


def test_quote_pins_server_price_and_rejects_client_amount(wallet_client):
    client, _store = wallet_client

    rejected = quote_request()
    rejected["hard_price"] = "0.00"
    response = client.post(
        "/wallet/quotes",
        json=rejected,
        headers=wallet_headers("quote:wo_123:42:original_narration_flash:v1"),
    )

    assert response.status_code == 400
    assert response.get_json()["error"]["code"] == "CLIENT_AMOUNT_REJECTED"

    response = client.post(
        "/wallet/quotes",
        json=quote_request(),
        headers=wallet_headers("quote:wo_123:42:original_narration_flash:v1"),
    )

    assert response.status_code == 201
    data = response.get_json()["data"]
    assert data["quote_id"].startswith("wq_")
    assert data["status"] == "ACTIVE"
    assert data["hard_price"] == "84.50"
    assert data["amount_points"] == "84.50"
    assert data["pricing_rule_version"] == 7
    assert data["expires_at"] == "2026-05-12T08:15:00Z"
    assert data["pricing_metadata"] == {
        "text_chars": 2500,
        "text_lines": 130,
        "billing_duration_minutes": 6,
    }


def test_quote_failure_modes_are_stable(wallet_client):
    client, store = wallet_client

    missing_price = quote_request(web_order_id="wo_missing", template_id=999)
    response = client.post(
        "/wallet/quotes",
        json=missing_price,
        headers=wallet_headers("quote:wo_missing:999:original_narration_flash:v1"),
    )
    assert response.status_code == 404
    assert response.get_json()["error"]["code"] == "PRICE_NOT_FOUND"

    first = create_quote(client, web_order_id="wo_conflict")
    conflict = quote_request(web_order_id="wo_conflict")
    conflict["combo_key"] = "different_combo"
    response = client.post(
        "/wallet/quotes",
        json=conflict,
        headers=wallet_headers("quote:wo_conflict:42:original_narration_flash:v1"),
    )
    assert first["quote_id"].startswith("wq_")
    assert response.status_code == 409
    assert response.get_json()["error"]["code"] == "IDEMPOTENCY_KEY_CONFLICT"

    store.set_pricing_unavailable(True)
    response = client.post(
        "/wallet/quotes",
        json=quote_request(web_order_id="wo_unavailable"),
        headers=wallet_headers("quote:wo_unavailable:42:original_narration_flash:v1"),
    )
    assert response.status_code == 503
    assert response.get_json()["error"]["code"] == "PRICING_UNAVAILABLE"
    assert response.get_json()["error"]["retryable"] is True


def test_freeze_is_idempotent_and_rejects_insufficient_balance(wallet_client):
    client, store = wallet_client
    quote = create_quote(client)

    response = client.post(
        "/wallet/freezes",
        json=freeze_request(quote["quote_id"]),
        headers=wallet_headers(f"freeze:wo_123:{quote['quote_id']}:v1"),
    )

    assert response.status_code == 201
    first = response.get_json()["data"]
    assert first["wallet_transaction_id"].startswith("wtx_")
    assert first["state"] == "FROZEN"
    assert first["amount_points"] == "84.50"
    assert store.get_account("toc", "web_user_123")["available_balance"] == "15.50"
    assert store.get_account("toc", "web_user_123")["frozen_balance"] == "84.50"

    retry = client.post(
        "/wallet/freezes",
        json=freeze_request(quote["quote_id"]),
        headers=wallet_headers(f"freeze:wo_123:{quote['quote_id']}:v1"),
    )

    assert retry.status_code == 200
    assert (
        retry.get_json()["data"]["wallet_transaction_id"]
        == first["wallet_transaction_id"]
    )
    assert store.get_account("toc", "web_user_123")["available_balance"] == "15.50"
    assert store.get_account("toc", "web_user_123")["frozen_balance"] == "84.50"

    low_quote = create_quote(
        client, web_order_id="wo_low", web_user_id="low_balance_user"
    )
    insufficient = client.post(
        "/wallet/freezes",
        json=freeze_request(
            low_quote["quote_id"], web_order_id="wo_low", web_user_id="low_balance_user"
        ),
        headers=wallet_headers(f"freeze:wo_low:{low_quote['quote_id']}:v1"),
    )

    assert insufficient.status_code == 402
    assert insufficient.get_json()["error"]["code"] == "INSUFFICIENT_BALANCE"
    assert store.get_transaction_by_order("toc", "low_balance_user", "wo_low") is None


def test_freeze_rejects_expired_quote(wallet_client):
    client, store = wallet_client
    quote = create_quote(client)

    import server

    server._wallet_service.now = lambda: FIXED_NOW + timedelta(seconds=901)
    response = client.post(
        "/wallet/freezes",
        json=freeze_request(quote["quote_id"]),
        headers=wallet_headers(f"freeze:wo_123:{quote['quote_id']}:v1"),
    )

    assert response.status_code == 410
    assert response.get_json()["error"]["code"] == "QUOTE_EXPIRED"
    assert store.get_transaction_by_order("toc", "web_user_123", "wo_123") is None


def test_freeze_retry_after_timeout_returns_existing_transaction(wallet_client):
    client, store = wallet_client
    quote = create_quote(client)
    frozen = freeze_quote(client, quote["quote_id"])

    import server

    server._wallet_service.now = lambda: FIXED_NOW + timedelta(seconds=901)
    retry = client.post(
        "/wallet/freezes",
        json=freeze_request(quote["quote_id"]),
        headers=wallet_headers(f"freeze-retry:wo_123:{quote['quote_id']}:v1"),
    )

    assert retry.status_code == 200
    assert (
        retry.get_json()["data"]["wallet_transaction_id"]
        == frozen["wallet_transaction_id"]
    )
    assert store.get_account("toc", "web_user_123")["available_balance"] == "15.50"
    assert store.get_account("toc", "web_user_123")["frozen_balance"] == "84.50"


def test_confirm_finalizes_once_and_query_returns_correlation(wallet_client):
    client, store = wallet_client
    quote = create_quote(client)
    frozen = freeze_quote(client, quote["quote_id"])

    response = client.post(
        "/wallet/confirms",
        json=confirm_request(frozen["wallet_transaction_id"]),
        headers=wallet_headers(
            f"confirm:{frozen['wallet_transaction_id']}:api_task_456:v1"
        ),
    )

    assert response.status_code == 200
    confirmed = response.get_json()["data"]
    assert confirmed["state"] == "CONFIRMED"
    assert confirmed["amount_points"] == "84.50"
    assert confirmed["correlation"]["api_task_id"] == "api_task_456"
    assert store.get_account("toc", "web_user_123")["available_balance"] == "15.50"
    assert store.get_account("toc", "web_user_123")["frozen_balance"] == "0.00"

    retry = client.post(
        "/wallet/confirms",
        json=confirm_request(frozen["wallet_transaction_id"]),
        headers=wallet_headers(
            f"confirm:{frozen['wallet_transaction_id']}:api_task_456:v1"
        ),
    )
    assert retry.status_code == 200
    assert retry.get_json()["data"]["confirmed_at"] == confirmed["confirmed_at"]

    query = client.get(
        f"/wallet/transactions/{frozen['wallet_transaction_id']}",
        query_string={"web_tenant_id": "toc", "web_user_id": "web_user_123"},
        headers=AUTH_HEADERS,
    )
    assert query.status_code == 200
    queried = query.get_json()["data"]
    assert queried["state"] == "CONFIRMED"
    assert queried["quote"]["quote_id"] == quote["quote_id"]
    assert queried["correlation"]["api_task_id"] == "api_task_456"
    bs = queried["billing_summary"]
    assert bs["hard_price"] == "84.50"
    assert bs["discount_amount"] == "0.00"
    assert bs["refunded_amount"] == "0.00"
    assert bs["net_consumption"] == "84.50"

    idempotency_query = client.get(
        f"/wallet/transactions/by-idempotency-key/freeze:wo_123:{quote['quote_id']}:v1",
        query_string={"web_tenant_id": "toc", "web_user_id": "web_user_123"},
        headers=AUTH_HEADERS,
    )
    assert idempotency_query.status_code == 200
    assert (
        idempotency_query.get_json()["data"]["wallet_transaction_id"]
        == frozen["wallet_transaction_id"]
    )

    conflict_body = confirm_request(frozen["wallet_transaction_id"])
    conflict_body["correlation"]["api_task_id"] = "api_task_other"
    conflict = client.post(
        "/wallet/confirms",
        json=conflict_body,
        headers=wallet_headers(
            f"confirm:{frozen['wallet_transaction_id']}:api_task_other:v1"
        ),
    )
    assert conflict.status_code == 409
    assert conflict.get_json()["error"]["code"] == "DUPLICATE_SUBMIT"


def test_confirm_requires_downstream_evidence(wallet_client):
    client, _store = wallet_client
    quote = create_quote(client)
    frozen = freeze_quote(client, quote["quote_id"])

    body = confirm_request(frozen["wallet_transaction_id"])
    body["correlation"] = {}
    response = client.post(
        "/wallet/confirms",
        json=body,
        headers=wallet_headers(
            f"confirm:{frozen['wallet_transaction_id']}:missing-evidence:v1"
        ),
    )

    assert response.status_code == 400
    assert response.get_json()["error"]["code"] == "BAD_REQUEST"


def test_confirm_blocks_legacy_consume_budget_evidence(wallet_client):
    client, store = wallet_client
    quote = create_quote(client)
    frozen = freeze_quote(client, quote["quote_id"])

    response = client.post(
        "/wallet/confirms",
        json=confirm_request_with_legacy_evidence(frozen["wallet_transaction_id"]),
        headers=wallet_headers(
            f"confirm:{frozen['wallet_transaction_id']}:legacy-guard:v1"
        ),
    )

    assert response.status_code == 409
    error = response.get_json()["error"]
    assert error["code"] == "WALLET_DOUBLE_BILLING_GUARD"
    assert error["details"]["web_order_id"] == "wo_123"
    assert (
        error["details"]["legacy_billing_evidence"]["consume_budget_request_id"]
        == "legacy_req_001"
    )
    transaction = store.get_transaction_by_order("toc", "web_user_123", "wo_123")
    assert transaction["state"] == "FROZEN"
    assert store.get_account("toc", "web_user_123")["available_balance"] == "15.50"
    assert store.get_account("toc", "web_user_123")["frozen_balance"] == "84.50"

    metrics = client.get("/metrics")
    assert metrics.status_code == 200
    assert b"wallet_double_billing_blocked_total" in metrics.data


def test_confirm_double_billing_guard_retries_without_settling(wallet_client):
    client, store = wallet_client
    quote = create_quote(client)
    frozen = freeze_quote(client, quote["quote_id"])
    guarded_body = confirm_request_with_legacy_evidence(frozen["wallet_transaction_id"])

    first = client.post(
        "/wallet/confirms",
        json=guarded_body,
        headers=wallet_headers(
            f"confirm:{frozen['wallet_transaction_id']}:legacy-guard:v1"
        ),
    )
    retry = client.post(
        "/wallet/confirms",
        json=guarded_body,
        headers=wallet_headers(
            f"confirm:{frozen['wallet_transaction_id']}:legacy-guard-retry:v1"
        ),
    )

    assert first.status_code == 409
    assert retry.status_code == 409
    assert retry.get_json()["error"]["code"] == "WALLET_DOUBLE_BILLING_GUARD"
    transaction = store.get_transaction_by_order("toc", "web_user_123", "wo_123")
    assert transaction["state"] == "FROZEN"
    assert store.get_account("toc", "web_user_123")["available_balance"] == "15.50"
    assert store.get_account("toc", "web_user_123")["frozen_balance"] == "84.50"

    clean_retry = client.post(
        "/wallet/confirms",
        json=confirm_request(frozen["wallet_transaction_id"]),
        headers=wallet_headers(
            f"confirm:{frozen['wallet_transaction_id']}:api_task_456:v1"
        ),
    )
    assert clean_retry.status_code == 200
    assert clean_retry.get_json()["data"]["state"] == "CONFIRMED"
    assert store.get_account("toc", "web_user_123")["frozen_balance"] == "0.00"


def test_confirm_double_billing_guard_uses_injected_evidence_source(wallet_client):
    client, store = wallet_client
    quote = create_quote(client)
    frozen = freeze_quote(client, quote["quote_id"])

    import server

    server._wallet_service.legacy_billing_evidence = lambda transaction, correlation: {
        "source": "fastagent-lookup",
        "web_order_id": transaction["web_order_id"],
        "api_request_id": correlation["api_request_id"],
    }

    response = client.post(
        "/wallet/confirms",
        json=confirm_request(frozen["wallet_transaction_id"]),
        headers=wallet_headers(
            f"confirm:{frozen['wallet_transaction_id']}:api_task_456:v1"
        ),
    )

    assert response.status_code == 409
    assert response.get_json()["error"]["code"] == "WALLET_DOUBLE_BILLING_GUARD"
    transaction = store.get_transaction_by_order("toc", "web_user_123", "wo_123")
    assert transaction["state"] == "FROZEN"


def test_refund_releases_once_and_blocks_confirm_after_refund(wallet_client):
    client, store = wallet_client
    quote = create_quote(client)
    frozen = freeze_quote(client, quote["quote_id"])

    response = client.post(
        "/wallet/refunds",
        json=refund_request(frozen["wallet_transaction_id"]),
        headers=wallet_headers(
            f"refund:{frozen['wallet_transaction_id']}:TASK_CREATION_FAILED:v1"
        ),
    )

    assert response.status_code == 200
    refunded = response.get_json()["data"]
    assert refunded["state"] == "REFUNDED"
    assert refunded["refund_reason_code"] == "TASK_CREATION_FAILED"
    assert store.get_account("toc", "web_user_123")["available_balance"] == "100.00"
    assert store.get_account("toc", "web_user_123")["frozen_balance"] == "0.00"

    retry = client.post(
        "/wallet/refunds",
        json=refund_request(frozen["wallet_transaction_id"]),
        headers=wallet_headers(
            f"refund:{frozen['wallet_transaction_id']}:TASK_CREATION_FAILED:v1"
        ),
    )
    assert retry.status_code == 200
    assert retry.get_json()["data"]["refunded_at"] == refunded["refunded_at"]

    query_after_refund = client.get(
        f"/wallet/transactions/{frozen['wallet_transaction_id']}",
        query_string={"web_tenant_id": "toc", "web_user_id": "web_user_123"},
        headers=AUTH_HEADERS,
    )
    assert query_after_refund.status_code == 200
    bs = query_after_refund.get_json()["data"]["billing_summary"]
    assert bs["hard_price"] == "84.50"
    assert bs["discount_amount"] == "0.00"
    assert bs["refunded_amount"] == "84.50"
    assert bs["net_consumption"] == "0.00"

    confirm_after_refund = client.post(
        "/wallet/confirms",
        json=confirm_request(frozen["wallet_transaction_id"]),
        headers=wallet_headers(
            f"confirm:{frozen['wallet_transaction_id']}:api_task_456:v1"
        ),
    )
    assert confirm_after_refund.status_code == 409
    assert confirm_after_refund.get_json()["error"]["code"] == "INVALID_STATE"


def test_refund_requires_failure_evidence(wallet_client):
    client, _store = wallet_client
    quote = create_quote(client)
    frozen = freeze_quote(client, quote["quote_id"])

    body = refund_request(frozen["wallet_transaction_id"])
    body["correlation"] = {}
    response = client.post(
        "/wallet/refunds",
        json=body,
        headers=wallet_headers(
            f"refund:{frozen['wallet_transaction_id']}:missing-evidence:v1"
        ),
    )

    assert response.status_code == 400
    assert response.get_json()["error"]["code"] == "BAD_REQUEST"


def test_finance_overbill_refund_credits_confirmed_transaction_once(wallet_client):
    client, store = wallet_client
    quote = create_quote(client)
    frozen = freeze_quote(client, quote["quote_id"])
    confirmed = client.post(
        "/wallet/confirms",
        json=confirm_request(frozen["wallet_transaction_id"]),
        headers=wallet_headers(
            f"confirm:{frozen['wallet_transaction_id']}:api_task_456:v1"
        ),
    )
    assert confirmed.status_code == 200
    assert store.get_account("toc", "web_user_123")["available_balance"] == "15.50"

    response = client.post(
        "/finance/refunds/overbill",
        json=overbill_refund_request(frozen["wallet_transaction_id"]),
        headers=wallet_headers(
            f"overbill-refund:{frozen['wallet_transaction_id']}:10.00:v1"
        ),
    )

    assert response.status_code == 200
    refunded = response.get_json()["data"]
    assert refunded["state"] == "CONFIRMED"
    assert refunded["refund_reason_code"] == "OVERBILLED_HARD_PRICE"
    assert refunded["refund_amount_points"] == "10.00"
    assert store.get_account("toc", "web_user_123")["available_balance"] == "25.50"
    assert store.get_account("toc", "web_user_123")["frozen_balance"] == "0.00"

    retry = client.post(
        "/finance/refunds/overbill",
        json=overbill_refund_request(frozen["wallet_transaction_id"]),
        headers=wallet_headers(
            f"overbill-refund:{frozen['wallet_transaction_id']}:10.00:v1"
        ),
    )
    assert retry.status_code == 200
    assert retry.get_json()["data"]["refunded_at"] == refunded["refunded_at"]

    query = client.get(
        f"/wallet/transactions/{frozen['wallet_transaction_id']}",
        query_string={"web_tenant_id": "toc", "web_user_id": "web_user_123"},
        headers=AUTH_HEADERS,
    )
    billing_summary = query.get_json()["data"]["billing_summary"]
    assert billing_summary["refunded_amount"] == "10.00"
    assert billing_summary["net_consumption"] == "74.50"


def test_finance_overbill_refund_rejects_invalid_amounts(wallet_client):
    client, _store = wallet_client
    quote = create_quote(client)
    frozen = freeze_quote(client, quote["quote_id"])
    confirmed = client.post(
        "/wallet/confirms",
        json=confirm_request(frozen["wallet_transaction_id"]),
        headers=wallet_headers(
            f"confirm:{frozen['wallet_transaction_id']}:api_task_456:v1"
        ),
    )
    assert confirmed.status_code == 200

    response = client.post(
        "/finance/refunds/overbill",
        json=overbill_refund_request(frozen["wallet_transaction_id"], "100.00"),
        headers=wallet_headers(
            f"overbill-refund:{frozen['wallet_transaction_id']}:100.00:v1"
        ),
    )

    assert response.status_code == 400
    assert response.get_json()["error"]["code"] == "REFUND_AMOUNT_INVALID"

    rounded_to_zero = client.post(
        "/finance/refunds/overbill",
        json=overbill_refund_request(frozen["wallet_transaction_id"], "0.005"),
        headers=wallet_headers(
            f"overbill-refund:{frozen['wallet_transaction_id']}:0.005:v1"
        ),
    )
    assert rounded_to_zero.status_code == 400
    assert rounded_to_zero.get_json()["error"]["code"] == "BAD_REQUEST"


def test_finance_quota_board_reports_per_account_wallet_usage(wallet_client):
    client, store = wallet_client
    quote = create_quote(client)
    frozen = freeze_quote(client, quote["quote_id"])
    confirmed = client.post(
        "/wallet/confirms",
        json=confirm_request(frozen["wallet_transaction_id"]),
        headers=wallet_headers(
            f"confirm:{frozen['wallet_transaction_id']}:api_task_456:v1"
        ),
    )
    assert confirmed.status_code == 200
    refund = client.post(
        "/finance/refunds/overbill",
        json=overbill_refund_request(frozen["wallet_transaction_id"]),
        headers=wallet_headers(
            f"overbill-refund:{frozen['wallet_transaction_id']}:10.00:v1"
        ),
    )
    assert refund.status_code == 200
    store.set_balance("toc", "refund_only_user", "100.00")
    refund_only_quote = create_quote(
        client, web_order_id="wo_refund_only", web_user_id="refund_only_user"
    )
    refund_only_frozen = freeze_quote(
        client,
        refund_only_quote["quote_id"],
        web_order_id="wo_refund_only",
        web_user_id="refund_only_user",
    )
    refund_only_body = refund_request(refund_only_frozen["wallet_transaction_id"])
    refund_only_body["web_user_id"] = "refund_only_user"
    refund_only_body["web_order_id"] = "wo_refund_only"
    refund_only = client.post(
        "/wallet/refunds",
        json=refund_only_body,
        headers=wallet_headers(
            f"refund:{refund_only_frozen['wallet_transaction_id']}:TASK_CREATION_FAILED:v1"
        ),
    )
    assert refund_only.status_code == 200

    response = client.get(
        "/finance/quota-board",
        query_string={"web_tenant_id": "toc", "limit": "10"},
        headers=AUTH_HEADERS,
    )

    assert response.status_code == 200
    payload = response.get_json()["data"]
    account = next(
        row for row in payload["accounts"] if row["web_user_id"] == "web_user_123"
    )
    assert account["available_balance"] == "25.50"
    assert account["frozen_balance"] == "0.00"
    assert account["confirmed_count"] == 1
    assert account["refunded_count"] == 1
    assert account["confirmed_points"] == "84.50"
    assert account["refunded_points"] == "10.00"
    assert account["net_confirmed_points"] == "74.50"
    refund_only_account = next(
        row for row in payload["accounts"] if row["web_user_id"] == "refund_only_user"
    )
    assert refund_only_account["confirmed_points"] == "0.00"
    assert refund_only_account["refunded_points"] == "84.50"
    assert refund_only_account["net_confirmed_points"] == "0.00"
    assert payload["summary"]["account_count"] == 3
    assert payload["summary"]["refunded_points"] == "94.50"
    assert payload["summary"]["net_confirmed_points"] == "74.50"


def test_finance_quota_board_rejects_empty_tenant_filter(wallet_client):
    client, _store = wallet_client

    response = client.get(
        "/finance/quota-board",
        query_string={"web_tenant_id": "", "limit": "10"},
        headers=AUTH_HEADERS,
    )

    assert response.status_code == 400
    assert response.get_json()["error"]["code"] == "BAD_REQUEST"


def test_billing_summary_by_order_covers_display_fields(wallet_client):
    client, _store = wallet_client
    quote = create_quote(client)
    frozen = freeze_quote(client, quote["quote_id"])

    def by_order():
        r = client.get(
            "/wallet/transactions/by-order/wo_123",
            query_string={"web_tenant_id": "toc", "web_user_id": "web_user_123"},
            headers=AUTH_HEADERS,
        )
        assert r.status_code == 200
        return r.get_json()["data"]["billing_summary"]

    # FROZEN — net_consumption is null (not yet settled)
    bs = by_order()
    assert bs["hard_price"] == "84.50"
    assert bs["discount_amount"] == "0.00"
    assert bs["refunded_amount"] == "0.00"
    assert bs["net_consumption"] is None

    # CONFIRMED — net_consumption equals hard_price
    client.post(
        "/wallet/confirms",
        json=confirm_request(frozen["wallet_transaction_id"]),
        headers=wallet_headers(
            f"confirm:{frozen['wallet_transaction_id']}:api_task_456:v1"
        ),
    )
    bs = by_order()
    assert bs["hard_price"] == "84.50"
    assert bs["discount_amount"] == "0.00"
    assert bs["refunded_amount"] == "0.00"
    assert bs["net_consumption"] == "84.50"


def test_billing_summary_by_order_refunded_state(wallet_client):
    client, _store = wallet_client
    quote = create_quote(client)
    frozen = freeze_quote(client, quote["quote_id"])

    client.post(
        "/wallet/refunds",
        json=refund_request(frozen["wallet_transaction_id"]),
        headers=wallet_headers(
            f"refund:{frozen['wallet_transaction_id']}:TASK_CREATION_FAILED:v1"
        ),
    )

    r = client.get(
        "/wallet/transactions/by-order/wo_123",
        query_string={"web_tenant_id": "toc", "web_user_id": "web_user_123"},
        headers=AUTH_HEADERS,
    )
    assert r.status_code == 200
    bs = r.get_json()["data"]["billing_summary"]
    assert bs["hard_price"] == "84.50"
    assert bs["discount_amount"] == "0.00"
    assert bs["refunded_amount"] == "84.50"
    assert bs["net_consumption"] == "0.00"


def test_hard_price_steps_report_quote_pending_and_expired_states(wallet_client):
    client, _store = wallet_client
    quote = create_quote(client)

    response = client.get(
        "/wallet/hard-price/steps/by-order/wo_123",
        query_string={"web_tenant_id": "toc", "web_user_id": "web_user_123"},
        headers=AUTH_HEADERS,
    )

    assert response.status_code == 200
    data = response.get_json()["data"]
    assert data["state"] == "QUOTED"
    assert data["quote"]["quote_id"] == quote["quote_id"]
    assert data["quote"]["hard_price"] == "84.50"
    assert data["wallet"]["deduction_state"] == "not_started"
    assert data["failure"] is None
    assert [step["step"] for step in data["steps"]] == [
        "quote",
        "freeze",
        "settlement",
    ]
    assert data["steps"][0]["status"] == "completed"
    assert data["steps"][1]["status"] == "pending"
    assert data["steps"][2]["status"] == "pending"

    import server

    server._wallet_service.now = lambda: FIXED_NOW + timedelta(seconds=901)
    expired = client.get(
        "/wallet/hard-price/steps/by-order/wo_123",
        query_string={"web_tenant_id": "toc", "web_user_id": "web_user_123"},
        headers=AUTH_HEADERS,
    )

    assert expired.status_code == 200
    expired_data = expired.get_json()["data"]
    assert expired_data["state"] == "QUOTE_EXPIRED"
    assert expired_data["quote"]["expired"] is True
    assert expired_data["failure"]["code"] == "QUOTE_EXPIRED"
    assert expired_data["steps"][1]["status"] == "blocked"
    assert expired_data["steps"][1]["failure"]["step"] == "freeze"


def test_hard_price_steps_report_wallet_deduction_states(wallet_client):
    client, _store = wallet_client
    quote = create_quote(client)
    frozen = freeze_quote(client, quote["quote_id"])

    response = client.get(
        "/wallet/hard-price/steps/by-order/wo_123",
        query_string={"web_tenant_id": "toc", "web_user_id": "web_user_123"},
        headers=AUTH_HEADERS,
    )

    assert response.status_code == 200
    data = response.get_json()["data"]
    assert data["state"] == "FROZEN"
    assert data["wallet"]["wallet_transaction_id"] == frozen["wallet_transaction_id"]
    assert data["wallet"]["deduction_state"] == "frozen"
    assert data["steps"][1]["status"] == "completed"
    assert data["steps"][2]["status"] == "pending"

    client.post(
        "/wallet/confirms",
        json=confirm_request(frozen["wallet_transaction_id"]),
        headers=wallet_headers(
            f"confirm:{frozen['wallet_transaction_id']}:api_task_456:v1"
        ),
    )
    confirmed = client.get(
        "/wallet/hard-price/steps/by-order/wo_123",
        query_string={"web_tenant_id": "toc", "web_user_id": "web_user_123"},
        headers=AUTH_HEADERS,
    ).get_json()["data"]

    assert confirmed["state"] == "CONFIRMED"
    assert confirmed["wallet"]["deduction_state"] == "deducted"
    assert confirmed["correlation"]["api_task_id"] == "api_task_456"
    assert confirmed["steps"][2]["status"] == "completed"


def test_hard_price_steps_do_not_cross_owner_or_fallback(wallet_client):
    client, _store = wallet_client
    create_quote(client)

    wrong_owner = client.get(
        "/wallet/hard-price/steps/by-order/wo_123",
        query_string={"web_tenant_id": "toc", "web_user_id": "other_user"},
        headers=AUTH_HEADERS,
    )
    assert wrong_owner.status_code == 404
    assert wrong_owner.get_json()["error"]["code"] == "HARD_PRICE_STATE_NOT_FOUND"

    missing = client.get(
        "/wallet/hard-price/steps/by-order/wo_missing",
        query_string={"web_tenant_id": "toc", "web_user_id": "web_user_123"},
        headers=AUTH_HEADERS,
    )
    assert missing.status_code == 404
    assert missing.get_json()["error"]["code"] == "HARD_PRICE_STATE_NOT_FOUND"


def test_order_subtask_mapping_reports_lifecycle_and_buckets(wallet_client):
    client, _store = wallet_client
    quote = create_quote(client)
    frozen = freeze_quote(client, quote["quote_id"])

    frozen_mapping = client.get(
        "/wallet/hard-price/order-subtasks/by-order/wo_123",
        query_string={"web_tenant_id": "toc", "web_user_id": "web_user_123"},
        headers=AUTH_HEADERS,
    )
    assert frozen_mapping.status_code == 200
    frozen_data = frozen_mapping.get_json()["data"]
    assert frozen_data["order"]["state"] == "FROZEN"
    assert frozen_data["order"]["wallet_transaction_id"] == frozen[
        "wallet_transaction_id"
    ]
    assert frozen_data["subtasks"][0]["web_master_task_id"] == "wmt_123"
    assert frozen_data["subtasks"][0]["bucket_id"] == "default"

    body = confirm_request(frozen["wallet_transaction_id"])
    body["correlation"]["bucket_id"] = "voiceover"
    body["correlation"]["subtasks"] = [
        {
            "web_master_task_id": "wmt_123",
            "api_task_id": "api_task_456",
            "api_request_id": "req_789",
            "bucket_id": "voiceover",
            "status": "completed",
            "attempt": 1,
            "amount_points": "50.00",
        },
        {
            "web_master_task_id": "wmt_123",
            "api_task_id": "api_task_457",
            "api_request_id": "req_790",
            "bucket_id": "translation",
            "status": "completed",
            "attempt": 1,
            "amount_points": "34.50",
        },
    ]
    confirmed = client.post(
        "/wallet/confirms",
        json=body,
        headers=wallet_headers(
            f"confirm:{frozen['wallet_transaction_id']}:multi-subtask:v1"
        ),
    )
    assert confirmed.status_code == 200

    response = client.get(
        "/wallet/hard-price/order-subtasks/by-order/wo_123",
        query_string={"web_tenant_id": "toc", "web_user_id": "web_user_123"},
        headers=AUTH_HEADERS,
    )
    assert response.status_code == 200
    data = response.get_json()["data"]
    assert data["order"]["state"] == "CONFIRMED"
    assert [subtask["api_task_id"] for subtask in data["subtasks"]] == [
        "api_task_456",
        "api_task_457",
    ]
    assert data["buckets"] == [
        {
            "bucket_id": "voiceover",
            "subtask_count": 1,
            "statuses": {"completed": 1},
            "amount_points": "50.00",
        },
        {
            "bucket_id": "translation",
            "subtask_count": 1,
            "statuses": {"completed": 1},
            "amount_points": "34.50",
        },
    ]
    assert data["failure"] is None
    assert data["audit"]["confirmed_at"] == "2026-05-12T08:00:00Z"


def test_order_subtask_mapping_reports_quote_only_and_expired_states(wallet_client):
    client, _store = wallet_client
    create_quote(client)

    quoted = client.get(
        "/wallet/hard-price/order-subtasks/by-order/wo_123",
        query_string={"web_tenant_id": "toc", "web_user_id": "web_user_123"},
        headers=AUTH_HEADERS,
    )
    assert quoted.status_code == 200
    quoted_data = quoted.get_json()["data"]
    assert quoted_data["order"]["state"] == "QUOTED"
    assert quoted_data["subtasks"] == []
    assert quoted_data["buckets"] == []
    assert quoted_data["failure"] is None

    import server

    server._wallet_service.now = lambda: FIXED_NOW + timedelta(seconds=901)
    expired = client.get(
        "/wallet/hard-price/order-subtasks/by-order/wo_123",
        query_string={"web_tenant_id": "toc", "web_user_id": "web_user_123"},
        headers=AUTH_HEADERS,
    )
    assert expired.status_code == 200
    expired_data = expired.get_json()["data"]
    assert expired_data["order"]["state"] == "QUOTE_EXPIRED"
    assert expired_data["failure"] == {
        "code": "QUOTE_EXPIRED",
        "message": "Quote expired before wallet freeze.",
        "retryable": False,
        "step": "freeze",
    }


def test_order_subtask_mapping_reports_failure_and_owner_boundary(wallet_client):
    client, _store = wallet_client
    quote = create_quote(client)
    frozen = freeze_quote(client, quote["quote_id"])
    client.post(
        "/wallet/refunds",
        json=refund_request(frozen["wallet_transaction_id"]),
        headers=wallet_headers(
            f"refund:{frozen['wallet_transaction_id']}:TASK_CREATION_FAILED:v1"
        ),
    )

    response = client.get(
        "/wallet/hard-price/order-subtasks/by-order/wo_123",
        query_string={"web_tenant_id": "toc", "web_user_id": "web_user_123"},
        headers=AUTH_HEADERS,
    )
    assert response.status_code == 200
    data = response.get_json()["data"]
    assert data["order"]["state"] == "REFUNDED"
    assert data["failure"]["code"] == "TASK_CREATION_FAILED"
    assert data["subtasks"][0]["status"] == "refunded"

    wrong_owner = client.get(
        "/wallet/hard-price/order-subtasks/by-order/wo_123",
        query_string={"web_tenant_id": "toc", "web_user_id": "other_user"},
        headers=AUTH_HEADERS,
    )
    assert wrong_owner.status_code == 404
    assert wrong_owner.get_json()["error"]["code"] == "ORDER_SUBTASK_MAPPING_NOT_FOUND"


def test_order_subtask_mapping_rejects_invalid_subtask_amount(wallet_client):
    client, _store = wallet_client
    quote = create_quote(client)
    frozen = freeze_quote(client, quote["quote_id"])

    body = confirm_request(frozen["wallet_transaction_id"])
    body["correlation"]["subtasks"] = [
        {
            "web_master_task_id": "wmt_123",
            "api_task_id": "api_task_456",
            "api_request_id": "req_789",
            "amount_points": "not-a-number",
        }
    ]
    response = client.post(
        "/wallet/confirms",
        json=body,
        headers=wallet_headers(
            f"confirm:{frozen['wallet_transaction_id']}:bad-amount:v1"
        ),
    )

    assert response.status_code == 400
    errors = response.get_json()["error"]["details"]["validation_errors"]
    assert errors[0]["loc"] == ["correlation", "subtasks", 0, "amount_points"]


def test_order_subtask_mapping_handles_stored_invalid_subtask_amount(wallet_client):
    client, store = wallet_client
    quote = create_quote(client)
    frozen = freeze_quote(client, quote["quote_id"])
    transaction_id = frozen["wallet_transaction_id"]
    store.transactions[transaction_id]["correlation"] = {
        "web_master_task_id": "wmt_123",
        "api_task_id": "api_task_456",
        "api_request_id": "req_789",
        "subtasks": [
            {
                "web_master_task_id": "wmt_123",
                "api_task_id": "api_task_456",
                "api_request_id": "req_789",
                "amount_points": "bad-persisted-value",
            }
        ],
    }

    response = client.get(
        "/wallet/hard-price/order-subtasks/by-order/wo_123",
        query_string={"web_tenant_id": "toc", "web_user_id": "web_user_123"},
        headers=AUTH_HEADERS,
    )

    assert response.status_code == 409
    assert response.get_json()["error"]["code"] == "ORDER_SUBTASK_MAPPING_INVALID"


def test_order_subtask_mapping_preserves_zero_attempt_and_amount(wallet_client):
    client, _store = wallet_client
    quote = create_quote(client)
    frozen = freeze_quote(client, quote["quote_id"])

    body = confirm_request(frozen["wallet_transaction_id"])
    body["correlation"]["subtasks"] = [
        {
            "web_master_task_id": "wmt_123",
            "api_task_id": "api_task_456",
            "api_request_id": "req_789",
            "bucket_id": "zero-cost",
            "status": "completed",
            "attempt": 0,
            "amount_points": "0.00",
        }
    ]
    response = client.post(
        "/wallet/confirms",
        json=body,
        headers=wallet_headers(
            f"confirm:{frozen['wallet_transaction_id']}:zero-subtask:v1"
        ),
    )
    assert response.status_code == 200

    mapping = client.get(
        "/wallet/hard-price/order-subtasks/by-order/wo_123",
        query_string={"web_tenant_id": "toc", "web_user_id": "web_user_123"},
        headers=AUTH_HEADERS,
    ).get_json()["data"]

    assert mapping["subtasks"][0]["attempt"] == 0
    assert mapping["subtasks"][0]["amount_points"] == "0.00"
    assert mapping["buckets"][0]["amount_points"] == "0.00"


def test_freeze_duplicate_mapping_conflict_returns_recoverable_409(wallet_client):
    client, _store = wallet_client
    quote = create_quote(client)
    frozen = freeze_quote(client, quote["quote_id"])

    conflicting = freeze_request(quote["quote_id"])
    conflicting["correlation"] = {"web_master_task_id": "wmt_other"}
    response = client.post(
        "/wallet/freezes",
        json=conflicting,
        headers=wallet_headers(f"freeze:wo_123:{quote['quote_id']}:conflict:v1"),
    )

    assert response.status_code == 409
    body = response.get_json()
    assert body["error"]["code"] == "ORDER_SUBTASK_MAPPING_CONFLICT"
    assert body["error"]["retryable"] is False

    mapping = client.get(
        "/wallet/hard-price/order-subtasks/by-order/wo_123",
        query_string={"web_tenant_id": "toc", "web_user_id": "web_user_123"},
        headers=AUTH_HEADERS,
    ).get_json()["data"]
    assert mapping["order"]["wallet_transaction_id"] == frozen[
        "wallet_transaction_id"
    ]
    assert mapping["subtasks"][0]["web_master_task_id"] == "wmt_123"

    missing_mapping = freeze_request(quote["quote_id"])
    missing_mapping["correlation"] = {}
    missing_response = client.post(
        "/wallet/freezes",
        json=missing_mapping,
        headers=wallet_headers(f"freeze:wo_123:{quote['quote_id']}:missing:v1"),
    )
    assert missing_response.status_code == 409
    assert missing_response.get_json()["error"]["code"] == (
        "ORDER_SUBTASK_MAPPING_CONFLICT"
    )


def test_owner_mismatch_does_not_replay_idempotent_response(wallet_client):
    client, _store = wallet_client
    quote = create_quote(client)
    frozen = freeze_quote(client, quote["quote_id"])

    body = confirm_request(frozen["wallet_transaction_id"])
    body["web_user_id"] = "other_user"
    response = client.post(
        "/wallet/confirms",
        json=body,
        headers=wallet_headers(
            f"confirm:{frozen['wallet_transaction_id']}:api_task_456:v1"
        ),
    )

    assert response.status_code == 404
    assert response.get_json()["error"]["code"] == "TRANSACTION_NOT_FOUND"
