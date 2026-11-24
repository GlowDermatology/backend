"""The display payment (customer contract §2.8) and the payment lookups.

One ranking, newest id first within a rank: (3) not a late capture and paid/refunded · (2) not a late
capture and pending · (1) a late capture (its own status: paid until its refund succeeds, then refunded) ·
(0) failed. A double payment shows the payment that paid the order; an order whose only money-taking
payment was captured too late shows that late capture."""

from __future__ import annotations

from collections.abc import Iterable

from django.db.models import (
    BigIntegerField,
    Case,
    IntegerField,
    OuterRef,
    QuerySet,
    Subquery,
    TextField,
    Value,
    When,
)
from django.db.models.fields.json import KeyTextTransform, KeyTransform
from django.db.models.functions import Cast, Coalesce
from django.db.models.lookups import Regex

from glowderma.core import enums
from glowderma.payments.models import (
    LATE_CAPTURE,
    SETTLED_PAYMENT_STATUSES,
    Payment,
)
from glowderma.payments.selectors import payment_rows


def _rank() -> Case:
    return Case(
        When(error_code=LATE_CAPTURE, then=Value(1)),
        When(status__in=list(SETTLED_PAYMENT_STATUSES), then=Value(3)),
        When(status=enums.PaymentStatus.PENDING, then=Value(2)),
        default=Value(0),
        output_field=IntegerField(),
    )


def _ranked(qs: QuerySet[Payment]) -> QuerySet[Payment]:
    return qs.annotate(rank=_rank()).order_by("-rank", "-id")


def display_rank(payment: Payment | payment_rows.PaymentRow) -> int:
    """The rank of one payment under the display rule (see above)."""
    if payment.error_code == LATE_CAPTURE:
        return 1
    if payment.status in SETTLED_PAYMENT_STATUSES:
        return 3
    if payment.status == enums.PaymentStatus.PENDING:
        return 2
    return 0


def display_payment(order_id: int) -> payment_rows.PaymentRow | None:
    """The payment an order shows (``None`` when it has none yet)."""
    payment = _ranked(Payment.objects.filter(order_id=order_id)).first()
    return None if payment is None else payment_rows.payment_row(payment)


def pick_display(payments: Iterable[Payment]) -> Payment | None:
    """The display payment among an order's prefetched payments (same rank as :func:`display_payment`)."""
    rows = list(payments)
    return max(rows, key=lambda payment: (display_rank(payment), payment.pk)) if rows else None


def display_status_subquery(order_ref: str = "pk") -> Coalesce:
    """``COALESCE(display payment status, 'pending')`` correlated on ``order_ref`` (``payment`` filter)."""
    ranked = _ranked(Payment.objects.filter(order_id=OuterRef(order_ref)))
    return Coalesce(Subquery(ranked.values("status")[:1]), Value(enums.PaymentStatus.PENDING))


def active_payment(order_id: int, status: str) -> payment_rows.PaymentRow | None:
    """The order's newest payment in ``status`` (``pending``: to confirm or fail; ``paid``: to refund).

    Late captures (customer contract §2.13.0) are never the order's active payment."""
    payment = (
        Payment.objects.filter(order_id=order_id, status=status)
        .exclude(error_code=LATE_CAPTURE)
        .order_by("-id")
        .first()
    )
    return None if payment is None else payment_rows.payment_row(payment)


def payments_of_order(order_id: int) -> list[payment_rows.PaymentRow]:
    """Every payment of the order, newest first."""
    return [payment_rows.payment_row(p) for p in Payment.objects.filter(order_id=order_id).order_by("-id")]


def payment_by_txn(provider: str, provider_txn_id: str) -> payment_rows.PaymentRow | None:
    """The payment a provider transaction id is bound to (``payment_provider_txn_unique``)."""
    if not provider_txn_id:
        return None
    payment = Payment.objects.filter(provider=provider, provider_txn_id=provider_txn_id).first()
    return None if payment is None else payment_rows.payment_row(payment)


# ``provider_state.time`` as a JSON integer that fits a bigint: the jsonb text of a number with no fraction or
# exponent (a string keeps its quotes, a boolean is ``true``/``false``), at most 18 digits.
_PAYME_TIME_INTEGER = r"^-?[0-9]{1,18}$"


def _payme_time() -> Case:
    """``(provider_state->>'time')::bigint``, NULL unless ``time`` is such an integer: the CASE keeps the cast
    from ever seeing a string, a fraction, a boolean or an out-of-range number (a cast error would fail every
    statement). An M3 expression index must index this same expression."""
    return Case(
        When(
            Regex(Cast(KeyTransform("time", "provider_state"), TextField()), _PAYME_TIME_INTEGER),
            then=Cast(KeyTextTransform("time", "provider_state"), BigIntegerField()),
        ),
        default=None,
        output_field=BigIntegerField(),
    )


def payme_statement(from_ms: int, to_ms: int) -> list[payment_rows.PaymentRow]:
    """Payme ``GetStatement``: Payme payments whose Payme transaction time ``provider_state.time`` lies in
    ``[from_ms, to_ms]``, ascending by that time, then id (customer contract §2.13.2; M2 filters on the JSON
    key, in SQL)."""
    payments = (
        Payment.objects.filter(provider=enums.PaymentProvider.PAYME)
        .exclude(provider_txn_id="")
        .annotate(payme_time=_payme_time())
        .filter(payme_time__range=(from_ms, to_ms))
        .order_by("payme_time", "id")
    )
    return [payment_rows.payment_row(payment) for payment in payments]


def get_payment(payment_id: int) -> payment_rows.PaymentRow | None:
    payment = Payment.objects.filter(pk=payment_id).first()
    return None if payment is None else payment_rows.payment_row(payment)


__all__ = [
    "active_payment",
    "display_payment",
    "display_rank",
    "display_status_subquery",
    "get_payment",
    "payme_statement",
    "payment_by_txn",
    "payments_of_order",
    "pick_display",
]
