from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import datetime
from typing import Any, Callable

from sqlalchemy import (
    and_,
    case,
    delete,
    desc,
    func,
    literal,
    select,
    text,
    tuple_,
    update,
)
from sqlalchemy.dialects.postgresql import insert as pg_insert

from db.tables import (
    wallet_accounts,
    wallet_idempotency_records,
    wallet_ledger_entries,
    wallet_quotes,
    wallet_transactions,
)
from pricing.queries import select_single_hard_price

from .common import account_id_for, money, money_str, utcnow
from .schema import WALLET_SCHEMA_SQL
from .store import WalletSession, WalletStore


class PostgresWalletStore(WalletStore):
    def __init__(self, connection_factory: Callable[[], Any]) -> None:
        self.connection_factory = connection_factory

    @staticmethod
    def schema_sql() -> str:
        return WALLET_SCHEMA_SQL

    def ensure_schema(self) -> None:
        conn = self.connection_factory()
        tx = conn.begin()
        try:
            conn.execute(text(self.schema_sql()))
            tx.commit()
        except Exception:
            tx.rollback()
            raise
        finally:
            conn.close()

    @contextmanager
    def transaction(self) -> WalletSession:
        conn = self.connection_factory()
        tx = conn.begin()
        try:
            yield PostgresWalletSession(conn)
            tx.commit()
        except Exception:
            tx.rollback()
            raise
        finally:
            conn.close()


class PostgresWalletSession(WalletSession):
    def __init__(self, connection: Any) -> None:
        self.conn = connection

    def get_price(self, template_id: int, combo_key: str) -> dict[str, Any] | None:
        return self._one(select_single_hard_price(template_id, combo_key))

    def get_quote(self, quote_id: str) -> dict[str, Any] | None:
        return self._one(
            select(wallet_quotes).where(wallet_quotes.c.quote_id == quote_id)
        )

    def get_quote_for_update(self, quote_id: str) -> dict[str, Any] | None:
        return self._one(
            select(wallet_quotes)
            .where(wallet_quotes.c.quote_id == quote_id)
            .with_for_update()
        )

    def get_quote_by_order(self, web_order_id: str) -> dict[str, Any] | None:
        return self._one(
            select(wallet_quotes).where(wallet_quotes.c.web_order_id == web_order_id)
        )

    def insert_quote(self, quote: dict[str, Any]) -> None:
        self.conn.execute(wallet_quotes.insert().values(self._quote_values(quote)))

    def update_quote(self, quote: dict[str, Any]) -> None:
        self.conn.execute(
            update(wallet_quotes)
            .where(wallet_quotes.c.quote_id == quote["quote_id"])
            .values(status=quote["status"], updated_at=quote["updated_at"])
        )

    def get_account_for_update(
        self,
        web_tenant_id: str,
        web_user_id: str,
    ) -> dict[str, Any]:
        row = self._one(
            select(wallet_accounts)
            .where(wallet_accounts.c.web_tenant_id == web_tenant_id)
            .where(wallet_accounts.c.web_user_id == web_user_id)
            .with_for_update()
        )
        if row:
            return row

        now = utcnow()
        account = {
            "wallet_account_id": account_id_for(web_tenant_id, web_user_id),
            "web_tenant_id": web_tenant_id,
            "web_user_id": web_user_id,
            "available_balance": "0.00",
            "frozen_balance": "0.00",
            "created_at": now,
            "updated_at": now,
        }
        stmt = (
            pg_insert(wallet_accounts)
            .values(account)
            .on_conflict_do_nothing(index_elements=["web_tenant_id", "web_user_id"])
        )
        self.conn.execute(stmt)
        row = self._one(
            select(wallet_accounts)
            .where(wallet_accounts.c.web_tenant_id == web_tenant_id)
            .where(wallet_accounts.c.web_user_id == web_user_id)
            .with_for_update()
        )
        if row:
            return row
        return account

    def update_account(self, account: dict[str, Any]) -> None:
        self.conn.execute(
            update(wallet_accounts)
            .where(wallet_accounts.c.wallet_account_id == account["wallet_account_id"])
            .values(
                available_balance=account["available_balance"],
                frozen_balance=account["frozen_balance"],
                updated_at=account["updated_at"],
            )
        )

    def get_transaction(self, wallet_transaction_id: str) -> dict[str, Any] | None:
        return self._one(
            select(wallet_transactions).where(
                wallet_transactions.c.wallet_transaction_id == wallet_transaction_id
            )
        )

    def get_transaction_for_update(
        self,
        wallet_transaction_id: str,
    ) -> dict[str, Any] | None:
        return self._one(
            select(wallet_transactions)
            .where(wallet_transactions.c.wallet_transaction_id == wallet_transaction_id)
            .with_for_update()
        )

    def get_transaction_by_order(
        self,
        web_tenant_id: str,
        web_user_id: str,
        web_order_id: str,
    ) -> dict[str, Any] | None:
        return self._one(
            select(wallet_transactions)
            .where(wallet_transactions.c.web_tenant_id == web_tenant_id)
            .where(wallet_transactions.c.web_user_id == web_user_id)
            .where(wallet_transactions.c.web_order_id == web_order_id)
        )

    def insert_transaction(self, transaction: dict[str, Any]) -> None:
        self.conn.execute(
            wallet_transactions.insert().values(self._transaction_values(transaction))
        )

    def update_transaction(self, transaction: dict[str, Any]) -> None:
        self.conn.execute(
            update(wallet_transactions)
            .where(
                wallet_transactions.c.wallet_transaction_id
                == transaction["wallet_transaction_id"]
            )
            .values(
                state=transaction["state"],
                confirmed_at=transaction["confirmed_at"],
                refunded_at=transaction["refunded_at"],
                refund_reason_code=transaction["refund_reason_code"],
                refund_reason_message=transaction["refund_reason_message"],
                correlation=transaction["correlation"],
                refund_correlation=transaction["refund_correlation"],
                updated_at=transaction["updated_at"],
            )
        )

    def insert_ledger_entry(self, entry: dict[str, Any]) -> None:
        self.conn.execute(wallet_ledger_entries.insert().values(entry))

    def get_refund_ledger_total(self, wallet_transaction_id: str) -> str:
        row = self.conn.execute(
            select(func.coalesce(func.sum(wallet_ledger_entries.c.amount_points), 0))
            .where(
                wallet_ledger_entries.c.wallet_transaction_id
                == wallet_transaction_id
            )
            .where(wallet_ledger_entries.c.entry_type == "REFUND")
        ).first()
        return money_str(row[0] if row else "0.00")

    def list_quota_board(
        self,
        *,
        web_tenant_id: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        account_filters = []
        if web_tenant_id:
            account_filters.append(wallet_accounts.c.web_tenant_id == web_tenant_id)
        account_scope = (
            select(wallet_accounts)
            .where(and_(*account_filters) if account_filters else literal(True))
            .order_by(wallet_accounts.c.web_tenant_id, wallet_accounts.c.web_user_id)
            .limit(limit)
            .cte("quota_accounts")
        )
        refund_amount = case(
            (
                wallet_ledger_entries.c.entry_type == "REFUND",
                wallet_ledger_entries.c.amount_points,
            ),
            else_=literal(0),
        )
        refund_summary = (
            select(
                wallet_ledger_entries.c.wallet_account_id,
                func.sum(refund_amount).label("refunded_points"),
            )
            .select_from(
                wallet_ledger_entries.join(
                    account_scope,
                    wallet_ledger_entries.c.wallet_account_id
                    == account_scope.c.wallet_account_id,
                )
            )
            .group_by(wallet_ledger_entries.c.wallet_account_id)
            .subquery()
        )
        confirmed_refund_summary = (
            select(
                wallet_ledger_entries.c.wallet_account_id,
                func.sum(wallet_ledger_entries.c.amount_points).label(
                    "confirmed_refunded_points"
                ),
            )
            .select_from(
                wallet_ledger_entries.join(
                    wallet_transactions,
                    wallet_ledger_entries.c.wallet_transaction_id
                    == wallet_transactions.c.wallet_transaction_id,
                ).join(
                    account_scope,
                    wallet_ledger_entries.c.wallet_account_id
                    == account_scope.c.wallet_account_id,
                )
            )
            .where(wallet_ledger_entries.c.entry_type == "REFUND")
            .where(wallet_transactions.c.state == "CONFIRMED")
            .group_by(wallet_ledger_entries.c.wallet_account_id)
            .subquery()
        )
        confirmed_amount = case(
            (
                wallet_transactions.c.state == "CONFIRMED",
                wallet_transactions.c.amount_points,
            ),
            else_=literal(0),
        )
        open_frozen_amount = case(
            (
                wallet_transactions.c.state == "FROZEN",
                wallet_transactions.c.amount_points,
            ),
            else_=literal(0),
        )
        confirmed_count = case(
            (wallet_transactions.c.state == "CONFIRMED", literal(1)),
            else_=literal(0),
        )
        refunded_count = case(
            (
                (wallet_transactions.c.state == "REFUNDED")
                | (wallet_transactions.c.refunded_at.is_not(None)),
                literal(1),
            ),
            else_=literal(0),
        )
        open_frozen_count = case(
            (wallet_transactions.c.state == "FROZEN", literal(1)),
            else_=literal(0),
        )
        transaction_summary = (
            select(
                wallet_transactions.c.wallet_account_id,
                func.count(wallet_transactions.c.wallet_transaction_id).label(
                    "transaction_count"
                ),
                func.sum(confirmed_count).label("confirmed_count"),
                func.sum(refunded_count).label("refunded_count"),
                func.sum(open_frozen_count).label("open_frozen_count"),
                func.sum(confirmed_amount).label("confirmed_points"),
                func.sum(open_frozen_amount).label("open_frozen_points"),
            )
            .select_from(
                wallet_transactions.join(
                    account_scope,
                    wallet_transactions.c.wallet_account_id
                    == account_scope.c.wallet_account_id,
                )
            )
            .group_by(wallet_transactions.c.wallet_account_id)
            .subquery()
        )
        statement = (
            select(
                account_scope,
                func.coalesce(transaction_summary.c.transaction_count, 0).label(
                    "transaction_count"
                ),
                func.coalesce(transaction_summary.c.confirmed_count, 0).label(
                    "confirmed_count"
                ),
                func.coalesce(transaction_summary.c.refunded_count, 0).label(
                    "refunded_count"
                ),
                func.coalesce(transaction_summary.c.open_frozen_count, 0).label(
                    "open_frozen_count"
                ),
                func.coalesce(transaction_summary.c.confirmed_points, 0).label(
                    "confirmed_points"
                ),
                func.coalesce(refund_summary.c.refunded_points, 0).label(
                    "refunded_points"
                ),
                func.coalesce(transaction_summary.c.open_frozen_points, 0).label(
                    "open_frozen_points"
                ),
                func.coalesce(
                    confirmed_refund_summary.c.confirmed_refunded_points, 0
                ).label("confirmed_refunded_points"),
            )
            .select_from(
                account_scope.outerjoin(
                    transaction_summary,
                    account_scope.c.wallet_account_id
                    == transaction_summary.c.wallet_account_id,
                ).outerjoin(
                    refund_summary,
                    account_scope.c.wallet_account_id
                    == refund_summary.c.wallet_account_id,
                ).outerjoin(
                    confirmed_refund_summary,
                    account_scope.c.wallet_account_id
                    == confirmed_refund_summary.c.wallet_account_id,
                )
            )
            .order_by(account_scope.c.web_tenant_id, account_scope.c.web_user_id)
        )
        rows = []
        for row in self.conn.execute(statement).mappings():
            data = self._row(row)
            confirmed_points = money_str(data["confirmed_points"])
            refunded_points = money_str(data["refunded_points"])
            data["confirmed_points"] = confirmed_points
            data["refunded_points"] = refunded_points
            data["open_frozen_points"] = money_str(data["open_frozen_points"])
            data["net_confirmed_points"] = money_str(
                money(confirmed_points) - money(data["confirmed_refunded_points"])
            )
            rows.append(data)
        return rows

    def get_idempotency(self, operation: str, key: str) -> dict[str, Any] | None:
        return self._one(
            select(wallet_idempotency_records)
            .where(wallet_idempotency_records.c.operation == operation)
            .where(wallet_idempotency_records.c.idempotency_key == key)
        )

    def get_idempotency_by_key(self, key: str) -> dict[str, Any] | None:
        return self._one(
            select(wallet_idempotency_records)
            .where(wallet_idempotency_records.c.idempotency_key == key)
            .order_by(desc(wallet_idempotency_records.c.first_seen_at))
            .limit(1)
        )

    def insert_idempotency(self, record: dict[str, Any]) -> None:
        stmt = (
            pg_insert(wallet_idempotency_records)
            .values(record)
            .on_conflict_do_nothing(index_elements=["operation", "idempotency_key"])
        )
        self.conn.execute(stmt)

    def delete_idempotency_before(
        self, cutoff: datetime, batch_size: int = 1000
    ) -> int:
        expired = (
            select(
                wallet_idempotency_records.c.operation,
                wallet_idempotency_records.c.idempotency_key,
            )
            .where(wallet_idempotency_records.c.first_seen_at < cutoff)
            .order_by(wallet_idempotency_records.c.first_seen_at)
            .limit(batch_size)
            .cte("expired")
        )
        stmt = delete(wallet_idempotency_records).where(
            tuple_(
                wallet_idempotency_records.c.operation,
                wallet_idempotency_records.c.idempotency_key,
            ).in_(select(expired.c.operation, expired.c.idempotency_key))
        )
        result = self.conn.execute(stmt)
        return int(result.rowcount or 0)

    def lock_idempotency(self, operation: str, key: str) -> None:
        self.conn.execute(
            text("SELECT pg_advisory_xact_lock(hashtext(:scope), hashtext(:key))"),
            {"scope": "wallet-idempotency", "key": f"{operation}:{key}"},
        )

    def lock_order(self, web_order_id: str) -> None:
        self.conn.execute(
            text("SELECT pg_advisory_xact_lock(hashtext(:scope), hashtext(:key))"),
            {"scope": "wallet-order", "key": web_order_id},
        )

    def _one(self, statement: Any) -> dict[str, Any] | None:
        result = self.conn.execute(statement)
        return self._row(result.mappings().first())

    def _quote_values(self, quote: dict[str, Any]) -> dict[str, Any]:
        return {
            "quote_id": quote["quote_id"],
            "status": quote["status"],
            "web_tenant_id": quote["web_tenant_id"],
            "web_user_id": quote["web_user_id"],
            "web_order_id": quote["web_order_id"],
            "template_id": quote["template_id"],
            "combo_key": quote["combo_key"],
            "hard_price": quote["hard_price"],
            "amount_points": quote["amount_points"],
            "pricing_rule_version": quote["pricing_rule_version"],
            "expires_at": quote["expires_at"],
            "pricing_metadata": quote["pricing_metadata"],
            "correlation": quote["correlation"],
            "created_at": quote["created_at"],
            "updated_at": quote["updated_at"],
        }

    def _transaction_values(self, transaction: dict[str, Any]) -> dict[str, Any]:
        return {
            "wallet_transaction_id": transaction["wallet_transaction_id"],
            "state": transaction["state"],
            "quote_id": transaction["quote_id"],
            "wallet_account_id": transaction["wallet_account_id"],
            "web_tenant_id": transaction["web_tenant_id"],
            "web_user_id": transaction["web_user_id"],
            "web_order_id": transaction["web_order_id"],
            "amount_points": transaction["amount_points"],
            "pricing_rule_version": transaction["pricing_rule_version"],
            "frozen_at": transaction["frozen_at"],
            "confirmed_at": transaction["confirmed_at"],
            "refunded_at": transaction["refunded_at"],
            "refund_reason_code": transaction["refund_reason_code"],
            "refund_reason_message": transaction["refund_reason_message"],
            "correlation": transaction["correlation"],
            "refund_correlation": transaction["refund_correlation"],
            "created_at": transaction["created_at"],
            "updated_at": transaction["updated_at"],
        }

    def _row(self, row: Any) -> dict[str, Any] | None:
        if not row:
            return None
        data = dict(row)
        for field in [
            "hard_price",
            "amount_points",
            "available_balance",
            "frozen_balance",
        ]:
            if field in data and data[field] is not None:
                data[field] = money_str(data[field])
        for field in [
            "pricing_metadata",
            "correlation",
            "refund_correlation",
            "response_body",
        ]:
            if field in data and isinstance(data[field], str):
                data[field] = json.loads(data[field])
        return data
