"""Yandex dispatch (plan §3.6): ``POST orders/{id}/dispatch/`` and the ``yandex.dispatch`` handler
(``commerce.tasks.dispatch_courier``) against the fake adapter — readiness, the happy path to delivered,
double clicks, two workers, timeouts after create and after accept, expired offers, warnings and the pricing
policy (review, limits, approval), a live previous claim and failed estimates."""

# ruff: noqa: F811 - the fixtures come from yandex_dispatch_support and are requested by name

from __future__ import annotations

import json
import threading
import uuid
from datetime import timedelta
from typing import Any

import pytest
from django.db import connection
from rest_framework.test import APIClient

from glowderma.accounts.models import StaffUser
from glowderma.commerce import selectors, services, tasks, topics, yandex_flow, yandex_ref
from glowderma.commerce.models import Order, OrderEvent
from glowderma.commerce.tests.support import pay, place
from glowderma.commerce.tests.yandex_dispatch_support import (  # noqa: F401 - fixtures
    END_OF_DAY,
    PAID_FEE,
    QUOTED_CARRIER_PRICE,
    calls,
    claim_id_of,
    dispatch,
    fresh_fake_yandex,
    message_of,
    no_pauses,
    open_warehouse,
    poll_until,
    run,
    shipment_of,
    yandex_order,
)
from glowderma.core import enums, outbox, timeutils
from glowderma.core.api.exceptions import Conflict
from glowderma.core.models import AuditEntry, OutboxMessage
from glowderma.core.seeding import pinned_clock
from glowderma.delivery.models import (
    CourierClaim,
    DeliveryIssue,
    DeliverySettings,
    ShipmentEvent,
    WarehouseClosure,
    WarehouseHours,
)
from glowderma.delivery.tests.factories import DeliveryIssueFactory
from glowderma.integrations.yandex_delivery import fake as fake_yandex
from glowderma.integrations.yandex_delivery.ports import ClaimRequest, YandexConflict, YandexUnavailable

pytestmark = pytest.mark.django_db

K = enums.DeliveryIssueKind
G = enums.ShipmentStage
ORDERS = "/api/v1/admin/orders/"


def _key() -> dict[str, Any]:
    return {"HTTP_IDEMPOTENCY_KEY": str(uuid.uuid4())}


def _open(order: Order, kind: str) -> DeliveryIssue | None:
    return DeliveryIssue.objects.filter(order=order, kind=kind, state="open").first()


@pytest.fixture(autouse=True)
def warehouse() -> DeliverySettings:
    return open_warehouse()


# --- the endpoint and its refusals --------------------------------------------------------------------------


def test_dispatch_endpoint_stores_the_parcel_and_enqueues_the_dispatch(
    staff_client: APIClient, staff: StaffUser, django_capture_on_commit_callbacks: Any
) -> None:
    order = yandex_order()
    detail = staff_client.get(f"{ORDERS}{order.pk}/").json()
    assert detail["available_actions"]["dispatch"] is True
    assert detail["available_actions"]["confirm_return"] is False
    response = staff_client.post(
        f"{ORDERS}{order.pk}/dispatch/", {"contents_verified": True, "weight_g": 640}, format="json", **_key()
    )
    assert response.status_code == 202, response.json()
    body = response.json()
    assert body["delivery"]["stage"] == "courier_requested"
    assert body["delivery"]["parcel_weight_g"] == 640
    assert body["available_actions"]["dispatch"] is False
    shipment = shipment_of(order)
    claim = shipment.claim
    assert claim is not None and claim.number == 1 and claim.ended_at is None and claim.claim_id == ""
    assert (claim.taxi_class, claim.method, claim.quoted_fee, claim.estimate_price) == (
        "courier",
        "standard",
        PAID_FEE,
        QUOTED_CARRIER_PRICE,
    )
    assert shipment.prepared_by_id == staff.pk and shipment.prepared_at is not None
    message = message_of(claim)
    assert message.topic == topics.YANDEX_DISPATCH
    assert message.payload == {
        "order_id": order.pk,
        "courier_claim_id": claim.pk,
        "request_id": str(claim.request_id),
    }
    audit = AuditEntry.objects.get(action=services.AUDIT_DISPATCH_REQUESTED)
    assert audit.after is not None and audit.after["weight_g"] == 640 and audit.entity_id == str(order.pk)
    assert ShipmentEvent.objects.filter(shipment=shipment, kind="claim_requested").count() == 1
    # nothing reached Yandex in the request
    assert fake_yandex._claims == {}


def test_double_click_is_a_conflict(staff_client: APIClient) -> None:
    order = yandex_order()
    body = {"contents_verified": True, "weight_g": 500}
    first = staff_client.post(f"{ORDERS}{order.pk}/dispatch/", body, format="json", **_key())
    second = staff_client.post(f"{ORDERS}{order.pk}/dispatch/", body, format="json", **_key())
    assert first.status_code == 202
    assert second.status_code == 409 and second.json()["code"] == "conflict"
    assert CourierClaim.objects.filter(shipment__order=order).count() == 1
    assert OutboxMessage.objects.filter(topic=topics.YANDEX_DISPATCH).count() == 1


def test_the_body_is_validated(staff_client: APIClient) -> None:
    order = yandex_order()
    url = f"{ORDERS}{order.pk}/dispatch/"
    for body, field in (
        ({"contents_verified": False, "weight_g": 500}, "contents_verified"),
        ({"weight_g": 500}, "contents_verified"),
        ({"contents_verified": True}, "weight_g"),
        ({"contents_verified": True, "weight_g": 0}, "weight_g"),
        ({"contents_verified": True, "weight_g": 20_001}, "weight_g"),
        ({"contents_verified": True, "weight_g": 500, "method": "drone"}, "method"),
        ({"contents_verified": True, "weight_g": 500, "approved_price": -1}, "approved_price"),
    ):
        response = staff_client.post(url, body, format="json", **_key())
        assert response.status_code == 400, body
        assert response.json()["errors"][0]["field"] == field
    assert (
        staff_client.post(
            f"{ORDERS}999999/dispatch/", {"contents_verified": True, "weight_g": 1}, format="json", **_key()
        ).status_code
        == 404
    )
    assert not CourierClaim.objects.exists()


@pytest.mark.parametrize(
    ("setup", "code"),
    [
        ("unpaid", "invalid_transition"),
        ("no_pin", "coordinates_required"),
        ("suspended", "suspended"),
        ("closed", "closed"),
        ("after_cutoff", "after_cutoff"),
        ("not_configured", "not_configured"),
        ("returns_unavailable", "returns_unavailable"),
        ("delivered", "invalid_transition"),
        ("cancel_requested", "conflict"),
        ("bts", "invalid_transition"),
        ("pickup", "invalid_transition"),
    ],
)
def test_dispatch_is_refused_before_readiness(staff_client: APIClient, setup: str, code: str) -> None:
    order = yandex_order(paid=setup != "unpaid", pin=setup != "no_pin")
    if setup == "suspended":
        DeliverySettings.objects.filter(pk=1).update(dispatch_suspended=True)
    elif setup == "closed":
        WarehouseClosure.objects.create(date=timeutils.local_date(timeutils.now()))
    elif setup == "after_cutoff":
        local = timeutils.now().astimezone(timeutils.TASHKENT)
        closes = (local + timedelta(minutes=30)).time() if local.hour < 23 else END_OF_DAY
        WarehouseHours.objects.filter(weekday=local.weekday()).update(closes_at=closes)
        DeliverySettings.objects.filter(pk=1).update(dispatch_cutoff_minutes=60)
    elif setup == "not_configured":
        DeliverySettings.objects.filter(pk=1).update(contact_phone="")
    elif setup == "returns_unavailable":
        DeliverySettings.objects.filter(pk=1).update(returns_accepted=False)
    elif setup == "delivered":
        Order.objects.filter(pk=order.pk).update(status="delivered", delivered_at=timeutils.now())
    elif setup == "cancel_requested":
        shipment = shipment_of(order)
        shipment.cancel_requested_at = timeutils.now()
        shipment.save(update_fields=["cancel_requested_at"])
    elif setup == "bts":
        order = pay(place(delivery_fee=PAID_FEE))
    elif setup == "pickup":
        order = pay(place(fulfilment="pickup"))
    detail = staff_client.get(f"{ORDERS}{order.pk}/").json()
    assert detail["available_actions"]["dispatch"] is False
    response = staff_client.post(
        f"{ORDERS}{order.pk}/dispatch/", {"contents_verified": True, "weight_g": 500}, format="json", **_key()
    )
    assert response.status_code == 409, response.json()
    assert response.json()["code"] == code
    assert not CourierClaim.objects.exists()
    assert not OutboxMessage.objects.filter(topic=topics.YANDEX_DISPATCH).exists()


def test_switching_method_needs_an_enabled_method_and_a_class_that_carries_the_weight(
    staff: StaffUser,
) -> None:
    order = yandex_order()
    DeliverySettings.objects.filter(pk=1).update(express_enabled=False)
    with pytest.raises(Conflict) as disabled:
        dispatch(order, staff.pk, method="express")
    assert disabled.value.get_codes() == "method_disabled"
    with pytest.raises(Conflict) as heavy:
        dispatch(order, staff.pk, weight_g=12_000)
    assert heavy.value.get_codes() == "too_heavy"
    DeliverySettings.objects.filter(pk=1).update(express_enabled=True)
    claim = dispatch(order, staff.pk, weight_g=12_000, method="express")
    assert (claim.method, claim.taxi_class, claim.estimate_price) == ("express", "express", None)
    assert shipment_of(order).method == "express"
    order.refresh_from_db()
    assert order.delivery_fee == PAID_FEE  # the paid fee never changes


def test_approved_price_needs_an_open_review(staff: StaffUser) -> None:
    order = yandex_order()
    with pytest.raises(Conflict):
        dispatch(order, staff.pk, approved_price=30_000)
    assert not CourierClaim.objects.exists()


def test_a_new_dispatch_resolves_the_failed_and_review_issues(staff: StaffUser) -> None:
    order = yandex_order()
    failed = DeliveryIssueFactory.create(order=order, kind=K.DISPATCH_FAILED, code="offer_expired")
    review = DeliveryIssueFactory.create(order=order, kind=K.DISPATCH_REVIEW, code="increase_over_limit")
    other = DeliveryIssueFactory.create(order=order, kind=K.INCIDENT, code="staff_report")
    dispatch(order, staff.pk, approved_price=25_000)
    for issue in (failed, review, other):
        issue.refresh_from_db()
    assert (failed.state, review.state, other.state) == ("resolved", "resolved", "open")


# --- the handler: happy path -------------------------------------------------------------------------------


def test_happy_path_to_delivered(
    staff: StaffUser, calls: dict[str, int], django_capture_on_commit_callbacks: Any
) -> None:
    order = yandex_order(fake={"offer_price": 21_000, "final_price": 21_500})
    claim = dispatch(order, staff.pk)
    assert run(claim) is None
    claim.refresh_from_db()
    assert claim.claim_id == fake_yandex.claim_id_for(str(claim.request_id))
    assert claim.accept_sent_at is not None and claim.accepted_at is not None and claim.ended_at is None
    assert claim.offer_price == 21_000
    assert calls["create_claim"] == 1 and calls["accept"] == 1
    shipment = shipment_of(order)
    assert (shipment.state, shipment.stage, shipment.tariff) == ("created", G.COURIER_REQUESTED, 21_000)
    created = OrderEvent.objects.get(order=order, type="shipment_created")
    assert created.data == {"tracking_number": "", "tariff": 21_000, "manual": False}
    assert created.actor_id == staff.pk
    # the courier comes: poll until delivered_finish
    poll_until(order, "performer_found")
    shipment = shipment_of(order)
    assert shipment.stage == G.COURIER_ASSIGNED
    assert shipment.claim.courier_name == fake_yandex.COURIER_NAME  # type: ignore[union-attr]
    assert shipment.claim.tracking_url.startswith(fake_yandex.TRACKING_BASE)  # type: ignore[union-attr]
    poll_until(order, "pickuped")
    order.refresh_from_db()
    assert order.status == "handed_to_courier"
    handed = OrderEvent.objects.get(order=order, type="handed_to_courier")
    assert (
        handed.source == "yandex"
        and handed.data["bts_status"] == "pickuped"
        and handed.data["via"] == "polling"
    )
    assert OutboxMessage.objects.filter(
        topic="push.order_status", payload__status="handed_to_courier"
    ).exists()
    poll_until(order, "delivered_finish")
    order.refresh_from_db()
    assert order.status == "delivered"
    assert OrderEvent.objects.filter(order=order, type="delivered", source="yandex").count() == 1
    shipment = shipment_of(order)
    claim = CourierClaim.objects.get(pk=claim.pk)
    assert (shipment.stage, claim.final_price, shipment.carrier_cost_total, shipment.cost_difference) == (
        G.DELIVERED,
        21_500,
        21_500,
        PAID_FEE - 21_500,
    )
    assert claim.ended_at is not None and claim.settled_at is not None
    # a settled claim is not polled any more
    before = calls.get("claims", 0)
    tasks.poll_yandex()
    assert calls.get("claims", 0) == before


def test_the_claim_request_carries_both_points_the_weight_and_our_callback(
    staff: StaffUser, monkeypatch: pytest.MonkeyPatch, settings: Any
) -> None:
    settings.YANDEX_DELIVERY_CALLBACK_URL = "https://api.example.uz/api/v1/webhooks/yandex-delivery/"
    seen: list[ClaimRequest] = []
    original = fake_yandex.FakeYandexDelivery.create_claim

    def spy(self: Any, request: ClaimRequest, **kwargs: Any) -> Any:
        seen.append(request)
        return original(self, request, **kwargs)

    monkeypatch.setattr(fake_yandex.FakeYandexDelivery, "create_claim", spy)
    order = yandex_order()
    claim = dispatch(order, staff.pk, weight_g=750)
    run(claim)
    (request,) = seen
    assert request.request_id == str(claim.request_id) and request.external_order_id == str(order.pk)
    assert request.taxi_class == "courier" and request.optional_return is False
    assert request.item.weight_kg is not None and float(request.item.weight_kg) == 0.75
    assert request.source.contact.email == "ombor@glowderma.uz" and request.source.comment == "2-qavat"
    assert request.destination.contact.phone == order.recipient_phone
    assert (request.destination.point.lat, request.destination.point.lon) == (
        float(order.address_latitude or 0),
        float(order.address_longitude or 0),
    )
    assert request.callback_url == (
        f"https://api.example.uz/api/v1/webhooks/yandex-delivery/?ref={yandex_ref.build(order.pk)}&"
    )


def test_no_callback_url_without_the_setting(staff: StaffUser, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[ClaimRequest] = []
    original = fake_yandex.FakeYandexDelivery.create_claim

    def spy(self: Any, request: ClaimRequest, **kwargs: Any) -> Any:
        seen.append(request)
        return original(self, request, **kwargs)

    monkeypatch.setattr(fake_yandex.FakeYandexDelivery, "create_claim", spy)
    order = yandex_order()
    run(dispatch(order, staff.pk))
    assert seen[0].callback_url is None


# --- the handler: idempotency, races, timeouts -------------------------------------------------------------


def test_a_redelivered_dispatch_never_creates_or_accepts_twice(
    staff: StaffUser, calls: dict[str, int]
) -> None:
    order = yandex_order()
    claim = dispatch(order, staff.pk)
    run(claim)
    OutboxMessage.objects.filter(pk=message_of(claim).pk).update(status="pending")
    run(claim)
    run(claim)
    assert calls["create_claim"] == 1 and calls["accept"] == 1
    assert len(fake_yandex._claims) == 1


@pytest.mark.django_db(transaction=True)
def test_two_workers_on_one_dispatch_one_waits_for_the_advisory_lock(
    calls: dict[str, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    from glowderma.accounts.tests.factories import StaffUserFactory
    from glowderma.commerce import locks
    from glowderma.core import tasks as core_tasks

    monkeypatch.setattr(core_tasks.dispatch_outbox, "delay", lambda *args, **kwargs: None)  # no eager run
    open_warehouse()
    staff = StaffUserFactory.create()
    order = yandex_order()
    claim = dispatch(order, staff.pk)
    held, release = threading.Event(), threading.Event()

    def other_worker() -> None:
        try:
            with locks.yandex_lock(order.pk):
                held.set()
                release.wait(10)
        finally:
            connection.close()

    thread = threading.Thread(target=other_worker)
    thread.start()
    try:
        assert held.wait(10)
        deferred = run(claim)
        assert isinstance(deferred, outbox.RetryLater) and "busy" in deferred.reason
        assert calls.get("create_claim", 0) == 0
    finally:
        release.set()
        thread.join(10)
    assert run(claim) is None
    assert calls["create_claim"] == 1 and calls["accept"] == 1


def test_timeout_after_create_retries_with_the_same_request_id(
    staff: StaffUser, calls: dict[str, int]
) -> None:
    order = yandex_order(fake={"create": "down_after_create"})
    claim = dispatch(order, staff.pk)
    with pytest.raises(YandexUnavailable):
        run(claim)
    assert claim_id_of(claim) == ""  # the answer was lost: nothing recorded
    assert len(fake_yandex._claims) == 1  # but Yandex created it
    assert run(claim) is None  # the retry with the same request_id returns that claim
    assert len(fake_yandex._claims) == 1
    assert claim_id_of(claim) == fake_yandex.claim_id_for(str(claim.request_id))
    assert calls["create_claim"] == 2 and calls["accept"] == 1


def test_create_not_processed_defers_without_spending_an_attempt(staff: StaffUser) -> None:
    order = yandex_order(fake={"create": "down"})
    claim = dispatch(order, staff.pk)
    deferred = run(claim)
    assert (
        isinstance(deferred, outbox.RetryLater)
        and deferred.give_up_after == yandex_flow.NOT_PROCESSED_GIVE_UP
    )
    assert claim_id_of(claim) == "" and fake_yandex._claims == {}


def test_a_definitive_refusal_fails_the_dispatch(staff: StaffUser) -> None:
    order = yandex_order(fake={"create": "rejected"})
    claim = dispatch(order, staff.pk)
    assert run(claim) is None
    claim.refresh_from_db()
    assert (claim.error_code, claim.ended_at is not None, claim.settled_at is not None) == (
        "address_not_found",
        True,
        True,
    )
    shipment = shipment_of(order)
    assert (shipment.stage, shipment.state) == (G.FAILED, "failed")
    issue = _open(order, K.DISPATCH_FAILED)
    assert issue is not None and issue.code == "address_not_found" and issue.details["order_id"] == order.pk
    assert selectors.dispatch_blocker(Order.objects.get(pk=order.pk), shipment) is None  # staff may retry


def test_timeout_after_accept_is_re_read_never_resent(
    staff: StaffUser, calls: dict[str, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    original = fake_yandex.FakeYandexDelivery.accept

    def lost_answer(self: Any, claim_id: str, version: int, **kwargs: Any) -> Any:
        original(self, claim_id, version, **kwargs)
        raise YandexUnavailable("timeout after send")

    monkeypatch.setattr(fake_yandex.FakeYandexDelivery, "accept", lost_answer)
    order = yandex_order()
    claim = dispatch(order, staff.pk)
    assert run(claim) is None  # the re-read shows the claim moved: acceptance recorded
    claim.refresh_from_db()
    assert claim.accepted_at is not None
    OutboxMessage.objects.filter(pk=message_of(claim).pk).update(status="pending")
    run(claim)
    assert calls["accept"] == 1


def test_accept_without_a_visible_outcome_waits_then_reports_unknown(
    staff: StaffUser, calls: dict[str, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    sent: list[str] = []

    def never_answers(self: Any, claim_id: str, version: int, **kwargs: Any) -> Any:
        sent.append(claim_id)
        raise YandexUnavailable("timeout")  # Yandex never saw it: the claim stays ready_for_approval

    monkeypatch.setattr(fake_yandex.FakeYandexDelivery, "accept", never_answers)
    order = yandex_order()
    claim = dispatch(order, staff.pk)
    deferred = run(claim)
    assert isinstance(deferred, outbox.RetryLater)
    claim.refresh_from_db()
    assert claim.accept_sent_at is not None and claim.accepted_at is None
    assert isinstance(run(claim), outbox.RetryLater)  # re-read only
    with pinned_clock(timeutils.now() + yandex_flow.ACCEPT_CONFIRM_WINDOW + timedelta(minutes=1)):
        assert run(claim) is None
    issue = _open(order, K.DISPATCH_UNKNOWN)
    assert issue is not None and issue.code == "accept_unconfirmed" and issue.urgent
    claim.refresh_from_db()
    assert claim.ended_at is None  # stays open and polled
    assert len(sent) == 1
    # the poll later sees Yandex fail it (an offer confirmed too late): the uncertainty is resolved
    fake_yandex.advance(claim.claim_id, "failed")
    tasks.poll_yandex()
    assert _open(order, K.DISPATCH_UNKNOWN) is None
    failed = _open(order, K.DISPATCH_FAILED)
    assert failed is not None and failed.code == "offer_expired"


def test_offer_expired_after_accept(staff: StaffUser) -> None:
    order = yandex_order(fake={"accept": "expired"})
    claim = dispatch(order, staff.pk)
    assert run(claim) is None
    claim.refresh_from_db()
    assert claim.status == "failed" and claim.ended_at is not None and claim.accepted_at is None
    issue = _open(order, K.DISPATCH_FAILED)
    assert issue is not None and issue.code == "offer_expired"
    assert shipment_of(order).stage == G.FAILED


def test_offer_expired_conflict_on_accept(staff: StaffUser, monkeypatch: pytest.MonkeyPatch) -> None:
    def expired(self: Any, claim_id: str, version: int, **kwargs: Any) -> Any:
        raise YandexConflict("offer_expired")

    monkeypatch.setattr(fake_yandex.FakeYandexDelivery, "accept", expired)
    order = yandex_order()
    claim = dispatch(order, staff.pk)
    assert run(claim) is None
    claim.refresh_from_db()
    assert claim.error_code == "offer_expired" and claim.ended_at is not None
    issue = _open(order, K.DISPATCH_FAILED)
    assert issue is not None and issue.code == "offer_expired"


def test_the_dead_letter_opens_dispatch_unknown(staff: StaffUser) -> None:
    order = yandex_order()
    claim = dispatch(order, staff.pk)
    message = message_of(claim)
    OutboxMessage.objects.filter(pk=message.pk).update(status="dead", last_error="boom")
    outbox.notify_dead(message)
    issue = _open(order, K.DISPATCH_UNKNOWN)
    assert issue is not None and issue.code == "dead_letter" and not issue.urgent
    claim.refresh_from_db()
    assert claim.ended_at is None


# --- estimate, warnings, policy ----------------------------------------------------------------------------


def test_estimating_failed_fails_the_dispatch(staff: StaffUser, calls: dict[str, int]) -> None:
    order = yandex_order(fake={"estimate": "failed"})
    claim = dispatch(order, staff.pk)
    assert run(claim) is None
    issue = _open(order, K.DISPATCH_FAILED)
    assert issue is not None and issue.code == "estimating.cant_construct_route"
    claim.refresh_from_db()
    assert claim.status == "estimating_failed" and claim.ended_at is not None and claim.settled_at is not None
    assert shipment_of(order).stage == G.FAILED
    assert calls.get("accept", 0) == 0


def test_a_slow_estimate_defers_then_gives_up_after_five_minutes(
    staff: StaffUser, calls: dict[str, int]
) -> None:
    order = yandex_order(fake={"estimate": "pending"})
    claim = dispatch(order, staff.pk)
    deferred = run(claim)
    assert isinstance(deferred, outbox.RetryLater) and deferred.reason == "yandex: estimating"
    assert calls["claim"] >= yandex_flow.ESTIMATE_READS
    with pinned_clock(timeutils.now() + yandex_flow.ESTIMATE_GIVE_UP + timedelta(seconds=1)):
        assert run(claim) is None
    issue = _open(order, K.DISPATCH_FAILED)
    assert issue is not None and issue.code == "estimating_timeout"
    assert calls.get("accept", 0) == 0
    assert calls.get("cancel", 0) == 1  # best effort: the unaccepted claim is cancelled


def test_warnings_send_the_price_to_review(staff: StaffUser, calls: dict[str, int]) -> None:
    order = yandex_order(fake={"warnings": ["not_fit_in_car"], "offer_price": 20_000})
    claim = dispatch(order, staff.pk)
    assert run(claim) is None
    assert calls.get("accept", 0) == 0
    issue = _open(order, K.DISPATCH_REVIEW)
    assert issue is not None and issue.code == "warnings" and issue.details["warnings"] == ["not_fit_in_car"]
    claim.refresh_from_db()
    assert claim.ended_at is not None and claim.error_code == "warnings"
    shipment = shipment_of(order)
    assert shipment.stage == G.AWAITING_DISPATCH
    assert ShipmentEvent.objects.filter(shipment=shipment, kind="price_review").count() == 1
    order.refresh_from_db()
    assert selectors.dispatch_blocker(order, shipment) is None


@pytest.mark.parametrize(
    ("offer", "accepted", "reason"),
    [
        (QUOTED_CARRIER_PRICE + 10_000, True, None),  # increase at the limit, loss 8 000 ≤ 15 000
        (QUOTED_CARRIER_PRICE + 10_001, False, "increase_over_limit"),
        (QUOTED_CARRIER_PRICE - 5_000, True, None),
    ],
)
def test_the_pricing_policy(staff: StaffUser, offer: int, accepted: bool, reason: str | None) -> None:
    order = yandex_order(fake={"offer_price": offer})
    claim = dispatch(order, staff.pk)
    run(claim)
    claim.refresh_from_db()
    assert (claim.accepted_at is not None) is accepted
    if reason is not None:
        issue = _open(order, K.DISPATCH_REVIEW)
        assert issue is not None and issue.code == reason
        assert issue.details["offer_price"] == offer and issue.details["paid_fee"] == PAID_FEE
        assert issue.details["increase"] == offer - QUOTED_CARRIER_PRICE


def test_a_loss_over_the_limit_is_reviewed(staff: StaffUser) -> None:
    DeliverySettings.objects.filter(pk=1).update(max_auto_increase=100_000)
    order = yandex_order(fake={"offer_price": PAID_FEE + 15_001})
    claim = dispatch(order, staff.pk)
    run(claim)
    issue = _open(order, K.DISPATCH_REVIEW)
    assert issue is not None and issue.code == "loss_over_limit" and issue.details["loss"] == 15_001


def test_approving_the_price_accepts_the_new_offer(staff: StaffUser, calls: dict[str, int]) -> None:
    order = yandex_order(fake={"offer_price": 45_000})
    first = dispatch(order, staff.pk)
    run(first)
    assert _open(order, K.DISPATCH_REVIEW) is not None
    # staff approve less than the offer: still reviewed
    second = dispatch(order, staff.pk, approved_price=44_000)
    assert _open(order, K.DISPATCH_REVIEW) is None  # resolved by the new dispatch
    run(second)
    review = _open(order, K.DISPATCH_REVIEW)
    assert review is not None and review.code == "above_approved"
    # the previous (never accepted) claim was cancelled at Yandex, best effort
    assert fake_yandex._claims[claim_id_of(first)].status == "cancelled"
    third = dispatch(order, staff.pk, approved_price=45_000)
    run(third)
    third.refresh_from_db()
    assert third.accepted_at is not None and third.approved_price == 45_000 and third.number == 3
    assert calls["accept"] == 1
    assert OrderEvent.objects.get(order=order, type="shipment_created").data["manual"] is True


def test_a_live_previous_claim_blocks_the_new_one(staff: StaffUser, calls: dict[str, int]) -> None:
    order = yandex_order()
    first = dispatch(order, staff.pk)
    run(first)
    first.refresh_from_db()
    # an inconsistent history: the accepted claim was closed although a courier is on it at Yandex
    fake_yandex.advance(first.claim_id, "performer_found")
    CourierClaim.objects.filter(pk=first.pk).update(ended_at=timeutils.now(), status="ready_for_approval")
    shipment = shipment_of(order)
    shipment.stage = G.FAILED
    shipment.save(update_fields=["stage"])
    second = dispatch(order, staff.pk)
    assert run(second) is None
    assert calls["create_claim"] == 1  # no second claim at Yandex
    second.refresh_from_db()
    first.refresh_from_db()
    assert second.ended_at is not None and second.error_code == "previous_claim_active"
    assert first.ended_at is None
    shipment = shipment_of(order)
    assert shipment.claim_id == first.pk and shipment.stage == G.COURIER_ASSIGNED
    issue = _open(order, K.DISPATCH_FAILED)
    assert issue is not None and issue.code == "previous_claim_active" and issue.urgent


def test_the_order_cancelled_before_the_handler_ran(staff: StaffUser, calls: dict[str, int]) -> None:
    order = yandex_order()
    claim = dispatch(order, staff.pk)
    Order.objects.filter(pk=order.pk).update(
        status="cancelled",
        cancelled_at=timeutils.now(),
        cancellation_reason="admin",
        cancellation_note="test",
        cancelled_by_id=staff.pk,
    )
    assert run(claim) is None
    claim.refresh_from_db()
    assert claim.ended_at is not None and claim.error_code == "order_closed"
    assert calls.get("create_claim", 0) == 0


def test_a_suspension_between_estimate_and_accept_stops_the_accept(
    staff: StaffUser, calls: dict[str, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    order = yandex_order()
    claim = dispatch(order, staff.pk)
    original = yandex_flow._stamp_accept

    def suspend_first(plan: Any, snapshot: Any) -> str:
        DeliverySettings.objects.filter(pk=1).update(dispatch_suspended=True)
        return original(plan, snapshot)

    monkeypatch.setattr(yandex_flow, "_stamp_accept", suspend_first)
    assert run(claim) is None
    assert calls.get("accept", 0) == 0
    issue = _open(order, K.DISPATCH_FAILED)
    assert issue is not None and issue.code == "suspended"
    claim.refresh_from_db()
    assert claim.ended_at is not None and claim.accept_sent_at is None
    assert fake_yandex._claims[claim.claim_id].status == "cancelled"


def test_an_adopted_claim_of_another_order_is_never_followed(
    staff: StaffUser, monkeypatch: pytest.MonkeyPatch
) -> None:
    from dataclasses import replace

    original = fake_yandex.FakeYandexDelivery.create_claim
    monkeypatch.setattr(
        fake_yandex.FakeYandexDelivery,
        "create_claim",
        lambda self, request, **kw: replace(original(self, request, **kw), external_order_id="1"),
    )
    order = yandex_order()
    claim = dispatch(order, staff.pk)
    assert run(claim) is None
    claim.refresh_from_db()
    assert claim.claim_id == "" and claim.error_code == "foreign_claim" and claim.ended_at is not None
    issue = _open(order, K.DISPATCH_UNKNOWN)
    assert issue is not None and issue.code == "foreign_claim"


# --- the live adapter over a scripted transport (no network) ----------------------------------------------


def _live_claim(name: str, order: Order, **changes: Any) -> dict[str, Any]:
    from glowderma.integrations.tests.live_fixtures import load

    document: dict[str, Any] = load("yandex", name)
    for point in document["route_points"]:
        if point.get("type") == "destination":
            point["external_order_id"] = str(order.pk)
    document["pricing"]["offer"]["valid_until"] = (timeutils.now() + timedelta(minutes=10)).isoformat()
    document.update(changes)
    return document


def test_the_live_adapter_creates_with_our_request_id_and_accepts_once(
    staff: StaffUser, monkeypatch: pytest.MonkeyPatch, settings: Any
) -> None:
    import httpx

    from glowderma.integrations import httpclient
    from glowderma.integrations.tests.live_fixtures import Script, reply
    from glowderma.integrations.yandex_delivery import protocol

    settings.YANDEX_DELIVERY_MODE = "live"
    settings.YANDEX_DELIVERY_TOKEN = "test-oauth-token"  # noqa: S105 - a test value
    settings.YANDEX_DELIVERY_CALLBACK_URL = "https://api.example.uz/api/v1/webhooks/yandex-delivery/"
    order = yandex_order()
    now = timeutils.now()
    ready = _live_claim("claim_ready", order, updated_ts=now.isoformat(), warnings=[])
    found = _live_claim("claim_performer_found", order, updated_ts=(now + timedelta(seconds=5)).isoformat())
    script = (
        Script()
        .on("POST", protocol.PATH_CREATE, httpx.Response(200, json=ready))
        .on("POST", protocol.PATH_ACCEPT, reply("yandex", "accept_ok"))
        .on("POST", protocol.PATH_INFO, httpx.Response(200, json=found))
        .on("GET", protocol.PATH_TRACKING_LINKS, reply("yandex", "tracking_links_ok"))
    )
    real = httpclient.new_client
    monkeypatch.setattr(httpclient, "new_client", lambda transport=None: real(script.transport))
    claim = dispatch(order, staff.pk, weight_g=1200)
    assert run(claim) is None
    (create,) = script.to("POST", protocol.PATH_CREATE)
    assert create.url.params["request_id"] == str(claim.request_id)
    sent = json.loads(create.content)
    assert sent["callback_properties"]["callback_url"].endswith(f"?ref={yandex_ref.build(order.pk)}&")
    assert sent["optional_return"] is False and sent["client_requirements"] == {"taxi_class": "courier"}
    assert sent["items"][0]["weight"] == 1.2
    assert len(script.to("POST", protocol.PATH_ACCEPT)) == 1
    claim.refresh_from_db()
    assert claim.claim_id == ready["id"] and claim.offer_price == 25_201 and claim.accepted_at is not None
    assert claim.status == "performer_found" and claim.tracking_url.startswith("https://")
    shipment = shipment_of(order)
    assert (shipment.state, shipment.stage, shipment.tariff) == ("created", G.COURIER_ASSIGNED, 25_201)
    # the bearer token never leaves the Authorization header
    assert all("test-oauth-token" not in str(request.url) for request in script.requests)


def test_bts_orders_are_untouched(staff: StaffUser, calls: dict[str, int]) -> None:
    from glowderma.core.api.exceptions import InvalidTransition
    from glowderma.delivery.models import Shipment

    order = pay(place(delivery_fee=PAID_FEE))
    assert OutboxMessage.objects.filter(topic="bts.create_shipment", payload__order_id=order.pk).count() == 1
    before = Shipment.objects.filter(order=order).values().get()
    with pytest.raises(InvalidTransition):
        services.request_dispatch(order.pk, staff.pk, weight_g=500)
    detail = selectors.order_detail(order.pk)
    assert detail is not None
    assert (
        detail.available_actions["dispatch"] is False and detail.available_actions["confirm_return"] is False
    )
    assert yandex_flow.run_poll().polled == 0
    assert Shipment.objects.filter(order=order).values().get() == before
    assert not CourierClaim.objects.exists() and calls == {}
