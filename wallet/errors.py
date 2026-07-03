from __future__ import annotations

from dataclasses import dataclass
from typing import Any


class WalletError(Exception):
    def __init__(
        self,
        http_status: int,
        code: str,
        message: str,
        *,
        retryable: bool = False,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.http_status = http_status
        self.code = code
        self.message = message
        self.retryable = retryable
        self.details = details or {}


@dataclass(frozen=True)
class ServiceResult:
    data: dict[str, Any]
    status_code: int = 200
