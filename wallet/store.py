from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime
from typing import Any


class WalletSession:
    def get_price(self, template_id: int, combo_key: str) -> dict[str, Any] | None:
        raise NotImplementedError

    def get_quote(self, quote_id: str) -> dict[str, Any] | None:
        raise NotImplementedError

    def get_quote_by_order(self, web_order_id: str) -> dict[str, Any] | None:
        raise NotImplementedError

    def insert_quote(self, quote: dict[str, Any]) -> None:
        raise NotImplementedError

    def update_quote(self, quote: dict[str, Any]) -> None:
        raise NotImplementedError

    def get_account_for_update(
        self,
        web_tenant_id: str,
        web_user_id: str,
    ) -> dict[str, Any]:
        raise NotImplementedError

    def update_account(self, account: dict[str, Any]) -> None:
        raise NotImplementedError

    def get_transaction(self, wallet_transaction_id: str) -> dict[str, Any] | None:
        raise NotImplementedError

    def get_transaction_by_order(
        self,
        web_tenant_id: str,
        web_user_id: str,
        web_order_id: str,
    ) -> dict[str, Any] | None:
        raise NotImplementedError

    def insert_transaction(self, transaction: dict[str, Any]) -> None:
        raise NotImplementedError

    def update_transaction(self, transaction: dict[str, Any]) -> None:
        raise NotImplementedError

    def insert_ledger_entry(self, entry: dict[str, Any]) -> None:
        raise NotImplementedError

    def get_refund_ledger_total(self, wallet_transaction_id: str) -> str:
        raise NotImplementedError

    def list_quota_board(
        self,
        *,
        web_tenant_id: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        raise NotImplementedError

    def get_idempotency(self, operation: str, key: str) -> dict[str, Any] | None:
        raise NotImplementedError

    def get_idempotency_by_key(self, key: str) -> dict[str, Any] | None:
        raise NotImplementedError

    def insert_idempotency(self, record: dict[str, Any]) -> None:
        raise NotImplementedError

    def delete_idempotency_before(
        self, cutoff: datetime, batch_size: int = 1000
    ) -> int:
        raise NotImplementedError

    def lock_idempotency(self, operation: str, key: str) -> None:
        return None

    def lock_order(self, web_order_id: str) -> None:
        return None

    def get_quote_for_update(self, quote_id: str) -> dict[str, Any] | None:
        return self.get_quote(quote_id)

    def get_transaction_for_update(
        self,
        wallet_transaction_id: str,
    ) -> dict[str, Any] | None:
        return self.get_transaction(wallet_transaction_id)


class WalletStore:
    @contextmanager
    def transaction(self) -> WalletSession:
        raise NotImplementedError
