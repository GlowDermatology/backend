"""The Uzum Checkout port (docs/integrations/uzum.md; Uzum Checkout API v1.10.3).

We register a ONE_STEP payment (``POST /api/v1/payment/register``, ``viewType=REDIRECT``) and send the payer
to the hosted card form Uzum returns. The form debits the card on its own; we learn the result from Uzum's
callback — which carries **no amount and no signature**, so it is only a hint — and from our own
``getOrderStatus`` call, the one source of truth. Refunds are ``POST /api/v1/acquiring/refund`` with our
``X-Operation-Id``; they finish asynchronously (the operation shows up in ``getOrderStatus.operations``).

``registry.get_uzum(scenario)`` picks the adapter from ``settings.UZUM_MODE`` (default ``fake``, never
``INTEGRATIONS_MODE``). Amounts at this port are **tiyin** (1 so'm = 100 tiyin): the callers convert with
:func:`.protocol.to_tiyin` and compare with :func:`.protocol.from_tiyin`.

Errors: :class:`UzumUnavailable` — no usable answer, the outcome is unknown (``sent`` is false only when
the request provably never left); its subclass :class:`UzumNotProcessed` — Uzum did not process the
request (nothing happened, safe to repeat); :class:`UzumRejected` — a definitive refusal with Uzum's numeric
``errorCode`` (as text) or ``http_<status>``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

# The customer waits on register: a shorter bound than the workers' calls.
REGISTER_TIMEOUT_SECONDS = 4.0
WORKER_TIMEOUT_SECONDS = 10.0


@dataclass(frozen=True, slots=True)
class Registration:
    """A registered payment: Uzum's ``orderId`` and the hosted form's URL."""

    order_id: str
    redirect_url: str


@dataclass(frozen=True, slots=True)
class Operation:
    operation_id: str
    operation_type: str  # AUTHORIZE | COMPLETE | REFUND | REVERSE | …
    state: str  # IN_PROGRESS | SUCCESS | FAIL
    merchant_operation_id: str = ""
    done_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class OrderStatus:
    """``getOrderStatus`` (amounts in tiyin)."""

    order_id: str
    status: str  # REGISTERED | AUTHORIZED | COMPLETED | DECLINED | REFUNDED | REVERSED | TOP_UP_COMPLETED
    merchant_order_id: str
    amount: int
    completed_amount: int
    refunded_amount: int
    reversed_amount: int
    operations: tuple[Operation, ...] = field(default_factory=tuple)

    def operation(self, merchant_operation_id: str) -> Operation | None:
        return next((op for op in self.operations if op.merchant_operation_id == merchant_operation_id), None)


@dataclass(frozen=True, slots=True)
class RefundAccepted:
    """Uzum accepted a refund; it completes later (``operation_id`` is Uzum's)."""

    operation_id: str


class UzumError(Exception):
    """Base class of the typed Uzum failures."""


class UzumUnavailable(UzumError):
    """No usable answer; the outcome of a call in flight is unknown unless ``sent`` is false."""

    def __init__(self, message: str, *, sent: bool = True) -> None:
        super().__init__(message)
        self.sent = sent


class UzumNotProcessed(UzumUnavailable):
    """Uzum did not process the request (a redirect, 401/403/408/425/429) or it never left: repeat later."""

    def __init__(self, message: str) -> None:
        super().__init__(message, sent=False)


class UzumRejected(UzumError):
    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code
        self.message = message or code


class UzumCheckoutPort(Protocol):
    name: str

    def is_configured(self) -> bool:
        """Settings only, nothing is sent (the readiness probe)."""
        ...

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
        """Register a ONE_STEP REDIRECT payment of ``amount`` tiyin under our ``order_number``."""
        ...

    def order_status(self, order_id: str) -> OrderStatus:
        """``getOrderStatus`` of Uzum's ``order_id``."""
        ...

    def refund(self, order_id: str, amount: int, operation_id: str) -> RefundAccepted:
        """Refund ``amount`` tiyin of a COMPLETED payment (``operation_id``, a UUID, is our idempotency
        key)."""
        ...
