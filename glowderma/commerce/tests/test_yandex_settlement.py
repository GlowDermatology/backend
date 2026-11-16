"""Yandex claims the poll settles late (plan §3.7, §3.8): what a claim was closed for survives the 24 h
``no_answer`` settlement, and a paid cancel's fee is counted once however its answer, the claim's status and a
busy order interleave — reproductions from the round-2 review of the phase-3 fixes."""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any

import pytest
from rest_framework.test import APIClient

from glowderma.accounts.models import StaffUser
from glowderma.commerce import delivery_review, services, yandex_flow
from glowderma.commerce.models import Order
from glowderma.commerce.tests.test_yandex_cancel import _accepted, _cancel, _move
from glowderma.commerce.tests.yandex_dispatch_support import (  # noqa: F401 - fixtures
    dispatch,
    fresh_fake_yandex,
    no_pauses,
    open_warehouse,
    run,
    shipment_of,
    yandex_order,
)
from glowderma.core import enums, outbox, timeutils
from glowderma.core.api.exceptions import Conflict
from glowderma.core.seeding import pinned_clock
from glowderma.delivery.models import CourierClaim, DeliveryIssue
from glowderma.integrations.yandex_delivery import fake as fake_yandex
from glowderma.integrations.yandex_delivery.ports import YandexUnavailable
from glowderma.integrations.yandex_delivery.registry import get_yandex_delivery

pytestmark = pytest.mark.django_db
K = enums.DeliveryIssueKind
ORDERS = "/api/v1/admin/orders/"


@pytest.fixture(autouse=True)
def warehouse() -> Any:
    return open_warehouse()


@pytest.fixture(autouse=True)
def no_carrier_pauses(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(services, "_carrier_pause", lambda _seconds: None)


def _open(order: Order, kind: str) -> DeliveryIssue | None:
    return DeliveryIssue.objects.filter(order=order, kind=kind, state="open").first()


def _lost(self: Any, claim_id: str, *args: Any, **kwargs: Any) -> None:
    raise YandexUnavailable("timeout after send (test)")


def test_a_claim_withdrawn_for_a_cancel_keeps_cancel_incomplete_after_the_no_answer_settlement(
    staff: StaffUser, staff_client: APIClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    order = yandex_order(fake={"accept": "timeout"})
    claim = dispatch(order, staff.pk)
    assert isinstance(run(claim), outbox.RetryLater)
    monkeypatch.setattr(fake_yandex.FakeYandexDelivery, "cancel", _lost)
    response = staff_client.post(
        f"{ORDERS}{order.pk}/cancel/",
        {"reason": "Mijoz bekor qildi"},
        format="json",
        HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()),
    )
    assert response.status_code == 409
    later = timeutils.now() + yandex_flow.ACCEPT_CONFIRM_WINDOW + timedelta(minutes=1)
    with pinned_clock(later):
        assert run(claim) is None
    claim = CourierClaim.objects.get(pk=claim.pk)
    assert claim.error_code == "cancel_requested" and claim.settled_at is None
    with pinned_clock(later + timedelta(minutes=1)):
        delivery_review.review_delivery_issues()
    incomplete = _open(order, K.CANCEL_INCOMPLETE)
    assert incomplete is not None and incomplete.urgent and incomplete.code == "carrier_cancelled"
    # nobody repeats the cancel for a day: the poll settles the claim, but keeps why it was closed
    day = later + timedelta(hours=25)
    with pinned_clock(day):
        assert yandex_flow.run_poll().settled_unanswered == 1
    claim = CourierClaim.objects.get(pk=claim.pk)
    assert claim.settled_at is not None and claim.error_code == "cancel_requested"
    with pinned_clock(day + timedelta(minutes=1)):
        delivery_review.review_delivery_issues()
    incomplete.refresh_from_db()
    assert incomplete.state == "open"  # the order is still open with its cancel unfinished
    order.refresh_from_db()
    assert order.status == enums.OrderStatus.PROCESSING


def _callback_then(order: Order, *, lost: bool) -> Any:
    def cancel(self: Any, claim_id: str, *args: Any, **kwargs: Any) -> None:
        # Yandex took it, and a callback applied cancelled_with_payment before our answer came back
        fake_yandex.advance(claim_id, "cancelled_with_payment")
        services.apply_yandex_claim(order.pk, get_yandex_delivery().claim(claim_id), source="callback")
        if lost:
            raise YandexUnavailable("timeout after send (test)")

    return cancel


@pytest.mark.parametrize("lost", [True, False])
def test_a_paid_cancel_yandex_shows_before_our_marker_counts_its_fee_once(
    staff: StaffUser, staff_client: APIClient, monkeypatch: pytest.MonkeyPatch, lost: bool
) -> None:
    order, claim = _accepted(staff)
    _move(order, claim, "pickup_arrived")
    claim.refresh_from_db()
    fee = claim.offer_price
    monkeypatch.setattr(fake_yandex.FakeYandexDelivery, "cancel", _callback_then(order, lost=lost))
    response = _cancel(staff_client, order)
    assert response.status_code == 200, response.json()
    claim.refresh_from_db()
    assert claim.cancel_fee == fee
    assert shipment_of(order).carrier_cost_total == fee


@pytest.mark.parametrize("lost", [True, False])
def test_a_paid_cancel_marked_without_the_order_lock_counts_its_fee_at_the_24_hour_settlement(
    staff: StaffUser, staff_client: APIClient, monkeypatch: pytest.MonkeyPatch, lost: bool
) -> None:
    # the order stays busy past CARRIER_RECORD_WAIT and Yandex's cancelled_with_payment was recorded before
    # our marker: no writer counts the fee then, so the settlement of the unpriced claim does
    order, claim = _accepted(staff)
    _move(order, claim, "pickup_arrived")
    claim.refresh_from_db()
    fee = claim.offer_price
    monkeypatch.setattr(fake_yandex.FakeYandexDelivery, "cancel", _callback_then(order, lost=lost))
    original = services.lock_order

    def busy(order_id: int, **kwargs: Any) -> Any:
        if kwargs.get("wait") is services.CARRIER_RECORD_WAIT:
            raise Conflict(services.MESSAGE_ORDER_BUSY)
        return original(order_id, **kwargs)

    monkeypatch.setattr(services, "lock_order", busy)
    response = _cancel(staff_client, order)
    assert response.status_code == 409, response.json()
    claim.refresh_from_db()
    assert claim.cancel_sent_at is not None and claim.cancel_price == fee
    monkeypatch.setattr(services, "lock_order", original)
    assert _cancel(staff_client, order).status_code == 200
    later = timeutils.now() + timedelta(hours=25)
    with pinned_clock(later):
        yandex_flow._settle_unknown_costs(later)
    claim.refresh_from_db()
    assert claim.settled_at is not None and claim.cancel_fee == fee
    assert shipment_of(order).carrier_cost_total == fee
    # counted once: another settlement pass changes nothing
    with pinned_clock(later + timedelta(minutes=5)):
        yandex_flow._settle_unknown_costs(later + timedelta(minutes=5))
    assert shipment_of(order).carrier_cost_total == fee


def test_a_lost_paid_cancel_answer_with_claims_info_down_counts_its_fee_on_the_retry(
    staff: StaffUser, staff_client: APIClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    order, claim = _accepted(staff)
    _move(order, claim, "pickup_arrived")
    claim.refresh_from_db()
    fee = claim.offer_price
    taken = _callback_then(order, lost=True)
    state = {"cancelled": False}

    def cancel(self: Any, claim_id: str, *args: Any, **kwargs: Any) -> None:
        try:
            taken(self, claim_id, *args, **kwargs)
        finally:
            state["cancelled"] = True

    original_claim = fake_yandex.FakeYandexDelivery.claim

    def claim_read(self: Any, claim_id: str, **kwargs: Any) -> Any:
        if state["cancelled"]:
            raise YandexUnavailable("claims/info down (test)")
        return original_claim(self, claim_id, **kwargs)

    monkeypatch.setattr(fake_yandex.FakeYandexDelivery, "cancel", cancel)
    monkeypatch.setattr(fake_yandex.FakeYandexDelivery, "claim", claim_read)
    assert _cancel(staff_client, order).status_code == 409
    monkeypatch.setattr(fake_yandex.FakeYandexDelivery, "claim", original_claim)
    assert _cancel(staff_client, order).status_code == 200
    claim.refresh_from_db()
    assert claim.cancel_fee == fee
    assert shipment_of(order).carrier_cost_total == fee
