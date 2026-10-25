"""The Click port as the refund flow needs it (design 02 §2.2, 03 §5 ``refund.submit``).

Protocol callbacks (prepare/complete) are M2 webhook views; M1 only reverses payments. ``registry.get_click
(scenario)`` picks the adapter from ``settings.CLICK_MODE`` (default ``INTEGRATIONS_MODE``); ``scenario`` is
the order's ``metadata["fake"]["click"]`` (the fake reads it, a live adapter ignores it).

The handler always asks :meth:`ClickPort.payment_status` before reversing (a redelivery after a crash must
find an already reversed payment instead of reversing twice). :class:`ClickUnavailable` is a transport error;
:class:`ClickRefundRejected` a definitive refusal.
"""

from dataclasses import dataclass
from typing import Literal, Protocol

type PaymentState = Literal["paid", "reversed"]


@dataclass(frozen=True, slots=True)
class RefundReceipt:
    provider_refund_id: str


class ClickError(Exception):
    """Base class of the typed Click failures."""


class ClickUnavailable(ClickError):
    """Click did not answer (transport error); the outcome of a call in flight is unknown."""


class ClickRefundRejected(ClickError):
    def __init__(self, error_code: str, message: str = "") -> None:
        super().__init__(message or error_code)
        self.error_code = error_code
        self.message = message or error_code


class ClickPort(Protocol):
    def payment_status(self, provider_txn_id: str) -> PaymentState:
        """Whether the payment is still ``paid`` or already ``reversed`` at Click."""
        ...

    def refund(self, provider_txn_id: str, amount: int) -> RefundReceipt:
        """Reverse the payment (full amount); raises ``ClickRefundRejected`` / ``ClickUnavailable``."""
        ...
