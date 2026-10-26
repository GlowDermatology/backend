"""The Click port (design 02 §2.2, 03 §5 ``refund.submit``; customer contract §2.7.2, §2.13).

Protocol callbacks (Prepare/Complete) are the M2 webhook views in ``commerce/api/webhooks/``; their pure helpers
(signature, amounts, error table) live in :mod:`.protocol`. ``registry.get_click(scenario)`` picks the adapter
from ``settings.CLICK_MODE`` (default ``INTEGRATIONS_MODE``); ``scenario`` is the order's
``metadata["fake"]["click"]`` (the fake reads it, a live adapter ignores it).

Refund and status calls are keyed by **Click's payment id of the Merchant API** — our
``Payment.provider_state["click_paydoc_id"]`` (``DELETE …/payment/reversal/{service_id}/{payment_id}``, ``GET
…/payment/status/{service_id}/{payment_id}``), never ``click_trans_id`` (customer contract §2.13.0, verify M3).
The refund handler always asks :meth:`ClickPort.payment_status` before reversing (a redelivery after a crash
must find an already reversed payment instead of reversing twice). :class:`ClickUnavailable` is a transport
error; :class:`ClickRefundRejected` a definitive refusal.

Bounded calls (normative for every adapter, M3 live included): no call blocks longer than
:data:`CONNECT_TIMEOUT_SECONDS` to connect and :data:`TOTAL_TIMEOUT_SECONDS` in total (a timeout is a
:class:`ClickUnavailable`). The ``refund.submit`` handler relies on it: a refund claimed longer ago than the
outbox lease (5 minutes) cannot still have a reversal call in flight, so a redelivery may submit again.
"""

from dataclasses import dataclass
from typing import Literal, Protocol

type PaymentState = Literal["paid", "reversed"]

# Transport bounds every adapter honours (see the module docstring); a timeout raises ClickUnavailable.
CONNECT_TIMEOUT_SECONDS = 3.0
TOTAL_TIMEOUT_SECONDS = 10.0


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
    def payment_status(self, payment_ref: str) -> PaymentState:
        """Whether the payment (``click_paydoc_id``) is still ``paid`` or already ``reversed`` at Click."""
        ...

    def refund(self, payment_ref: str, amount: int) -> RefundReceipt:
        """Reverse the payment ``click_paydoc_id`` (full amount); raises ``ClickRefundRejected`` /
        ``ClickUnavailable``."""
        ...

    def pay_url(self, account_ref: str, amount: int, return_url: str) -> str:
        """The Click pay page for ``account_ref`` (customer contract §2.7.2, verify M3)."""
        ...
