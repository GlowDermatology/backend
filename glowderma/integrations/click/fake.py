"""Deterministic Click for tests, the demo seed and local end-to-end runs (``CLICK_MODE=fake``).

Refund and status calls are keyed by the payment's ``click_paydoc_id`` (customer contract §2.13.0). Scenario
switches (``Order.metadata["fake"]["click"]``, design 03 §1.3, plan §6; the older top-level
``metadata["fake"]["refund"]`` is read as a fallback by ``commerce``):

* ``refund`` absent / ``"succeeded"`` → ``payment_status`` answers ``"paid"`` until the payment is reversed,
  ``refund`` reverses it (``provider_refund_id = "CLKR-<ref digits>"``) and later status checks answer
  ``"reversed"``; ``"failed"`` → ``refund`` raises :class:`ClickRefundRejected` ``refund_rejected``;
  ``"unknown"`` → ``refund`` raises :class:`ClickUnavailable` after "sending" the request, and every later
  ``payment_status`` of that payment raises too; ``"down"`` → every call raises :class:`ClickUnavailable`
  before anything is sent;
* ``status``: ``"paid"`` / ``"reversed"`` / ``"down"`` → what ``payment_status`` answers regardless of the
refund
  outcome (the fresh status check of a staff resolve, §2.13.0);
* ``transaction_id`` / ``paydoc_id``: fixed ids for ``dev/…/simulate/``.

Reversed and "lost" payments are remembered per process (the fake's "Click database"). Under the local/test
settings (``DEV_FEATURES_ALLOWED``) every refund/status call is recorded in :data:`calls` (``(method,
payment_ref)``) so tests assert the ``click_paydoc_id`` the handlers used; under the production settings the
fake
records nothing (customer contract §6.14).

Transaction ids (design 03 §7.8): ``CLK-`` + ``str(7_741_000_000 + order_id × 37)[:10]`` unless
``scenario["transaction_id"]`` is set (the design's ``CLK-7741920385`` for №10248).
"""

from collections.abc import Mapping
from typing import Any

from django.conf import settings

from glowderma.core import integration_modes

from . import protocol
from .ports import ClickRefundRejected, ClickUnavailable, PaymentState, RefundReceipt

TXN_BASE = 7_741_000_000
REFUND_REJECTED = "refund_rejected"

_reversed: set[str] = set()
_lost: set[str] = set()
calls: list[tuple[str, str]] = []


def reset() -> None:
    """Forget every reversal the fake performed and every recorded call (tests)."""
    _reversed.clear()
    _lost.clear()
    calls.clear()


def transaction_id(order_id: int, scenario: Mapping[str, Any] | None = None) -> str:
    override = (scenario or {}).get("transaction_id")
    if override:
        return str(override)
    return "CLK-" + str(TXN_BASE + order_id * 37)[:10]


def paydoc_id(order_id: int, scenario: Mapping[str, Any] | None = None) -> str:
    """The fake Click payment document id of an order (``scenario["paydoc_id"]`` wins)."""
    override = (scenario or {}).get("paydoc_id")
    if override:
        return str(override)
    return str(5_500_000_000 + order_id)


def _record(method: str, payment_ref: str) -> None:
    if integration_modes.dev_features_allowed():
        calls.append((method, payment_ref))


class FakeClick:
    name = "click"

    def __init__(self, scenario: Mapping[str, Any] | None = None) -> None:
        self.scenario: Mapping[str, Any] = scenario or {}

    @property
    def outcome(self) -> str:
        return str(self.scenario.get("refund") or "succeeded")

    def payment_status(self, payment_ref: str) -> PaymentState:
        _record("payment_status", payment_ref)
        forced = self.scenario.get("status")
        if forced == "down":
            raise ClickUnavailable("Click did not answer (fake: status down)")
        if forced in ("paid", "reversed"):
            return "reversed" if forced == "reversed" else "paid"
        if self.outcome == "down" or payment_ref in _lost:
            raise ClickUnavailable("Click did not answer (fake)")
        return "reversed" if payment_ref in _reversed else "paid"

    def refund(self, payment_ref: str, amount: int) -> RefundReceipt:
        _record("refund", payment_ref)
        outcome = self.outcome
        if outcome == "down":
            raise ClickUnavailable("Click did not answer (fake: down)")
        if outcome == "failed":
            raise ClickRefundRejected(REFUND_REJECTED, "Click refused the reversal (fake)")
        if outcome == "unknown":
            _lost.add(payment_ref)
            raise ClickUnavailable("Click did not answer after the reversal was sent (fake: unknown)")
        _reversed.add(payment_ref)
        digits = "".join(ch for ch in payment_ref if ch.isdigit())
        return RefundReceipt(provider_refund_id=f"CLKR-{digits}")

    def pay_url(self, account_ref: str, amount: int, return_url: str) -> str:
        return protocol.pay_url(
            str(settings.CLICK_PAY_URL),
            service_id=str(settings.CLICK_SERVICE_ID),
            merchant_id=str(settings.CLICK_MERCHANT_ID),
            amount=amount,
            account_ref=account_ref,
            return_url=return_url,
        )
