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
from glowderma.commerce import delivery_review, selectors, services, tasks, topics, yandex_flow, yandex_ref
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
from glowderma.delivery import selectors as delivery_selectors
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
from glowderma.integrations.yandex_delivery.ports import (
    CANCEL_FREE,
    CANCEL_PAID,
    CancelTerms,
    ClaimRequest,
    YandexConflict,
    YandexUnavailable,
)
from glowderma.integrations.yandex_delivery.registry import get_yandex_delivery
from glowderma.payments.models import Refund

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


def test_an_accept_without_a_visible_outcome_waits_then_ends_the_dispatch(
    staff: StaffUser, calls: dict[str, int]
) -> None:
    # the answer is lost and Yandex never took the accept: the claim stays ready_for_approval
    order = yandex_order(fake={"accept": "timeout"})
    claim = dispatch(order, staff.pk)
    deferred = run(claim)
    assert isinstance(deferred, outbox.RetryLater)
    claim.refresh_from_db()
    assert claim.accept_sent_at is not None and claim.accepted_at is None
    assert isinstance(run(claim), outbox.RetryLater)  # re-read only, never a second accept
    with pinned_clock(timeutils.now() + yandex_flow.ACCEPT_CONFIRM_WINDOW + timedelta(minutes=1)):
        assert run(claim) is None
    claim.refresh_from_db()
    assert claim.ended_at is not None and claim.error_code == "accept_unconfirmed"
    assert claim.settled_at is not None  # Yandex took the free cancel: nothing left to follow
    assert fake_yandex._claims[claim.claim_id].status == "cancelled"
    failed = _open(order, K.DISPATCH_FAILED)
    assert failed is not None and failed.code == "accept_unconfirmed" and not failed.urgent
    assert _open(order, K.DISPATCH_UNKNOWN) is None
    shipment = shipment_of(order)
    assert shipment.stage == G.FAILED
    order.refresh_from_db()
    assert selectors.dispatch_blocker(order, shipment) is None  # staff may call a courier again
    assert calls["accept"] == 1
    assert dispatch(order, staff.pk).number == 2


def test_an_unconfirmed_accept_ends_once_the_offer_expired_and_claims_info_cannot_lag(
    staff: StaffUser, calls: dict[str, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(fake_yandex, "OFFER_VALID_MINUTES", 1)
    order = yandex_order(fake={"accept": "timeout"})
    claim = dispatch(order, staff.pk)
    start = timeutils.now()
    assert isinstance(run(claim), outbox.RetryLater)
    # the offer has expired, but a read this soon after the accept may still lag an acceptance
    with pinned_clock(start + timedelta(seconds=90)):
        assert isinstance(run(claim), outbox.RetryLater)
    # past the documented lag and the offer, still before ACCEPT_CONFIRM_WINDOW: the dispatch ends
    with pinned_clock(start + yandex_flow.INFO_LAG + timedelta(seconds=30)):
        assert run(claim) is None
    claim.refresh_from_db()
    assert claim.ended_at is not None and claim.settled_at is not None  # Yandex took the free cancel
    failed = _open(order, K.DISPATCH_FAILED)
    assert failed is not None and failed.code == "accept_unconfirmed"
    assert calls["accept"] == 1


def test_an_unconfirmed_accept_yandex_will_not_cancel_free_stays_polled_and_a_late_acceptance_is_raised(
    staff: StaffUser, calls: dict[str, int]
) -> None:
    # cancel-info is not free for a claim the read shows before acceptance: that read is probably stale
    order = yandex_order(fake={"accept": "timeout", "cancel": "paid"})
    claim = dispatch(order, staff.pk)
    assert isinstance(run(claim), outbox.RetryLater)
    reads = calls["claim"]
    with pinned_clock(timeutils.now() + yandex_flow.ACCEPT_CONFIRM_WINDOW + timedelta(minutes=1)):
        assert run(claim) is None  # the offer has expired too: that alone settles nothing
    assert calls["claim"] == reads + 2  # the confirm read, then one fresh read after the refusal
    assert calls.get("cancel", 0) == 0  # never a paid cancel
    claim = CourierClaim.objects.get(pk=claim.pk)
    assert claim.ended_at is not None and claim.error_code == "accept_unconfirmed"
    assert claim.settled_at is None  # Yandex took no cancel: the poll keeps reading it
    assert claim.pk in [pk for pk, _order, _claim in delivery_selectors.unsettled_claims()]
    failed = _open(order, K.DISPATCH_FAILED)
    assert failed is not None and failed.code == "accept_unconfirmed"
    shipment = shipment_of(order)
    assert shipment.stage == G.FAILED
    order.refresh_from_db()
    assert selectors.dispatch_blocker(order, shipment) is None
    # Yandex had taken the accept after all: the poll still reads the claim and raises it
    fake_yandex.advance(claim.claim_id, "performer_found")
    tasks.poll_yandex()
    claim.refresh_from_db()
    assert claim.accepted_at is not None
    issue = _open(order, K.DISPATCH_UNKNOWN)
    assert issue is not None and issue.code == "unexpected_acceptance" and issue.urgent


@pytest.mark.parametrize("refusal", ["terms_not_free", "cancel_refused"])
def test_an_unconfirmed_accept_yandex_refuses_to_cancel_is_read_again_and_a_late_acceptance_followed(
    staff: StaffUser, calls: dict[str, int], monkeypatch: pytest.MonkeyPatch, refusal: str
) -> None:
    order = yandex_order(fake={"accept": "timeout"})
    claim = dispatch(order, staff.pk)
    assert isinstance(run(claim), outbox.RetryLater)
    claim_id = claim_id_of(claim)
    sent: list[str] = []

    def accepted_meanwhile(self: Any, claim_id: str, **kwargs: Any) -> CancelTerms:
        fake_yandex.advance(claim_id, "performer_lookup")  # the read before was stale: Yandex accepted it
        if refusal == "terms_not_free":
            return CancelTerms(state=CANCEL_PAID, price=PAID_FEE)
        return CancelTerms(state=CANCEL_FREE)

    def refused(self: Any, claim_id: str, *args: Any, **kwargs: Any) -> None:
        sent.append(claim_id)
        raise YandexConflict("inappropriate_status")

    monkeypatch.setattr(fake_yandex.FakeYandexDelivery, "cancel_terms", accepted_meanwhile)
    monkeypatch.setattr(fake_yandex.FakeYandexDelivery, "cancel", refused)
    with pinned_clock(timeutils.now() + yandex_flow.ACCEPT_CONFIRM_WINDOW + timedelta(minutes=1)):
        assert run(claim) is None
    claim = CourierClaim.objects.get(pk=claim.pk)
    assert claim.ended_at is None and claim.accepted_at is not None and claim.status == "performer_lookup"
    assert claim.error_code == ""
    assert _open(order, K.DISPATCH_FAILED) is None
    shipment = shipment_of(order)
    assert (shipment.stage, shipment.state) == (G.COURIER_REQUESTED, "created")
    assert OrderEvent.objects.filter(order=order, type="shipment_created").count() == 1
    assert sent == ([] if refusal == "terms_not_free" else [claim_id])
    assert calls["accept"] == 1


def test_an_unconfirmed_accept_during_a_carrier_cancel_sends_no_second_cancel(
    staff: StaffUser, staff_client: APIClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(services, "_carrier_pause", lambda _seconds: None)
    order = yandex_order(fake={"accept": "timeout"})
    claim = dispatch(order, staff.pk)
    assert isinstance(run(claim), outbox.RetryLater)
    sent: list[str] = []

    def lost(self: Any, claim_id: str, *args: Any, **kwargs: Any) -> None:
        sent.append(claim_id)
        raise YandexUnavailable("timeout after send (test)")  # outcome unknown; Yandex cancelled nothing

    monkeypatch.setattr(fake_yandex.FakeYandexDelivery, "cancel", lost)
    response = staff_client.post(
        f"{ORDERS}{order.pk}/cancel/", {"reason": "Mijoz bekor qildi"}, format="json", **_key()
    )
    assert response.status_code == 409 and response.json()["code"] == "conflict", response.json()
    claim.refresh_from_db()
    assert claim.cancel_sent_at is not None  # the carrier cancel owns the claim now
    before = shipment_of(order)
    assert before.cancel_requested_at is not None
    later = timeutils.now() + yandex_flow.ACCEPT_CONFIRM_WINDOW + timedelta(minutes=1)
    with pinned_clock(later):
        assert run(claim) is None
    assert sent == [claim.claim_id]  # never a second claims/cancel
    claim = CourierClaim.objects.get(pk=claim.pk)
    assert claim.ended_at is not None and claim.error_code == yandex_flow.CODE_CANCEL_REQUESTED
    assert claim.settled_at is None  # still polled
    assert _open(order, K.DISPATCH_FAILED) is None
    shipment = shipment_of(order)
    assert shipment.stage == before.stage and shipment.cancel_requested_at == before.cancel_requested_at
    order.refresh_from_db()
    assert order.status == "processing"
    # nobody repeats the cancel: the review beat flags it
    with pinned_clock(later + timedelta(minutes=1)):
        delivery_review.review_delivery_issues()
    incomplete = _open(order, K.CANCEL_INCOMPLETE)
    assert incomplete is not None and incomplete.urgent and incomplete.code == "carrier_cancelled"
    # the repeated cancel finds no open claim: the order is cancelled and refunded, nothing more is sent
    retry = staff_client.post(
        f"{ORDERS}{order.pk}/cancel/", {"reason": "Mijoz bekor qildi"}, format="json", **_key()
    )
    assert retry.status_code == 200, retry.json()
    order.refresh_from_db()
    assert order.status == "cancelled" and Refund.objects.get(payment__order=order).amount == order.total
    incomplete.refresh_from_db()
    assert incomplete.state == "resolved"
    assert sent == [claim.claim_id]


def _meanwhile(monkeypatch: pytest.MonkeyPatch, claim: CourierClaim, status: str) -> None:
    """A poll applies Yandex's ``status`` of the claim between a dispatch run's read and its close."""

    def poll_first(port: Any, order_id: int, snapshot: Any) -> str:
        fake_yandex.advance(claim.claim_id, status)
        services.apply_yandex_claim(order_id, get_yandex_delivery().claim(claim.claim_id), source="poll")
        return yandex_flow._CANCEL_UNANSWERED

    monkeypatch.setattr(yandex_flow, "_cancel_if_unaccepted", poll_first)


def test_an_acceptance_a_poll_records_while_the_dispatch_ends_is_kept(
    staff: StaffUser, monkeypatch: pytest.MonkeyPatch
) -> None:
    order = yandex_order(fake={"accept": "timeout"})
    claim = dispatch(order, staff.pk)
    assert isinstance(run(claim), outbox.RetryLater)
    claim_id_of(claim)
    _meanwhile(monkeypatch, claim, "performer_found")
    with pinned_clock(timeutils.now() + yandex_flow.ACCEPT_CONFIRM_WINDOW + timedelta(minutes=1)):
        assert run(claim) is None
    claim = CourierClaim.objects.get(pk=claim.pk)
    assert claim.ended_at is None and claim.accepted_at is not None and claim.error_code == ""
    assert _open(order, K.DISPATCH_FAILED) is None
    shipment = shipment_of(order)
    assert (shipment.stage, shipment.state) == (G.COURIER_ASSIGNED, "created")


def test_a_claim_that_ends_while_the_dispatch_ends_it_keeps_what_its_writer_recorded(
    staff: StaffUser, monkeypatch: pytest.MonkeyPatch
) -> None:
    order = yandex_order(fake={"accept": "timeout"})
    claim = dispatch(order, staff.pk)
    assert isinstance(run(claim), outbox.RetryLater)
    claim_id_of(claim)
    _meanwhile(monkeypatch, claim, "failed")  # Yandex failed the confirmed offer meanwhile
    with pinned_clock(timeutils.now() + yandex_flow.ACCEPT_CONFIRM_WINDOW + timedelta(minutes=1)):
        assert run(claim) is None
    claim = CourierClaim.objects.get(pk=claim.pk)
    assert claim.status == "failed" and claim.ended_at is not None and claim.error_code == ""
    failed = _open(order, K.DISPATCH_FAILED)
    assert failed is not None and failed.code == "offer_expired"  # the writer's code, not overwritten
    assert shipment_of(order).stage == G.FAILED


def test_an_accept_yandex_did_not_process_is_sent_once_more(staff: StaffUser, calls: dict[str, int]) -> None:
    order = yandex_order(fake={"accept": "down_once"})  # 429 / 401 / 403 / a redirect / not sent
    claim = dispatch(order, staff.pk)
    deferred = run(claim)
    assert isinstance(deferred, outbox.RetryLater)
    assert deferred.give_up_after == yandex_flow.NOT_PROCESSED_GIVE_UP
    claim.refresh_from_db()
    assert claim.accept_sent_at is None and claim.accepted_at is None and claim.ended_at is None
    assert run(claim) is None  # the offer re-checked, one accept again
    claim = CourierClaim.objects.get(pk=claim.pk)
    assert claim.accept_sent_at is not None and claim.accepted_at is not None
    assert calls["accept"] == 2 and calls["create_claim"] == 1
    assert OrderEvent.objects.filter(order=order, type="shipment_created").count() == 1
    run(claim)
    assert calls["accept"] == 2  # never a third


def test_an_old_version_conflict_on_accept_is_sent_again_with_the_fresh_version(
    staff: StaffUser, calls: dict[str, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    versions: list[int] = []
    counting = fake_yandex.FakeYandexDelivery.accept

    def spy(self: Any, claim_id: str, version: int, **kwargs: Any) -> Any:
        versions.append(version)
        return counting(self, claim_id, version, **kwargs)

    monkeypatch.setattr(fake_yandex.FakeYandexDelivery, "accept", spy)
    order = yandex_order(fake={"accept": "old_version_once"})  # the claim was edited meanwhile
    claim = dispatch(order, staff.pk)
    deferred = run(claim)
    assert isinstance(deferred, outbox.RetryLater) and "old_version" in deferred.reason
    claim.refresh_from_db()
    assert claim.accept_sent_at is None and claim.ended_at is None
    assert run(claim) is None
    claim.refresh_from_db()
    assert claim.accepted_at is not None and claim.version == 2
    assert versions == [1, 2] and calls["accept"] == 2
    assert _open(order, K.DISPATCH_FAILED) is None


def test_an_accept_never_processed_until_the_offer_expired_fails_the_dispatch(
    staff: StaffUser, calls: dict[str, int]
) -> None:
    order = yandex_order(fake={"accept": "down"})
    claim = dispatch(order, staff.pk)
    assert isinstance(run(claim), outbox.RetryLater)
    with pinned_clock(timeutils.now() + timedelta(minutes=fake_yandex.OFFER_VALID_MINUTES + 1)):
        assert run(claim) is None  # the re-check sees the expired offer: no accept is sent
    claim.refresh_from_db()
    assert claim.ended_at is not None and claim.error_code == "offer_expired" and claim.accepted_at is None
    failed = _open(order, K.DISPATCH_FAILED)
    assert failed is not None and failed.code == "offer_expired"
    assert calls["accept"] == 1
    assert fake_yandex._claims[claim.claim_id].status == "cancelled"
    order.refresh_from_db()
    assert selectors.dispatch_blocker(order, shipment_of(order)) is None


def test_another_conflict_on_accept_ends_the_dispatch_at_once(
    staff: StaffUser, calls: dict[str, int]
) -> None:
    order = yandex_order(fake={"accept": "conflict"})  # 409 inappropriate_status: Yandex took nothing
    claim = dispatch(order, staff.pk)
    assert run(claim) is None
    claim.refresh_from_db()
    assert claim.ended_at is not None and claim.error_code == "inappropriate_status"
    assert claim.settled_at is not None  # the free cancel went through
    assert fake_yandex._claims[claim.claim_id].status == "cancelled"
    failed = _open(order, K.DISPATCH_FAILED)
    assert failed is not None and failed.code == "inappropriate_status"
    shipment = shipment_of(order)
    assert (shipment.stage, shipment.state) == (G.FAILED, "failed")
    order.refresh_from_db()
    assert selectors.dispatch_blocker(order, shipment) is None
    assert calls["accept"] == 1


def test_a_claims_info_refusal_after_the_accept_closes_the_claim_but_keeps_polling_it(
    staff: StaffUser, calls: dict[str, int]
) -> None:
    order = yandex_order(fake={"accept": "timeout", "info": "not_found"})
    claim = dispatch(order, staff.pk)
    deferred = run(claim)  # inside the window a 4xx of claims/info is retried, nothing ends
    assert isinstance(deferred, outbox.RetryLater) and "refused" in deferred.reason
    claim.refresh_from_db()
    assert claim.ended_at is None and claim.accept_sent_at is not None
    with pinned_clock(timeutils.now() + yandex_flow.ACCEPT_CONFIRM_WINDOW + timedelta(minutes=1)):
        assert run(claim) is None
    claim = CourierClaim.objects.get(pk=claim.pk)
    assert claim.ended_at is not None and claim.settled_at is None and claim.error_code == "not_found"
    assert claim.pk in [pk for pk, _order, _claim in delivery_selectors.unsettled_claims()]
    issue = _open(order, K.DISPATCH_UNKNOWN)
    assert issue is not None and issue.code == "not_found" and issue.urgent
    shipment = shipment_of(order)
    assert shipment.stage == G.FAILED
    order.refresh_from_db()
    assert selectors.dispatch_blocker(order, shipment) is None
    assert calls["accept"] == 1
    # the poll still reads it: an acceptance Yandex shows later is raised, never lost
    fake_yandex.advance(claim.claim_id, "performer_lookup")
    tasks.poll_yandex()
    claim.refresh_from_db()
    assert claim.accepted_at is not None and claim.settled_at is None
    issue = _open(order, K.DISPATCH_UNKNOWN)
    assert issue is not None and issue.code == "unexpected_acceptance" and issue.urgent


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


def _die(claim: CourierClaim) -> None:
    message = message_of(claim)
    OutboxMessage.objects.filter(pk=message.pk).update(status="dead", last_error="boom")
    outbox.notify_dead(message)


def test_a_dead_dispatch_without_a_yandex_id_fails_and_can_be_dispatched_again(staff: StaffUser) -> None:
    order = yandex_order()
    claim = dispatch(order, staff.pk)
    _die(claim)  # e.g. ten create timeouts: no claim id, so no accept can have been sent
    claim.refresh_from_db()
    assert claim.ended_at is not None and claim.settled_at is not None and claim.error_code == "dead_letter"
    issue = _open(order, K.DISPATCH_FAILED)
    assert issue is not None and issue.code == "dead_letter" and not issue.urgent
    assert _open(order, K.DISPATCH_UNKNOWN) is None
    shipment = shipment_of(order)
    assert (shipment.stage, shipment.state) == (G.FAILED, "failed")
    order.refresh_from_db()
    assert selectors.dispatch_blocker(order, shipment) is None
    assert dispatch(order, staff.pk).number == 2


@pytest.mark.parametrize(
    "fake",
    [
        {"estimate": "pending"},  # Yandex's id is stored, no accept sent yet
        {"accept": "down"},  # 30 minutes of "not processed" accepts: the stamp was released each time
    ],
)
def test_a_dead_dispatch_with_no_accept_outstanding_fails_and_the_next_dispatch_cancels_its_claim(
    staff: StaffUser, calls: dict[str, int], fake: dict[str, str]
) -> None:
    order = yandex_order(fake=fake)
    claim = dispatch(order, staff.pk)
    assert isinstance(run(claim), outbox.RetryLater)
    claim.refresh_from_db()
    assert claim.claim_id and claim.accept_sent_at is None
    _die(claim)
    claim.refresh_from_db()
    assert claim.ended_at is not None and claim.error_code == "dead_letter"
    assert claim.settled_at is None  # Yandex knows it: still polled until it shows how it ended
    assert claim.pk in [pk for pk, _order, _claim in delivery_selectors.unsettled_claims()]
    issue = _open(order, K.DISPATCH_FAILED)
    assert issue is not None and issue.code == "dead_letter" and not issue.urgent
    assert _open(order, K.DISPATCH_UNKNOWN) is None
    shipment = shipment_of(order)
    assert (shipment.stage, shipment.state) == (G.FAILED, "failed")
    order.refresh_from_db()
    assert selectors.dispatch_blocker(order, shipment) is None  # staff may call a courier again
    second = dispatch(order, staff.pk)
    assert second.number == 2
    assert isinstance(run(second), outbox.RetryLater)  # step 1 read the first claim and cancelled it
    assert fake_yandex._claims[claim.claim_id].status == "cancelled"
    second.refresh_from_db()
    assert second.claim_id and second.claim_id != claim.claim_id
    tasks.poll_yandex()  # Yandex shows the first claim cancelled: settled, no longer read
    assert CourierClaim.objects.get(pk=claim.pk).settled_at is not None
    assert calls.get("accept", 0) == (2 if "accept" in fake else 0)


def test_a_dead_dispatch_after_the_accept_opens_an_urgent_dispatch_unknown_and_a_requeue_resumes_it(
    staff: StaffUser, calls: dict[str, int]
) -> None:
    order = yandex_order(fake={"accept": "timeout"})
    claim = dispatch(order, staff.pk)
    assert isinstance(run(claim), outbox.RetryLater)
    _die(claim)
    issue = _open(order, K.DISPATCH_UNKNOWN)
    assert issue is not None and issue.code == "dead_letter" and issue.urgent  # a courier may come
    claim.refresh_from_db()
    assert claim.ended_at is None
    # the requeued message finds the accept still not taken after the window: it ends and the
    # uncertainty it opened is resolved
    with pinned_clock(timeutils.now() + yandex_flow.ACCEPT_CONFIRM_WINDOW + timedelta(minutes=1)):
        assert run(claim) is None
    assert _open(order, K.DISPATCH_UNKNOWN) is None
    failed = _open(order, K.DISPATCH_FAILED)
    assert failed is not None and failed.code == "accept_unconfirmed"
    assert calls["accept"] == 1


def test_an_unexpected_acceptance_stays_open_when_the_current_accept_ends(staff: StaffUser) -> None:
    order = yandex_order(fake={"accept": "conflict"})
    services.open_delivery_issue(order.pk, K.DISPATCH_UNKNOWN, "unexpected_acceptance", urgent=True)
    run(dispatch(order, staff.pk))
    issue = _open(order, K.DISPATCH_UNKNOWN)  # about another claim Yandex accepted: never resolved here
    assert issue is not None and issue.code == "unexpected_acceptance"
    assert _open(order, K.DISPATCH_FAILED) is not None


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


@pytest.mark.parametrize(
    ("external", "extras", "followed"),
    [
        (None, ("1",), False),  # only the item's extra_id is echoed, and it is another order's
        ("ours", ("1",), False),  # one echoed reference that is not ours is enough
        (None, (), True),  # nothing echoed (optional in the docs): it came back for our request_id
        ("ours", (), True),
    ],
)
def test_the_adopted_claim_guard_refuses_only_a_reference_that_is_not_ours(
    staff: StaffUser,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    external: str | None,
    extras: tuple[str, ...],
    followed: bool,
) -> None:
    from dataclasses import replace

    order = yandex_order()
    ours = str(order.pk)
    original = fake_yandex.FakeYandexDelivery.create_claim

    def echo(self: Any, request: ClaimRequest, **kw: Any) -> Any:
        return replace(
            original(self, request, **kw),
            external_order_id=ours if external == "ours" else external,
            item_extra_ids=extras,
        )

    monkeypatch.setattr(fake_yandex.FakeYandexDelivery, "create_claim", echo)
    claim = dispatch(order, staff.pk)
    with caplog.at_level("WARNING", logger="glowderma.commerce.yandex_flow"):
        assert run(claim) is None
    claim.refresh_from_db()
    assert (claim.accepted_at is not None) is followed
    assert (_open(order, K.DISPATCH_UNKNOWN) is None) is followed
    missing = [record for record in caplog.records if "yandex.claim_ref_missing" in record.getMessage()]
    if followed and external is None:
        (record,) = missing
        assert record.args == {"order_id": order.pk, "claim": 1}  # ids only (a lone mapping argument)
    else:
        assert missing == []


# --- the live adapter over a scripted transport (no network) ----------------------------------------------


def _live_claim(name: str, order: Order, **changes: Any) -> dict[str, Any]:
    from glowderma.integrations.tests.live_fixtures import load

    document: dict[str, Any] = load("yandex", name)
    for point in document["route_points"]:
        if point.get("type") == "destination":
            point["external_order_id"] = str(order.pk)
    for item in document.get("items", []):
        item["extra_id"] = str(order.pk)
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


def test_a_documented_403_of_claims_create_fails_the_dispatch_at_once(
    staff: StaffUser, monkeypatch: pytest.MonkeyPatch, settings: Any
) -> None:
    import httpx

    from glowderma.integrations import httpclient
    from glowderma.integrations.tests.live_fixtures import Script
    from glowderma.integrations.yandex_delivery import protocol

    settings.YANDEX_DELIVERY_MODE = "live"
    settings.YANDEX_DELIVERY_TOKEN = "test-oauth-token"  # noqa: S105 - a test value
    refusal = {"code": "forbidden_by_antifake", "message": "Claim creation is forbidden"}
    script = Script().on("POST", protocol.PATH_CREATE, httpx.Response(403, json=refusal))
    real = httpclient.new_client
    monkeypatch.setattr(httpclient, "new_client", lambda transport=None: real(script.transport))
    order = yandex_order()
    claim = dispatch(order, staff.pk)
    assert run(claim) is None  # a definitive refusal, not "not processed": no 30 minutes of resends
    claim.refresh_from_db()
    assert claim.ended_at is not None and claim.error_code == "forbidden_by_antifake"
    issue = _open(order, K.DISPATCH_FAILED)
    assert issue is not None and issue.code == "forbidden_by_antifake"
    assert len(script.to("POST", protocol.PATH_CREATE)) == 1
    order.refresh_from_db()
    assert selectors.dispatch_blocker(order, shipment_of(order)) is None


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
