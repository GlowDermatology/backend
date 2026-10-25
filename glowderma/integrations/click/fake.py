"""Deterministic Click for tests, the demo seed and local end-to-end runs (``CLICK_MODE=fake``).

Scenario switch: ``Order.metadata["fake"]["click"]["refund"]`` (design 03 §1.3; the older top-level
``metadata["fake"]["refund"]`` is read as a fallback by ``commerce``):

* absent / ``"succeeded"`` → ``payment_status`` answers ``"paid"`` until the payment is reversed, ``refund``
  reverses it (``provider_refund_id = "CLKR-<txn digits>"``) and later status checks answer ``"reversed"``;
* ``"failed"`` → ``refund`` raises :class:`ClickRefundRejected` ``refund_rejected`` (a definitive refusal);
* ``"unknown"`` → ``refund`` raises :class:`ClickUnavailable` after "sending" the request, and every later
  ``payment_status`` of that transaction raises too (the refund becomes ``unknown`` and is never retried);
* ``"down"`` → every call raises :class:`ClickUnavailable` before anything is sent (the outbox retries; ten
  failures make the message dead and the refund ``failed`` with ``dead_letter``).

Reversed and "lost" transactions are remembered per process (the fake's "Click database"), so "Click
reversed the payment, then the worker crashed" is adopted by the redelivery's status check.

Transaction ids (design 03 §7.8): ``CLK-`` + ``str(7_741_000_000 + order_id × 37)[:10]`` unless
``scenario["transaction_id"]`` is set (the design's ``CLK-7741920385`` for №10248).
"""

from collections.abc import Mapping
from typing import Any

from .ports import ClickRefundRejected, ClickUnavailable, PaymentState, RefundReceipt

TXN_BASE = 7_741_000_000
REFUND_REJECTED = "refund_rejected"

_reversed: set[str] = set()
_lost: set[str] = set()


def reset() -> None:
    """Forget every reversal the fake performed (tests)."""
    _reversed.clear()
    _lost.clear()


def transaction_id(order_id: int, scenario: Mapping[str, Any] | None = None) -> str:
    override = (scenario or {}).get("transaction_id")
    if override:
        return str(override)
    return "CLK-" + str(TXN_BASE + order_id * 37)[:10]


class FakeClick:
    name = "click"

    def __init__(self, scenario: Mapping[str, Any] | None = None) -> None:
        self.scenario: Mapping[str, Any] = scenario or {}

    @property
    def outcome(self) -> str:
        return str(self.scenario.get("refund") or "succeeded")

    def payment_status(self, provider_txn_id: str) -> PaymentState:
        if self.outcome == "down" or provider_txn_id in _lost:
            raise ClickUnavailable("Click did not answer (fake)")
        return "reversed" if provider_txn_id in _reversed else "paid"

    def refund(self, provider_txn_id: str, amount: int) -> RefundReceipt:
        outcome = self.outcome
        if outcome == "down":
            raise ClickUnavailable("Click did not answer (fake: down)")
        if outcome == "failed":
            raise ClickRefundRejected(REFUND_REJECTED, "Click refused the reversal (fake)")
        if outcome == "unknown":
            _lost.add(provider_txn_id)
            raise ClickUnavailable("Click did not answer after the reversal was sent (fake: unknown)")
        _reversed.add(provider_txn_id)
        digits = "".join(ch for ch in provider_txn_id if ch.isdigit())
        return RefundReceipt(provider_refund_id=f"CLKR-{digits}")
