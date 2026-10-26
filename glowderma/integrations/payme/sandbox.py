"""Payme Merchant API requests as Payme would send them (plan §5.2): the offline sandbox suite and
``dev/…/simulate/``. Pure helpers: JSON-RPC bodies and the Basic ``Authorization`` header."""

from __future__ import annotations

from itertools import count
from typing import Any

from django.conf import settings

from . import protocol

_ids = count(1)


def auth_header(key: str | None = None) -> str:
    return protocol.basic_auth(settings.PAYME_KEY if key is None else key)


def request(method: str, params: dict[str, Any], *, request_id: int | None = None) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": next(_ids) if request_id is None else request_id,
        "method": method,
        "params": params,
    }


def account(account_ref: str) -> dict[str, str]:
    return {str(settings.PAYME_ACCOUNT_FIELD): account_ref}


def check_perform(account_ref: str, amount_tiyin: int) -> dict[str, Any]:
    return request("CheckPerformTransaction", {"amount": amount_tiyin, "account": account(account_ref)})


def create(transaction_id: str, account_ref: str, amount_tiyin: int, time_ms: int) -> dict[str, Any]:
    return request(
        "CreateTransaction",
        {"id": transaction_id, "time": time_ms, "amount": amount_tiyin, "account": account(account_ref)},
    )


def perform(transaction_id: str) -> dict[str, Any]:
    return request("PerformTransaction", {"id": transaction_id})


def cancel(transaction_id: str, reason: int) -> dict[str, Any]:
    return request("CancelTransaction", {"id": transaction_id, "reason": reason})


def check(transaction_id: str) -> dict[str, Any]:
    return request("CheckTransaction", {"id": transaction_id})


def statement(from_ms: int, to_ms: int) -> dict[str, Any]:
    return request("GetStatement", {"from": from_ms, "to": to_ms})
