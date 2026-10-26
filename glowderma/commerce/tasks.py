"""Outbox handlers, dead-letter hooks and the beat task of `commerce` (design 03 §5, §4.1 rows 3/14/15).

Imported at startup by ``CommerceConfig.ready`` so the handlers exist in every process that dispatches the
outbox. Delivery is at-least-once: each handler is idempotent at the provider boundary.

* ``bts.create_shipment`` — under the Order lock: skip unless the order is ``processing`` and the shipment is
  not created yet, and skip a request BTS already answered (``request_no``); ask BTS for ``clientId =
  str(order_id)`` **before** creating (a redelivery after "BTS created the parcel, then the worker crashed"
  adopts it); ``shipment_created`` / ``shipment_failed`` (source ``admin`` + actor for a manual retry). For an
  order cancelled meanwhile the lookup still runs and a parcel found is cancelled at BTS (orphan). Only
  transport errors raise (the dispatcher retries).
* ``billz.push_sale`` — skip when the purchase is already pushed; push (Billz de-duplicates on the order
  UUID), ``loyalty.services.link_billz_sale`` and ``billz_sale_recorded`` once. A conflict raises (→ dead
  letter after the retries), never a silent success.
* ``refund.submit`` — Click: status check by ``provider_txn_id`` before any reversal; ``mark_submitting``
  (a fenced compare-and-set) is committed before the reversal call; ``succeeded`` / ``failed`` / ``unknown``
  (+ events); ``unknown`` is never retried automatically. Raises only on a transport error **before**
  submission, so a message that dies always finds the refund ``requested``.

Dead letters (``outbox.on_dead``): ``bts.create_shipment`` → shipment ``failed`` (``dead_letter``) +
``shipment_failed``; ``refund.submit`` → refund ``failed`` (``dead_letter``, never ``unknown``) +
``refund_failed``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from celery import shared_task  # type: ignore[import-untyped]
from celery.signals import worker_ready  # type: ignore[import-untyped]
from django.conf import settings
from django.core.cache import cache
from django.db import transaction

from glowderma.commerce import selectors, services, topics
from glowderma.commerce.billz_sales import SaleLine, get_sale_port
from glowderma.commerce.models import Order
from glowderma.core import enums, integration_modes, outbox, timeutils
from glowderma.core.api.exceptions import InvalidTransition
from glowderma.core.models import OutboxMessage
from glowderma.delivery import selectors as delivery_selectors
from glowderma.delivery import services as delivery_services
from glowderma.delivery.models import Shipment
from glowderma.integrations import selectors as integration_selectors
from glowderma.integrations import services as integration_services
from glowderma.integrations.bts.ports import BtsRejected, BtsUnavailable, CreateRequest, Parcel
from glowderma.integrations.bts.registry import get_bts
from glowderma.integrations.click.ports import ClickRefundRejected, ClickUnavailable
from glowderma.integrations.click.registry import get_click
from glowderma.loyalty import selectors as loyalty_selectors
from glowderma.loyalty import services as loyalty_services
from glowderma.payments import selectors as payment_selectors
from glowderma.payments import services as payment_services

logger = logging.getLogger(__name__)

_E = enums.OrderEventType


def _payload(message: OutboxMessage) -> dict[str, Any]:
    if not isinstance(message.payload, dict):
        raise ValueError(f"Outbox message {message.pk} has no object payload")
    return message.payload


# --- bts.create_shipment ------------------------------------------------------------------------------------


def _shipment_source(payload: dict[str, Any]) -> tuple[bool, int | None]:
    manual = bool(payload.get("manual"))
    actor_id = payload.get("actor_id")
    return manual, (int(actor_id) if manual and actor_id is not None else None)


def _request_no(payload: dict[str, Any]) -> int | None:
    value = payload.get("request_no")
    return None if value is None else int(value)


def _adopt_orphan(order: Order, scenario: dict[str, Any], request_no: int | None, now: datetime) -> None:
    """The order was cancelled while this create request was in flight: if the request reached BTS (the
    worker crashed after BTS created the parcel), the parcel exists although the cancel saw none — cancel it
    at BTS so no courier collects it (a transport error raises: the outbox retries)."""
    parcel = get_bts(scenario).find_by_client_id(str(order.pk))
    if parcel is None:
        return
    outcome = delivery_services.cancel_orphan(
        order.pk, parcel, scenario=scenario, at=now, request_no=request_no
    )
    if outcome.ok:
        logger.warning(
            "Cancelled the orphan BTS parcel %s of cancelled order %s", parcel.tracking_number, order.pk
        )
    else:
        logger.error(
            "BTS refused to cancel the orphan parcel %s of cancelled order %s: %s",
            parcel.tracking_number,
            order.pk,
            outcome.reason,
        )


def create_shipment(payload: dict[str, Any]) -> None:
    """The ``bts.create_shipment`` effect (callable inline by the seeder under a pinned clock).

    A request BTS already answered (its ``request_no`` names a ``created`` / ``create_failed`` shipment
    event) or one superseded by a newer request is skipped without calling BTS: a redelivery after
    "``mark_failed`` committed, then the worker died" must not ask BTS a second time.
    """
    order_id = int(payload["order_id"])
    manual, actor_id = _shipment_source(payload)
    request_no = _request_no(payload)
    with transaction.atomic():
        order = services.lock_order(order_id)
        if order.fulfilment != enums.FulfilmentMethod.DELIVERY:
            return
        shipment = delivery_selectors.shipment_info(order_id)
        if shipment is None or shipment.state in (Shipment.State.CREATED, Shipment.State.CANCELLED):
            return
        if request_no is not None and (
            request_no < shipment.create_requests
            or delivery_selectors.create_request_answered(order_id, request_no)
        ):
            return  # answered before, or superseded by a newer (manual) request
        scenario = services.fake_scenario(order, "bts")
        now = timeutils.now()
        if order.status == enums.OrderStatus.CANCELLED:
            _adopt_orphan(order, scenario, request_no, now)
            return
        if order.status != enums.OrderStatus.PROCESSING:
            return  # delivered meanwhile: nothing to create (03 §4.1 row 6)
        port = get_bts(scenario)
        attempt = shipment.attempts + 1
        parcel = port.find_by_client_id(str(order_id))
        if parcel is None:
            try:
                parcel = port.create(
                    CreateRequest(
                        client_id=str(order_id),
                        attempt=attempt,
                        recipient_name=order.recipient_name,
                        recipient_phone=order.recipient_phone,
                        address=order.address_text,
                        region_code=order.address_region_code,
                        city_code=order.address_city_code,
                        comment=order.address_comment,
                    )
                )
            except BtsRejected as exc:
                number = delivery_services.mark_failed(
                    order_id,
                    error_code=exc.error_code,
                    error=exc.message,
                    at=now,
                    manual=manual,
                    request_no=request_no,
                )
                if number is not None:
                    services.record_event_once(
                        order,
                        _E.SHIPMENT_FAILED,
                        {"attempt": number, "manual": manual, "error_code": exc.error_code},
                        actor_id=actor_id,
                        at=now,
                    )
                return
        tariff = parcel.tariff if parcel.tariff is not None else order.delivery_fee
        delivery_services.mark_created(
            order_id,
            tracking_number=parcel.tracking_number,
            tariff=tariff,
            bts_order_id=parcel.bts_order_id,
            bts_status=parcel.status,
            at=now,
            manual=manual,
            payload={"tracking_number": parcel.tracking_number, "status": parcel.status},
            request_no=request_no,
        )
        services.record_event_once(
            order,
            _E.SHIPMENT_CREATED,
            {"tracking_number": parcel.tracking_number, "tariff": tariff, "manual": manual},
            actor_id=actor_id,
            at=now,
        )


@outbox.register(topics.BTS_CREATE_SHIPMENT)
def handle_bts_create_shipment(message: OutboxMessage) -> None:
    create_shipment(_payload(message))


def on_bts_create_dead(message: OutboxMessage) -> None:
    """Ten transport failures: the shipment is ``failed`` (``dead_letter``) and retryable by the admin.

    Runs once per dead message; every dead create request (the automatic one, then each manual retry) gets
    its own ``shipment_failed {attempt, manual, error_code: dead_letter}``. A second notice of the same
    message is a no-op (``mark_failed`` finds the request answered).
    """
    payload = _payload(message)
    order_id = int(payload["order_id"])
    manual, actor_id = _shipment_source(payload)
    now = timeutils.now()
    with transaction.atomic():
        order = services.lock_order(order_id)
        if order.status != enums.OrderStatus.PROCESSING:
            return
        shipment = Shipment.objects.filter(order_id=order_id).first()
        if shipment is None or shipment.create_message_id not in (None, message.pk):
            return  # a newer create request owns the shipment now
        number = delivery_services.mark_failed(
            order_id,
            error_code=topics.DEAD_LETTER_ERROR_CODE,
            error=message.last_error,
            at=now,
            manual=manual,
            request_no=_request_no(payload),
        )
        if number is not None:
            services.record_event_once(
                order,
                _E.SHIPMENT_FAILED,
                {"attempt": number, "manual": manual, "error_code": topics.DEAD_LETTER_ERROR_CODE},
                actor_id=actor_id,
                at=now,
            )


# --- billz.push_sale ----------------------------------------------------------------------------------------


def push_sale(payload: dict[str, Any]) -> None:
    """The ``billz.push_sale`` effect (callable inline by the seeder under a pinned clock)."""
    purchase_id = int(payload["purchase_id"])
    order_id = int(payload["order_id"])
    order_uuid = UUID(str(payload["billz_order_uuid"]))
    info = loyalty_selectors.billz_push_info(purchase_id)
    if info is None:
        raise LookupError(f"Purchase {purchase_id} does not exist")
    if info.billz_pushed_at is not None:
        return
    if integration_modes.fake_in_production("billz"):
        # customer contract §6.14: never record a Billz sale the fake did not send; staff enter it by hand
        logger.warning("billz.sale_not_pushed %s", {"order_id": order_id, "purchase_id": purchase_id})
        return
    order = Order.objects.get(pk=order_id)
    lines = [
        SaleLine(billz_id=item.billz_id, quantity=item.quantity, unit_price=item.unit_price)
        for item in order.items.order_by("position")
    ]
    receipt = get_sale_port(services.fake_scenario(order, "billz")).push_sale(
        order_uuid, lines, without_cashback=True
    )
    now = timeutils.now()
    with transaction.atomic():
        locked = services.lock_order(order_id)
        loyalty_services.link_billz_sale(
            purchase_id, billz_order_uuid=order_uuid, billz_sale_id=receipt.billz_sale_id, pushed_at=now
        )
        if not locked.events.filter(type=_E.BILLZ_SALE_RECORDED).exists():
            services.record_event(locked, _E.BILLZ_SALE_RECORDED, {"billz_order_id": str(order_uuid)}, at=now)


@outbox.register(topics.BILLZ_PUSH_SALE)
def handle_billz_push_sale(message: OutboxMessage) -> None:
    push_sale(_payload(message))


# --- refund.submit ------------------------------------------------------------------------------------------

# The outbox lease of ``core.tasks`` (a message whose worker has not finished after it is handed to another
# worker). A refund claimed longer ago than this cannot have a reversal call in flight any more (the Click
# port bounds every call to seconds), so a redelivery may submit it again; a younger claim belongs to a
# worker that is still waiting for Click and resolves the refund itself.
RESUBMIT_AFTER = timedelta(minutes=5)


def _record(order_id: int, result: payment_services.RefundResult) -> None:
    with transaction.atomic():
        order = services.lock_order(order_id)
        services.record_refund_outcome(order, result, at=timeutils.now())


def _claim(refund_id: int, **expected: Any) -> payment_services.RefundResult | None:
    """``mark_submitting`` as a compare-and-set; ``None`` when another worker owns the refund now."""
    try:
        return payment_services.mark_submitting(refund_id, timeutils.now(), **expected)
    except payment_services.StaleRefundClaim as exc:
        logger.info("refund.submit: not claiming refund %s: %s", refund_id, exc)
        return None


def _unknown(refund_id: int, order_id: int, error: str, attempt: int) -> None:
    result = payment_services.mark_unknown(refund_id, error, timeutils.now(), expected_attempts=attempt)
    if result.changed:
        _record(order_id, result)


def _succeed(refund_id: int, order_id: int, provider_refund_id: str) -> None:
    """Click reversed the payment: ``succeeded`` (from ``submitting`` / ``unknown``)."""
    with transaction.atomic():
        order = services.lock_order(order_id)
        try:
            result = payment_services.mark_succeeded(
                refund_id, timeutils.now(), provider_refund_id=provider_refund_id
            )
        except InvalidTransition:
            # a stale worker learnt of a reversal after the refund was closed as failed: alert, never rewrite
            logger.error("refund.submit: Click reversed refund %s, but it is closed already", refund_id)
            return
        services.record_refund_outcome(order, result, at=timeutils.now())


def _resolve_after_failure(
    refund_id: int, order_id: int, payment_ref: str, port: Any, error: str, attempt: int
) -> None:
    """A transport failure after the reversal was sent: ask once more; inconclusive → ``unknown``."""
    try:
        status = port.payment_status(payment_ref)
    except ClickUnavailable as exc:
        _unknown(refund_id, order_id, f"{error}; status check: {exc}", attempt)
        return
    if status == "reversed":
        _succeed(refund_id, order_id, "")
        return
    _unknown(refund_id, order_id, f"{error}; payment still {status}", attempt)


def _reversed(port: Any, payment_ref: str) -> bool:
    try:
        return bool(port.payment_status(payment_ref) == "reversed")
    except ClickUnavailable:
        return False


PAYMENT_NOT_PAID = "payment_not_paid"


def _fail_unpaid(refund: payment_selectors.RefundRow) -> None:
    """The payment is no longer ``paid`` (a later refund returned the money): end the refund ``failed``
    ``payment_not_paid`` with its ``refund_failed`` event and never call Click (customer contract §2.13.0)."""
    with transaction.atomic():
        order = services.lock_order(refund.order_id)
        current = payment_selectors.get_refund(refund.id)
        if current is None or current.state not in (
            enums.RefundState.REQUESTED,
            enums.RefundState.SUBMITTING,
        ):
            return
        result = payment_services.mark_failed(
            refund.id, PAYMENT_NOT_PAID, "the payment is no longer paid", timeutils.now()
        )
        if result.changed:
            services.record_refund_outcome(order, result, at=timeutils.now())


def submit_refund(payload: dict[str, Any]) -> None:
    """The ``refund.submit`` effect (02 §2.2 handler; callable inline by the seeder).

    Re-checks before any port call (customer contract §2.13.0): the payment must still be ``paid`` (else the
    refund ends ``failed`` ``payment_not_paid``), and a refund whose payment has no ``click_paydoc_id`` — or
    while the Click port is fake under the production settings — becomes ``manual_required`` (its
    ``refund_requested`` event stays as written). Every Click call is keyed by the payment's
    ``click_paydoc_id`` (``RefundRow.provider_payment_ref``), never ``click_trans_id``.

    ``requested``: status check (a transport error raises, the refund stays ``requested``), claim
    (compare-and-set), reverse. ``submitting`` (a redelivery): status check first — reversed → ``succeeded``,
    check failed → ``unknown``; not reversed → re-claim only when the previous claim is older than the
    outbox lease (:data:`RESUBMIT_AFTER`), else leave it to the worker that holds it. Every outcome is
    written with the attempt this worker claimed (a newer attempt wins), and a refusal is double-checked
    against the payment status (Click refuses a duplicate of a reversal it performed).
    """
    refund_id = int(payload["refund_id"])
    refund = payment_selectors.get_refund(refund_id)
    if refund is None:
        raise LookupError(f"Refund {refund_id} does not exist")
    if refund.state not in (enums.RefundState.REQUESTED, enums.RefundState.SUBMITTING):
        return  # succeeded / failed / manual_required / unknown: nothing to do (unknown is never retried)
    if refund.provider != enums.PaymentProvider.CLICK:
        return
    payment = payment_selectors.get_payment(refund.payment_id)
    if payment is None or payment.status != enums.PaymentStatus.PAID:
        _fail_unpaid(refund)
        return
    ref = refund.provider_payment_ref
    if not ref or integration_modes.callbacks_refused("click"):
        if refund.state == enums.RefundState.REQUESTED:
            payment_services.mark_manual_required(refund_id, timeutils.now())
        else:
            # ``submitting``: a worker may have sent the reversal before the switch (or crashed after Click
            # answered), so its outcome at Click is unknown — never left ``submitting``, which no one sees
            reason = "click fake: reversal outcome unknown" if ref else "no click_paydoc_id"
            _unknown(refund_id, refund.order_id, reason, refund.attempts)
        return
    order = Order.objects.get(pk=refund.order_id)
    port = get_click(services.fake_scenario(order, "click"))
    if refund.state == enums.RefundState.REQUESTED:
        # status check before anything is sent: a transport error here raises (the refund stays requested)
        already = port.payment_status(ref) == "reversed"
        claim = _claim(refund_id, expected_state=enums.RefundState.REQUESTED)
        if claim is None:
            return
        if already:
            _succeed(refund_id, order.pk, "")
            return
    else:
        # a redelivery: the reversal may have gone through already
        try:
            status = port.payment_status(ref)
        except ClickUnavailable as exc:
            _unknown(refund_id, order.pk, f"status check: {exc}", refund.attempts)
            return
        if status == "reversed":
            _succeed(refund_id, order.pk, "")
            return
        claim = _claim(
            refund_id,
            expected_state=enums.RefundState.SUBMITTING,
            expected_attempts=refund.attempts,
            resubmit_after=RESUBMIT_AFTER,
        )
        if claim is None:
            return
    try:
        receipt = port.refund(ref, refund.amount)
    except ClickRefundRejected as exc:
        if _reversed(port, ref):
            _succeed(refund_id, order.pk, "")
            return
        with transaction.atomic():
            locked = services.lock_order(order.pk)
            result = payment_services.mark_failed(
                refund_id, exc.error_code, exc.message, timeutils.now(), expected_attempts=claim.attempts
            )
            if result.changed:
                services.record_refund_outcome(locked, result, at=timeutils.now())
        return
    except ClickUnavailable as exc:
        _resolve_after_failure(refund_id, order.pk, ref, port, str(exc), claim.attempts)
        return
    _succeed(refund_id, order.pk, receipt.provider_refund_id)


@outbox.register(topics.REFUND_SUBMIT)
def handle_refund_submit(message: OutboxMessage) -> None:
    submit_refund(_payload(message))


def on_refund_submit_dead(message: OutboxMessage) -> None:
    """Ten transport failures before submission: refund ``failed`` (``dead_letter``), never ``unknown``.

    The state is re-read under the Order → Payment locks (``expected_state="requested"``): a refund a stale
    worker moved to ``submitting`` meanwhile is left to that worker."""
    payload = _payload(message)
    refund = payment_selectors.get_refund(int(payload["refund_id"]))
    if refund is None:
        return
    with transaction.atomic():
        order = services.lock_order(refund.order_id)
        result = payment_services.mark_failed(
            refund.id,
            topics.DEAD_LETTER_ERROR_CODE,
            message.last_error,
            timeutils.now(),
            expected_state=enums.RefundState.REQUESTED,
        )
        if result.changed:
            services.record_refund_outcome(order, result, at=timeutils.now())


outbox.on_dead(topics.BTS_CREATE_SHIPMENT)(on_bts_create_dead)
outbox.on_dead(topics.REFUND_SUBMIT)(on_refund_submit_dead)


# --- beat ---------------------------------------------------------------------------------------------------


@shared_task(name="commerce.expire_reservations")  # type: ignore[untyped-decorator]
def expire_reservations() -> list[int]:
    """Every minute: cancel the unpaid orders whose reservation ran out (03 §4.1 row 3)."""
    return services.expire_due_reservations()


__all__ = [
    "create_shipment",
    "expire_reservations",
    "handle_billz_push_sale",
    "handle_bts_create_shipment",
    "handle_refund_submit",
    "on_bts_create_dead",
    "on_refund_submit_dead",
    "poll_bts",
    "push_sale",
    "report_fake_provider_open_money",
    "run_bts_poll",
    "submit_refund",
]


# --- BTS polling (customer contract §2.13.3) ---------------------------------------------------------------

POLL_LOCK_KEY = "gd:lock:poll_bts"
POLL_LOCK_SECONDS = 600
POLL_CURSOR_KEY = "poll_cursor"


@dataclass(frozen=True, slots=True)
class PollResult:
    tracked: list[int]  # order ids whose parcel was tracked
    rejected: int
    unavailable: int
    stopped: bool


def stable_payload(parcel: Parcel) -> dict[str, Any]:
    """The stable fields of a tracked parcel (never the whole document, whose volatile fields would make every
    poll a new status): tracking number, status code and BTS's status time when it sends one."""
    payload: dict[str, Any] = {"tracking_number": parcel.tracking_number, "status": parcel.status}
    if parcel.status_at:
        payload["status_at"] = parcel.status_at
    return payload


def run_bts_poll() -> PollResult:
    """One polling run: single flight (``cache.add`` lock), round robin after
    ``IntegrationState(bts).data["poll_cursor"]`` (written after every attempted parcel), at most
    ``BTS_POLL_BATCH`` parcels and ``BTS_POLL_MAX_SECONDS``; per-parcel failures are skipped, the run stops
    only
    after ``BTS_POLL_MAX_CONSECUTIVE_FAILURES`` transport failures in a row. Outcome: stopped →
    ``record_failure``; ≥ 1 tracked → ``record_sync_success``; attempted but none tracked →
    ``record_failure`` +
    ERROR ``bts.poll_nothing_tracked``; no candidate → nothing written. Returns at once while BTS is fake
    under
    the production settings."""
    empty = PollResult(tracked=[], rejected=0, unavailable=0, stopped=False)
    if integration_modes.fake_in_production("bts"):
        return empty
    if not cache.add(POLL_LOCK_KEY, 1, timeout=POLL_LOCK_SECONDS):
        return empty
    try:
        return _poll()
    finally:
        cache.delete(POLL_LOCK_KEY)


def _poll() -> PollResult:
    started = timeutils.now()
    stale_before = started - timedelta(minutes=settings.BTS_POLL_STALE_MINUTES)
    candidates = delivery_selectors.stale_shipments(stale_before)
    if not candidates:
        return PollResult(tracked=[], rejected=0, unavailable=0, stopped=False)
    state = integration_selectors.provider_state("bts")
    raw_cursor = (
        state.data.get(POLL_CURSOR_KEY) if state is not None and isinstance(state.data, dict) else None
    )
    cursor = int(raw_cursor) if isinstance(raw_cursor, int) else 0
    after = [row for row in candidates if row[0] > cursor]
    batch = (after or candidates)[: settings.BTS_POLL_BATCH]
    tracked: list[int] = []
    rejected = unavailable = streak = 0
    stopped = False
    for shipment_id, order_id, tracking_number in batch:
        if (timeutils.now() - started).total_seconds() >= settings.BTS_POLL_MAX_SECONDS:
            break
        order = Order.objects.get(pk=order_id)
        try:
            parcel = get_bts(services.fake_scenario(order, "bts")).track(tracking_number)
        except BtsUnavailable:
            unavailable += 1
            streak += 1
            logger.warning("bts.poll_parcel_failed %s", {"shipment_id": shipment_id, "kind": "unavailable"})
            integration_services.set_state_data("bts", POLL_CURSOR_KEY, shipment_id)
            if streak >= settings.BTS_POLL_MAX_CONSECUTIVE_FAILURES:
                stopped = True
                break
            continue
        except BtsRejected:
            rejected += 1
            streak = 0
            logger.warning("bts.poll_parcel_failed %s", {"shipment_id": shipment_id, "kind": "rejected"})
            integration_services.set_state_data("bts", POLL_CURSOR_KEY, shipment_id)
            continue
        streak = 0
        tracked.append(order_id)
        services.apply_bts_status(
            order_id, parcel.status, enums.ShipmentVia.POLLING, payload=stable_payload(parcel)
        )
        integration_services.set_state_data("bts", POLL_CURSOR_KEY, shipment_id)
    if stopped:
        integration_services.record_failure("bts", f"poll stopped: {streak} transport failures in a row")
    elif tracked:
        integration_services.record_sync_success("bts")
    elif rejected or unavailable:
        integration_services.record_failure(
            "bts", f"poll: no parcel tracked (rejected {rejected}, unavailable {unavailable})"
        )
        logger.error("bts.poll_nothing_tracked %s", {"rejected": rejected, "unavailable": unavailable})
    return PollResult(tracked=tracked, rejected=rejected, unavailable=unavailable, stopped=stopped)


@shared_task(name="commerce.poll_bts")  # type: ignore[untyped-decorator]
def poll_bts() -> list[int]:
    """BTS polling fallback (beat ``commerce-poll-bts`` every 600 s): the order ids whose parcel was
    tracked."""
    return run_bts_poll().tracked


# --- fake payment providers holding open money (customer contract §6.14)
# -------------------------------------


def report_fake_provider_open_money() -> list[str]:
    """At worker start: one ERROR ``providers.fake_with_open_money {provider, open_transactions, open_refunds,
    order_ids}`` per payment provider that is fake under the production settings and still holds open money
    (the operator procedure of §6.14 was skipped). Writes nothing; returns the providers reported."""
    reported = []
    for provider in (enums.PaymentProvider.CLICK, enums.PaymentProvider.PAYME):
        if not integration_modes.fake_in_production(provider):
            continue
        money = selectors.provider_open_money(provider)
        if money.open_transactions == 0 and money.open_refunds == 0:
            continue
        logger.error(
            "providers.fake_with_open_money %s",
            {
                "provider": str(provider),
                "open_transactions": money.open_transactions,
                "open_refunds": money.open_refunds,
                "order_ids": money.order_ids,
            },
        )
        reported.append(str(provider))
    return reported


@worker_ready.connect  # type: ignore[untyped-decorator]
def _report_open_money_at_worker_start(**_kwargs: Any) -> None:
    try:
        report_fake_provider_open_money()
    except Exception:  # the check must never keep a worker from starting
        logger.exception("providers.fake_with_open_money check failed")
