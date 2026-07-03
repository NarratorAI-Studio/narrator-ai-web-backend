from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

import psycopg2
import psycopg2.errors
from sqlalchemy.exc import IntegrityError

from wallet import WalletError, WalletService, account_id_for


FIXED_NOW = datetime(2026, 5, 12, 8, 0, 0, tzinfo=timezone.utc)
AUTH_HEADERS = {"Authorization": "Bearer test-wallet-token"}


def wallet_headers(idempotency_key: str) -> dict[str, str]:
    return {**AUTH_HEADERS, "Idempotency-Key": idempotency_key}


def quote_request(web_order_id: str = "wo_pg_race") -> dict[str, Any]:
    return {
        "web_tenant_id": "toc",
        "web_user_id": "web_user_123",
        "web_order_id": web_order_id,
        "template_id": 42,
        "combo_key": "original_narration_flash",
        "client_correlation_id": "browser-request-1",
    }


def confirm_request(wallet_transaction_id: str) -> dict[str, Any]:
    return {
        "wallet_transaction_id": wallet_transaction_id,
        "web_tenant_id": "toc",
        "web_user_id": "web_user_123",
        "web_order_id": "wo_pg_race",
        "correlation": {
            "web_master_task_id": "wmt_123",
            "api_task_id": "api_task_456",
            "api_request_id": "req_789",
            "api_correlation_id": "corr_abc",
        },
    }


def refund_request(wallet_transaction_id: str) -> dict[str, Any]:
    return {
        "wallet_transaction_id": wallet_transaction_id,
        "web_tenant_id": "toc",
        "web_user_id": "web_user_123",
        "web_order_id": "wo_pg_race",
        "reason_code": "TASK_CREATION_FAILED",
        "reason_message": "Downstream task creation failed before any task was accepted.",
        "correlation": {
            "web_master_task_id": "wmt_123",
            "api_request_id": "req_789",
            "api_error_code": "VALIDATION_FAILED",
            "reconciliation_status": "NO_TASK_CREATED",
        },
    }


class InterleavingFrozenTransactionStore:
    """A lightweight Postgres-like store that exposes stale transaction reads."""

    def __init__(self) -> None:
        self.wallet_transaction_id = "wtx_interleaved"
        self.quote_id = "wq_interleaved"
        self.wallet_account_id = account_id_for("toc", "web_user_123")
        self.read_barrier = threading.Barrier(2)
        self.transaction_lock = threading.Lock()
        self.account_lock = threading.Lock()
        self.ledger_entries: list[dict[str, Any]] = []
        self.idempotency: list[dict[str, Any]] = []
        self.account = {
            "wallet_account_id": self.wallet_account_id,
            "web_tenant_id": "toc",
            "web_user_id": "web_user_123",
            "available_balance": "15.50",
            "frozen_balance": "84.50",
            "created_at": FIXED_NOW,
            "updated_at": FIXED_NOW,
        }
        self.quote = {
            "quote_id": self.quote_id,
            "status": "CONSUMED",
            "web_tenant_id": "toc",
            "web_user_id": "web_user_123",
            "web_order_id": "wo_pg_race",
            "template_id": 42,
            "combo_key": "original_narration_flash",
            "hard_price": "84.50",
            "amount_points": "84.50",
            "pricing_rule_version": 7,
            "expires_at": FIXED_NOW + timedelta(minutes=15),
            "pricing_metadata": {},
            "correlation": {},
            "created_at": FIXED_NOW,
            "updated_at": FIXED_NOW,
        }
        self.transaction_row = {
            "wallet_transaction_id": self.wallet_transaction_id,
            "state": "FROZEN",
            "quote_id": self.quote_id,
            "wallet_account_id": self.wallet_account_id,
            "web_tenant_id": "toc",
            "web_user_id": "web_user_123",
            "web_order_id": "wo_pg_race",
            "amount_points": "84.50",
            "pricing_rule_version": 7,
            "frozen_at": FIXED_NOW,
            "confirmed_at": None,
            "refunded_at": None,
            "refund_reason_code": None,
            "refund_reason_message": None,
            "correlation": {"web_master_task_id": "wmt_123"},
            "refund_correlation": {},
            "created_at": FIXED_NOW,
            "updated_at": FIXED_NOW,
        }

    @contextmanager
    def transaction(self):
        session = InterleavingFrozenTransactionSession(self)
        try:
            yield session
        finally:
            session.release_transaction_lock()
            session.release_account_lock()


class InterleavingFrozenTransactionSession:
    def __init__(self, store: InterleavingFrozenTransactionStore) -> None:
        self.store = store
        self._has_account_lock = False
        self._has_transaction_lock = False

    def release_account_lock(self) -> None:
        if self._has_account_lock:
            self._has_account_lock = False
            self.store.account_lock.release()

    def release_transaction_lock(self) -> None:
        if self._has_transaction_lock:
            self._has_transaction_lock = False
            self.store.transaction_lock.release()

    def get_idempotency(self, operation: str, key: str) -> dict[str, Any] | None:
        return None

    def insert_idempotency(self, record: dict[str, Any]) -> None:
        self.store.idempotency.append(record.copy())

    def get_transaction(self, wallet_transaction_id: str) -> dict[str, Any] | None:
        if wallet_transaction_id != self.store.wallet_transaction_id:
            return None
        row = self.store.transaction_row.copy()
        self.store.read_barrier.wait(timeout=5)
        return row

    def get_transaction_for_update(
        self, wallet_transaction_id: str
    ) -> dict[str, Any] | None:
        self.store.transaction_lock.acquire()
        self._has_transaction_lock = True
        if wallet_transaction_id != self.store.wallet_transaction_id:
            return None
        return self.store.transaction_row.copy()

    def get_account_for_update(
        self, web_tenant_id: str, web_user_id: str
    ) -> dict[str, Any]:
        self.store.account_lock.acquire()
        self._has_account_lock = True
        return self.store.account.copy()

    def update_account(self, account: dict[str, Any]) -> None:
        self.store.account = account.copy()
        self.release_account_lock()

    def update_transaction(self, transaction: dict[str, Any]) -> None:
        self.store.transaction_row = transaction.copy()

    def insert_ledger_entry(self, entry: dict[str, Any]) -> None:
        self.store.ledger_entries.append(entry.copy())

    def get_quote(self, quote_id: str) -> dict[str, Any] | None:
        if quote_id != self.store.quote_id:
            return None
        return self.store.quote.copy()


def test_confirm_and_refund_same_frozen_transaction_cannot_both_terminally_migrate():
    store = InterleavingFrozenTransactionStore()
    service = WalletService(store=store, now=lambda: FIXED_NOW)

    def call_confirm_or_refund(operation: str) -> tuple[Any, ...]:
        try:
            if operation == "confirm":
                result = service.confirm(
                    confirm_request(store.wallet_transaction_id),
                    "confirm:wtx_interleaved:v1",
                )
            else:
                result = service.refund(
                    refund_request(store.wallet_transaction_id),
                    "refund:wtx_interleaved:v1",
                )
            return ("ok", operation, result.status_code, result.data["state"])
        except WalletError as error:
            return ("wallet_error", operation, error.http_status, error.code)

    with ThreadPoolExecutor(max_workers=2) as executor:
        confirm_future = executor.submit(call_confirm_or_refund, "confirm")
        refund_future = executor.submit(call_confirm_or_refund, "refund")
        results = [confirm_future.result(timeout=5), refund_future.result(timeout=5)]

    successes = [result for result in results if result[0] == "ok"]
    terminal_entries = [
        entry
        for entry in store.ledger_entries
        if entry["entry_type"] in {"CONFIRM", "REFUND"}
    ]

    assert len(successes) == 1, results
    assert len(terminal_entries) == 1
    assert Decimal(store.account["frozen_balance"]) >= Decimal("0.00")


class OperationalErrorStore:
    @contextmanager
    def transaction(self):
        raise psycopg2.OperationalError("could not connect to wallet postgres")
        yield


class SqlAlchemyUniqueViolationStore:
    @contextmanager
    def transaction(self):
        raise IntegrityError(
            "INSERT INTO wallet_quotes ...",
            {},
            psycopg2.errors.UniqueViolation(
                "duplicate key value violates unique constraint"
            ),
        )
        yield


def test_wallet_operational_error_is_stable_json_error(monkeypatch):
    import server

    old_testing = server.app.config.get("TESTING")
    old_propagate = server.app.config.get("PROPAGATE_EXCEPTIONS")
    server.app.config.update(TESTING=False, PROPAGATE_EXCEPTIONS=False)
    monkeypatch.setenv("WALLET_BFF_AUTH_TOKEN", "test-wallet-token")
    monkeypatch.setattr(
        server,
        "_wallet_service",
        WalletService(store=OperationalErrorStore(), now=lambda: FIXED_NOW),
    )

    try:
        response = server.app.test_client().post(
            "/wallet/quotes",
            json=quote_request(),
            headers=wallet_headers("quote:operational-error:v1"),
        )
    finally:
        server.app.config.update(
            TESTING=old_testing,
            PROPAGATE_EXCEPTIONS=old_propagate,
        )
        monkeypatch.setattr(server, "_wallet_service", None)

    assert response.status_code == 503
    assert response.is_json
    payload = response.get_json()
    assert payload["success"] is False
    assert payload["error"]["retryable"] is True
    assert isinstance(payload["error"]["code"], str)


def test_wallet_sqlalchemy_unique_violation_is_stable_duplicate_json(monkeypatch):
    import server

    old_testing = server.app.config.get("TESTING")
    old_propagate = server.app.config.get("PROPAGATE_EXCEPTIONS")
    server.app.config.update(TESTING=False, PROPAGATE_EXCEPTIONS=False)
    monkeypatch.setenv("WALLET_BFF_AUTH_TOKEN", "test-wallet-token")
    monkeypatch.setattr(
        server,
        "_wallet_service",
        WalletService(
            store=SqlAlchemyUniqueViolationStore(),
            now=lambda: FIXED_NOW,
        ),
    )

    try:
        response = server.app.test_client().post(
            "/wallet/quotes",
            json=quote_request(),
            headers=wallet_headers("quote:sqlalchemy-unique:v1"),
        )
    finally:
        server.app.config.update(
            TESTING=old_testing,
            PROPAGATE_EXCEPTIONS=old_propagate,
        )
        monkeypatch.setattr(server, "_wallet_service", None)

    assert response.status_code == 409
    assert response.is_json
    payload = response.get_json()
    assert payload["success"] is False
    assert payload["error"]["code"] == "DUPLICATE_SUBMIT"


class UniqueIdempotencyRaceStore:
    def __init__(self) -> None:
        self.idempotency_lookup_count = 0
        self.raced_record: dict[str, Any] | None = None
        self.inserted_quote: dict[str, Any] | None = None

    @contextmanager
    def transaction(self):
        yield UniqueIdempotencyRaceSession(self)


class UniqueIdempotencyRaceSession:
    def __init__(self, store: UniqueIdempotencyRaceStore) -> None:
        self.store = store

    def get_idempotency(self, operation: str, key: str) -> dict[str, Any] | None:
        self.store.idempotency_lookup_count += 1
        if self.store.idempotency_lookup_count == 1:
            return None
        return self.store.raced_record.copy() if self.store.raced_record else None

    def insert_idempotency(self, record: dict[str, Any]) -> None:
        self.store.raced_record = record.copy()
        raise psycopg2.errors.UniqueViolation(
            "duplicate key value violates unique constraint "
            '"wallet_idempotency_records_pkey"'
        )

    def get_quote_by_order(self, web_order_id: str) -> dict[str, Any] | None:
        return None

    def get_price(self, template_id: int, combo_key: str) -> dict[str, Any] | None:
        return {
            "template_id": template_id,
            "combo_key": combo_key,
            "hard_price": "84.50",
            "pricing_rule_version": 7,
            "text_chars": 2500,
            "text_lines": 130,
            "billing_duration_minutes": 6,
        }

    def insert_quote(self, quote: dict[str, Any]) -> None:
        self.store.inserted_quote = quote.copy()


def test_idempotency_unique_race_returns_existing_or_stable_conflict(monkeypatch):
    import server

    old_testing = server.app.config.get("TESTING")
    old_propagate = server.app.config.get("PROPAGATE_EXCEPTIONS")
    server.app.config.update(TESTING=False, PROPAGATE_EXCEPTIONS=False)
    monkeypatch.setenv("WALLET_BFF_AUTH_TOKEN", "test-wallet-token")
    monkeypatch.setattr(
        server,
        "_wallet_service",
        WalletService(store=UniqueIdempotencyRaceStore(), now=lambda: FIXED_NOW),
    )

    try:
        response = server.app.test_client().post(
            "/wallet/quotes",
            json=quote_request("wo_idempotency_race"),
            headers=wallet_headers("quote:wo_idempotency_race:v1"),
        )
    finally:
        server.app.config.update(
            TESTING=old_testing,
            PROPAGATE_EXCEPTIONS=old_propagate,
        )
        monkeypatch.setattr(server, "_wallet_service", None)

    assert response.status_code in {200, 409}
    assert response.is_json
    payload = response.get_json()
    if response.status_code == 200:
        assert payload["success"] is True
        assert payload["data"]["web_order_id"] == "wo_idempotency_race"
    else:
        assert payload["success"] is False
        assert payload["error"]["code"] in {
            "DUPLICATE_SUBMIT",
            "IDEMPOTENCY_KEY_CONFLICT",
        }
