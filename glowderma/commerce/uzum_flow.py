"""The Uzum Checkout flows of an order (docs/integrations/uzum.md): start a payment, learn its outcome,
reconcile, refund.

**Starting a payment** (:func:`prepare_form`, before ``PayView``'s ``@idempotent`` transaction; no row lock
is held while Uzum answers): a payment row owns at most
one Uzum order. While its hosted form is live the stored link is handed out again (a second tap never
registers a second form, so the customer cannot pay twice). A spent form — its session is over — is replaced:
the row fails ``abandoned`` and a new pending row registers a new form under a fresh order number
(``<prefix>-<payment id>-<nonce>``: a register whose answer was lost may have taken the previous number, and
nobody holds that form's link). A form is only registered while the reservation outlives it by enough that its
session ends inside ``PAYMENT_IN_FLIGHT_GRACE_SECONDS`` (else 409 ``order_expired``).

**Learning the outcome**: Uzum's callback carries no amount and no signature, so it is only a hint
(``webhooks/uzum.py`` → outbox ``uzum.sync_order``); :func:`sync_payment` asks ``getOrderStatus`` with no
lock held and :func:`apply_status` decides under the Order lock: ``COMPLETED`` with **our** order number and
**exactly** the amount (tiyin) honours the capture (``mark_paid``) or records it as a late capture with an
automatic refund (``late_capture_case``); ``DECLINED`` fails the payment; a ``REGISTERED`` form whose
session is over fails it ``session_expired``; anything else is logged and left. Refund operations are
closed from the same answer.

**Reconcile** (:func:`reconcile`, beat ``commerce-reconcile-uzum``): pending Uzum payments, Uzum payments we
failed within :data:`LATE_WINDOW` (a capture whose callbacks were all lost — Uzum retries five times), and
Uzum refunds still ``submitting``.

**Refunds** (:func:`submit_refund`, the ``refund.submit`` handler's Uzum branch): ``X-Operation-Id`` is
derived from the refund row (:func:`.protocol.operation_id`), so a resend after a lost answer is deduplicated
by Uzum (3028) instead of refunding twice. ``errorCode 0`` only means "accepted": the refund stays
``submitting`` until the operation is ``SUCCESS`` / ``FAIL`` in ``getOrderStatus``.

Logs carry ids and codes only — never a card, a phone number or a payment page URL.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
from datetime import datetime, timedelta
from typing import Any

from django.conf import settings
from django.db import transaction

from glowderma.accounts.models import Customer
from glowderma.commerce import locks, selectors, services, topics
from glowderma.commerce.models import Order
from glowderma.core import alerts, enums, integration_modes, outbox, timeutils
from glowderma.core.api.exceptions import OrderExpired, ProviderUnavailable
from glowderma.integrations.uzum import protocol
from glowderma.integrations.uzum.ports import (
    OrderStatus,
    UzumNotProcessed,
    UzumRejected,
    UzumUnavailable,
)
from glowderma.integrations.uzum.registry import get_uzum
from glowderma.payments import selectors as payment_selectors
from glowderma.payments import services as payment_services
from glowderma.payments.models import UZUM_SETTLE_MARGIN, Payment, Refund, uzum_session_over

logger = logging.getLogger(__name__)

PROVIDER = enums.PaymentProvider.UZUM.value
# How long after we failed an Uzum payment the reconciler still asks Uzum about it (a lost capture).
LATE_WINDOW = timedelta(hours=24)
# A refund Uzum accepted but has not finished after this long is raised for staff (still polled).
REFUND_STUCK_AFTER = timedelta(hours=2)
# A refund claimed without Uzum's answer is sent again (same X-Operation-Id) after the outbox lease.
REFUND_RESEND_AFTER = timedelta(minutes=5)
# Failure codes of an Uzum payment that Uzum itself reported as final (never asked about again).
UZUM_FINAL_FAILURES = ("declined", "session_expired")


def client_id(customer: Customer) -> str:
    """The ``clientId`` Uzum keys saved cards by: an HMAC of the customer that changes when the account is
    started afresh (a dormant reset moves ``history_from``, a re-registration sets ``reactivated_at``), so
    the next holder of a recycled phone number never sees the previous holder's cards. Never the phone."""
    message = f"uzum-client:{customer.pk}:{customer.history_from}:{customer.reactivated_at}"
    digest = hmac.new(str(settings.PAYMENT_REF_KEY).encode(), message.encode(), hashlib.sha256)
    return digest.hexdigest()[:32]


def _session_ends(state: dict[str, Any]) -> datetime | None:
    raw = state.get("session_expires_at")
    if isinstance(raw, str) and raw:
        try:
            return datetime.fromisoformat(raw)
        except ValueError:
            return None
    return None


def form_window_open(order: Order, now: datetime) -> bool:
    """Whether a new form registered now would end inside the reservation's grace (module docstring)."""
    expires = order.reservation_expires_at
    timeout = timedelta(seconds=int(settings.UZUM_SESSION_TIMEOUT_SECS))
    grace = timedelta(seconds=int(settings.PAYMENT_IN_FLIGHT_GRACE_SECONDS))
    return expires is not None and now + timeout + UZUM_SETTLE_MARGIN <= expires + grace


def stored_link(payment: payment_selectors.PaymentRow, now: datetime) -> str | None:
    """The payment's registered form while its session lives, else ``None``."""
    state = payment.provider_state if isinstance(payment.provider_state, dict) else {}
    url = state.get("redirect_url")
    if payment.provider != PROVIDER or payment.status != enums.PaymentStatus.PENDING:
        return None
    if not state.get("registered_at") or uzum_session_over(state, now) or not isinstance(url, str) or not url:
        return None
    return url


def prepare_form(order_id: int, *, customer: Customer, language: str, return_url: str) -> None:
    """Register the order's Uzum form if it needs one — ``PayView.post`` runs it **before** its
    ``@idempotent`` transaction, holding no row lock while Uzum answers (AGENTS.md: plan under the Order lock
    and commit, call the provider holding only the advisory lock ``uzum:pay:{order_id}``, re-lock and
    record). An order the pay handler will refuse is left alone (the handler answers why); a live form is
    kept; a spent one is replaced by a new pending payment. Raises ``OrderExpired`` (too little reservation
    left), ``PaymentInProgress`` (a spent form that may still take money) and ``ProviderUnavailable`` (Uzum
    did not register it: nothing is attached, the next tap tries again with a new order number)."""
    now = timeutils.now()
    with transaction.atomic():
        order = services.lock_order(order_id)
        if order.status != enums.OrderStatus.AWAITING_PAYMENT or selectors.honoured_paid(order.pk):
            return
        if PROVIDER not in integration_modes.payable_providers():
            return
        if not selectors.is_payable(order, None, now, attaching=True):
            return
        flying = selectors.inflight_payment(order.pk, now)
        if flying is not None and flying.provider != PROVIDER:
            return
        pending = payment_selectors.active_payment(order.pk, enums.PaymentStatus.PENDING)
        if pending is not None and stored_link(pending, now) is not None:
            return
        renew = pending is not None and pending.provider == PROVIDER and bool(pending.transaction_id)
        payment = services.ensure_payable_payment(order.pk, PROVIDER, renew=renew, at=now)
        if not form_window_open(order, now):
            raise OrderExpired()
        scenario = services.fake_scenario(order, PROVIDER)
    try:
        with locks.uzum_pay_lock(order_id):
            current = payment_selectors.get_payment(payment.id)
            if current is None or current.status != enums.PaymentStatus.PENDING or current.transaction_id:
                return  # another tap registered it (or the payment moved on) meanwhile
            _register(
                order_id, current, scenario, customer=customer, language=language, return_url=return_url
            )
    except locks.UzumBusy:
        return  # another tap is registering right now; the handler answers what is stored


def _register(
    order_id: int,
    payment: payment_selectors.PaymentRow,
    scenario: dict[str, Any],
    *,
    customer: Customer,
    language: str,
    return_url: str,
) -> None:
    timeout = int(settings.UZUM_SESSION_TIMEOUT_SECS)
    order_number = protocol.new_order_number(str(settings.UZUM_ORDER_PREFIX), payment.id)
    try:
        registration = get_uzum(scenario).register(
            order_number=order_number,
            amount=protocol.to_tiyin(payment.amount),
            client_id=client_id(customer),
            language=language,
            success_url=return_url,
            failure_url=return_url,
            session_timeout=timeout,
        )
    except UzumRejected as exc:
        logger.error("uzum.register_rejected %s", {"payment_id": payment.id, "code": exc.code})
        raise ProviderUnavailable() from exc
    except UzumUnavailable as exc:
        # nothing is attached: a form registered with a lost answer is held by nobody (a fresh number next)
        logger.warning("uzum.register_unavailable %s", {"payment_id": payment.id, "sent": exc.sent})
        raise ProviderUnavailable() from exc
    registered_at = timeutils.now()
    with transaction.atomic():
        order = services.lock_order(order_id)
        current = payment_selectors.get_payment(payment.id)
        if (
            current is None
            or current.status != enums.PaymentStatus.PENDING
            or current.transaction_id
            or order.status != enums.OrderStatus.AWAITING_PAYMENT
        ):
            # the order moved on while Uzum answered: nobody gets this form's link, so it is never paid
            logger.warning("uzum.form_unused %s", {"payment_id": payment.id, "order_id": order_id})
            return
        payment_services.attach_transaction(
            payment.id,
            registration.order_id,
            {
                "order_number": order_number,
                "registered_at": registered_at.isoformat(),
                "session_expires_at": (registered_at + timedelta(seconds=timeout)).isoformat(),
                "redirect_url": registration.redirect_url,
            },
        )


# --- outcome ------------------------------------------------------------------------------------------------


def _alert(event: str, details: dict[str, Any], scope: str) -> None:
    logger.error("%s %s", event, details)
    transaction.on_commit(lambda: alerts.alert(event, details, scope=scope), robust=True)


def _captured(status: OrderStatus, payment: payment_selectors.PaymentRow) -> bool:
    return status.status == protocol.COMPLETED and protocol.matches_som(
        status.completed_amount, payment.amount
    )


def apply_status(payment_id: int, status: OrderStatus, *, now: datetime | None = None) -> str:
    """Apply a fresh ``getOrderStatus`` to the payment under the Order lock; returns what happened
    (``paid`` | ``late_capture`` | ``failed`` | ``unchanged`` | ``mismatch``) for logs and tests."""
    first = payment_selectors.get_payment(payment_id)
    if first is None or first.provider != PROVIDER:
        return "unchanged"
    with transaction.atomic():
        order = services.lock_order(first.order_id)
        instant = now or timeutils.now()
        payment = payment_selectors.get_payment(payment_id)
        assert payment is not None  # noqa: S101 - payments are never deleted
        state = payment.provider_state
        if (
            status.order_id != (payment.transaction_id or "").lower()
            or status.merchant_order_id != state.get("order_number")
            or not protocol.matches_som(status.amount, payment.amount)
        ):
            _alert("uzum.status_mismatch", {"payment_id": payment.id, "order_id": order.pk}, str(payment.id))
            return "mismatch"
        payment_services.merge_provider_state(
            payment.id, {"uzum_status": status.status, "checked_at": instant.isoformat()}
        )
        outcome = "unchanged"
        if payment.status in (enums.PaymentStatus.PENDING, enums.PaymentStatus.FAILED):
            outcome = _apply_payment(order, payment, status, instant)
        _apply_refunds(order, payment, status, instant)
        return outcome


def _apply_payment(
    order: Order, payment: payment_selectors.PaymentRow, status: OrderStatus, now: datetime
) -> str:
    txn = payment.transaction_id or ""
    if status.status == protocol.COMPLETED:
        if not _captured(status, payment) or status.refunded_amount or status.reversed_amount:
            _alert(
                "uzum.capture_unexpected", {"payment_id": payment.id, "order_id": order.pk}, str(payment.id)
            )
            return "mismatch"
        raw = {"completed_at": now.isoformat()}
        case = services.late_capture_case(order, payment, now)
        if case is None:
            services.mark_paid(order.pk, payment.id, PROVIDER, txn, raw=raw)
            return "paid"
        services.record_late_capture(order.pk, payment.id, PROVIDER, txn, case=case, raw=raw)
        return "late_capture"
    if payment.status != enums.PaymentStatus.PENDING:
        if status.status in (protocol.DECLINED, protocol.REGISTERED):
            payment_services.merge_provider_state(payment.id, {"final_checked": True})
        return "unchanged"
    if status.status == protocol.DECLINED:
        services.fail_payment_effects(order.pk, payment.id, "declined", provider_txn_id=txn)
        payment_services.merge_provider_state(payment.id, {"final_checked": True})
        return "failed"
    if status.status == protocol.REGISTERED and uzum_session_over(payment.provider_state, now):
        services.fail_payment_effects(order.pk, payment.id, "session_expired", provider_txn_id=txn)
        payment_services.merge_provider_state(payment.id, {"final_checked": True})
        return "failed"
    if status.status not in (protocol.REGISTERED,):
        # AUTHORIZED on a ONE_STEP payment, REFUNDED before we captured, …: never guessed
        _alert("uzum.status_unexpected", {"payment_id": payment.id, "status": status.status}, str(payment.id))
    return "unchanged"


def _apply_refunds(
    order: Order, payment: payment_selectors.PaymentRow, status: OrderStatus, now: datetime
) -> None:
    for refund in Refund.objects.filter(payment_id=payment.id, state=enums.RefundState.SUBMITTING).order_by(
        "id"
    ):
        operation = status.operation(protocol.operation_id(refund.pk))
        if operation is None or operation.operation_type != protocol.OP_REFUND:
            continue
        if operation.state == protocol.OP_SUCCESS:
            result = payment_services.mark_succeeded(
                refund.pk, now, provider_refund_id=operation.operation_id, provider_state={"done": True}
            )
            services.record_refund_outcome(order, result, at=now)
        elif operation.state == protocol.OP_FAIL:
            result = payment_services.mark_failed(
                refund.pk, "uzum_refund_failed", "Uzum reported the refund operation as failed", now
            )
            services.record_refund_outcome(order, result, at=now)


def sync_payment(payment_id: int) -> str:
    """Ask Uzum for the payment's status (no lock held) and apply it. A transport failure raises (the
    outbox retries the ``uzum.sync_order`` message; the reconciler asks again)."""
    payment = payment_selectors.get_payment(payment_id)
    if payment is None or payment.provider != PROVIDER or not payment.transaction_id:
        return "unchanged"
    if integration_modes.callbacks_refused(PROVIDER):
        return "unchanged"  # a fake under the production settings honours no capture
    order = Order.objects.get(pk=payment.order_id)
    port = get_uzum(services.fake_scenario(order, PROVIDER))
    try:
        status = port.order_status(payment.transaction_id)
    except UzumNotProcessed as exc:
        raise outbox.RetryLater(timedelta(seconds=30), str(exc)) from exc
    except UzumRejected as exc:
        logger.warning("uzum.status_rejected %s", {"payment_id": payment.id, "code": exc.code})
        return "unchanged"
    return apply_status(payment.id, status)


def enqueue_sync(payment_id: int, key: str) -> None:
    outbox.enqueue(
        topics.UZUM_SYNC_ORDER,
        {"payment_id": payment_id},
        dedupe_key=topics.uzum_sync_order_key(payment_id, key),
    )


# --- reconcile ----------------------------------------------------------------------------------------------


def reconcile(now: datetime | None = None) -> dict[str, int]:
    """The beat (``commerce.reconcile_uzum``): sync pending and recently failed Uzum payments and the
    payments of ``submitting`` Uzum refunds; resend refunds Uzum never answered. Counts only."""
    if integration_modes.callbacks_refused(PROVIDER):
        return {"payments": 0, "refunds": 0}
    instant = now or timeutils.now()
    payments = set(
        Payment.objects.filter(provider=PROVIDER, status=enums.PaymentStatus.PENDING)
        .exclude(provider_txn_id="")
        .values_list("id", flat=True)
    )
    payments |= {
        pk
        for pk, state in Payment.objects.filter(
            provider=PROVIDER, status=enums.PaymentStatus.FAILED, failed_at__gte=instant - LATE_WINDOW
        )
        .exclude(provider_txn_id="")
        .values_list("id", "provider_state")
        if not (isinstance(state, dict) and state.get("final_checked"))
    }
    refunds = list(
        Refund.objects.filter(payment__provider=PROVIDER, state=enums.RefundState.SUBMITTING).values_list(
            "id", "payment_id", "provider_state", "last_attempt_at"
        )
    )
    for refund_id, payment_id, state, last_attempt in refunds:
        accepted = isinstance(state, dict) and state.get("accepted_at")
        if accepted:
            payments.add(payment_id)
            if last_attempt is not None and last_attempt < instant - REFUND_STUCK_AFTER:
                logger.warning("uzum.refund_slow %s", {"refund_id": refund_id})
        elif last_attempt is None or last_attempt < instant - REFUND_RESEND_AFTER:
            refund = payment_selectors.get_refund(refund_id)
            if refund is not None:
                _run(lambda refund=refund: submit_refund(refund), f"refund {refund_id}")
    for payment_id in sorted(payments):
        _run(lambda payment_id=payment_id: sync_payment(payment_id), f"payment {payment_id}")
    return {"payments": len(payments), "refunds": len(refunds)}


def _run(step: Any, what: str) -> None:
    try:
        step()
    except outbox.RetryLater, UzumUnavailable:
        logger.info("uzum.reconcile: %s deferred (Uzum unavailable)", what)
    except Exception:
        logger.exception("uzum.reconcile: %s failed", what)


# --- refunds ------------------------------------------------------------------------------------------------


def _claim(refund: payment_selectors.RefundRow) -> payment_services.RefundResult | None:
    try:
        if refund.state == enums.RefundState.REQUESTED:
            return payment_services.mark_submitting(refund.id, timeutils.now())
        return payment_services.mark_submitting(
            refund.id,
            timeutils.now(),
            expected_state=enums.RefundState.SUBMITTING,
            expected_attempts=refund.attempts,
            resubmit_after=REFUND_RESEND_AFTER,
        )
    except payment_services.StaleRefundClaim as exc:
        logger.info("uzum.refund: not claiming refund %s: %s", refund.id, exc)
        return None


def _fail(refund_id: int, order_id: int, code: str, message: str, attempt: int) -> None:
    with transaction.atomic():
        order = services.lock_order(order_id)
        result = payment_services.mark_failed(
            refund_id, code, message, timeutils.now(), expected_attempts=attempt
        )
        if result.changed:
            services.record_refund_outcome(order, result, at=timeutils.now())


def submit_refund(refund: payment_selectors.RefundRow) -> None:
    """The Uzum branch of ``refund.submit`` (module docstring). The caller checked that the refund is
    ``requested`` / ``submitting`` and its payment ``paid``."""
    stored = Refund.objects.get(pk=refund.id)
    if refund.state == enums.RefundState.SUBMITTING and stored.provider_state.get("accepted_at"):
        return  # Uzum has it: the status sync closes it
    if integration_modes.callbacks_refused(PROVIDER):
        if refund.state == enums.RefundState.REQUESTED:
            payment_services.mark_manual_required(
                refund.id, timeutils.now(), reason=enums.RefundManualReason.UZUM_FAKE
            )
        else:
            result = payment_services.mark_unknown(
                refund.id,
                "uzum fake: refund outcome unknown",
                timeutils.now(),
                expected_attempts=refund.attempts,
            )
            if result.changed:
                with transaction.atomic():
                    order = services.lock_order(refund.order_id)
                    services.record_refund_outcome(order, result, at=timeutils.now())
        return
    order_ref = refund.provider_payment_ref
    claim = _claim(refund)
    if claim is None:
        return
    operation = protocol.operation_id(refund.id)
    payment_services.merge_refund_state(refund.id, {"operation_id": operation})
    order = Order.objects.get(pk=refund.order_id)
    port = get_uzum(services.fake_scenario(order, PROVIDER))
    try:
        accepted = port.refund(order_ref, protocol.to_tiyin(refund.amount), operation)
    except UzumNotProcessed as exc:
        payment_services.release_claim(refund.id, str(exc), timeutils.now(), expected_attempts=claim.attempts)
        raise
    except UzumUnavailable:
        # may have reached Uzum: stays submitting; the reconciler resends with the same X-Operation-Id
        logger.warning("uzum.refund_unavailable %s", {"refund_id": refund.id})
        payment_services.merge_refund_state(refund.id, {"sent_unknown_at": timeutils.now().isoformat()})
        return
    except UzumRejected as exc:
        if exc.code == str(protocol.ERR_OPERATION_EXISTS):
            _accepted(refund.id, "")
            return
        if exc.code == str(protocol.ERR_REFUND_AMOUNT) and _already_refunded(port, order_ref, refund.amount):
            _accepted(refund.id, "")
            return
        _fail(refund.id, refund.order_id, f"uzum_{exc.code}"[:32], exc.message, claim.attempts)
        if exc.code in {
            str(code) for code in (protocol.ERR_REFUND_CART_MISSING, protocol.ERR_REFUND_TIMEOUT)
        }:
            _alert("uzum.refund_refused", {"refund_id": refund.id, "code": exc.code}, str(refund.id))
        return
    _accepted(refund.id, accepted.operation_id)


def _already_refunded(port: Any, order_ref: str, amount: int) -> bool:
    try:
        status = port.order_status(order_ref)
    except UzumUnavailable, UzumRejected:
        return False
    return bool(status.refunded_amount >= protocol.to_tiyin(amount))


def _accepted(refund_id: int, uzum_operation_id: str) -> None:
    payment_services.merge_refund_state(
        refund_id, {"accepted_at": timeutils.now().isoformat(), "uzum_operation_id": uzum_operation_id}
    )
    refund = payment_selectors.get_refund(refund_id)
    if refund is not None:
        enqueue_sync(refund.payment_id, f"refund:{refund_id}")


__all__ = [
    "LATE_WINDOW",
    "apply_status",
    "client_id",
    "enqueue_sync",
    "form_window_open",
    "prepare_form",
    "reconcile",
    "stored_link",
    "submit_refund",
    "sync_payment",
]
