"""The Yandex Delivery calls of an order (plan §3.6 dispatch, §3.7 status sync; decisions 6, 7, 9, 10).

Every flow here talks to Yandex **outside** any database transaction: it plans under the Order lock and
commits, calls the port holding only the advisory lock ``yandex:{order_id}`` (:mod:`glowderma.commerce.locks`,
shared with the carrier-side cancel), then re-locks the order and records the answer through
``commerce.services.apply_yandex_claim`` (the single writer of claim statuses) or the ``delivery.services``
claim writers. Each step commits before the next call, so a redelivered message resumes where the last one
stopped:

* a claim without Yandex's id → step 1 (the previous claim must be provably finished) and step 2
  (``claims/create`` with the claim's own ``request_id`` — a retry after a timeout returns the same claim);
* a claim with an id → step 3 (wait for the estimate) and step 4 (warnings / pricing policy → review);
* step 5 stamps ``accept_sent_at`` and commits **before** ``claims/accept`` is sent: a claim that has the
  stamp is only ever re-read (never a second accept), until Yandex moves it past ``ready_for_approval``.

Outcomes the outbox understands: a transport failure that may have reached Yandex raises (retried with
backoff, ``dead`` after 10 attempts → issue ``dispatch_unknown``); "not processed" (429, 401/403, …) and
"wait" (estimating, acceptance not visible yet, the order busy) defer (``RetryLater``, no attempt spent).
Logs carry ids, codes and amounts only — never the courier's or the customer's phone, name or address.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, NoReturn

from django.conf import settings
from django.core.cache import cache
from django.db import transaction

from glowderma.commerce import locks, services, yandex_ref
from glowderma.commerce.models import Order
from glowderma.core import enums, integration_modes, outbox, timeutils
from glowderma.delivery import pricing
from glowderma.delivery import selectors as delivery_selectors
from glowderma.delivery import services as delivery_services
from glowderma.delivery import yandex as delivery_yandex
from glowderma.delivery.models import DeliverySettings, Shipment, ShipmentEvent
from glowderma.integrations import services as integration_services
from glowderma.integrations.yandex_delivery.ports import (
    CANCEL_FREE,
    Claim,
    ClaimRequest,
    Contact,
    ParcelItem,
    Point,
    RoutePoint,
    YandexConflict,
    YandexDeliveryPort,
    YandexError,
    YandexRejected,
    YandexUnavailable,
)
from glowderma.integrations.yandex_delivery.registry import get_yandex_delivery

logger = logging.getLogger(__name__)

_I = enums.DeliveryIssueKind
_G = enums.ShipmentStage
OPEN_ORDER_STATUSES = (enums.OrderStatus.PROCESSING, enums.OrderStatus.HANDED_TO_COURIER)
ESTIMATING = frozenset({"new", "estimating"})

# Step 3: a few short reads while Yandex estimates ("a few seconds"), then defer; give up after 5 minutes.
ESTIMATE_READS = 3
ESTIMATE_PAUSE_SECONDS = 1.0
ESTIMATE_GIVE_UP = timedelta(minutes=5)
# Step 5: after the accept, re-read until the claim moves; still ``ready_for_approval`` this long after the
# accept was sent → issue ``dispatch_unknown`` (the claim stays open and polled; nothing is resent).
ACCEPT_CONFIRM_WINDOW = timedelta(minutes=15)
RETRY_DELAY = timedelta(seconds=10)
BUSY_DELAY = timedelta(seconds=15)
# Yandex answered "not processed" (429, 401/403, 408/425, a redirect): nothing happened, try again later.
NOT_PROCESSED_DELAY = timedelta(seconds=60)
NOT_PROCESSED_GIVE_UP = timedelta(minutes=30)
# The deferral window the nightly consistency report (K6) allows ``yandex.dispatch`` (outbox.RetryLater).
DISPATCH_DEFERRAL_WINDOW = timedelta(minutes=45)
# A callback's claims/info that still shows an older updated_ts is re-read this long (docs: 5–30 s lag).
SYNC_STALE_WINDOW = timedelta(minutes=2)

POLL_LOCK_KEY = "gd:lock:poll_yandex"
POLL_LOCK_SECONDS = 300
COST_UNKNOWN_AFTER = timedelta(hours=24)

# Dispatch failure codes of our own (Yandex's codes pass through as they are).
CODE_ESTIMATING_TIMEOUT = "estimating_timeout"
CODE_OFFER_EXPIRED = "offer_expired"
CODE_PREVIOUS_ACTIVE = "previous_claim_active"
CODE_FOREIGN_CLAIM = "foreign_claim"
CODE_ACCEPT_UNCONFIRMED = "accept_unconfirmed"
CODE_DEAD_LETTER = "dead_letter"
CODE_ORDER_CLOSED = "order_closed"
CODE_CANCEL_REQUESTED = "cancel_requested"
CODE_OFFER_MISSING = "offer_missing"
CODE_WARNINGS = "warnings"
CODE_NOT_FOUND = "not_found"

_sleep: Callable[[float], None] = time.sleep  # a module attribute so tests skip the pauses


# --- shared helpers -----------------------------------------------------------------------------------------


def _not_processed(exc: YandexUnavailable, what: str) -> NoReturn:
    """A Yandex failure as the outbox needs it: "may have been processed" re-raises (retried with backoff,
    the same request); "provably not processed" defers without spending an attempt."""
    if exc.outcome_unknown:
        raise exc
    raise outbox.RetryLater(
        NOT_PROCESSED_DELAY, f"yandex {what}: not processed", give_up_after=NOT_PROCESSED_GIVE_UP
    )


def scenario_of(order_id: int) -> dict[str, Any]:
    order = Order.objects.only("metadata").get(pk=order_id)
    return services.fake_scenario(order, "yandex")


def store_tracking_link(port: YandexDeliveryPort, order_id: int, claim_pk: int, claim_id: str) -> str | None:
    """Best effort: the destination's tracking link of an open claim (``None`` while Yandex has none)."""
    try:
        url = port.tracking_link(claim_id)
    except YandexError as exc:
        logger.info(
            "yandex.tracking_link_unavailable %s", {"order_id": order_id, "error": type(exc).__name__}
        )
        return None
    if not url:
        return None
    with transaction.atomic():
        services.lock_order(order_id)
        delivery_services.set_tracking_url(claim_pk, url)
    return url


def _cancel_if_unaccepted(port: YandexDeliveryPort, order_id: int, snapshot: Claim) -> None:
    """Best effort: cancel a claim Yandex shows as never accepted (it summons nobody; this keeps Yandex's
    list clean and stops a lagging acceptance). Free terms only."""
    if snapshot.status not in delivery_yandex.PRE_ACCEPT:
        return
    try:
        terms = port.cancel_terms(snapshot.claim_id)
        if terms.state == CANCEL_FREE:
            port.cancel(snapshot.claim_id, snapshot.version, CANCEL_FREE)
    except YandexError as exc:
        logger.info("yandex.cancel_unaccepted_failed %s", {"order_id": order_id, "error": type(exc).__name__})


def _cancel_unaccepted(port: YandexDeliveryPort, order_id: int, claim_id: str) -> None:
    """:func:`_cancel_if_unaccepted` after a fresh read of ``claim_id`` (nothing when it has no id)."""
    if not claim_id:
        return
    try:
        current = port.claim(claim_id)
    except YandexError as exc:
        logger.info("yandex.cancel_unaccepted_failed %s", {"order_id": order_id, "error": type(exc).__name__})
        return
    _cancel_if_unaccepted(port, order_id, current)


# --- dispatch (plan §3.6) -----------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Plan:
    order_id: int
    claim: delivery_selectors.ClaimRow
    previous: delivery_selectors.ClaimRow | None
    scenario: dict[str, Any]
    paid_fee: int
    max_auto_increase: int
    max_loss_per_shipment: int
    stop: str  # "" = carry on; else why the dispatch is abandoned (order closed, cancel requested)
    request: ClaimRequest | None  # claims/create (only while the claim has no Yandex id yet)
    refusal: str  # "" = the request could be built; else why not (not_configured, coordinates_required)


def claim_request(
    order: Order, claim: delivery_selectors.ClaimRow, shipment: Shipment, row: DeliverySettings
) -> ClaimRequest | str:
    """The ``claims/create`` request of ``claim`` — the warehouse (point A, its contact, e-mail and courier
    comment) to the order's address snapshot and pin (point B, the recipient's name and phone: they travel in
    the request body only, never in a log) — or the code of what is missing."""
    if not delivery_selectors.warehouse_configured(row):
        return "not_configured"
    if order.address_latitude is None or order.address_longitude is None:
        return "coordinates_required"
    assert row.warehouse_latitude is not None and row.warehouse_longitude is not None  # noqa: S101
    weight = shipment.parcel_weight_g or row.default_item_weight_g
    return ClaimRequest(
        request_id=claim.request_id,
        external_order_id=str(order.pk),
        source=RoutePoint(
            point=Point(
                lon=float(row.warehouse_longitude),
                lat=float(row.warehouse_latitude),
                fullname=row.warehouse_address.strip(),
            ),
            contact=Contact(name=row.contact_name.strip(), phone=row.contact_phone, email=row.contact_email),
            comment=row.courier_comment,
        ),
        destination=RoutePoint(
            point=Point(
                lon=float(order.address_longitude),
                lat=float(order.address_latitude),
                fullname=order.address_text,
            ),
            contact=Contact(name=order.recipient_name, phone=order.recipient_phone),
            comment=order.address_comment,
        ),
        item=ParcelItem(
            title=f"GlowDerma №{order.pk}",
            quantity=1,
            weight_kg=Decimal(weight) / Decimal(1000),
            cost_value=order.items_subtotal,
        ),
        taxi_class=claim.taxi_class,
        optional_return=False,
        skip_confirmation_source=bool(settings.YANDEX_DELIVERY_SKIP_CONFIRMATION_SOURCE),
        skip_confirmation_destination=bool(settings.YANDEX_DELIVERY_SKIP_CONFIRMATION_DESTINATION),
        callback_url=yandex_ref.callback_url(order.pk),
    )


def _plan(order_id: int, request_id: str) -> _Plan | None:
    """Under the Order lock (committed before any call): the claim of ``request_id`` and what to do with it
    (``None``: nothing — an unknown or ended claim)."""
    with transaction.atomic():
        order = services.lock_order(order_id)
        claim = delivery_selectors.claim_by_request(request_id)
        if claim is None or claim.order_id != order_id or not claim.is_open:
            return None
        shipment = Shipment.objects.get(pk=claim.shipment_id)
        row = delivery_selectors.settings_row()
        stop = ""
        if order.status not in OPEN_ORDER_STATUSES:
            stop = CODE_ORDER_CLOSED
        elif shipment.cancel_requested_at is not None:
            stop = CODE_CANCEL_REQUESTED
        request: ClaimRequest | None = None
        refusal = ""
        if not claim.claim_id and not stop:
            built = claim_request(order, claim, shipment, row)
            if isinstance(built, str):
                refusal = built
            else:
                request = built
        return _Plan(
            order_id=order_id,
            claim=claim,
            previous=delivery_selectors.previous_claim(claim),
            scenario=services.fake_scenario(order, "yandex"),
            paid_fee=order.delivery_fee,
            max_auto_increase=row.max_auto_increase,
            max_loss_per_shipment=row.max_loss_per_shipment,
            stop=stop,
            request=request,
            refusal=refusal,
        )


def _apply(plan: _Plan, snapshot: Claim) -> services.ClaimApplied:
    return services.apply_yandex_claim(plan.order_id, snapshot, source="dispatch")


def _read(port: YandexDeliveryPort, claim_id: str) -> Claim:
    try:
        return port.claim(claim_id)
    except YandexUnavailable as exc:
        _not_processed(exc, "claims/info")


def _close(plan: _Plan, code: str, *, issue: str | None, stage: str | None = None, **details: Any) -> None:
    """End the dispatch of ``plan.claim`` without a courier: the claim is closed (``code``), the shipment
    shows ``stage`` and staff get the ``issue`` (``dispatch_failed`` / ``dispatch_review`` /
    ``dispatch_unknown``) with ``details`` (ids and amounts)."""
    now = timeutils.now()
    with transaction.atomic():
        order = services.lock_order(plan.order_id)
        delivery_services.close_claim(plan.claim.id, at=now, error_code=code)
        if stage is not None:
            delivery_services.set_dispatch_stage(
                plan.order_id, plan.claim.id, stage=services.parked_stage(order, stage), at=now
            )
        if issue is not None:
            services.open_delivery_issue(
                plan.order_id, issue, code, details={"claim_number": plan.claim.number, **details}
            )
    logger.warning(
        "yandex.dispatch_stopped %s", {"order_id": plan.order_id, "claim": plan.claim.number, "code": code}
    )


def _fail(port: YandexDeliveryPort, plan: _Plan, code: str, *, claim_id: str = "") -> None:
    """A dispatch that cannot summon a courier: cancel the (never accepted) claim at Yandex if it exists,
    close it, stage ``failed``, issue ``dispatch_failed`` (``code``)."""
    _cancel_unaccepted(port, plan.order_id, claim_id or plan.claim.claim_id)
    _close(plan, code, issue=_I.DISPATCH_FAILED, stage=_G.FAILED)


def _settle_previous(port: YandexDeliveryPort, plan: _Plan) -> bool:
    """Step 1: a new claim only when the previous one is provably finished (decision 7). Unless Yandex
    already reported it finished, a fresh ``claims/info``: finished (or the parcel back at the warehouse:
    ``returned``) → go on; never accepted → best-effort cancel, go on; alive (a courier was searched for) →
    refuse: the new claim is closed, the previous one is followed again, urgent issue ``dispatch_failed``
    (``previous_claim_active``)."""
    previous = plan.previous
    if previous is None or not previous.claim_id:
        return True
    if previous.ended_at is not None and delivery_yandex.is_finished(previous.status):
        return True
    try:
        snapshot = _read(port, previous.claim_id)
    except YandexRejected:
        return True  # Yandex does not know it: nothing can come for it
    _apply(plan, snapshot)
    status = snapshot.status
    if delivery_yandex.is_finished(status) or status in delivery_yandex.RETURNED_STATUSES:
        return True
    if status in delivery_yandex.PRE_ACCEPT:
        _cancel_if_unaccepted(port, plan.order_id, snapshot)
        return True
    now = timeutils.now()
    with transaction.atomic():
        services.lock_order(plan.order_id)
        delivery_services.restore_previous_claim(plan.order_id, plan.claim.id, previous.id, at=now)
        services.open_delivery_issue(
            plan.order_id,
            _I.DISPATCH_FAILED,
            CODE_PREVIOUS_ACTIVE,
            urgent=True,
            details={"claim_number": plan.claim.number, "previous_claim_number": previous.number},
        )
    logger.error("yandex.previous_claim_active %s", {"order_id": plan.order_id, "claim": previous.number})
    return False


def _create(port: YandexDeliveryPort, plan: _Plan) -> Claim | None:
    """Step 2: ``claims/create`` with the claim's ``request_id``; the claim id is committed at once. ``None``
    when the dispatch ended here (a definitive refusal, or a claim that is not ours)."""
    assert plan.request is not None  # noqa: S101
    try:
        snapshot = port.create_claim(plan.request)
    except YandexRejected as exc:
        _close(plan, exc.code[:64] or "rejected", issue=_I.DISPATCH_FAILED, stage=_G.FAILED)
        return None
    except YandexUnavailable as exc:
        _not_processed(exc, "claims/create")
    if snapshot.external_order_id != str(plan.order_id):
        # a replayed request_id must return our own claim: never follow (or accept) anything else
        _close(plan, CODE_FOREIGN_CLAIM, issue=_I.DISPATCH_UNKNOWN, stage=_G.FAILED)
        logger.error("yandex.foreign_claim %s", {"order_id": plan.order_id, "claim": plan.claim.number})
        return None
    with transaction.atomic():
        services.lock_order(plan.order_id)
        delivery_services.attach_claim(plan.claim.id, snapshot.claim_id)
    logger.info("yandex.claim_created %s", {"order_id": plan.order_id, "claim": plan.claim.number})
    _apply(plan, snapshot)
    return snapshot


def _await_estimate(port: YandexDeliveryPort, plan: _Plan, snapshot: Claim) -> Claim | None:
    """Step 3: a few short reads while Yandex estimates, then defer; ``None`` when the claim did not reach
    ``ready_for_approval`` (``estimating_failed`` was recorded by the writer; 5 minutes → give up)."""
    for _attempt in range(ESTIMATE_READS):
        if snapshot.status not in ESTIMATING:
            break
        _sleep(ESTIMATE_PAUSE_SECONDS)
        snapshot = _read(port, snapshot.claim_id)
        _apply(plan, snapshot)
    if snapshot.status in ESTIMATING:
        if timeutils.now() - plan.claim.created_at > ESTIMATE_GIVE_UP:
            _fail(port, plan, CODE_ESTIMATING_TIMEOUT, claim_id=snapshot.claim_id)
            return None
        raise outbox.RetryLater(RETRY_DELAY, "yandex: estimating")
    if snapshot.status == "ready_for_approval":
        return snapshot
    return None  # estimating_failed, accepted elsewhere, …: the writer recorded it


def _review(plan: _Plan, snapshot: Claim, code: str, decision: pricing.Decision | None) -> None:
    """Step 4: nothing is accepted; the claim is closed and staff review the price (approve → a new
    dispatch with ``approved_price``, another method, or cancel)."""
    paid = plan.paid_fee
    details: dict[str, Any] = {
        "offer_price": snapshot.offer_price,
        "estimate_price": plan.claim.estimate_price,
        "paid_fee": paid,
        "approved_price": plan.claim.approved_price,
        "increase": decision.increase if decision is not None else None,
        "loss": decision.loss if decision is not None else None,
        "warnings": list(snapshot.warnings)[:10],
    }
    now = timeutils.now()
    with transaction.atomic():
        order = services.lock_order(plan.order_id)
        delivery_services.close_claim(plan.claim.id, at=now, error_code=code)
        delivery_services.set_dispatch_stage(
            plan.order_id,
            plan.claim.id,
            stage=services.parked_stage(order, _G.AWAITING_DISPATCH),
            at=now,
            event_kind=ShipmentEvent.Kind.PRICE_REVIEW,
            payload={
                "claim_id": snapshot.claim_id,
                "claim_number": plan.claim.number,
                "code": code,
                **details,
            },
        )
        services.open_delivery_issue(
            plan.order_id, _I.DISPATCH_REVIEW, code, details={"claim_number": plan.claim.number, **details}
        )
    logger.warning(
        "yandex.dispatch_review %s",
        {"order_id": plan.order_id, "claim": plan.claim.number, "code": code, "offer": snapshot.offer_price},
    )


def _stamp_accept(plan: _Plan, snapshot: Claim) -> str:
    """Step 5's re-check under the Order lock, then ``accept_sent_at`` (committed before the accept goes
    out). ``""`` = send the accept; else why not (the claim is left for the caller to close)."""
    now = timeutils.now()
    with transaction.atomic():
        order = services.lock_order(plan.order_id)
        claim = delivery_selectors.claim_row(plan.claim.id)
        if claim is None or not claim.is_open or claim.accept_sent_at is not None:
            return "stale"
        shipment = Shipment.objects.get(pk=claim.shipment_id)
        if order.status not in OPEN_ORDER_STATUSES:
            return CODE_ORDER_CLOSED
        if shipment.cancel_requested_at is not None:
            return CODE_CANCEL_REQUESTED
        refusal = delivery_selectors.warehouse_calendar(timeutils.local_date(now)).dispatch_refusal(now)
        if refusal is not None:
            return refusal
        if snapshot.offer_valid_until is not None and snapshot.offer_valid_until <= now:
            return CODE_OFFER_EXPIRED
        delivery_services.stamp_accept_sent(claim.id, at=now)
        return ""


def _decide_and_accept(port: YandexDeliveryPort, plan: _Plan, snapshot: Claim) -> None:
    """Steps 4–5: warnings or the pricing policy → review; else ``claims/accept`` exactly once."""
    if snapshot.warnings:
        _review(plan, snapshot, CODE_WARNINGS, None)
        return
    if snapshot.offer_price is None:
        _review(plan, snapshot, CODE_OFFER_MISSING, None)
        return
    quoted = plan.claim.estimate_price if plan.claim.estimate_price is not None else plan.paid_fee
    decision = pricing.dispatch_decision(
        quoted_carrier_price=quoted,
        dispatch_price=snapshot.offer_price,
        paid_fee=plan.paid_fee,
        max_auto_increase=plan.max_auto_increase,
        max_loss_per_shipment=plan.max_loss_per_shipment,
        approved_price=plan.claim.approved_price,
    )
    if decision.action == "review":
        _review(plan, snapshot, decision.reason, decision)
        return
    refused = _stamp_accept(plan, snapshot)
    if refused == "stale":
        return
    if refused in (CODE_ORDER_CLOSED, CODE_CANCEL_REQUESTED):
        _cancel_unaccepted(port, plan.order_id, snapshot.claim_id)
        _close(plan, refused, issue=None)
        return
    if refused:
        _fail(port, plan, refused, claim_id=snapshot.claim_id)
        return
    logger.info("yandex.accept_sent %s", {"order_id": plan.order_id, "claim": plan.claim.number})
    try:
        port.accept(snapshot.claim_id, snapshot.version)
    except YandexConflict as exc:
        if exc.code == CODE_OFFER_EXPIRED:  # Yandex refused: nothing was accepted
            _close(plan, CODE_OFFER_EXPIRED, issue=_I.DISPATCH_FAILED, stage=_G.FAILED)
            return
        logger.warning("yandex.accept_conflict %s", {"order_id": plan.order_id, "code": exc.code[:64]})
    except YandexRejected as exc:  # 404: Yandex does not know the claim; nothing can come for it
        _close(plan, exc.code[:64] or CODE_NOT_FOUND, issue=_I.DISPATCH_FAILED, stage=_G.FAILED)
        return
    except YandexUnavailable as exc:  # the outcome is unknown: never resent, the claim is re-read
        logger.warning(
            "yandex.accept_unanswered %s", {"order_id": plan.order_id, "unknown": exc.outcome_unknown}
        )
    _confirm(port, plan, snapshot.claim_id)


def _confirm(port: YandexDeliveryPort, plan: _Plan, claim_id: str) -> None:
    """After the accept was sent: re-read until the status moves past ``ready_for_approval`` (the writer
    records the acceptance, the ``shipment_created`` event and the stage), then the tracking link. An
    expired offer (``failed``) is recorded by the writer as ``dispatch_failed`` ``offer_expired``. Still not
    moved after :data:`ACCEPT_CONFIRM_WINDOW` → ``dispatch_unknown`` (the claim stays open and polled)."""
    try:
        snapshot = _read(port, claim_id)
    except YandexRejected:
        _close(plan, CODE_NOT_FOUND, issue=_I.DISPATCH_UNKNOWN)
        return
    _apply(plan, snapshot)
    if snapshot.status in delivery_yandex.PRE_ACCEPT:
        sent = plan.claim.accept_sent_at or timeutils.now()
        if timeutils.now() - sent > ACCEPT_CONFIRM_WINDOW:
            with transaction.atomic():
                services.lock_order(plan.order_id)
                services.open_delivery_issue(
                    plan.order_id,
                    _I.DISPATCH_UNKNOWN,
                    CODE_ACCEPT_UNCONFIRMED,
                    urgent=True,
                    details={"claim_number": plan.claim.number},
                )
            return
        raise outbox.RetryLater(RETRY_DELAY, "yandex: waiting for the acceptance to show")
    if snapshot.status in delivery_yandex.ACCEPT_IMPLIED:
        logger.info("yandex.claim_accepted %s", {"order_id": plan.order_id, "claim": plan.claim.number})
        store_tracking_link(port, plan.order_id, plan.claim.id, claim_id)


def _dispatch(order_id: int, request_id: str) -> None:
    plan = _plan(order_id, request_id)
    if plan is None:
        return
    port = get_yandex_delivery(plan.scenario)
    claim = plan.claim
    if claim.accept_sent_at is not None:
        _confirm(port, plan, claim.claim_id)
        return
    if plan.stop:
        _cancel_unaccepted(port, order_id, claim.claim_id)
        _close(plan, plan.stop, issue=None)
        return
    if not claim.claim_id:
        if plan.refusal:
            _close(plan, plan.refusal, issue=_I.DISPATCH_FAILED, stage=_G.FAILED)
            return
        if not _settle_previous(port, plan):
            return
        created = _create(port, plan)
        if created is None:
            return
        snapshot: Claim | None = created
    else:
        try:
            snapshot = _read(port, claim.claim_id)
        except YandexRejected:
            _close(plan, CODE_NOT_FOUND, issue=_I.DISPATCH_FAILED, stage=_G.FAILED)
            return
        _apply(plan, snapshot)
    assert snapshot is not None  # noqa: S101
    ready = _await_estimate(port, plan, snapshot)
    if ready is None:
        return
    _decide_and_accept(port, plan, ready)


def dispatch(payload: dict[str, Any]) -> None:
    """The ``yandex.dispatch`` effect (``commerce.tasks.dispatch_courier``): steps 1–6 of plan §3.6 under
    the advisory lock ``yandex:{order_id}``; a busy lock defers the message."""
    order_id = int(payload["order_id"])
    request_id = str(payload["request_id"])
    try:
        with locks.yandex_lock(order_id):
            _dispatch(order_id, request_id)
    except locks.YandexBusy as exc:
        raise outbox.RetryLater(BUSY_DELAY, "yandex: the order is busy") from exc


def on_dispatch_dead(payload: dict[str, Any]) -> None:
    """The dispatch died (ten transport failures, or deferred too long): issue ``dispatch_unknown``
    (urgent once the accept was sent: a courier may come). The claim stays open and polled; requeueing the
    message resumes it."""
    order_id = int(payload["order_id"])
    claim = delivery_selectors.claim_by_request(str(payload["request_id"]))
    if claim is None or not claim.is_open:
        return
    with transaction.atomic():
        services.lock_order(order_id)
        services.open_delivery_issue(
            order_id,
            _I.DISPATCH_UNKNOWN,
            CODE_DEAD_LETTER,
            urgent=claim.accept_sent_at is not None,
            details={"claim_number": claim.number},
        )


# --- status sync (plan §3.7) --------------------------------------------------------------------------------


def _parse_ts(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    text = value.strip().replace(" ", "+").replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def sync_claim(
    payload: dict[str, Any], *, received_at: datetime | None = None
) -> services.ClaimApplied | None:
    """The ``yandex.sync_claim`` effect (a callback's hint): a fresh ``claims/info`` of that claim of that
    order → ``apply_yandex_claim`` (``source="callback"``). A ``claims/info`` that still shows an older
    ``updated_ts`` than the callback named is re-read a little later (docs: it may lag 5–30 s); the 60 s poll
    catches up anything else."""
    order_id = int(payload["order_id"])
    claim_id = str(payload["claim_id"])
    claim = delivery_selectors.claim_by_claim_id(order_id, claim_id)
    if claim is None:
        return None
    port = get_yandex_delivery(scenario_of(order_id))
    try:
        snapshot = port.claim(claim_id)
    except YandexRejected as exc:
        logger.warning("yandex.sync_rejected %s", {"order_id": order_id, "code": exc.code[:64]})
        return None
    except YandexUnavailable as exc:
        _not_processed(exc, "claims/info")
    hinted = _parse_ts(payload.get("updated_ts"))
    if hinted is not None and snapshot.updated_ts is not None and snapshot.updated_ts < hinted:
        if received_at is None or timeutils.now() - received_at < SYNC_STALE_WINDOW:
            raise outbox.RetryLater(RETRY_DELAY, "yandex: claims/info is behind the callback")
    result = services.apply_yandex_claim(order_id, snapshot, source="callback")
    if result.needs_tracking_link and result.claim_pk is not None:
        store_tracking_link(port, order_id, result.claim_pk, claim_id)
    return result


@dataclass(frozen=True, slots=True)
class PollResult:
    applied: list[int]  # order ids whose claim snapshot was applied
    polled: int = 0
    failed: bool = False
    apply_failed: int = 0
    settled_unknown: int = 0


def _settle_unknown_costs(now: datetime) -> int:
    """Finished, accepted claims without a ``final_price`` 24 h after their end → ``cost_unknown``."""
    count = 0
    for claim_pk, order_id in delivery_selectors.claims_without_final_price(now - COST_UNKNOWN_AFTER):
        with transaction.atomic():
            order = services.lock_order(order_id)
            if delivery_services.settle_cost_unknown(
                claim_pk, paid_fee=order.delivery_fee, at=timeutils.now()
            ):
                count += 1
    return count


def run_poll() -> PollResult:
    """One ``commerce.poll_yandex`` run (beat every 60 s, the reliable path of decision 10): single flight
    (``cache.add`` lock), ``claims/bulk_info`` over every claim with a Yandex id and no settled cost, each
    snapshot through ``apply_yandex_claim`` (``source="poll"``; one failing claim never stops the others),
    then the 24 h ``cost_unknown`` settlement. Health: a failed ``bulk_info`` →
    ``delivery.services.record_yandex_failure("poll", code)`` (``IntegrationState(yandex)`` failure run; the
    third in a row alerts ``delivery.provider_failing``), an answered one → ``record_sync_success("yandex")``;
    no candidate → nothing written. A no-op while the
    fake runs under the production settings."""
    empty = PollResult(applied=[])
    if integration_modes.fake_in_production("yandex"):
        return empty
    if not cache.add(POLL_LOCK_KEY, 1, timeout=POLL_LOCK_SECONDS):
        return empty
    try:
        return _poll()
    finally:
        cache.delete(POLL_LOCK_KEY)


def _poll() -> PollResult:
    now = timeutils.now()
    rows = delivery_selectors.unsettled_claims()
    applied: list[int] = []
    apply_failed = 0
    if rows:
        orders = {claim_id: order_id for _pk, order_id, claim_id in rows}
        port = get_yandex_delivery()
        try:
            snapshots = port.claims(list(orders))
        except YandexError as exc:
            # IntegrationState(yandex) counts the run (attention.yandex_failing); the third failure in a row
            # alerts delivery.provider_failing (plan §3.9)
            delivery_services.record_yandex_failure("poll", delivery_services.yandex_failure_code(exc))
            logger.warning("yandex.poll_failed %s", {"claims": len(orders), "error": type(exc).__name__})
            return PollResult(applied=[], polled=len(orders), failed=True)
        for snapshot in snapshots:
            order_id = orders.get(snapshot.claim_id)
            if order_id is None:
                continue
            try:
                result = services.apply_yandex_claim(order_id, snapshot, source="poll")
            except Exception:  # noqa: BLE001 - one poison claim must not stop the poll
                apply_failed += 1
                logger.exception("yandex.poll_apply_failed %s", {"order_id": order_id})
                continue
            if result.applied:
                applied.append(order_id)
            if result.needs_tracking_link and result.claim_pk is not None:
                store_tracking_link(port, order_id, result.claim_pk, snapshot.claim_id)
        integration_services.record_sync_success("yandex")
    settled = _settle_unknown_costs(now)
    return PollResult(applied=applied, polled=len(rows), apply_failed=apply_failed, settled_unknown=settled)


__all__ = [
    "ACCEPT_CONFIRM_WINDOW",
    "COST_UNKNOWN_AFTER",
    "DISPATCH_DEFERRAL_WINDOW",
    "ESTIMATE_GIVE_UP",
    "POLL_LOCK_KEY",
    "PollResult",
    "claim_request",
    "dispatch",
    "on_dispatch_dead",
    "run_poll",
    "scenario_of",
    "store_tracking_link",
    "sync_claim",
]
