from __future__ import annotations

import threading
from contextlib import contextmanager
from datetime import datetime
from typing import Any, Callable

from .common import account_id_for, money, money_str, utcnow
from .errors import WalletError
from .store import WalletSession, WalletStore


class InMemoryWalletStore(WalletStore):
    def __init__(self, now: Callable[[], datetime] = utcnow) -> None:
        self.now = now
        self._lock = threading.RLock()
        self.prices: dict[tuple[int, str], dict[str, Any]] = {}
        self.pricing_unavailable = False
        self.accounts: dict[tuple[str, str], dict[str, Any]] = {}
        self.quotes: dict[str, dict[str, Any]] = {}
        self.quote_by_order: dict[str, str] = {}
        self.transactions: dict[str, dict[str, Any]] = {}
        self.transaction_by_order: dict[tuple[str, str, str], str] = {}
        self.idempotency: dict[tuple[str, str], dict[str, Any]] = {}
        self.ledger_entries: list[dict[str, Any]] = []

    @contextmanager
    def transaction(self) -> WalletSession:
        with self._lock:
            yield InMemoryWalletSession(self)

    def upsert_price(
        self,
        *,
        template_id: int,
        combo_key: str,
        hard_price: str,
        pricing_rule_version: int,
        text_chars: int,
        text_lines: int,
    ) -> None:
        self.prices[(template_id, combo_key)] = {
            "template_id": template_id,
            "combo_key": combo_key,
            "hard_price": money_str(hard_price),
            "pricing_rule_version": pricing_rule_version,
            "text_chars": text_chars,
            "text_lines": text_lines,
            "billing_duration_minutes": (text_lines + 24) // 25,
        }

    def set_balance(
        self,
        web_tenant_id: str,
        web_user_id: str,
        available_balance: str,
        frozen_balance: str = "0.00",
    ) -> None:
        account_id = account_id_for(web_tenant_id, web_user_id)
        now = self.now()
        self.accounts[(web_tenant_id, web_user_id)] = {
            "wallet_account_id": account_id,
            "web_tenant_id": web_tenant_id,
            "web_user_id": web_user_id,
            "available_balance": money_str(available_balance),
            "frozen_balance": money_str(frozen_balance),
            "created_at": now,
            "updated_at": now,
        }

    def set_pricing_unavailable(self, unavailable: bool) -> None:
        self.pricing_unavailable = unavailable

    def get_account(self, web_tenant_id: str, web_user_id: str) -> dict[str, Any]:
        return self.accounts[(web_tenant_id, web_user_id)].copy()

    def get_transaction_by_order(
        self,
        web_tenant_id: str,
        web_user_id: str,
        web_order_id: str,
    ) -> dict[str, Any] | None:
        key = (web_tenant_id, web_user_id, web_order_id)
        tx_id = self.transaction_by_order.get(key)
        if not tx_id:
            return None
        return self.transactions[tx_id].copy()


class InMemoryWalletSession(WalletSession):
    def __init__(self, store: InMemoryWalletStore) -> None:
        self.store = store

    def get_price(self, template_id: int, combo_key: str) -> dict[str, Any] | None:
        if self.store.pricing_unavailable:
            raise WalletError(
                503,
                "PRICING_UNAVAILABLE",
                "Pricing data is temporarily unavailable.",
                retryable=True,
            )
        row = self.store.prices.get((template_id, combo_key))
        return row.copy() if row else None

    def get_quote(self, quote_id: str) -> dict[str, Any] | None:
        row = self.store.quotes.get(quote_id)
        return row.copy() if row else None

    def get_quote_by_order(self, web_order_id: str) -> dict[str, Any] | None:
        quote_id = self.store.quote_by_order.get(web_order_id)
        if not quote_id:
            return None
        return self.get_quote(quote_id)

    def insert_quote(self, quote: dict[str, Any]) -> None:
        self.store.quotes[quote["quote_id"]] = quote.copy()
        self.store.quote_by_order[quote["web_order_id"]] = quote["quote_id"]

    def update_quote(self, quote: dict[str, Any]) -> None:
        self.store.quotes[quote["quote_id"]] = quote.copy()

    def get_account_for_update(
        self,
        web_tenant_id: str,
        web_user_id: str,
    ) -> dict[str, Any]:
        key = (web_tenant_id, web_user_id)
        if key not in self.store.accounts:
            self.store.set_balance(web_tenant_id, web_user_id, "0.00")
        return self.store.accounts[key].copy()

    def update_account(self, account: dict[str, Any]) -> None:
        key = (account["web_tenant_id"], account["web_user_id"])
        self.store.accounts[key] = account.copy()

    def get_transaction(self, wallet_transaction_id: str) -> dict[str, Any] | None:
        row = self.store.transactions.get(wallet_transaction_id)
        return row.copy() if row else None

    def get_transaction_by_order(
        self,
        web_tenant_id: str,
        web_user_id: str,
        web_order_id: str,
    ) -> dict[str, Any] | None:
        key = (web_tenant_id, web_user_id, web_order_id)
        tx_id = self.store.transaction_by_order.get(key)
        if not tx_id:
            return None
        return self.get_transaction(tx_id)

    def insert_transaction(self, transaction: dict[str, Any]) -> None:
        self.store.transactions[transaction["wallet_transaction_id"]] = (
            transaction.copy()
        )
        key = (
            transaction["web_tenant_id"],
            transaction["web_user_id"],
            transaction["web_order_id"],
        )
        self.store.transaction_by_order[key] = transaction["wallet_transaction_id"]

    def update_transaction(self, transaction: dict[str, Any]) -> None:
        self.store.transactions[transaction["wallet_transaction_id"]] = (
            transaction.copy()
        )

    def insert_ledger_entry(self, entry: dict[str, Any]) -> None:
        self.store.ledger_entries.append(entry.copy())

    def get_refund_ledger_total(self, wallet_transaction_id: str) -> str:
        total = sum(
            money(entry["amount_points"])
            for entry in self.store.ledger_entries
            if entry["wallet_transaction_id"] == wallet_transaction_id
            and entry["entry_type"] == "REFUND"
        )
        return money_str(total)

    def list_quota_board(
        self,
        *,
        web_tenant_id: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        rows = []
        for account in self.store.accounts.values():
            if web_tenant_id and account["web_tenant_id"] != web_tenant_id:
                continue
            transactions = [
                tx
                for tx in self.store.transactions.values()
                if tx["wallet_account_id"] == account["wallet_account_id"]
            ]
            transaction_by_id = {
                tx["wallet_transaction_id"]: tx for tx in transactions
            }
            refund_total = sum(
                money(entry["amount_points"])
                for entry in self.store.ledger_entries
                if entry["wallet_account_id"] == account["wallet_account_id"]
                and entry["entry_type"] == "REFUND"
            )
            confirmed_refund_total = sum(
                money(entry["amount_points"])
                for entry in self.store.ledger_entries
                if entry["wallet_account_id"] == account["wallet_account_id"]
                and entry["entry_type"] == "REFUND"
                and transaction_by_id.get(
                    entry["wallet_transaction_id"], {}
                ).get("state")
                == "CONFIRMED"
            )
            confirmed_total = sum(
                money(tx["amount_points"])
                for tx in transactions
                if tx["state"] == "CONFIRMED"
            )
            open_frozen_total = sum(
                money(tx["amount_points"])
                for tx in transactions
                if tx["state"] == "FROZEN"
            )
            rows.append(
                {
                    **account.copy(),
                    "transaction_count": len(transactions),
                    "confirmed_count": sum(
                        1 for tx in transactions if tx["state"] == "CONFIRMED"
                    ),
                    "refunded_count": sum(
                        1
                        for tx in transactions
                        if tx["state"] == "REFUNDED" or tx.get("refunded_at")
                    ),
                    "open_frozen_count": sum(
                        1 for tx in transactions if tx["state"] == "FROZEN"
                    ),
                    "confirmed_points": money_str(confirmed_total),
                    "refunded_points": money_str(refund_total),
                    "open_frozen_points": money_str(open_frozen_total),
                    "net_confirmed_points": money_str(
                        confirmed_total - confirmed_refund_total
                    ),
                }
            )
        rows.sort(key=lambda row: (row["web_tenant_id"], row["web_user_id"]))
        return rows[:limit]

    def get_idempotency(self, operation: str, key: str) -> dict[str, Any] | None:
        row = self.store.idempotency.get((operation, key))
        return row.copy() if row else None

    def get_idempotency_by_key(self, key: str) -> dict[str, Any] | None:
        for (_operation, idempotency_key), row in self.store.idempotency.items():
            if idempotency_key == key:
                return row.copy()
        return None

    def insert_idempotency(self, record: dict[str, Any]) -> None:
        self.store.idempotency[(record["operation"], record["idempotency_key"])] = (
            record.copy()
        )

    def delete_idempotency_before(
        self, cutoff: datetime, batch_size: int = 1000
    ) -> int:
        expired_keys = [
            key
            for key, record in self.store.idempotency.items()
            if record["first_seen_at"] < cutoff
        ][:batch_size]
        for key in expired_keys:
            del self.store.idempotency[key]
        return len(expired_keys)
