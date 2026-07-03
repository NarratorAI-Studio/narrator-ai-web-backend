from __future__ import annotations

from decimal import Decimal, InvalidOperation
import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from .errors import WalletError


class StrictWalletModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class QuoteRequest(StrictWalletModel):
    web_tenant_id: str = Field(min_length=1)
    web_user_id: str = Field(min_length=1)
    web_order_id: str = Field(min_length=1)
    template_id: int
    combo_key: str = Field(min_length=1)
    client_correlation_id: str | None = None

    @field_validator("template_id")
    @classmethod
    def template_id_must_not_be_bool(cls, value: int) -> int:
        if isinstance(value, bool):
            raise ValueError("template_id must be an integer")
        return value


class FreezeRequest(StrictWalletModel):
    quote_id: str = Field(min_length=1)
    web_tenant_id: str = Field(min_length=1)
    web_user_id: str = Field(min_length=1)
    web_order_id: str = Field(min_length=1)
    correlation: dict[str, Any] = Field(default_factory=dict)


class OrderSubtaskEvidence(StrictWalletModel):
    web_master_task_id: str = Field(min_length=1)
    api_task_id: str = Field(min_length=1)
    api_request_id: str = Field(min_length=1)
    bucket_id: str | None = None
    status: str | None = None
    attempt: int | None = None
    amount_points: str | None = None
    failure_code: str | None = None
    failure_message: str | None = None

    @field_validator("amount_points")
    @classmethod
    def amount_points_must_be_decimal(cls, value: str | None) -> str | None:
        if value is None:
            return None
        try:
            amount = Decimal(value)
        except (InvalidOperation, ValueError) as exc:
            raise ValueError("amount_points must be a decimal string") from exc
        if not amount.is_finite():
            raise ValueError("amount_points must be finite")
        if amount < 0:
            raise ValueError("amount_points must be non-negative")
        return f"{amount.quantize(Decimal('0.01')):.2f}"


class ConfirmCorrelation(StrictWalletModel):
    web_master_task_id: str = Field(min_length=1)
    api_task_id: str = Field(min_length=1)
    api_request_id: str = Field(min_length=1)
    api_correlation_id: str | None = None
    legacy_consume_budget_evidence: dict[str, Any] | None = None
    bucket_id: str | None = None
    subtasks: list[OrderSubtaskEvidence] | None = None


class ConfirmRequest(StrictWalletModel):
    wallet_transaction_id: str = Field(min_length=1)
    web_tenant_id: str = Field(min_length=1)
    web_user_id: str = Field(min_length=1)
    web_order_id: str = Field(min_length=1)
    correlation: ConfirmCorrelation


class RefundCorrelation(StrictWalletModel):
    web_master_task_id: str = Field(min_length=1)
    api_request_id: str = Field(min_length=1)
    api_error_code: str | None = None
    reconciliation_status: str = Field(min_length=1)


class RefundRequest(StrictWalletModel):
    wallet_transaction_id: str = Field(min_length=1)
    web_tenant_id: str = Field(min_length=1)
    web_user_id: str = Field(min_length=1)
    web_order_id: str = Field(min_length=1)
    reason_code: str = Field(min_length=1)
    reason_message: str = Field(min_length=1)
    correlation: RefundCorrelation


def normalize_positive_money(value: str) -> str:
    if not re.fullmatch(r"\d+\.\d{2}", value):
        raise ValueError("amount must use two decimal places")
    try:
        amount = Decimal(value)
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("amount must be a decimal string") from exc
    if not amount.is_finite():
        raise ValueError("amount must be finite")
    if amount <= 0:
        raise ValueError("amount must be positive")
    return f"{amount:.2f}"


class OverbillRefundEvidence(StrictWalletModel):
    source: str = Field(min_length=1)
    operator_id: str = Field(min_length=1)
    evidence_url: str | None = None
    note: str | None = None


class OverbillRefundRequest(StrictWalletModel):
    wallet_transaction_id: str = Field(min_length=1)
    web_tenant_id: str = Field(min_length=1)
    web_user_id: str = Field(min_length=1)
    web_order_id: str = Field(min_length=1)
    refund_amount_points: str = Field(min_length=1)
    reason_code: str = Field(min_length=1)
    reason_message: str = Field(min_length=1)
    evidence: OverbillRefundEvidence

    @field_validator("refund_amount_points")
    @classmethod
    def refund_amount_must_be_positive_money(cls, value: str) -> str:
        return normalize_positive_money(value)


def validate_model(model: type[BaseModel], body: dict[str, Any]) -> dict[str, Any]:
    try:
        return model.model_validate(body).model_dump(exclude_none=True)
    except ValidationError as exc:
        raise WalletError(
            400,
            "BAD_REQUEST",
            "Invalid wallet request body.",
            details={
                "validation_errors": exc.errors(
                    include_context=False,
                    include_input=False,
                    include_url=False,
                )
            },
        ) from exc
