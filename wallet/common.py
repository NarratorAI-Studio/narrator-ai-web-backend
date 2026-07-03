from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from .errors import WalletError


TWOPLACES = Decimal("0.01")
CLIENT_AMOUNT_FIELDS = {
    "locked_price",
    "hard_price",
    "final_amount",
    "amount",
    "charge_amount",
}
IDEMPOTENCY_ALLOWED_CHARS = set(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789:_.-"
)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def money(value: str | int | Decimal) -> Decimal:
    return Decimal(str(value)).quantize(TWOPLACES, rounding=ROUND_HALF_UP)


def money_str(value: str | int | Decimal) -> str:
    return format(money(value), ".2f")


def iso_z(value: datetime | str | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def canonical_request_hash(operation: str, body: dict[str, Any]) -> str:
    payload = json.dumps(
        {"operation": operation, "body": body},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def require_fields(body: dict[str, Any], fields: list[str]) -> None:
    missing = [field for field in fields if field not in body]
    if missing:
        raise WalletError(
            400,
            "BAD_REQUEST",
            f"Missing required field(s): {', '.join(missing)}.",
        )


def validate_idempotency_key(key: str | None) -> str:
    if not key:
        raise WalletError(400, "BAD_REQUEST", "Idempotency-Key header is required.")
    if len(key) > 255 or any(char not in IDEMPOTENCY_ALLOWED_CHARS for char in key):
        raise WalletError(400, "BAD_REQUEST", "Invalid Idempotency-Key format.")
    return key


def account_id_for(web_tenant_id: str, web_user_id: str) -> str:
    digest = hashlib.sha256(f"{web_tenant_id}:{web_user_id}".encode()).hexdigest()
    return f"wwa_{digest[:24]}"


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"
