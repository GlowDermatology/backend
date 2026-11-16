"""The delivery exception queue's beat and alerts (plan §3.9): ``commerce.review_delivery_issues``
(preparation deadline in working minutes, slow courier search, returns while the warehouse is closed,
half-done cancels, reminders), the single alert point (``delivery.services.open_issue``: ids and codes
only, inside the transaction) and the Yandex account health (``attention.yandex_failing`` +
``delivery.provider_failing``) from the price check and the poll. Yandex is the fake adapter."""

# ruff: noqa: F811 - the fixtures come from the support modules and are requested by name

from __future__ import annotations

import uuid
from datetime import datetime, time, timedelta
from typing import Any
from unittest import mock

import pytest
from django.core.cache import cache
from django.db import transaction
from django.test import override_settings
from rest_framework.test import APIClient

from glowderma.accounts.models import Customer, StaffUser
from glowderma.commerce import delivery_admin, delivery_review, services, tasks, yandex_flow
from glowderma.commerce.models import Order
from glowderma.commerce.tests.support import product
from glowderma.commerce.tests.yandex_checkout_support import calculate, configure_warehouse, fill, option
from glowderma.commerce.tests.yandex_checkout_support import pinned_address as saved_address
from glowderma.commerce.tests.yandex_dispatch_support import (  # noqa: F401 - fixtures
    dispatch,
    fresh_fake_yandex,
    no_pauses,
    open_warehouse,
    run,
    shipment_of,
    yandex_order,
)
from glowderma.core import alerts, enums, timeutils
from glowderma.core.models import OutboxMessage
from glowderma.core.seeding import pinned_clock
from glowderma.core.timeutils import TASHKENT
from glowderma.delivery import services as delivery_services
from glowderma.delivery.models import (
    CourierClaim,
    DeliveryIssue,
    DeliverySettings,
    WarehouseClosure,
    WarehouseHours,
)
from glowderma.integrations import selectors as integration_selectors
from glowderma.integrations.yandex_delivery import fake as fake_yandex
from glowderma.integrations.yandex_delivery.ports import YandexRejected, YandexUnavailable
from glowderma.integrations.yandex_delivery.registry import get_yandex_delivery

pytestmark = pytest.mark.django_db

K = enums.DeliveryIssueKind
G = enums.ShipmentStage
ALERTS = {"ALERT_WEBHOOK_URL": "https://alerts.example.invalid/hook"}
ISSUE_ALERT_KEYS = {"order_id", "kind", "code", "urgent", "issue_id"}  # what the Telegram bot renders
# A Tuesday in Tashkent; the warehouse works 09:00–18:00 in the preparation tests.
DAY = datetime(2026, 11, 17, tzinfo=TASHKENT)


@pytest.fixture(autouse=True)
def warehouse() -> DeliverySettings:
    return open_warehouse()


@pytest.fixture(autouse=True)
def no_poll_lock() -> Any:
    cache.delete(yandex_flow.POLL_LOCK_KEY)
    yield
    cache.delete(yandex_flow.POLL_LOCK_KEY)


def _at(day: int, hour: int, minute: int = 0) -> datetime:
    return DAY + timedelta(days=day, hours=hour, minutes=minute)


def _review(instant: datetime) -> delivery_review.ReviewResult:
    with pinned_clock(instant):
        return delivery_review.review_delivery_issues()


def _open(order: Order, kind: str) -> DeliveryIssue | None:
    return DeliveryIssue.objects.filter(order=order, kind=kind, state="open").first()


def _alerts(event: str) -> list[OutboxMessage]:
    return list(OutboxMessage.objects.filter(topic="alert.send", payload__event=event).order_by("id"))


def _office_hours() -> None:
    for weekday in range(7):
        WarehouseHours.objects.update_or_create(
            weekday=weekday, defaults={"opens_at": time(9), "closes_at": time(18)}
        )
    DeliverySettings.objects.filter(pk=1).update(prep_deadline_minutes=120)


def _accepted(staff: StaffUser, **fake: Any) -> tuple[Order, CourierClaim]:
    order = yandex_order(fake=fake or None)
    claim = dispatch(order, staff.pk)
    assert run(claim) is None
    claim.refresh_from_db()
    assert claim.accepted_at is not None
    return order, claim


def _move(order: Order, claim: CourierClaim, *statuses: str) -> None:
    for status in statuses:
        fake_yandex.advance(claim.claim_id, status)
        services.apply_yandex_claim(order.pk, get_yandex_delivery().claim(claim.claim_id), source="poll")


# --- preparation deadline (working minutes) -----------------------------------------------------------------


@override_settings(**ALERTS)
def test_preparation_overdue_counts_working_minutes_and_says_why(staff: StaffUser) -> None:
    _office_hours()
    order = yandex_order()
    Order.objects.filter(pk=order.pk).update(paid_at=_at(0, 17))  # 60 working minutes left today
    assert not _review(_at(0, 20)).opened  # the warehouse is closed: the clock stopped at 18:00
    assert not _review(_at(1, 9, 59)).opened  # 119 working minutes
    _review(_at(1, 10, 1))
    issue = _open(order, K.PREPARATION_OVERDUE)
    assert issue is not None and (issue.code, issue.urgent) == ("overdue", False)
    assert issue.details["order_id"] == order.pk and issue.details["overdue_minutes"] == 1
    assert not _alerts("delivery.issue_opened")  # not urgent
    updated = issue.updated_at
    _review(_at(1, 10, 6))  # nothing changed: the issue is left alone
    issue.refresh_from_db()
    assert issue.updated_at == updated
    DeliverySettings.objects.filter(pk=1).update(dispatch_suspended=True)
    _review(_at(1, 10, 11))
    issue.refresh_from_db()
    assert issue.code == "suspended"
    DeliverySettings.objects.filter(pk=1).update(dispatch_suspended=False)
    _review(_at(1, 19))  # closed now
    issue.refresh_from_db()
    assert issue.code == "closed"
    with pinned_clock(_at(2, 9, 30)):
        dispatch(order, staff.pk)  # the parcel is prepared, a courier called
    issue.refresh_from_db()
    assert issue.state == "resolved"
    assert not _review(_at(2, 9, 35)).opened


def test_preparation_overdue_closes_once_the_order_is_no_longer_waiting(staff: StaffUser) -> None:
    _office_hours()
    order = yandex_order()
    Order.objects.filter(pk=order.pk).update(paid_at=_at(0, 9))
    _review(_at(0, 11, 30))
    issue = _open(order, K.PREPARATION_OVERDUE)
    assert issue is not None
    Order.objects.filter(pk=order.pk).update(status="delivered", delivered_at=_at(0, 11, 40))
    result = _review(_at(0, 11, 45))
    assert (order.pk, K.PREPARATION_OVERDUE) in result.resolved
    issue.refresh_from_db()
    assert issue.state == "resolved"


# --- courier search -----------------------------------------------------------------------------------------


@override_settings(**ALERTS)
def test_a_courier_search_over_30_minutes_is_urgent_and_closes_once_a_courier_is_found(
    staff: StaffUser,
) -> None:
    order, claim = _accepted(staff)
    accepted = claim.accepted_at
    assert accepted is not None
    assert not _review(accepted + timedelta(minutes=29)).opened
    _review(accepted + timedelta(minutes=31))
    issue = _open(order, K.DISPATCH_FAILED)
    assert issue is not None and (issue.code, issue.urgent) == ("courier_search_slow", True)
    (alert,) = _alerts("delivery.issue_opened")
    assert alert.payload["fields"] == {
        "order_id": order.pk,
        "kind": "dispatch_failed",
        "code": "courier_search_slow",
        "urgent": True,
        "issue_id": issue.pk,
    }
    _move(order, claim, "performer_found")
    _review(accepted + timedelta(minutes=36))
    issue.refresh_from_db()
    assert issue.state == "resolved"


def test_a_slow_search_another_status_turned_into_another_case_is_left_open(staff: StaffUser) -> None:
    order, claim = _accepted(staff)
    accepted = claim.accepted_at
    assert accepted is not None
    _review(accepted + timedelta(minutes=31))
    _move(order, claim, "performer_not_found")  # the writer refreshes the case: no courier at all
    _review(accepted + timedelta(minutes=36))
    issue = _open(order, K.DISPATCH_FAILED)
    assert issue is not None and issue.code == "performer_not_found"


# --- returns while the warehouse is closed ------------------------------------------------------------------


@override_settings(**ALERTS)
def test_a_return_arriving_while_the_warehouse_is_closed_is_urgent_until_it_opens(
    staff: StaffUser, staff_client: APIClient
) -> None:
    order, claim = _accepted(staff)
    _move(order, claim, "pickuped", "returning")
    now = timeutils.now()
    WarehouseClosure.objects.create(date=timeutils.local_date(now))
    _review(now)
    issue = _open(order, K.RETURN_PENDING)
    assert issue is not None and (issue.code, issue.urgent) == ("warehouse_closed", True)
    assert [a.payload["fields"]["code"] for a in _alerts("delivery.issue_opened")] == ["warehouse_closed"]
    WarehouseClosure.objects.all().delete()
    _review(now + timedelta(minutes=5))
    issue.refresh_from_db()
    assert (issue.state, issue.code, issue.urgent) == ("open", "returning", False)
    DeliverySettings.objects.filter(pk=1).update(returns_accepted=False)
    _review(now + timedelta(minutes=10))
    issue.refresh_from_db()
    assert (issue.code, issue.urgent) == ("warehouse_closed", True)
    _move(order, claim, "return_arrived", "returned")  # back at the warehouse: staff confirm it
    _review(now + timedelta(minutes=15))
    issue.refresh_from_db()
    assert (issue.state, issue.code, issue.urgent) == ("open", "returned", False)
    response = staff_client.post(
        f"/api/v1/admin/orders/{order.pk}/confirm-return/",
        {},
        format="json",
        HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()),
    )
    assert response.status_code == 200, response.json()
    issue.refresh_from_db()
    assert issue.state == "resolved"


# --- a cancel Yandex confirmed after the staff gave up ------------------------------------------------------


def test_a_return_yandex_confirmed_later_asks_staff_to_finish_the_cancel(
    staff: StaffUser, staff_client: APIClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(services, "_carrier_pause", lambda _seconds: None)
    order, claim = _accepted(staff)
    _move(order, claim, "pickuped")
    monkeypatch.setattr(fake_yandex.FakeYandexDelivery, "request_return", lambda self, *a, **kw: None)
    response = staff_client.post(
        f"/api/v1/admin/orders/{order.pk}/cancel/",
        {"reason": "Mijoz bekor qildi"},
        format="json",
        HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()),
    )
    assert response.status_code == 409 and response.json()["code"] == "return_requested"
    stamped = shipment_of(order).cancel_requested_at
    assert stamped is not None
    _review(stamped + timedelta(minutes=11))
    assert _open(order, K.CANCEL_INCOMPLETE) is None  # Yandex has not confirmed the return
    _move(order, claim, "returning")
    _review(stamped + timedelta(minutes=16))
    issue = _open(order, K.CANCEL_INCOMPLETE)
    assert issue is not None and (issue.code, issue.urgent) == ("returning", True)
    services.cancel_order(order.pk, "Mijoz bekor qildi", staff.pk)
    issue.refresh_from_db()
    assert issue.state == "resolved"


# --- reminders ----------------------------------------------------------------------------------------------


@override_settings(**ALERTS)
def test_an_urgent_issue_nobody_touched_is_reminded_every_30_minutes() -> None:
    order = yandex_order()
    calm = yandex_order()
    start = timeutils.now()
    with pinned_clock(start):
        issue_id, _ = delivery_services.open_issue(
            order.pk, K.PARCEL_UNACCOUNTED, code="failed", urgent=True, details={"order_id": order.pk}
        )
        delivery_services.open_issue(
            calm.pk, K.DISPATCH_FAILED, code="offer_expired", urgent=False, details={"order_id": calm.pk}
        )
    assert not _review(start + timedelta(minutes=29)).reminded
    assert _review(start + timedelta(minutes=31)).reminded == [issue_id]
    assert not _review(start + timedelta(minutes=36)).reminded  # just reminded
    with pinned_clock(start + timedelta(minutes=62)):
        assert tasks.review_delivery_issues()["reminded"] == 1
    issue = DeliveryIssue.objects.get(pk=issue_id)
    assert issue.reminders == 2 and issue.reminded_at == start + timedelta(minutes=62)
    assert issue.updated_at == start  # a reminder is no update
    reminders = _alerts("delivery.issue_reminder")
    assert len(reminders) == 2
    expected = {
        "order_id": order.pk,
        "kind": "parcel_unaccounted",
        "code": "failed",
        "urgent": True,
        "issue_id": issue_id,
    }
    assert all(message.payload["fields"] == expected for message in reminders)
    delivery_services.resolve_kind(order.pk, K.PARCEL_UNACCOUNTED, "Topildi.")
    assert not _review(start + timedelta(minutes=200)).reminded


# --- the single alert point ---------------------------------------------------------------------------------


@override_settings(**ALERTS)
def test_opening_an_issue_alerts_once_it_is_urgent_with_ids_and_codes_only(
    django_capture_on_commit_callbacks: Any,
) -> None:
    order = yandex_order()
    recorded: list[tuple[str, dict[str, Any], str]] = []
    original = alerts.alert

    def recording(event: str, fields: Any = None, *, scope: str = "") -> bool:
        recorded.append((event, dict(fields or {}), scope))
        return original(event, fields, scope=scope)

    with mock.patch.object(alerts, "alert", side_effect=recording):
        issue_id, created = delivery_services.open_issue(
            order.pk, K.DISPATCH_UNKNOWN, code="dead_letter", urgent=False, details={"order_id": order.pk}
        )
        assert created and recorded == []  # not urgent: the queue and the badge only
        delivery_services.open_issue(
            order.pk, K.DISPATCH_UNKNOWN, code="dead_letter", urgent=True, details={"order_id": order.pk}
        )  # escalated
        delivery_services.open_issue(
            order.pk, K.DISPATCH_UNKNOWN, code="dead_letter", urgent=True, details={"order_id": order.pk}
        )  # still urgent: no second alert
        with django_capture_on_commit_callbacks() as callbacks:
            other_id, _ = delivery_services.open_issue(
                order.pk, K.PARCEL_UNACCOUNTED, code="failed", urgent=True, details={"order_id": order.pk}
            )
        assert callbacks  # the outbox hands the alert to the dispatcher only once the transaction commits
    assert [(event, scope) for event, _fields, scope in recorded] == [
        ("delivery.issue_opened", str(issue_id)),
        ("delivery.issue_opened", str(other_id)),
    ]
    for _event, fields, _scope in recorded:
        assert set(fields) == ISSUE_ALERT_KEYS
        assert alerts.scrub_fields(fields) == fields  # nothing had to be redacted: ids, codes, a bool
    messages = _alerts("delivery.issue_opened")
    assert [m.payload["fields"]["kind"] for m in messages] == ["dispatch_unknown", "parcel_unaccounted"]


@override_settings(**ALERTS)
def test_an_issue_rolled_back_never_alerts() -> None:
    order = yandex_order()
    with pytest.raises(RuntimeError), transaction.atomic():
        delivery_services.open_issue(
            order.pk, K.PARCEL_UNACCOUNTED, code="failed", urgent=True, details={"order_id": order.pk}
        )
        raise RuntimeError("the writer failed after opening the issue")
    assert not DeliveryIssue.objects.exists()
    assert not _alerts("delivery.issue_opened")


def test_without_a_webhook_nothing_is_queued() -> None:
    order = yandex_order()
    delivery_services.open_issue(
        order.pk, K.PARCEL_UNACCOUNTED, code="failed", urgent=True, details={"order_id": order.pk}
    )
    assert not OutboxMessage.objects.filter(topic="alert.send").exists()


@override_settings(**ALERTS)
def test_the_status_writer_alerts_through_the_same_point(staff: StaffUser) -> None:
    order, claim = _accepted(staff)
    _move(order, claim, "pickuped", "cancelled_with_items_on_hands")
    issue = _open(order, K.PARCEL_UNACCOUNTED)
    assert issue is not None
    (alert,) = _alerts("delivery.issue_opened")
    assert alert.payload["fields"] == {
        "order_id": order.pk,
        "kind": "parcel_unaccounted",
        "code": "cancelled_with_items_on_hands",
        "urgent": True,
        "issue_id": issue.pk,
    }


# --- Yandex account health ----------------------------------------------------------------------------------


@override_settings(**ALERTS)
def test_repeated_failures_mark_yandex_failing_and_alert_once_per_run() -> None:
    for _ in range(delivery_services.PROVIDER_FAILING_AFTER + 1):
        delivery_services.record_yandex_failure("poll", "unauthorized")
    state = integration_selectors.provider_state("yandex")
    assert state is not None and state.consecutive_failures == 4
    (alert,) = _alerts("delivery.provider_failing")
    assert alert.payload["fields"] == {"provider": "yandex", "code": "unauthorized"}
    assert alert.dedupe_key.startswith(
        f"alert:delivery.provider_failing:{timeutils.local_date(timeutils.now()).isoformat()}:"
    )
    with override_settings(YANDEX_DELIVERY_MODE="live"):
        assert delivery_admin.yandex_failing() is True
    assert delivery_services.record_yandex_success() is True
    assert delivery_services.record_yandex_success() is False  # healthy: nothing written
    with override_settings(YANDEX_DELIVERY_MODE="live"):
        assert delivery_admin.yandex_failing() is False


def _quote_ready(customer: Customer) -> Any:
    configure_warehouse()
    fill(customer, (product(price=300_000), 1))
    return saved_address(customer)


@override_settings(**ALERTS)
def test_account_failures_of_the_price_check_are_counted_and_outages_are_not(
    customer_client: APIClient, customer: Customer, monkeypatch: pytest.MonkeyPatch
) -> None:
    address = _quote_ready(customer)
    failure: list[Exception] = [YandexUnavailable("HTTP 401 (test)", outcome_unknown=False, status=401)]

    def failing(self: Any, *args: Any, **kwargs: Any) -> Any:
        raise failure[0]

    monkeypatch.setattr(fake_yandex.FakeYandexDelivery, "check_price", failing)
    body = calculate(customer_client, address).json()
    assert option(body, "yandex", "standard")["unavailable_reason"] == "provider_unavailable"
    calculate(customer_client, address)  # standard + express, twice: four failures in a row
    state = integration_selectors.provider_state("yandex")
    assert state is not None and state.consecutive_failures == 4
    assert state.last_error == "check_price: unauthorized"
    (alert,) = _alerts("delivery.provider_failing")
    assert alert.payload["fields"] == {"provider": "yandex", "code": "unauthorized"}

    failure[0] = YandexUnavailable("timeout (test)")  # an outage on the customer path: not the account's
    calculate(customer_client, address)
    state = integration_selectors.provider_state("yandex")
    assert state is not None and state.consecutive_failures == 4

    failure[0] = YandexRejected("estimating.cargocorp_payment_failure", "payment (test)")
    body = calculate(customer_client, address).json()
    assert (
        option(body, "yandex", "express")["unavailable_reason"] == "provider_unavailable"
    )  # not out_of_zone
    state = integration_selectors.provider_state("yandex")
    assert state is not None and state.last_error == "check_price: estimating.cargocorp_payment_failure"

    monkeypatch.undo()  # Yandex answers again: the failure run ends
    body = calculate(customer_client, address).json()
    assert option(body, "yandex", "standard")["available"] is True
    state = integration_selectors.provider_state("yandex")
    assert state is not None and state.consecutive_failures == 0


@override_settings(**ALERTS)
def test_a_failing_poll_is_counted_and_alerts_on_the_third_run(
    staff: StaffUser, monkeypatch: pytest.MonkeyPatch
) -> None:
    _accepted(staff)

    def refused(self: Any, *args: Any, **kwargs: Any) -> Any:
        raise YandexUnavailable("HTTP 403 (test)", outcome_unknown=False, status=403)

    monkeypatch.setattr(fake_yandex.FakeYandexDelivery, "claims", refused)
    for _ in range(3):
        tasks.poll_yandex()
    state = integration_selectors.provider_state("yandex")
    assert state is not None and state.consecutive_failures == 3 and state.last_error == "poll: forbidden"
    (alert,) = _alerts("delivery.provider_failing")
    assert alert.payload["fields"] == {"provider": "yandex", "code": "forbidden"}
    monkeypatch.undo()
    tasks.poll_yandex()
    state = integration_selectors.provider_state("yandex")
    assert state is not None and state.consecutive_failures == 0


def test_the_beat_is_scheduled_every_five_minutes() -> None:
    from config.celery import app

    assert app.conf.beat_schedule["commerce-review-delivery-issues"] == {
        "task": "commerce.review_delivery_issues",
        "schedule": 300.0,
    }
