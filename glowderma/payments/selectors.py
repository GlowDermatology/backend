"""Read side of `payments` (design 02 §2.1 display payment, payments list, dashboard revenue; §2.2 refunds).

Rows leave the module as frozen dataclasses with the contract's field names (``transaction_id`` = the
provider transaction id, ``""`` → ``None``). A selector may traverse ``Payment``'s own FK upward
(``order__customer`` for the list and its search, 03 §0.1) but never a reverse relation of an upper module.
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, TypedDict

from django.db.models import (
    BigIntegerField,
    Case,
    Exists,
    F,
    IntegerField,
    OuterRef,
    Q,
    QuerySet,
    Subquery,
    Sum,
    TextField,
    Value,
    When,
)
from django.db.models.fields.json import KeyTextTransform, KeyTransform
from django.db.models.functions import Cast, Coalesce, TruncDay, TruncHour
from django.db.models.lookups import Regex

from glowderma.core import enums, timeutils
from glowderma.core.consistency import Finding, finding
from glowderma.core.timeutils import TASHKENT
from glowderma.payments.models import (
    ATTENTION_REFUND_STATES,
    LATE_CAPTURE,
    OPEN_REFUND_STATES,
    SETTLED_PAYMENT_STATUSES,
    Payment,
    Refund,
)

SEARCH_MIN_PHONE_DIGITS = 3


@dataclass(frozen=True, slots=True)
class PaymentRow:
    id: int
    order_id: int
    provider: str
    status: str
    amount: int
    transaction_id: str | None
    created_at: datetime
    confirmed_at: datetime | None
    provider_state: dict[str, Any]
    error_code: str = ""
    failed_at: datetime | None = None

    @property
    def late_capture(self) -> bool:
        return self.error_code == LATE_CAPTURE


@dataclass(frozen=True, slots=True)
class RefundRow:
    """Contract ``Refund`` (+ the internals the handlers need)."""

    id: int
    payment_id: int
    order_id: int
    state: str
    provider: str
    amount: int
    requested_at: datetime
    updated_at: datetime
    completed_at: datetime | None
    provider_txn_id: str
    attempts: int
    submitted_at: datetime | None
    error_code: str
    # The provider's payment reference for refund/status calls: Click's ``click_paydoc_id`` (customer contract
    # §2.13.0, never ``click_trans_id``); ``""`` when the payment has none (seeded/legacy rows).
    provider_payment_ref: str = ""
    # Why the refund is (or was) ``manual_required`` (admin contract §2.2 ``Refund.manual_reason``): the
    # stored ``Refund.manual_reason``; for a ``manual_required`` row stored without one (a factory row, a
    # row the M3 backfill could not place) the reason its payment implies; ``None`` otherwise.
    manual_reason: str | None = None


def click_paydoc_id(provider_state: Any) -> str:
    """``provider_state["click_paydoc_id"]`` as text, ``""`` when absent."""
    value = provider_state.get("click_paydoc_id") if isinstance(provider_state, dict) else None
    return "" if value in (None, "") else str(value)


def payment_row(payment: Payment) -> PaymentRow:
    return PaymentRow(
        id=payment.pk,
        order_id=payment.order_id,
        provider=payment.provider,
        status=payment.status,
        amount=payment.amount,
        transaction_id=payment.provider_txn_id or None,
        created_at=payment.created_at,
        confirmed_at=payment.confirmed_at,
        provider_state=dict(payment.provider_state or {}),
        error_code=payment.error_code,
        failed_at=payment.failed_at,
    )


def refund_row(refund: Refund, payment: Payment | None = None) -> RefundRow:
    owner = payment or refund.payment
    return RefundRow(
        id=refund.pk,
        payment_id=owner.pk,
        order_id=owner.order_id,
        state=refund.state,
        provider=owner.provider,
        amount=refund.amount,
        requested_at=refund.requested_at,
        updated_at=refund.updated_at,
        completed_at=refund.completed_at,
        provider_txn_id=owner.provider_txn_id,
        attempts=refund.attempts,
        submitted_at=refund.submitted_at,
        error_code=refund.error_code,
        provider_payment_ref=click_paydoc_id(owner.provider_state)
        if owner.provider == enums.PaymentProvider.CLICK
        else "",
        manual_reason=refund_manual_reason_of(refund, owner),
    )


def refund_manual_reason_of(refund: Refund, payment: Payment) -> str | None:
    """``Refund.manual_reason`` for the API (:attr:`RefundRow.manual_reason`). The fallback of a
    ``manual_required`` row without a stored reason never assumes Payme for a Click refund: Payme →
    ``payme``; Click → ``no_paydoc`` without a ``click_paydoc_id``, else ``fake_mode`` (the only other reason
    a Click refund is manual)."""
    if refund.manual_reason:
        return str(refund.manual_reason)
    if refund.state != enums.RefundState.MANUAL_REQUIRED:
        return None
    if payment.provider == enums.PaymentProvider.PAYME:
        return enums.RefundManualReason.PAYME.value
    if not click_paydoc_id(payment.provider_state):
        return enums.RefundManualReason.NO_PAYDOC.value
    return enums.RefundManualReason.FAKE_MODE.value


# --- display payment (customer contract §2.8): one ranking, newest id first within a rank -------------------
# (3) not a late capture and paid/refunded · (2) not a late capture and pending · (1) a late capture (its own
# status: paid until its refund succeeds, then refunded) · (0) failed. A double payment shows the payment
# that paid
# the order; an order whose only money-taking payment was captured too late shows that late capture.


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


def display_rank(payment: Payment | PaymentRow) -> int:
    """The rank of one payment under the display rule (see above)."""
    if payment.error_code == LATE_CAPTURE:
        return 1
    if payment.status in SETTLED_PAYMENT_STATUSES:
        return 3
    if payment.status == enums.PaymentStatus.PENDING:
        return 2
    return 0


def display_payment(order_id: int) -> PaymentRow | None:
    """The payment an order shows (``None`` when it has none yet)."""
    payment = _ranked(Payment.objects.filter(order_id=order_id)).first()
    return None if payment is None else payment_row(payment)


def pick_display(payments: Iterable[Payment]) -> Payment | None:
    """The display payment among an order's prefetched payments (same rank as :func:`display_payment`)."""
    rows = list(payments)
    return max(rows, key=lambda payment: (display_rank(payment), payment.pk)) if rows else None


def display_status_subquery(order_ref: str = "pk") -> Coalesce:
    """``COALESCE(display payment status, 'pending')`` correlated on ``order_ref`` (``payment`` filter)."""
    ranked = _ranked(Payment.objects.filter(order_id=OuterRef(order_ref)))
    return Coalesce(Subquery(ranked.values("status")[:1]), Value(enums.PaymentStatus.PENDING))


def active_payment(order_id: int, status: str) -> PaymentRow | None:
    """The order's newest payment in ``status`` (``pending``: to confirm or fail; ``paid``: to refund).

    Late captures (customer contract §2.13.0) are never the order's active payment."""
    payment = (
        Payment.objects.filter(order_id=order_id, status=status)
        .exclude(error_code=LATE_CAPTURE)
        .order_by("-id")
        .first()
    )
    return None if payment is None else payment_row(payment)


def payments_of_order(order_id: int) -> list[PaymentRow]:
    """Every payment of the order, newest first."""
    return [payment_row(p) for p in Payment.objects.filter(order_id=order_id).order_by("-id")]


def payment_by_txn(provider: str, provider_txn_id: str) -> PaymentRow | None:
    """The payment a provider transaction id is bound to (``payment_provider_txn_unique``)."""
    if not provider_txn_id:
        return None
    payment = Payment.objects.filter(provider=provider, provider_txn_id=provider_txn_id).first()
    return None if payment is None else payment_row(payment)


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


def payme_statement(from_ms: int, to_ms: int) -> list[PaymentRow]:
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
    return [payment_row(payment) for payment in payments]


def get_payment(payment_id: int) -> PaymentRow | None:
    payment = Payment.objects.filter(pk=payment_id).first()
    return None if payment is None else payment_row(payment)


# --- refunds ------------------------------------------------------------------------------------------------


def get_refund(refund_id: int) -> RefundRow | None:
    refund = Refund.objects.select_related("payment").filter(pk=refund_id).first()
    return None if refund is None else refund_row(refund)


def refunds_for_order(order_id: int) -> list[RefundRow]:
    """Every refund of the order's payments, newest first (``requested_at desc, id desc``)."""
    rows = (
        Refund.objects.select_related("payment")
        .filter(payment__order_id=order_id)
        .order_by("-requested_at", "-id")
    )
    return [refund_row(refund) for refund in rows]


def refunds_by_payment(payment_ids: Iterable[int]) -> dict[int, list[RefundRow]]:
    """``{payment_id: [Refund newest first]}`` for a page of payments (one query)."""
    result: dict[int, list[RefundRow]] = defaultdict(list)
    rows = (
        Refund.objects.select_related("payment")
        .filter(payment_id__in=list(payment_ids))
        .order_by("-requested_at", "-id")
    )
    for refund in rows:
        result[refund.payment_id].append(refund_row(refund))
    return result


def _superseded_failure() -> Q:
    """A ``failed`` refund that needs no action any more (admin contract §2.10, §2.12): a **later** refund of
    the same payment succeeded (a staff retry returned the money), or the payment is ``refunded``."""
    later_succeeded = Refund.objects.filter(
        payment_id=OuterRef("payment_id"), state=enums.RefundState.SUCCEEDED, pk__gt=OuterRef("pk")
    )
    return Q(state=enums.RefundState.FAILED) & (
        Q(payment__status=enums.PaymentStatus.REFUNDED) | Q(Exists(later_succeeded))
    )


def refunds_needing_action() -> int:
    """Dashboard ``attention.refunds_needing_action``: refunds ``failed``/``unknown``/``manual_required``,
    without the ``failed`` ones a later succeeded refund of the payment superseded or whose payment is
    ``refunded`` (admin contract §2.10)."""
    return (
        Refund.objects.filter(state__in=list(ATTENTION_REFUND_STATES)).exclude(_superseded_failure()).count()
    )


# --- payments list (contract §2.12) -------------------------------------------------------------------------


class PaymentFilters(TypedDict, total=False):
    status: str
    provider: str
    refund_state: str
    period: str
    date_from: date
    date_to: date
    search: str
    ordering: str


def search_q(term: str, *, id_field: str, phone_field: str) -> Q:
    """``№``-less id containment, or (≥ 3 digits) phone containment — the orders/payments search rule."""
    number = term.removeprefix("№").strip()
    condition = Q(**{f"{id_field}__contains": number}) if number else Q(pk__in=[])
    digits = re.sub(r"\D", "", term)
    if len(digits) >= SEARCH_MIN_PHONE_DIGITS:
        condition |= Q(**{f"{phone_field}__contains": digits})
    return condition


def payments_list(filters: PaymentFilters, now: datetime | None = None) -> QuerySet[Payment]:
    """``GET payments/``: filters ``status``, ``provider``, ``refund_state`` (any refund in that state),
    ``period``/``date_from``/``date_to`` on ``created_at`` (Tashkent, as ``orders/``), ``search`` (order id,
    transaction id, ≥ 3-digit phone); ordering ``±created_at`` with ties by ``id``."""
    instant = now or timeutils.now()
    qs = Payment.objects.select_related("order__customer")
    if "status" in filters:
        qs = qs.filter(status=filters["status"])
    if "provider" in filters:
        qs = qs.filter(provider=filters["provider"])
    if "refund_state" in filters:
        refunds = Refund.objects.filter(payment_id=OuterRef("pk"), state=filters["refund_state"])
        if filters["refund_state"] == enums.RefundState.FAILED:
            refunds = refunds.exclude(_superseded_failure())  # admin contract §2.12: same rule as §2.10
        qs = qs.filter(Exists(refunds))
    bounds = timeutils.period_bounds(filters.get("period", enums.OrderPeriod.ALL), instant)
    qs = qs.filter(**{f"created_at__{key}": value for key, value in bounds.items()})
    date_bounds = timeutils.date_range_bounds(filters.get("date_from"), filters.get("date_to"))
    qs = qs.filter(**{f"created_at__{key}": value for key, value in date_bounds.items()})
    term = filters.get("search", "").strip()
    if term:
        qs = qs.annotate(order_id_text=Cast("order_id", output_field=TextField()))
        condition = search_q(term, id_field="order_id_text", phone_field="order__customer__phone")
        condition |= Q(provider_txn_id__icontains=term)
        qs = qs.filter(condition)
    if filters.get("ordering") == "created_at":
        return qs.order_by("created_at", "id")
    return qs.order_by("-created_at", "-id")


# --- dashboard revenue (contract §2.10, O8) -----------------------------------------------------------------


def revenue_buckets(start: datetime, end: datetime, unit: str) -> dict[datetime, int]:
    """``{bucket start (Tashkent, aware): revenue}`` for payments confirmed in ``[start, end)``.

    Revenue = Σ ``amount`` of ``paid``/``refunded`` payments by confirmation time **minus** their succeeded
    refunds, attributed to the payment's own bucket — never negative (refunds ≤ amount). Late captures are
    excluded (customer contract §2.13.0: never order revenue). ``unit`` is ``hour`` or ``day``.
    """
    trunc = TruncHour if unit == enums.DashboardBucket.HOUR else TruncDay
    refunded = (
        Refund.objects.filter(payment_id=OuterRef("pk"), state=enums.RefundState.SUCCEEDED)
        .order_by()
        .values("payment_id")
        .annotate(total=Sum("amount"))
        .values("total")[:1]
    )
    rows = (
        Payment.objects.filter(
            status__in=list(SETTLED_PAYMENT_STATUSES), confirmed_at__gte=start, confirmed_at__lt=end
        )
        .exclude(error_code=LATE_CAPTURE)
        .annotate(
            bucket=trunc("confirmed_at", tzinfo=TASHKENT),
            net=F("amount") - Coalesce(Subquery(refunded, output_field=BigIntegerField()), Value(0)),
        )
        .order_by()
        .values("bucket")
        .annotate(revenue=Sum("net"))
        .values_list("bucket", "revenue")
    )
    return {bucket.astimezone(TASHKENT): int(revenue or 0) for bucket, revenue in rows}


def revenue_by_day_and_channel(start: datetime, end: datetime) -> dict[tuple[date, str], int]:
    """``{(Tashkent day, order channel): revenue}`` for payments confirmed in ``[start, end)`` — the rule of
    :func:`revenue_buckets` (paid/refunded net of succeeded refunds, late captures excluded) split by the
    order's ``channel`` (the ops console's sales chart). One query over ``payment_confirmed_idx``."""
    refunded = (
        Refund.objects.filter(payment_id=OuterRef("pk"), state=enums.RefundState.SUCCEEDED)
        .order_by()
        .values("payment_id")
        .annotate(total=Sum("amount"))
        .values("total")[:1]
    )
    rows = (
        Payment.objects.filter(
            status__in=list(SETTLED_PAYMENT_STATUSES), confirmed_at__gte=start, confirmed_at__lt=end
        )
        .exclude(error_code=LATE_CAPTURE)
        .annotate(
            day=TruncDay("confirmed_at", tzinfo=TASHKENT),
            net=F("amount") - Coalesce(Subquery(refunded, output_field=BigIntegerField()), Value(0)),
        )
        .order_by()
        .values("day", "order__channel")
        .annotate(revenue=Sum("net"))
        .values_list("day", "order__channel", "revenue")
    )
    return {
        (day.astimezone(TASHKENT).date(), str(channel)): int(revenue or 0) for day, channel, revenue in rows
    }


# --- nightly consistency report (reporting.consistency_report, docs/ops/monitoring.md) ----------------------
# Each returns a core.consistency.Finding (count + the first ids, one statement); the comment names the index
# that bounds the scan.


def stuck_pending_payments(expired_before: datetime, *, limit: int) -> Finding:
    """K2: ``pending`` payments holding a provider transaction whose order's reservation ran out before
    ``expired_before`` (the report passes now − ``PAYMENT_IN_FLIGHT_GRACE_SECONDS`` − its threshold): the
    capture neither completed nor was cancelled. Payme keeps a state-1 transaction open up to its own 12 h
    timeout, so a Payme row can show here until Payme cancels it.

    Plan: ``payment_status_created_idx`` range scan on ``status = 'pending'`` (only the open payments) with
    ``created_at < expired_before`` — implied, because a payment is created before its order's reservation
    runs out — then the order by its primary key."""
    queryset = Payment.objects.filter(
        status=enums.PaymentStatus.PENDING,
        created_at__lt=expired_before,
        order__reservation_expires_at__lt=expired_before,
    ).exclude(provider_txn_id="")
    return finding(queryset, limit=limit)


def click_paid_without_paydoc(confirmed_since: datetime, *, limit: int) -> Finding:
    """K2: Click payments ``paid`` since ``confirmed_since`` without ``provider_state.click_paydoc_id`` —
    their refund cannot be sent to Click (customer contract §2.13.0) and becomes ``manual_required``.

    Plan: ``payment_confirmed_idx`` (partial on paid/refunded) range scan on ``confirmed_at >=
    confirmed_since`` (the report's look-back), filtered by provider and the JSON key."""
    paydoc = KeyTextTransform("click_paydoc_id", "provider_state")
    queryset = (
        Payment.objects.filter(
            provider=enums.PaymentProvider.CLICK,
            status=enums.PaymentStatus.PAID,
            confirmed_at__gte=confirmed_since,
        )
        .annotate(paydoc=paydoc)
        .filter(Q(paydoc__isnull=True) | Q(paydoc=""))
    )
    return finding(queryset, limit=limit)


def open_refunds(requested_before: datetime, *, limit: int) -> Finding:
    """K3: refunds still open (``requested``, ``submitting``, ``unknown``, ``manual_required``) that were
    requested before ``requested_before``.

    Plan: the partial unique index ``refund_one_open_per_payment`` covers exactly the open refunds (one per
    payment at most); ``requested_at`` is filtered over them."""
    queryset = Refund.objects.filter(state__in=list(OPEN_REFUND_STATES), requested_at__lt=requested_before)
    return finding(queryset, limit=limit)


def failed_refunds_needing_action(*, limit: int) -> Finding:
    """K3: ``failed`` refunds that still need staff — the rule of admin contract §2.10
    ``attention.refunds_needing_action`` (not superseded by a later succeeded refund, payment not
    ``refunded``).

    Plan: ``refund_attention_idx`` (partial on failed/unknown/manual_required) range scan on ``state =
    'failed'``; the payment by its primary key and the later refunds through ``refund_payment_idx``."""
    queryset = Refund.objects.filter(state=enums.RefundState.FAILED).exclude(_superseded_failure())
    return finding(queryset, limit=limit)


def late_captures_not_refunded(confirmed_before: datetime, *, limit: int) -> Finding:
    """K5: refused captures (``error_code = 'late_capture'``, customer contract §2.13.0) captured before
    ``confirmed_before`` whose money is not back: still ``paid`` and no ``succeeded`` refund.

    Plan: ``payment_late_capture_open_idx`` (payments.0003, partial on late_capture ∧ paid) range scan on
    ``confirmed_at < confirmed_before``; the refunds through ``refund_payment_idx``."""
    succeeded = Refund.objects.filter(payment_id=OuterRef("pk"), state=enums.RefundState.SUCCEEDED)
    queryset = Payment.objects.filter(
        error_code=LATE_CAPTURE, status=enums.PaymentStatus.PAID, confirmed_at__lt=confirmed_before
    ).exclude(Exists(succeeded))
    return finding(queryset, limit=limit)


__all__ = [
    "PaymentFilters",
    "PaymentRow",
    "RefundRow",
    "active_payment",
    "click_paid_without_paydoc",
    "click_paydoc_id",
    "display_payment",
    "display_rank",
    "display_status_subquery",
    "failed_refunds_needing_action",
    "get_payment",
    "get_refund",
    "late_captures_not_refunded",
    "open_refunds",
    "payme_statement",
    "payment_by_txn",
    "payment_row",
    "payments_of_order",
    "payments_list",
    "pick_display",
    "refund_manual_reason_of",
    "refund_row",
    "refunds_by_payment",
    "refunds_for_order",
    "refunds_needing_action",
    "revenue_by_day_and_channel",
    "revenue_buckets",
    "search_q",
    "stuck_pending_payments",
]
