"""The delivery exception queue's beat (Yandex Delivery plan §3.9): ``commerce.review_delivery_issues``
every 5 minutes. No Yandex call: it reads what the dispatch, the status writer and the cancel recorded.

Self-closing rules, each re-checked under the order's lock before it writes (a busy order is skipped: the
next run takes it):

* ``preparation_overdue`` — a paid Yandex order still ``awaiting_dispatch`` once ``prep_deadline_minutes``
  **working** minutes (the warehouse calendar's ``add_working_minutes``) passed since ``paid_at``; code
  ``suspended`` / ``closed`` (also past the day's dispatch cutoff) / ``not_configured`` /
  ``returns_unavailable`` when that is why no courier can be called now, else ``overdue``. Resolved once the
  order is dispatched or closed.
* ``dispatch_failed`` ``courier_search_slow`` (urgent) — Yandex accepted the claim more than 30 minutes ago
  and still has no courier; resolved once a courier is found or the claim ends (another code that a status
  wrote in the meantime is left alone).
* ``return_pending`` ``warehouse_closed`` (urgent) — a parcel is on its way back while the warehouse is closed
  or does not accept returns; back to ``returning`` (not urgent) once it opens, ``returned`` / resolved once
  the parcel is back / confirmed.
* ``cancel_incomplete`` (urgent) — staff asked for a carrier cancel more than 10 minutes ago, Yandex shows the
  claim cancelled or the claim was withdrawn for the cancel (error code ``cancel_requested``: by the cancel,
  or by a dispatch run that found the stamp) (``carrier_cancelled``), or the parcel is returning
  (``returning``), and the order is still open: a cancel that stopped between its two phases; resolved by
  repeating the cancel (no second carrier call), by a delivered order or by a confirmed return.
* reminders — every open urgent issue untouched for 30 minutes (``delivery.services.remind_issues``).

Alerts (ids and codes only) come from ``delivery.services``: an issue that becomes urgent queues
``delivery.issue_opened``, a reminder ``delivery.issue_reminder`` — both outbox messages of the same
transaction.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from django.db import transaction

from glowderma.commerce import services
from glowderma.commerce.models import Order
from glowderma.core import enums, timeutils
from glowderma.delivery import selectors as delivery_selectors
from glowderma.delivery import services as delivery_services
from glowderma.delivery import warehouse
from glowderma.delivery import yandex as delivery_yandex

logger = logging.getLogger(__name__)

_I = enums.DeliveryIssueKind
_G = enums.ShipmentStage
_S = enums.OrderStatus

COURIER_SEARCH_LIMIT = timedelta(minutes=30)
CANCEL_INCOMPLETE_AFTER = timedelta(minutes=10)
CODE_OVERDUE = "overdue"
CODE_COURIER_SEARCH_SLOW = "courier_search_slow"
CODE_WAREHOUSE_CLOSED = "warehouse_closed"
CODE_RETURNING = "returning"
CODE_RETURNED = "returned"
CODE_CARRIER_CANCELLED = "carrier_cancelled"
# Why no courier can be called now → the preparation_overdue code (a day past its cutoff counts as closed).
PREPARATION_CODES: dict[str, str] = {
    warehouse.REFUSAL_NOT_CONFIGURED: "not_configured",
    warehouse.REFUSAL_SUSPENDED: "suspended",
    warehouse.REFUSAL_CLOSED: "closed",
    warehouse.REFUSAL_AFTER_CUTOFF: "closed",
    warehouse.REFUSAL_RETURNS_UNAVAILABLE: "returns_unavailable",
}
NOTE_DISPATCHED = "Buyurtma jo‘natildi yoki yopildi."
NOTE_COURIER_FOUND = "Kuryer topildi yoki kuryer buyurtmasi yakunlandi."
NOTE_RETURN_CLOSED = "Posilka qabul qilindi yoki qaytish yakunlandi."
NOTE_CANCEL_DONE = "Buyurtma bekor qilindi yoki holati o‘zgardi."


@dataclass(slots=True)
class ReviewResult:
    """One run: ``(order id, kind)`` of every issue opened / changed and resolved, the reminded issue ids."""

    opened: list[tuple[int, str]] = field(default_factory=list)
    resolved: list[tuple[int, str]] = field(default_factory=list)
    reminded: list[int] = field(default_factory=list)


def _locked(order_id: int, change: Callable[[Order], bool]) -> bool:
    """Run ``change`` under the order's row lock (skipped when another transaction holds it)."""
    try:
        with transaction.atomic():
            order = services.lock_order(order_id, skip_locked=True)
            return change(order)
    except services.OrderNotFound:  # busy (skip_locked) or gone: the next run looks again
        return False


def _open_map(kind: str) -> dict[int, delivery_selectors.IssueInfo]:
    return {issue.order_id: issue for issue in delivery_selectors.open_issues(kind=kind)}


def _stage(order_id: int) -> str:
    info = delivery_selectors.shipment_info(order_id)
    return info.stage if info is not None and info.provider == enums.DeliveryProvider.YANDEX else ""


def _open(
    result: ReviewResult, order_id: int, kind: str, code: str, *, urgent: bool, **details: object
) -> None:
    services.open_delivery_issue(order_id, kind, code, urgent=urgent, details=details)
    result.opened.append((order_id, kind))


def _resolve(result: ReviewResult, order_id: int, kind: str, note: str, code: str | None = None) -> None:
    if _locked(order_id, lambda _order: delivery_services.resolve_kind(order_id, kind, note, code=code)):
        result.resolved.append((order_id, kind))


# --- preparation deadline -----------------------------------------------------------------------------------


def _preparation(now: datetime, result: ReviewResult) -> None:
    rows = (
        Order.objects.filter(
            status=_S.PROCESSING,
            fulfilment=enums.FulfilmentMethod.DELIVERY,
            paid_at__isnull=False,
            shipment__provider=enums.DeliveryProvider.YANDEX,
            shipment__stage=_G.AWAITING_DISPATCH,
        )
        .order_by("pk")
        .values_list("pk", "paid_at")
    )
    candidates = [(order_id, paid_at) for order_id, paid_at in rows if paid_at is not None]
    open_rows = _open_map(_I.PREPARATION_OVERDUE)
    if candidates:
        row = delivery_selectors.settings_row()
        today = timeutils.local_date(now)
        oldest = min(timeutils.local_date(paid_at) for _pk, paid_at in candidates)
        current = delivery_selectors.warehouse_calendar(today)
        history = current if oldest >= today else delivery_selectors.warehouse_calendar(oldest)
        refusal = current.dispatch_refusal(now)
        code = PREPARATION_CODES.get(refusal, CODE_OVERDUE) if refusal is not None else CODE_OVERDUE
        for order_id, paid_at in candidates:
            existing = open_rows.pop(order_id, None)
            deadline = history.add_working_minutes(paid_at, row.prep_deadline_minutes)
            if deadline is not None and deadline > now:
                if existing is not None:  # the deadline moved (settings changed): not overdue any more
                    _resolve(result, order_id, _I.PREPARATION_OVERDUE, NOTE_DISPATCHED)
                continue
            if existing is not None and existing.code == code:
                continue

            def overdue(order: Order, order_id: int = order_id, deadline: datetime | None = deadline) -> bool:
                if order.status != _S.PROCESSING or _stage(order_id) != _G.AWAITING_DISPATCH:
                    return False
                minutes = int((now - deadline).total_seconds() // 60) if deadline is not None else None
                _open(result, order_id, _I.PREPARATION_OVERDUE, code, urgent=False, overdue_minutes=minutes)
                return True

            _locked(order_id, overdue)
    for order_id in open_rows:  # dispatched, cancelled or no longer awaiting dispatch
        _resolve(result, order_id, _I.PREPARATION_OVERDUE, NOTE_DISPATCHED)


# --- courier search -----------------------------------------------------------------------------------------


def _courier_search(now: datetime, result: ReviewResult) -> None:
    slow = dict(delivery_selectors.slow_courier_searches(now - COURIER_SEARCH_LIMIT))
    open_rows = _open_map(_I.DISPATCH_FAILED)
    processing = set(
        Order.objects.filter(pk__in=list(slow), status=_S.PROCESSING).values_list("pk", flat=True)
    )
    for order_id, claim_number in slow.items():
        existing = open_rows.get(order_id)
        if order_id not in processing or existing is not None:
            continue  # an open dispatch_failed (this one or another case) is not overwritten

        def flag(order: Order, order_id: int = order_id, claim_number: int = claim_number) -> bool:
            if order.status != _S.PROCESSING:
                return False
            if delivery_selectors.has_open_issue(order_id, _I.DISPATCH_FAILED):
                return False
            if (order_id, claim_number) not in delivery_selectors.slow_courier_searches(
                now - COURIER_SEARCH_LIMIT
            ):
                return False
            _open(
                result,
                order_id,
                _I.DISPATCH_FAILED,
                CODE_COURIER_SEARCH_SLOW,
                urgent=True,
                claim_number=claim_number,
            )
            return True

        _locked(order_id, flag)
    for order_id, issue in open_rows.items():
        if issue.code == CODE_COURIER_SEARCH_SLOW and order_id not in processing:
            _resolve(result, order_id, _I.DISPATCH_FAILED, NOTE_COURIER_FOUND, code=CODE_COURIER_SEARCH_SLOW)


# --- returns while the warehouse is closed ------------------------------------------------------------------


def _returns(now: datetime, result: ReviewResult) -> None:
    returning = set(delivery_selectors.yandex_orders_at({_G.RETURNING}))
    open_rows = _open_map(_I.RETURN_PENDING)
    calendar = delivery_selectors.warehouse_calendar(timeutils.local_date(now))
    blocked = not calendar.is_open(now) or not calendar.returns_accepted
    for order_id in sorted(returning):
        existing = open_rows.get(order_id)
        if blocked:
            if existing is not None and existing.code == CODE_WAREHOUSE_CLOSED and existing.urgent:
                continue

            def closed(order: Order, order_id: int = order_id) -> bool:
                if order.status == _S.DELIVERED or _stage(order_id) != _G.RETURNING:
                    return False
                _open(result, order_id, _I.RETURN_PENDING, CODE_WAREHOUSE_CLOSED, urgent=True)
                return True

            _locked(order_id, closed)
        elif existing is not None and existing.code == CODE_WAREHOUSE_CLOSED:
            _locked(order_id, _calming(result, order_id, CODE_RETURNING))
    for order_id, issue in open_rows.items():
        if issue.code != CODE_WAREHOUSE_CLOSED or order_id in returning:
            continue
        if _stage(order_id) == _G.RETURNED:  # back at the warehouse: staff confirm it
            _locked(order_id, _calming(result, order_id, CODE_RETURNED))
        else:
            _resolve(result, order_id, _I.RETURN_PENDING, NOTE_RETURN_CLOSED, code=CODE_WAREHOUSE_CLOSED)


def _calming(result: ReviewResult, order_id: int, code: str) -> Callable[[Order], bool]:
    """The warehouse takes the parcel again: the return stays tracked (``code``), no longer urgent."""

    def calm(_order: Order) -> bool:
        issue = next(iter(delivery_selectors.open_issues(order_id=order_id, kind=_I.RETURN_PENDING)), None)
        if issue is None or issue.code != CODE_WAREHOUSE_CLOSED:
            return False
        _open(result, order_id, _I.RETURN_PENDING, code, urgent=False)
        return True

    return calm


# --- a cancel that stopped between its phases ---------------------------------------------------------------


def _carrier_done(request: delivery_selectors.CancelRequest) -> str | None:
    if request.claim_ended and request.claim_status in delivery_services.OUR_CANCEL_STATUSES:
        return CODE_CARRIER_CANCELLED
    if request.claim_ended and request.claim_error_code == delivery_services.CANCEL_WITHDRAWN:
        # the claim was withdrawn for this cancel (by the cancel itself, or by a dispatch run that found the
        # stamp) and nothing summons a courier any more, yet the order is still open
        return CODE_CARRIER_CANCELLED
    if request.stage in delivery_yandex.RETURN_STAGES:
        return CODE_RETURNING
    return None


def _cancel_incomplete(now: datetime, result: ReviewResult) -> None:
    requests = {
        request.order_id: request
        for request in delivery_selectors.cancel_requests(now - CANCEL_INCOMPLETE_AFTER)
    }
    statuses = dict(Order.objects.filter(pk__in=list(requests)).values_list("pk", "status"))
    open_rows = _open_map(_I.CANCEL_INCOMPLETE)
    still: set[int] = set()
    for order_id, request in requests.items():
        code = _carrier_done(request)
        if code is None or statuses.get(order_id) not in (_S.PROCESSING, _S.HANDED_TO_COURIER):
            continue
        still.add(order_id)
        existing = open_rows.get(order_id)
        if existing is not None and existing.code == code and existing.urgent:
            continue

        def flag(order: Order, order_id: int = order_id, code: str = code) -> bool:
            if order.status not in (_S.PROCESSING, _S.HANDED_TO_COURIER):
                return False
            _open(
                result,
                order_id,
                _I.CANCEL_INCOMPLETE,
                code,
                urgent=True,
                claim_number=requests[order_id].claim_number,
            )
            return True

        _locked(order_id, flag)
    for order_id in open_rows:
        if order_id not in still:
            _resolve(result, order_id, _I.CANCEL_INCOMPLETE, NOTE_CANCEL_DONE)


def review_delivery_issues(now: datetime | None = None) -> ReviewResult:
    """One run of the beat (module doc); returns what it changed."""
    instant = now or timeutils.now()
    result = ReviewResult()
    _preparation(instant, result)
    _courier_search(instant, result)
    _returns(instant, result)
    _cancel_incomplete(instant, result)
    result.reminded.extend(delivery_services.remind_issues(at=instant))
    if result.opened or result.resolved or result.reminded:
        logger.info(
            "delivery.review %s",
            {"opened": result.opened, "resolved": result.resolved, "reminded": result.reminded},
        )
    return result


__all__ = [
    "CANCEL_INCOMPLETE_AFTER",
    "CODE_CARRIER_CANCELLED",
    "CODE_COURIER_SEARCH_SLOW",
    "CODE_OVERDUE",
    "CODE_WAREHOUSE_CLOSED",
    "COURIER_SEARCH_LIMIT",
    "ReviewResult",
    "review_delivery_issues",
]
