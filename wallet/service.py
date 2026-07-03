from __future__ import annotations

from datetime import datetime, timedelta
from decimal import DecimalException
from typing import Any, Callable

from .common import (
    CLIENT_AMOUNT_FIELDS,
    canonical_request_hash,
    iso_z,
    money,
    money_str,
    new_id,
    utcnow,
    validate_idempotency_key,
)
from .errors import ServiceResult, WalletError
from .metrics import WALLET_DOUBLE_BILLING_BLOCKED
from .models import (
    ConfirmRequest,
    FreezeRequest,
    OverbillRefundRequest,
    QuoteRequest,
    RefundRequest,
    validate_model,
)
from .store import WalletSession, WalletStore


class WalletService:
    def __init__(
        self,
        *,
        store: WalletStore,
        now: Callable[[], datetime] = utcnow,
        legacy_billing_evidence: Callable[
            [dict[str, Any], dict[str, Any]], dict[str, Any] | None
        ]
        | None = None,
        quote_ttl_seconds: int = 900,
        idempotency_ttl_hours: int = 168,
    ) -> None:
        self.store = store
        self.now = now
        self.legacy_billing_evidence = legacy_billing_evidence
        self.quote_ttl_seconds = quote_ttl_seconds
        self.idempotency_ttl_hours = idempotency_ttl_hours

    def create_quote(self, body: dict[str, Any], idempotency_key: str) -> ServiceResult:
        validate_idempotency_key(idempotency_key)
        if CLIENT_AMOUNT_FIELDS.intersection(body):
            raise WalletError(
                400,
                "CLIENT_AMOUNT_REJECTED",
                "Client-supplied final charge amounts are not accepted.",
            )
        if "wallet_account_id" in body:
            raise WalletError(
                400,
                "CLIENT_ACCOUNT_REJECTED",
                "wallet_account_id is resolved server-side.",
            )
        body = validate_model(QuoteRequest, body)

        operation = "quote"
        request_hash = canonical_request_hash(operation, body)
        owner = self._owner(body)
        now = self.now()
        expires_at = now + timedelta(seconds=self.quote_ttl_seconds)

        with self.store.transaction() as tx:
            self._lock_idempotency(tx, operation, idempotency_key)
            replay = self._check_idempotency(
                tx, operation, idempotency_key, request_hash, owner
            )
            if replay:
                return ServiceResult(replay["response_body"]["data"], 200)

            self._lock_order(tx, body["web_order_id"])
            replay = self._check_idempotency(
                tx, operation, idempotency_key, request_hash, owner
            )
            if replay:
                return ServiceResult(replay["response_body"]["data"], 200)

            existing = tx.get_quote_by_order(body["web_order_id"])
            if existing:
                if self._quote_matches_request(existing, body):
                    data = self._quote_response(existing)
                    self._save_idempotency(
                        tx,
                        operation,
                        idempotency_key,
                        request_hash,
                        owner,
                        data,
                        200,
                    )
                    return ServiceResult(data, 200)
                raise WalletError(
                    409,
                    "DUPLICATE_SUBMIT",
                    "A different quote already exists for this order.",
                )

            price = tx.get_price(body["template_id"], body["combo_key"])
            if not price:
                raise WalletError(404, "PRICE_NOT_FOUND", "Hard price not found.")

            quote = {
                "quote_id": new_id("wq"),
                "status": "ACTIVE",
                "web_tenant_id": body["web_tenant_id"],
                "web_user_id": body["web_user_id"],
                "web_order_id": body["web_order_id"],
                "template_id": body["template_id"],
                "combo_key": body["combo_key"],
                "hard_price": money_str(price["hard_price"]),
                "amount_points": money_str(price["hard_price"]),
                "pricing_rule_version": int(price["pricing_rule_version"]),
                "expires_at": expires_at,
                "pricing_metadata": {
                    "text_chars": price.get("text_chars"),
                    "text_lines": price.get("text_lines"),
                    "billing_duration_minutes": price.get("billing_duration_minutes"),
                },
                "correlation": {
                    "client_correlation_id": body.get("client_correlation_id")
                },
                "created_at": now,
                "updated_at": now,
            }
            tx.insert_quote(quote)
            data = self._quote_response(quote)
            self._save_idempotency(
                tx,
                operation,
                idempotency_key,
                request_hash,
                owner,
                data,
                201,
            )
            return ServiceResult(data, 201)

    def freeze(self, body: dict[str, Any], idempotency_key: str) -> ServiceResult:
        validate_idempotency_key(idempotency_key)
        if "wallet_account_id" in body:
            raise WalletError(
                400,
                "CLIENT_ACCOUNT_REJECTED",
                "wallet_account_id is resolved server-side.",
            )
        body = validate_model(FreezeRequest, body)

        operation = "freeze"
        request_hash = canonical_request_hash(operation, body)
        owner = self._owner(body)
        now = self.now()

        with self.store.transaction() as tx:
            self._lock_idempotency(tx, operation, idempotency_key)
            replay = self._check_idempotency(
                tx, operation, idempotency_key, request_hash, owner
            )
            if replay:
                return ServiceResult(replay["response_body"]["data"], 200)

            self._lock_order(tx, body["web_order_id"])
            replay = self._check_idempotency(
                tx, operation, idempotency_key, request_hash, owner
            )
            if replay:
                return ServiceResult(replay["response_body"]["data"], 200)

            quote = self._get_quote_for_update(tx, body["quote_id"])
            if not quote or not self._owner_matches(quote, owner):
                raise WalletError(404, "TRANSACTION_NOT_FOUND", "Quote not found.")
            if quote["web_order_id"] != body["web_order_id"]:
                raise WalletError(409, "DUPLICATE_SUBMIT", "Quote order mismatch.")

            existing = tx.get_transaction_by_order(*owner)
            if existing:
                if existing["quote_id"] != quote["quote_id"]:
                    raise WalletError(
                        409,
                        "DUPLICATE_SUBMIT",
                        "A different transaction already exists for this order.",
                    )
                if not self._freeze_correlation_matches(
                    existing["correlation"], body.get("correlation", {})
                ):
                    raise WalletError(
                        409,
                        "ORDER_SUBTASK_MAPPING_CONFLICT",
                        "Freeze correlation does not match the stored order-to-subtask mapping.",
                    )
                data = self._transaction_response(tx, existing)
                self._save_idempotency(
                    tx,
                    operation,
                    idempotency_key,
                    request_hash,
                    owner,
                    data,
                    200,
                    existing["wallet_transaction_id"],
                )
                return ServiceResult(data, 200)

            if quote["expires_at"] <= now:
                raise WalletError(410, "QUOTE_EXPIRED", "Quote expired.")

            account = tx.get_account_for_update(
                quote["web_tenant_id"], quote["web_user_id"]
            )
            amount = money(quote["amount_points"])
            if money(account["available_balance"]) < amount:
                raise WalletError(
                    402,
                    "INSUFFICIENT_BALANCE",
                    "Insufficient wallet balance.",
                )

            account["available_balance"] = money_str(
                money(account["available_balance"]) - amount
            )
            account["frozen_balance"] = money_str(
                money(account["frozen_balance"]) + amount
            )
            account["updated_at"] = now
            tx.update_account(account)

            quote["status"] = "CONSUMED"
            quote["updated_at"] = now
            tx.update_quote(quote)

            transaction = {
                "wallet_transaction_id": new_id("wtx"),
                "state": "FROZEN",
                "quote_id": quote["quote_id"],
                "wallet_account_id": account["wallet_account_id"],
                "web_tenant_id": quote["web_tenant_id"],
                "web_user_id": quote["web_user_id"],
                "web_order_id": quote["web_order_id"],
                "amount_points": quote["amount_points"],
                "pricing_rule_version": quote["pricing_rule_version"],
                "frozen_at": now,
                "confirmed_at": None,
                "refunded_at": None,
                "refund_reason_code": None,
                "refund_reason_message": None,
                "correlation": body.get("correlation", {}),
                "refund_correlation": {},
                "created_at": now,
                "updated_at": now,
            }
            tx.insert_transaction(transaction)
            self._insert_ledger(tx, "FREEZE", account, transaction, now)
            data = self._transaction_response(tx, transaction)
            self._save_idempotency(
                tx,
                operation,
                idempotency_key,
                request_hash,
                owner,
                data,
                201,
                transaction["wallet_transaction_id"],
            )
            return ServiceResult(data, 201)

    def confirm(self, body: dict[str, Any], idempotency_key: str) -> ServiceResult:
        validate_idempotency_key(idempotency_key)
        body = validate_model(ConfirmRequest, body)
        operation = "confirm"
        request_hash = canonical_request_hash(operation, body)
        owner = self._owner(body)
        now = self.now()

        with self.store.transaction() as tx:
            self._lock_idempotency(tx, operation, idempotency_key)
            replay = self._check_idempotency(
                tx, operation, idempotency_key, request_hash, owner
            )
            if replay:
                return ServiceResult(replay["response_body"]["data"], 200)

            transaction = self._get_owned_transaction(
                tx,
                body["wallet_transaction_id"],
                owner,
                for_update=True,
            )
            correlation = body.get("correlation", {})
            if transaction["state"] == "REFUNDED":
                raise WalletError(409, "INVALID_STATE", "Transaction already refunded.")
            self._guard_against_double_billing(transaction, correlation)
            if transaction["state"] == "CONFIRMED":
                if not self._correlation_matches(
                    transaction["correlation"], correlation
                ):
                    raise WalletError(
                        409,
                        "DUPLICATE_SUBMIT",
                        "Confirm correlation does not match the stored transaction.",
                    )
                data = self._transaction_response(tx, transaction)
                self._save_idempotency(
                    tx,
                    operation,
                    idempotency_key,
                    request_hash,
                    owner,
                    data,
                    200,
                    transaction["wallet_transaction_id"],
                )
                return ServiceResult(data, 200)

            account = tx.get_account_for_update(
                transaction["web_tenant_id"],
                transaction["web_user_id"],
            )
            amount = money(transaction["amount_points"])
            account["frozen_balance"] = money_str(
                money(account["frozen_balance"]) - amount
            )
            account["updated_at"] = now
            tx.update_account(account)

            transaction["state"] = "CONFIRMED"
            transaction["correlation"] = correlation
            transaction["confirmed_at"] = now
            transaction["updated_at"] = now
            tx.update_transaction(transaction)
            self._insert_ledger(tx, "CONFIRM", account, transaction, now)
            data = self._transaction_response(tx, transaction)
            self._save_idempotency(
                tx,
                operation,
                idempotency_key,
                request_hash,
                owner,
                data,
                200,
                transaction["wallet_transaction_id"],
            )
            return ServiceResult(data, 200)

    def refund(self, body: dict[str, Any], idempotency_key: str) -> ServiceResult:
        validate_idempotency_key(idempotency_key)
        body = validate_model(RefundRequest, body)
        operation = "refund"
        request_hash = canonical_request_hash(operation, body)
        owner = self._owner(body)
        now = self.now()

        with self.store.transaction() as tx:
            self._lock_idempotency(tx, operation, idempotency_key)
            replay = self._check_idempotency(
                tx, operation, idempotency_key, request_hash, owner
            )
            if replay:
                return ServiceResult(replay["response_body"]["data"], 200)

            transaction = self._get_owned_transaction(
                tx,
                body["wallet_transaction_id"],
                owner,
                for_update=True,
            )
            refund_correlation = body.get("correlation", {})
            if transaction["state"] == "CONFIRMED":
                raise WalletError(
                    409, "INVALID_STATE", "Transaction already confirmed."
                )
            if transaction["state"] == "REFUNDED":
                if not self._refund_matches(transaction, body, refund_correlation):
                    raise WalletError(
                        409,
                        "DUPLICATE_SUBMIT",
                        "Refund evidence does not match the stored transaction.",
                    )
                data = self._transaction_response(tx, transaction)
                self._save_idempotency(
                    tx,
                    operation,
                    idempotency_key,
                    request_hash,
                    owner,
                    data,
                    200,
                    transaction["wallet_transaction_id"],
                )
                return ServiceResult(data, 200)

            account = tx.get_account_for_update(
                transaction["web_tenant_id"],
                transaction["web_user_id"],
            )
            amount = money(transaction["amount_points"])
            account["available_balance"] = money_str(
                money(account["available_balance"]) + amount
            )
            account["frozen_balance"] = money_str(
                money(account["frozen_balance"]) - amount
            )
            account["updated_at"] = now
            tx.update_account(account)

            transaction["state"] = "REFUNDED"
            transaction["refunded_at"] = now
            transaction["refund_reason_code"] = body["reason_code"]
            transaction["refund_reason_message"] = body["reason_message"]
            transaction["refund_correlation"] = refund_correlation
            transaction["updated_at"] = now
            tx.update_transaction(transaction)
            self._insert_ledger(tx, "REFUND", account, transaction, now)
            data = self._transaction_response(tx, transaction)
            self._save_idempotency(
                tx,
                operation,
                idempotency_key,
                request_hash,
                owner,
                data,
                200,
                transaction["wallet_transaction_id"],
            )
            return ServiceResult(data, 200)

    def refund_overbill(
        self, body: dict[str, Any], idempotency_key: str
    ) -> ServiceResult:
        validate_idempotency_key(idempotency_key)
        body = validate_model(OverbillRefundRequest, body)
        operation = "overbill_refund"
        request_hash = canonical_request_hash(operation, body)
        owner = self._owner(body)
        now = self.now()

        with self.store.transaction() as tx:
            self._lock_idempotency(tx, operation, idempotency_key)
            replay = self._check_idempotency(
                tx, operation, idempotency_key, request_hash, owner
            )
            if replay:
                return ServiceResult(replay["response_body"]["data"], 200)

            transaction = self._get_owned_transaction(
                tx,
                body["wallet_transaction_id"],
                owner,
                for_update=True,
            )
            if transaction["state"] != "CONFIRMED":
                raise WalletError(
                    409,
                    "INVALID_STATE",
                    "Only confirmed template price transactions can be overbill-refunded.",
                )

            refund_amount = money(body["refund_amount_points"])
            transaction_amount = money(transaction["amount_points"])
            already_refunded = money(
                tx.get_refund_ledger_total(transaction["wallet_transaction_id"])
            )
            if already_refunded > 0:
                raise WalletError(
                    409,
                    "DUPLICATE_SUBMIT",
                    "Transaction already has refund ledger evidence.",
                )
            if refund_amount > transaction_amount:
                raise WalletError(
                    400,
                    "REFUND_AMOUNT_INVALID",
                    "refund_amount_points cannot exceed transaction amount.",
                    details={
                        "transaction_amount_points": money_str(transaction_amount),
                        "refund_amount_points": money_str(refund_amount),
                    },
                )

            account = tx.get_account_for_update(
                transaction["web_tenant_id"],
                transaction["web_user_id"],
            )
            account["available_balance"] = money_str(
                money(account["available_balance"]) + refund_amount
            )
            account["updated_at"] = now
            tx.update_account(account)

            transaction["refunded_at"] = now
            transaction["refund_reason_code"] = body["reason_code"]
            transaction["refund_reason_message"] = body["reason_message"]
            transaction["refund_correlation"] = {
                "type": "OVERBILL_REFUND",
                "refund_amount_points": money_str(refund_amount),
                "evidence": body["evidence"],
            }
            transaction["updated_at"] = now
            tx.update_transaction(transaction)
            self._insert_ledger(
                tx,
                "REFUND",
                account,
                transaction,
                now,
                amount_points=money_str(refund_amount),
            )
            data = self._transaction_response(tx, transaction)
            self._save_idempotency(
                tx,
                operation,
                idempotency_key,
                request_hash,
                owner,
                data,
                200,
                transaction["wallet_transaction_id"],
            )
            return ServiceResult(data, 200)

    def get_transaction(
        self,
        wallet_transaction_id: str,
        web_tenant_id: str,
        web_user_id: str,
    ) -> ServiceResult:
        with self.store.transaction() as tx:
            transaction = tx.get_transaction(wallet_transaction_id)
            if not transaction:
                raise WalletError(
                    404,
                    "TRANSACTION_NOT_FOUND",
                    "Transaction not found.",
                )
            if (
                transaction["web_tenant_id"] != web_tenant_id
                or transaction["web_user_id"] != web_user_id
            ):
                raise WalletError(
                    404,
                    "TRANSACTION_NOT_FOUND",
                    "Transaction not found.",
                )
            return ServiceResult(self._query_response(tx, transaction))

    def get_transaction_by_order(
        self,
        web_tenant_id: str,
        web_user_id: str,
        web_order_id: str,
    ) -> ServiceResult:
        with self.store.transaction() as tx:
            transaction = tx.get_transaction_by_order(
                web_tenant_id,
                web_user_id,
                web_order_id,
            )
            if not transaction:
                raise WalletError(
                    404,
                    "TRANSACTION_NOT_FOUND",
                    "Transaction not found.",
                )
            return ServiceResult(self._query_response(tx, transaction))

    def get_hard_price_step_state_by_order(
        self,
        web_tenant_id: str,
        web_user_id: str,
        web_order_id: str,
    ) -> ServiceResult:
        owner = (web_tenant_id, web_user_id, web_order_id)
        now = self.now()
        with self.store.transaction() as tx:
            quote = tx.get_quote_by_order(web_order_id)
            if not quote or not self._owner_matches(quote, owner):
                raise WalletError(
                    404,
                    "HARD_PRICE_STATE_NOT_FOUND",
                    "Hard-price step state not found for this order.",
                )

            transaction = tx.get_transaction_by_order(
                web_tenant_id,
                web_user_id,
                web_order_id,
            )
            return ServiceResult(
                self._hard_price_step_state_response(tx, quote, transaction, now)
            )

    def get_order_subtask_mapping_by_order(
        self,
        web_tenant_id: str,
        web_user_id: str,
        web_order_id: str,
    ) -> ServiceResult:
        owner = (web_tenant_id, web_user_id, web_order_id)
        now = self.now()
        with self.store.transaction() as tx:
            quote = tx.get_quote_by_order(web_order_id)
            if not quote or not self._owner_matches(quote, owner):
                raise WalletError(
                    404,
                    "ORDER_SUBTASK_MAPPING_NOT_FOUND",
                    "Order-to-subtask mapping not found for this order.",
                )

            transaction = tx.get_transaction_by_order(
                web_tenant_id,
                web_user_id,
                web_order_id,
            )
            return ServiceResult(
                self._order_subtask_mapping_response(quote, transaction, now)
            )

    def get_transaction_by_idempotency_key(
        self,
        idempotency_key: str,
        web_tenant_id: str,
        web_user_id: str,
    ) -> ServiceResult:
        validate_idempotency_key(idempotency_key)
        with self.store.transaction() as tx:
            record = tx.get_idempotency_by_key(idempotency_key)
            if (
                not record
                or record["web_tenant_id"] != web_tenant_id
                or record["web_user_id"] != web_user_id
            ):
                raise WalletError(
                    404,
                    "TRANSACTION_NOT_FOUND",
                    "Transaction not found.",
                )
            wallet_transaction_id = record.get("wallet_transaction_id")
            if not wallet_transaction_id:
                response_data = record.get("response_body", {}).get("data", {})
                wallet_transaction_id = response_data.get("wallet_transaction_id")
            if not wallet_transaction_id:
                raise WalletError(
                    404,
                    "TRANSACTION_NOT_FOUND",
                    "Transaction not found.",
                )
            transaction = tx.get_transaction(wallet_transaction_id)
            if (
                not transaction
                or transaction["web_tenant_id"] != web_tenant_id
                or transaction["web_user_id"] != web_user_id
            ):
                raise WalletError(
                    404,
                    "TRANSACTION_NOT_FOUND",
                    "Transaction not found.",
                )
            return ServiceResult(self._query_response(tx, transaction))

    def get_quota_board(
        self,
        *,
        web_tenant_id: str | None = None,
        limit: int = 100,
    ) -> ServiceResult:
        if limit < 1 or limit > 500:
            raise WalletError(
                400,
                "BAD_REQUEST",
                "limit must be between 1 and 500.",
            )
        with self.store.transaction() as tx:
            rows = [
                self._quota_board_row(row)
                for row in tx.list_quota_board(
                    web_tenant_id=web_tenant_id or None,
                    limit=limit,
                )
            ]
            return ServiceResult(
                {
                    "web_tenant_id": web_tenant_id or None,
                    "limit": limit,
                    "accounts": rows,
                    "summary": self._quota_board_summary(rows),
                }
            )

    def cleanup_expired_idempotency_records(self, batch_size: int = 1000) -> int:
        cutoff = self.now() - timedelta(hours=self.idempotency_ttl_hours)
        total_deleted = 0
        while True:
            with self.store.transaction() as tx:
                deleted = tx.delete_idempotency_before(cutoff, batch_size=batch_size)
            total_deleted += deleted
            if deleted < batch_size:
                return total_deleted

    def _lock_idempotency(
        self,
        tx: WalletSession,
        operation: str,
        idempotency_key: str,
    ) -> None:
        lock = getattr(tx, "lock_idempotency", None)
        if lock:
            lock(operation, idempotency_key)

    def _lock_order(self, tx: WalletSession, web_order_id: str) -> None:
        lock = getattr(tx, "lock_order", None)
        if lock:
            lock(web_order_id)

    def _get_quote_for_update(
        self,
        tx: WalletSession,
        quote_id: str,
    ) -> dict[str, Any] | None:
        getter = getattr(tx, "get_quote_for_update", None)
        if getter:
            return getter(quote_id)
        return tx.get_quote(quote_id)

    def _owner(self, body: dict[str, Any]) -> tuple[str, str, str]:
        return (
            str(body["web_tenant_id"]),
            str(body["web_user_id"]),
            str(body["web_order_id"]),
        )

    def _owner_matches(
        self,
        row: dict[str, Any],
        owner: tuple[str, str, str],
    ) -> bool:
        return (
            row["web_tenant_id"],
            row["web_user_id"],
            row["web_order_id"],
        ) == owner

    def _quote_matches_request(
        self,
        quote: dict[str, Any],
        body: dict[str, Any],
    ) -> bool:
        return (
            quote["web_tenant_id"] == body["web_tenant_id"]
            and quote["web_user_id"] == body["web_user_id"]
            and quote["template_id"] == body["template_id"]
            and quote["combo_key"] == body["combo_key"]
        )

    def _check_idempotency(
        self,
        tx: WalletSession,
        operation: str,
        idempotency_key: str,
        request_hash: str,
        owner: tuple[str, str, str],
    ) -> dict[str, Any] | None:
        record = tx.get_idempotency(operation, idempotency_key)
        if not record:
            return None
        if (
            record["web_tenant_id"],
            record["web_user_id"],
            record["web_order_id"],
        ) != owner:
            raise WalletError(
                404,
                "TRANSACTION_NOT_FOUND",
                "Transaction not found.",
            )
        if record["request_hash"] != request_hash:
            raise WalletError(
                409,
                "IDEMPOTENCY_KEY_CONFLICT",
                "Idempotency key was reused with a different request body.",
            )
        return record

    def _save_idempotency(
        self,
        tx: WalletSession,
        operation: str,
        idempotency_key: str,
        request_hash: str,
        owner: tuple[str, str, str],
        data: dict[str, Any],
        status_code: int,
        wallet_transaction_id: str | None = None,
    ) -> None:
        now = self.now()
        tx.insert_idempotency(
            {
                "operation": operation,
                "idempotency_key": idempotency_key,
                "request_hash": request_hash,
                "response_status": status_code,
                "response_body": {"success": True, "data": data},
                "web_tenant_id": owner[0],
                "web_user_id": owner[1],
                "web_order_id": owner[2],
                "wallet_transaction_id": wallet_transaction_id,
                "first_seen_at": now,
                "last_replay_at": now,
            }
        )

    def _get_owned_transaction(
        self,
        tx: WalletSession,
        wallet_transaction_id: str,
        owner: tuple[str, str, str],
        *,
        for_update: bool = False,
    ) -> dict[str, Any]:
        getter = tx.get_transaction
        if for_update:
            getter = getattr(tx, "get_transaction_for_update", tx.get_transaction)
        transaction = getter(wallet_transaction_id)
        if not transaction or not self._owner_matches(transaction, owner):
            raise WalletError(
                404,
                "TRANSACTION_NOT_FOUND",
                "Transaction not found.",
            )
        return transaction

    def _insert_ledger(
        self,
        tx: WalletSession,
        entry_type: str,
        account: dict[str, Any],
        transaction: dict[str, Any],
        now: datetime,
        *,
        amount_points: str | None = None,
    ) -> None:
        tx.insert_ledger_entry(
            {
                "ledger_entry_id": new_id("wle"),
                "wallet_transaction_id": transaction["wallet_transaction_id"],
                "wallet_account_id": account["wallet_account_id"],
                "entry_type": entry_type,
                "amount_points": amount_points or transaction["amount_points"],
                "balance_available_after": account["available_balance"],
                "balance_frozen_after": account["frozen_balance"],
                "created_at": now,
            }
        )

    def _quote_response(self, quote: dict[str, Any]) -> dict[str, Any]:
        return {
            "quote_id": quote["quote_id"],
            "status": quote["status"],
            "web_tenant_id": quote["web_tenant_id"],
            "web_user_id": quote["web_user_id"],
            "web_order_id": quote["web_order_id"],
            "template_id": quote["template_id"],
            "combo_key": quote["combo_key"],
            "hard_price": money_str(quote["hard_price"]),
            "amount_points": money_str(quote["amount_points"]),
            "pricing_rule_version": quote["pricing_rule_version"],
            "expires_at": iso_z(quote["expires_at"]),
            "pricing_metadata": quote["pricing_metadata"],
            "correlation": quote["correlation"],
        }

    def _transaction_response(
        self,
        tx: WalletSession,
        transaction: dict[str, Any],
    ) -> dict[str, Any]:
        quote = tx.get_quote(transaction["quote_id"])
        expires_at = quote["expires_at"] if quote else None
        data = {
            "wallet_transaction_id": transaction["wallet_transaction_id"],
            "state": transaction["state"],
            "quote_id": transaction["quote_id"],
            "wallet_account_id": transaction["wallet_account_id"],
            "web_tenant_id": transaction["web_tenant_id"],
            "web_user_id": transaction["web_user_id"],
            "web_order_id": transaction["web_order_id"],
            "amount_points": money_str(transaction["amount_points"]),
            "pricing_rule_version": transaction["pricing_rule_version"],
            "frozen_at": iso_z(transaction["frozen_at"]),
            "expires_at": iso_z(expires_at),
            "confirmed_at": iso_z(transaction["confirmed_at"]),
            "refunded_at": iso_z(transaction["refunded_at"]),
            "correlation": transaction["correlation"],
        }
        if transaction["state"] == "REFUNDED":
            data["refund_reason_code"] = transaction["refund_reason_code"]
        elif transaction.get("refunded_at"):
            data["refund_reason_code"] = transaction["refund_reason_code"]
            data["refund_amount_points"] = (
                transaction.get("refund_correlation", {}).get("refund_amount_points")
            )
        return data

    def _query_response(
        self,
        tx: WalletSession,
        transaction: dict[str, Any],
    ) -> dict[str, Any]:
        quote = tx.get_quote(transaction["quote_id"])
        state = transaction["state"]
        amount = money_str(transaction["amount_points"])
        is_refunded = state == "REFUNDED"
        is_confirmed = state == "CONFIRMED"
        refund_amount = money_str(tx.get_refund_ledger_total(transaction["wallet_transaction_id"]))
        net_consumption = None
        if is_refunded:
            net_consumption = "0.00"
        elif is_confirmed:
            net_consumption = money_str(money(amount) - money(refund_amount))
        return {
            "wallet_transaction_id": transaction["wallet_transaction_id"],
            "state": state,
            "quote": {
                "quote_id": quote["quote_id"],
                "template_id": quote["template_id"],
                "combo_key": quote["combo_key"],
                "amount_points": money_str(quote["amount_points"]),
                "pricing_rule_version": quote["pricing_rule_version"],
                "expires_at": iso_z(quote["expires_at"]),
            },
            "billing_summary": {
                "hard_price": money_str(quote["amount_points"]),
                "discount_amount": "0.00",
                "refunded_amount": refund_amount,
                "net_consumption": net_consumption,
            },
            "wallet_account_id": transaction["wallet_account_id"],
            "web_tenant_id": transaction["web_tenant_id"],
            "web_user_id": transaction["web_user_id"],
            "web_order_id": transaction["web_order_id"],
            "correlation": {
                "web_master_task_id": transaction["correlation"].get(
                    "web_master_task_id"
                ),
                "api_task_id": transaction["correlation"].get("api_task_id"),
                "api_request_id": transaction["correlation"].get("api_request_id"),
                "api_correlation_id": transaction["correlation"].get(
                    "api_correlation_id"
                ),
            },
            "timestamps": {
                "created_at": iso_z(transaction["created_at"]),
                "frozen_at": iso_z(transaction["frozen_at"]),
                "confirmed_at": iso_z(transaction["confirmed_at"]),
                "refunded_at": iso_z(transaction["refunded_at"]),
                "updated_at": iso_z(transaction["updated_at"]),
            },
        }

    def _quota_board_row(self, row: dict[str, Any]) -> dict[str, Any]:
        return {
            "wallet_account_id": row["wallet_account_id"],
            "web_tenant_id": row["web_tenant_id"],
            "web_user_id": row["web_user_id"],
            "available_balance": money_str(row["available_balance"]),
            "frozen_balance": money_str(row["frozen_balance"]),
            "transaction_count": int(row["transaction_count"]),
            "confirmed_count": int(row["confirmed_count"]),
            "refunded_count": int(row["refunded_count"]),
            "open_frozen_count": int(row["open_frozen_count"]),
            "confirmed_points": money_str(row["confirmed_points"]),
            "refunded_points": money_str(row["refunded_points"]),
            "open_frozen_points": money_str(row["open_frozen_points"]),
            "net_confirmed_points": money_str(row["net_confirmed_points"]),
            "updated_at": iso_z(row["updated_at"]),
        }

    def _quota_board_summary(self, rows: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "account_count": len(rows),
            "available_balance": money_str(
                sum(money(row["available_balance"]) for row in rows)
            ),
            "frozen_balance": money_str(
                sum(money(row["frozen_balance"]) for row in rows)
            ),
            "confirmed_points": money_str(
                sum(money(row["confirmed_points"]) for row in rows)
            ),
            "refunded_points": money_str(
                sum(money(row["refunded_points"]) for row in rows)
            ),
            "open_frozen_points": money_str(
                sum(money(row["open_frozen_points"]) for row in rows)
            ),
            "net_confirmed_points": money_str(
                sum(money(row["net_confirmed_points"]) for row in rows)
            ),
        }

    def _hard_price_step_state_response(
        self,
        tx: WalletSession,
        quote: dict[str, Any],
        transaction: dict[str, Any] | None,
        now: datetime,
    ) -> dict[str, Any]:
        amount = money_str(quote["amount_points"])
        quote_expired = quote["expires_at"] <= now and transaction is None
        failure = None
        if quote_expired:
            failure = {
                "code": "QUOTE_EXPIRED",
                "message": "Quote expired before wallet freeze.",
                "retryable": False,
                "step": "freeze",
            }

        if transaction is None:
            state = "QUOTE_EXPIRED" if quote_expired else "QUOTED"
            wallet_transaction_id = None
            wallet_account_id = None
            transaction_state = None
            frozen_at = None
            confirmed_at = None
            refunded_at = None
            correlation = {}
            refund_reason_code = None
        else:
            state = transaction["state"]
            wallet_transaction_id = transaction["wallet_transaction_id"]
            wallet_account_id = transaction["wallet_account_id"]
            transaction_state = transaction["state"]
            frozen_at = transaction["frozen_at"]
            confirmed_at = transaction["confirmed_at"]
            refunded_at = transaction["refunded_at"]
            correlation = transaction["correlation"]
            refund_reason_code = transaction["refund_reason_code"]

        steps = [
            {
                "step": "quote",
                "status": "completed",
                "hard_price_state": quote["status"],
                "wallet_transaction_id": None,
                "amount_points": amount,
                "deduction_state": "not_started",
                "failure": None,
            },
            {
                "step": "freeze",
                "status": self._freeze_step_status(transaction, quote_expired),
                "hard_price_state": transaction_state or quote["status"],
                "wallet_transaction_id": wallet_transaction_id,
                "amount_points": amount,
                "deduction_state": "frozen" if transaction else "not_started",
                "failure": failure if quote_expired else None,
            },
            {
                "step": "settlement",
                "status": self._settlement_step_status(transaction, quote_expired),
                "hard_price_state": transaction_state or quote["status"],
                "wallet_transaction_id": wallet_transaction_id,
                "amount_points": amount,
                "deduction_state": self._settlement_deduction_state(transaction),
                "failure": None,
            },
        ]

        return {
            "web_tenant_id": quote["web_tenant_id"],
            "web_user_id": quote["web_user_id"],
            "web_order_id": quote["web_order_id"],
            "state": state,
            "quote": {
                "quote_id": quote["quote_id"],
                "template_id": quote["template_id"],
                "combo_key": quote["combo_key"],
                "hard_price": money_str(quote["hard_price"]),
                "amount_points": amount,
                "pricing_rule_version": quote["pricing_rule_version"],
                "expires_at": iso_z(quote["expires_at"]),
                "expired": quote_expired,
                "pricing_metadata": quote["pricing_metadata"],
                "correlation": quote["correlation"],
            },
            "wallet": {
                "wallet_transaction_id": wallet_transaction_id,
                "wallet_account_id": wallet_account_id,
                "transaction_state": transaction_state,
                "amount_points": amount,
                "deduction_state": self._settlement_deduction_state(transaction),
                "frozen_at": iso_z(frozen_at),
                "confirmed_at": iso_z(confirmed_at),
                "refunded_at": iso_z(refunded_at),
                "refund_reason_code": refund_reason_code,
            },
            "correlation": {
                "web_master_task_id": correlation.get("web_master_task_id"),
                "api_task_id": correlation.get("api_task_id"),
                "api_request_id": correlation.get("api_request_id"),
                "api_correlation_id": correlation.get("api_correlation_id"),
            },
            "failure": failure,
            "steps": steps,
        }

    def _order_subtask_mapping_response(
        self,
        quote: dict[str, Any],
        transaction: dict[str, Any] | None,
        now: datetime,
    ) -> dict[str, Any]:
        subtasks = self._subtasks_from_transaction(transaction)
        buckets: dict[str, dict[str, Any]] = {}
        for subtask in subtasks:
            bucket_id = subtask["bucket_id"]
            bucket = buckets.setdefault(
                bucket_id,
                {
                    "bucket_id": bucket_id,
                    "subtask_count": 0,
                    "statuses": {},
                    "amount_points": "0.00",
                },
            )
            bucket["subtask_count"] += 1
            status = subtask["status"]
            bucket["statuses"][status] = bucket["statuses"].get(status, 0) + 1
            bucket["amount_points"] = money_str(
                money(bucket["amount_points"]) + money(subtask["amount_points"])
            )

        quote_expired = quote["expires_at"] <= now and transaction is None
        failure = None
        if quote_expired:
            failure = {
                "code": "QUOTE_EXPIRED",
                "message": "Quote expired before wallet freeze.",
                "retryable": False,
                "step": "freeze",
            }
        elif transaction and transaction["state"] == "REFUNDED":
            failure = {
                "code": transaction["refund_reason_code"],
                "message": transaction["refund_reason_message"],
                "retryable": False,
                "step": "settlement",
            }

        return {
            "order": {
                "web_tenant_id": quote["web_tenant_id"],
                "web_user_id": quote["web_user_id"],
                "web_order_id": quote["web_order_id"],
                "quote_id": quote["quote_id"],
                "wallet_transaction_id": (
                    transaction["wallet_transaction_id"] if transaction else None
                ),
                "state": self._mapping_state(quote, transaction, quote_expired),
                "template_id": quote["template_id"],
                "combo_key": quote["combo_key"],
                "amount_points": money_str(quote["amount_points"]),
                "pricing_rule_version": quote["pricing_rule_version"],
            },
            "subtasks": subtasks,
            "buckets": list(buckets.values()),
            "failure": failure,
            "audit": {
                "quote_created_at": iso_z(quote["created_at"]),
                "quote_expires_at": iso_z(quote["expires_at"]),
                "frozen_at": iso_z(transaction["frozen_at"] if transaction else None),
                "confirmed_at": iso_z(
                    transaction["confirmed_at"] if transaction else None
                ),
                "refunded_at": iso_z(
                    transaction["refunded_at"] if transaction else None
                ),
                "updated_at": iso_z(
                    transaction["updated_at"] if transaction else quote["updated_at"]
                ),
            },
        }

    def _subtasks_from_transaction(
        self,
        transaction: dict[str, Any] | None,
    ) -> list[dict[str, Any]]:
        if not transaction:
            return []

        correlation = transaction["correlation"] or {}
        raw_subtasks = correlation.get("subtasks") or []
        if not raw_subtasks and correlation.get("web_master_task_id"):
            raw_subtasks = [
                {
                    "web_master_task_id": correlation.get("web_master_task_id"),
                    "api_task_id": correlation.get("api_task_id"),
                    "api_request_id": correlation.get("api_request_id"),
                    "bucket_id": correlation.get("bucket_id"),
                }
            ]

        result = []
        for index, subtask in enumerate(raw_subtasks, start=1):
            amount_points = subtask.get("amount_points")
            if amount_points is None:
                amount_points = transaction["amount_points"]
            try:
                normalized_amount_points = money_str(amount_points)
            except (DecimalException, ValueError) as exc:
                raise WalletError(
                    409,
                    "ORDER_SUBTASK_MAPPING_INVALID",
                    "Stored order-to-subtask mapping contains invalid amount_points.",
                ) from exc
            attempt = subtask.get("attempt")
            if attempt is None:
                attempt = index
            result.append(
                {
                    "web_master_task_id": subtask.get("web_master_task_id"),
                    "api_task_id": subtask.get("api_task_id"),
                    "api_request_id": subtask.get("api_request_id"),
                    "bucket_id": subtask.get("bucket_id") or "default",
                    "status": subtask.get("status") or transaction["state"].lower(),
                    "attempt": attempt,
                    "amount_points": normalized_amount_points,
                    "failure_code": subtask.get("failure_code"),
                    "failure_message": subtask.get("failure_message"),
                }
            )
        return result

    def _mapping_state(
        self,
        quote: dict[str, Any],
        transaction: dict[str, Any] | None,
        quote_expired: bool,
    ) -> str:
        if transaction:
            return transaction["state"]
        if quote_expired:
            return "QUOTE_EXPIRED"
        return "QUOTED"

    def _freeze_step_status(
        self,
        transaction: dict[str, Any] | None,
        quote_expired: bool,
    ) -> str:
        if transaction:
            return "completed"
        if quote_expired:
            return "blocked"
        return "pending"

    def _settlement_step_status(
        self,
        transaction: dict[str, Any] | None,
        quote_expired: bool,
    ) -> str:
        if not transaction:
            return "blocked" if quote_expired else "pending"
        if transaction["state"] in {"CONFIRMED", "REFUNDED"}:
            return "completed"
        return "pending"

    def _settlement_deduction_state(
        self,
        transaction: dict[str, Any] | None,
    ) -> str:
        if not transaction:
            return "not_started"
        if transaction["state"] == "CONFIRMED":
            return "deducted"
        if transaction["state"] == "REFUNDED":
            return "released"
        return "frozen"

    def _correlation_matches(
        self,
        stored: dict[str, Any],
        candidate: dict[str, Any],
    ) -> bool:
        keys = [
            "web_master_task_id",
            "api_task_id",
            "api_request_id",
            "api_correlation_id",
        ]
        return all(stored.get(key) == candidate.get(key) for key in keys)

    def _guard_against_double_billing(
        self,
        transaction: dict[str, Any],
        correlation: dict[str, Any],
    ) -> None:
        evidence = self._legacy_billing_evidence(transaction, correlation)
        if not evidence:
            return
        WALLET_DOUBLE_BILLING_BLOCKED.inc()
        raise WalletError(
            409,
            "WALLET_DOUBLE_BILLING_GUARD",
            "Legacy consume_budget evidence exists for this template price wallet order.",
            retryable=False,
            details={
                "web_order_id": transaction["web_order_id"],
                "wallet_transaction_id": transaction["wallet_transaction_id"],
                "legacy_billing_evidence": evidence,
            },
        )

    def _legacy_billing_evidence(
        self,
        transaction: dict[str, Any],
        correlation: dict[str, Any],
    ) -> dict[str, Any] | None:
        if self.legacy_billing_evidence:
            evidence = self.legacy_billing_evidence(transaction, correlation)
            if evidence:
                return evidence
        evidence = correlation.get("legacy_consume_budget_evidence")
        return evidence if isinstance(evidence, dict) and evidence else None

    def _freeze_correlation_matches(
        self,
        stored: dict[str, Any],
        candidate: dict[str, Any],
    ) -> bool:
        stored_master_task_id = stored.get("web_master_task_id")
        candidate_master_task_id = candidate.get("web_master_task_id")
        if stored_master_task_id:
            return stored_master_task_id == candidate_master_task_id
        return True

    def _refund_matches(
        self,
        transaction: dict[str, Any],
        body: dict[str, Any],
        refund_correlation: dict[str, Any],
    ) -> bool:
        if transaction["refund_reason_code"] != body["reason_code"]:
            return False
        if transaction["refund_reason_message"] != body["reason_message"]:
            return False
        return transaction["refund_correlation"] == refund_correlation
