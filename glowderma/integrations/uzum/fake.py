"""Deterministic Uzum Checkout for tests, the demo seed and local end-to-end runs (``UZUM_MODE=fake``).

The fake keeps its "Uzum database" — registered orders, their status and operations — in the Django cache
(``uzum:fake:order:<orderId>``), so the web process that registers and the worker that asks for the status
see the same order under the local settings (Redis). Scenario switches (``Order.metadata["fake"]["uzum"]``):

* ``register``: ``"down"`` → :class:`UzumNotProcessed`; ``"unknown"`` → the order is registered but
  :class:`UzumUnavailable` is raised (the answer was lost); ``"rejected"`` → ``UzumRejected("3001")``;
* ``status``: ``"down"`` → :class:`UzumUnavailable`; an order status (``"COMPLETED"``, ``"DECLINED"``, …)
  → reported regardless of what happened (``COMPLETED`` with the full amount completed);
* ``refund``: absent / ``"succeeded"`` → the refund operation is ``SUCCESS`` at once; ``"pending"`` →
  ``IN_PROGRESS`` until :func:`finish_refund`; ``"failed"`` → ``FAIL``; ``"rejected"`` →
  ``UzumRejected("3021")``; ``"unknown"`` → applied, then :class:`UzumUnavailable`; ``"down"`` →
  :class:`UzumNotProcessed`.

:func:`complete` / :func:`decline` play the payer on the hosted form (tests, ``dev/…/simulate/``). The fake's
payment page is the success URL itself (a local run returns straight to the order page). Under the
local/test settings every call is recorded in :data:`calls`; under the production settings nothing is.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from typing import Any

from django.core.cache import cache

from glowderma.core import integration_modes, timeutils

from . import protocol
from .ports import (
    Operation,
    OrderStatus,
    RefundAccepted,
    Registration,
    UzumNotProcessed,
    UzumRejected,
    UzumUnavailable,
)

KEY = "uzum:fake:order:{}"
TTL_SECONDS = 7 * 24 * 3600
calls: list[tuple[str, str]] = []
_keys: set[str] = set()


def reset() -> None:
    """Forget every fake order and recorded call (tests)."""
    cache.delete_many(list(_keys))
    _keys.clear()
    calls.clear()


def _record(method: str, ref: str) -> None:
    if integration_modes.dev_features_allowed():
        calls.append((method, ref))


def _load(order_id: str) -> dict[str, Any] | None:
    value = cache.get(KEY.format(order_id))
    return value if isinstance(value, dict) else None


def _save(order: dict[str, Any]) -> None:
    key = KEY.format(order["order_id"])
    _keys.add(key)
    cache.set(key, order, timeout=TTL_SECONDS)


def _operation(op_type: str, state: str, merchant_operation_id: str = "") -> dict[str, Any]:
    return {
        "operation_id": str(uuid.uuid4()),
        "operation_type": op_type,
        "state": state,
        "merchant_operation_id": merchant_operation_id,
        "done_at": timeutils.now().isoformat() if state != protocol.OP_IN_PROGRESS else None,
    }


def complete(order_id: str) -> None:
    """The payer paid on the form: COMPLETED with the full amount (a no-op for an unknown order)."""
    order = _load(order_id)
    if order is None or order["status"] != protocol.REGISTERED:
        return
    order["status"] = protocol.COMPLETED
    order["completed_amount"] = order["amount"]
    order["operations"].append(_operation("COMPLETE", protocol.OP_SUCCESS))
    _save(order)


def decline(order_id: str) -> None:
    """The card was declined (or the session ended): DECLINED."""
    order = _load(order_id)
    if order is None or order["status"] != protocol.REGISTERED:
        return
    order["status"] = protocol.DECLINED
    order["operations"].append(_operation("AUTHORIZE", protocol.OP_FAIL))
    _save(order)


def finish_refund(order_id: str, operation_id: str, *, success: bool = True) -> None:
    """End a ``"pending"`` refund operation."""
    order = _load(order_id)
    if order is None:
        return
    for op in order["operations"]:
        if op["merchant_operation_id"] == operation_id and op["state"] == protocol.OP_IN_PROGRESS:
            op["state"] = protocol.OP_SUCCESS if success else protocol.OP_FAIL
            op["done_at"] = timeutils.now().isoformat()
            if success:
                _apply_refund(order, int(op["amount"]))
    _save(order)


def _apply_refund(order: dict[str, Any], amount: int) -> None:
    order["refunded_amount"] += amount
    if order["refunded_amount"] >= order["completed_amount"]:
        order["status"] = protocol.REFUNDED


def _status(order: dict[str, Any], forced: object) -> OrderStatus:
    status = str(order["status"])
    completed = int(order["completed_amount"])
    if isinstance(forced, str) and forced in protocol.ORDER_STATUSES:
        status = forced
        if forced == protocol.COMPLETED:
            completed = int(order["amount"])
    operations = tuple(
        Operation(
            operation_id=op["operation_id"],
            operation_type=op["operation_type"],
            state=op["state"],
            merchant_operation_id=op["merchant_operation_id"],
        )
        for op in order["operations"]
    )
    return OrderStatus(
        order_id=order["order_id"],
        status=status,
        merchant_order_id=order["order_number"],
        amount=int(order["amount"]),
        completed_amount=completed,
        refunded_amount=int(order["refunded_amount"]),
        reversed_amount=0,
        operations=operations,
    )


class FakeUzum:
    name = "uzum"

    def __init__(self, scenario: Mapping[str, Any] | None = None) -> None:
        self.scenario: Mapping[str, Any] = scenario or {}

    def is_configured(self) -> bool:
        return True

    def register(
        self,
        *,
        order_number: str,
        amount: int,
        client_id: str,
        language: str,
        success_url: str,
        failure_url: str,
        session_timeout: int,
    ) -> Registration:
        _record("register", order_number)
        outcome = self.scenario.get("register")
        if outcome == "down":
            raise UzumNotProcessed("Uzum did not answer (fake: down)")
        if outcome == "rejected":
            raise UzumRejected("3001", "Terminal not found (fake)")
        order_id = str(uuid.uuid4())
        _save(
            {
                "order_id": order_id,
                "order_number": order_number,
                "amount": amount,
                "status": protocol.REGISTERED,
                "completed_amount": 0,
                "refunded_amount": 0,
                "operations": [],
            }
        )
        if outcome == "unknown":
            raise UzumUnavailable("Uzum did not answer after the register was sent (fake: unknown)")
        return Registration(order_id=order_id, redirect_url=success_url)

    def order_status(self, order_id: str) -> OrderStatus:
        _record("order_status", order_id)
        forced = self.scenario.get("status")
        if forced == "down":
            raise UzumUnavailable("Uzum did not answer (fake: status down)")
        order = _load(order_id)
        if order is None:
            raise UzumRejected("3005", "Payment not found (fake)")
        return _status(order, forced)

    def refund(self, order_id: str, amount: int, operation_id: str) -> RefundAccepted:
        _record("refund", order_id)
        outcome = str(self.scenario.get("refund") or "succeeded")
        if outcome == "down":
            raise UzumNotProcessed("Uzum did not answer (fake: refund down)")
        if outcome == "rejected":
            raise UzumRejected(str(protocol.ERR_REFUND_AMOUNT), "Incorrect amount for refund (fake)")
        order = _load(order_id)
        if order is None:
            raise UzumRejected("3005", "Payment not found (fake)")
        if any(op["merchant_operation_id"] == operation_id for op in order["operations"]):
            raise UzumRejected(str(protocol.ERR_OPERATION_EXISTS), "Operation already exists (fake)")
        if order["status"] != protocol.COMPLETED:
            raise UzumRejected("3000", "Invalid payment status (fake)")
        state = {"pending": protocol.OP_IN_PROGRESS, "failed": protocol.OP_FAIL}.get(
            outcome, protocol.OP_SUCCESS
        )
        op = _operation(protocol.OP_REFUND, state, operation_id)
        op["amount"] = amount
        order["operations"].append(op)
        if state == protocol.OP_SUCCESS:
            _apply_refund(order, amount)
        _save(order)
        if outcome == "unknown":
            raise UzumUnavailable("Uzum did not answer after the refund was sent (fake: unknown)")
        return RefundAccepted(operation_id=op["operation_id"])
