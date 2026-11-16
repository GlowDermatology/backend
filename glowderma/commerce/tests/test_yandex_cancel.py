"""Carrier-aware cancellation, failed deliveries and returns of Yandex orders (plan §3.8): every row of the
cancellation matrix through ``POST orders/{id}/cancel/`` (``services.cancel_at_carrier`` before the
idempotent ``cancel_order``), a cancel that stopped between its phases, a double cancel, a cancel while a
dispatch holds the advisory lock, ``POST orders/{id}/confirm-return/`` and the manual ``delivered`` of an
unaccounted parcel. Yandex is the fake adapter (no network)."""

# ruff: noqa: F811 - the fixtures come from yandex_dispatch_support and are requested by name

from __future__ import annotations

import threading
import uuid
from dataclasses import replace
from datetime import timedelta
from typing import Any

import pytest
from django.db import connection, transaction
from django.test import override_settings
from rest_framework.test import APIClient

from glowderma.accounts.models import StaffUser
from glowderma.commerce import delivery_review, locks, selectors, services
from glowderma.commerce.models import Order, OrderEvent
from glowderma.commerce.tests.yandex_dispatch_support import (  # noqa: F401 - fixtures
    PAID_FEE,
    dispatch,
    fresh_fake_yandex,
    no_pauses,
    open_warehouse,
    run,
    shipment_of,
    yandex_order,
)
from glowderma.core import enums, timeutils
from glowderma.core.api.exceptions import NotCancellable
from glowderma.core.models import AuditEntry, OutboxMessage
from glowderma.core.seeding import pinned_clock
from glowderma.delivery import yandex as delivery_yandex
from glowderma.delivery.models import CourierClaim, DeliveryIssue, DeliverySettings, Shipment, ShipmentEvent
from glowderma.integrations.yandex_delivery import fake as fake_yandex
from glowderma.integrations.yandex_delivery.ports import YandexConflict, YandexRejected, YandexUnavailable
from glowderma.integrations.yandex_delivery.registry import get_yandex_delivery
from glowderma.payments.models import Refund

pytestmark = pytest.mark.django_db

ORDERS = "/api/v1/admin/orders/"
K = enums.DeliveryIssueKind
G = enums.ShipmentStage
ALERTS = {"ALERT_WEBHOOK_URL": "https://alerts.example.invalid/hook"}
PORT_METHODS = ("create_claim", "accept", "claim", "claims", "cancel", "cancel_terms", "request_return")


@pytest.fixture(autouse=True)
def warehouse() -> DeliverySettings:
    return open_warehouse()


@pytest.fixture(autouse=True)
def no_carrier_pauses(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(services, "_carrier_pause", lambda _seconds: None)


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    """How often each fake port method was called (the cancel / return calls included)."""
    counts: dict[str, int] = {}
    for name in PORT_METHODS:
        original = getattr(fake_yandex.FakeYandexDelivery, name)

        def counting(
            self: Any, *args: Any, _name: str = name, _original: Any = original, **kwargs: Any
        ) -> Any:
            counts[_name] = counts.get(_name, 0) + 1
            return _original(self, *args, **kwargs)

        monkeypatch.setattr(fake_yandex.FakeYandexDelivery, name, counting)
    return counts


def _key() -> str:
    return str(uuid.uuid4())


def _cancel(client: APIClient, order: Order, key: str | None = None) -> Any:
    return client.post(
        f"{ORDERS}{order.pk}/cancel/",
        {"reason": "Mijoz bekor qildi"},
        format="json",
        HTTP_IDEMPOTENCY_KEY=key or _key(),
    )


def _confirm(client: APIClient, order: Order, note: str = "") -> Any:
    body = {"note": note} if note else {}
    return client.post(
        f"{ORDERS}{order.pk}/confirm-return/", body, format="json", HTTP_IDEMPOTENCY_KEY=_key()
    )


def _accepted(staff: StaffUser, **fake: Any) -> tuple[Order, CourierClaim]:
    """A paid Yandex order whose claim Yandex accepted (courier search running)."""
    order = yandex_order(fake=fake or None)
    claim = dispatch(order, staff.pk)
    assert run(claim) is None
    claim.refresh_from_db()
    assert claim.accepted_at is not None
    return order, claim


def _move(order: Order, claim: CourierClaim, *statuses: str) -> None:
    """The fake courier moves the claim through ``statuses``; each snapshot goes through the writer."""
    for status in statuses:
        fake_yandex.advance(claim.claim_id, status)
        snapshot = get_yandex_delivery().claim(claim.claim_id)
        services.apply_yandex_claim(order.pk, snapshot, source="poll")


def _open(order: Order, kind: str) -> DeliveryIssue | None:
    return DeliveryIssue.objects.filter(order=order, kind=kind, state="open").first()


def _carrier_calls(calls: dict[str, int]) -> dict[str, int]:
    return {name: calls[name] for name in ("cancel", "cancel_terms", "request_return") if name in calls}


def _assert_unchanged(order: Order, status: str) -> None:
    order.refresh_from_db()
    assert order.status == status
    assert not Refund.objects.filter(payment__order=order).exists()
    assert not OrderEvent.objects.filter(order=order, type="cancelled").exists()


# --- no carrier call ----------------------------------------------------------------------------------------


def test_an_unpaid_yandex_order_is_cancelled_without_any_carrier_call(
    staff_client: APIClient, calls: dict[str, int]
) -> None:
    order = yandex_order(paid=False)
    response = _cancel(staff_client, order)
    assert response.status_code == 200, response.json()
    order.refresh_from_db()
    assert order.status == "cancelled"
    assert calls == {}
    assert shipment_of(order).cancel_requested_at is None


def test_a_paid_order_without_a_claim_is_refunded_without_a_carrier_call(
    staff_client: APIClient, calls: dict[str, int]
) -> None:
    order = yandex_order()
    response = _cancel(staff_client, order)
    assert response.status_code == 200, response.json()
    order.refresh_from_db()
    assert order.status == "cancelled"
    assert Refund.objects.get(payment__order=order).amount == order.total
    assert OrderEvent.objects.get(order=order, type="cancelled").data["bts_cancelled"] is False
    assert calls == {}
    assert shipment_of(order).cancel_requested_at is None


# --- an open claim before pickup ----------------------------------------------------------------------------


def test_a_courier_on_the_way_is_cancelled_free_then_the_order_is_refunded(
    staff: StaffUser, staff_client: APIClient, calls: dict[str, int]
) -> None:
    order, claim = _accepted(staff)
    _move(order, claim, "performer_found")
    response = _cancel(staff_client, order)
    assert response.status_code == 200, response.json()
    claim.refresh_from_db()
    assert (claim.status, claim.cancel_fee) == ("cancelled", None)
    assert claim.ended_at is not None and claim.settled_at is not None
    shipment = shipment_of(order)
    assert (shipment.state, shipment.stage, shipment.carrier_cost_total) == ("cancelled", "cancelled", 0)
    assert shipment.cancel_requested_at is not None and shipment.cancelled_at is not None
    order.refresh_from_db()
    assert order.status == "cancelled"
    assert Refund.objects.get(payment__order=order).amount == order.total
    assert OrderEvent.objects.get(order=order, type="cancelled").data["bts_cancelled"] is True
    assert _carrier_calls(calls) == {"cancel_terms": 1, "cancel": 1}
    assert calls["accept"] == 1  # never a second accept
    assert not _open(order, K.DISPATCH_FAILED)  # our own cancel is no failed dispatch
    audit = AuditEntry.objects.get(action=services.AUDIT_CARRIER_CANCEL).after or {}
    assert audit["outcome"] == "cancelled" and audit["cancel_state"] == "free"
    assert response.json()["delivery"]["stage"] == "cancelled"


def test_a_courier_at_the_warehouse_is_cancelled_with_a_fee_counted_once(
    staff: StaffUser, staff_client: APIClient, calls: dict[str, int]
) -> None:
    order, claim = _accepted(staff)
    _move(order, claim, "pickup_arrived")
    claim.refresh_from_db()
    fee = claim.offer_price
    assert fee is not None
    response = _cancel(staff_client, order)
    assert response.status_code == 200, response.json()
    claim.refresh_from_db()
    assert (claim.status, claim.cancel_fee, claim.final_price) == ("cancelled_with_payment", fee, fee)
    shipment = shipment_of(order)
    assert shipment.carrier_cost_total == fee  # the fee and Yandex's final price are one cost
    assert shipment.cost_difference == PAID_FEE - fee
    order.refresh_from_db()
    assert order.status == "cancelled" and Refund.objects.get(payment__order=order).amount == order.total
    services.apply_yandex_claim(order.pk, get_yandex_delivery().claim(claim.claim_id), source="poll")
    assert shipment_of(order).carrier_cost_total == fee
    audit = AuditEntry.objects.get(action=services.AUDIT_CARRIER_CANCEL).after or {}
    assert (audit["cancel_state"], audit["cancel_fee"]) == ("paid", fee)


def _conflicting_cancel(self: Any, claim_id: str, version: int, state: Any, **kwargs: Any) -> None:
    raise YandexConflict("inappropriate_status", "too late (test)")


def _rejected_terms(self: Any, claim_id: str, **kwargs: Any) -> Any:
    raise YandexRejected("not_found", "unknown claim (test)")


def _unreadable(self: Any, claim_id: str, **kwargs: Any) -> Any:
    raise YandexUnavailable("claims/info did not answer (test)")


@pytest.mark.parametrize("refusal", ["terms_unavailable", "conflict", "rejected"])
def test_yandex_refusing_the_cancel_is_409_and_nothing_changes(
    staff: StaffUser,
    staff_client: APIClient,
    calls: dict[str, int],
    monkeypatch: pytest.MonkeyPatch,
    refusal: str,
) -> None:
    order, claim = _accepted(staff, **({"cancel": "unavailable"} if refusal == "terms_unavailable" else {}))
    _move(order, claim, "performer_found")
    if refusal == "conflict":
        monkeypatch.setattr(fake_yandex.FakeYandexDelivery, "cancel", _conflicting_cancel)
    elif refusal == "rejected":
        monkeypatch.setattr(fake_yandex.FakeYandexDelivery, "cancel_terms", _rejected_terms)
    response = _cancel(staff_client, order)
    assert response.status_code == 409, response.json()
    assert response.json()["code"] == "not_cancellable"
    assert response.json()["message"] == services.MESSAGE_YANDEX_REFUSED
    _assert_unchanged(order, "processing")
    claim.refresh_from_db()
    assert claim.ended_at is None and claim.status == "performer_found"
    assert shipment_of(order).cancel_requested_at is None  # the stamp was cleared
    assert selectors.order_detail(order.pk).available_actions["cancel"] is True  # type: ignore[union-attr]
    audit = AuditEntry.objects.get(action=services.AUDIT_CARRIER_CANCEL).after or {}
    assert audit["outcome"] in ("refused", "unavailable_terms")


def test_yandex_not_answering_is_503_and_the_stamp_is_cleared(
    staff: StaffUser, staff_client: APIClient, calls: dict[str, int]
) -> None:
    order, claim = _accepted(staff, cancel="down")
    _move(order, claim, "performer_found")
    response = _cancel(staff_client, order)
    assert response.status_code == 503, response.json()
    assert response.json()["code"] == "provider_unavailable"
    _assert_unchanged(order, "processing")
    assert shipment_of(order).cancel_requested_at is None
    assert calls.get("cancel", 0) == 0


def test_a_cancel_whose_answer_was_lost_is_read_back_and_never_resent(
    staff: StaffUser, staff_client: APIClient, calls: dict[str, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    order, claim = _accepted(staff)
    _move(order, claim, "performer_found")
    original = fake_yandex.FakeYandexDelivery.cancel  # the counting wrapper of ``calls``

    def lost(self: Any, *args: Any, **kwargs: Any) -> None:
        original(self, *args, **kwargs)
        raise YandexUnavailable("timeout after send (test)")  # outcome unknown

    monkeypatch.setattr(fake_yandex.FakeYandexDelivery, "cancel", lost)
    response = _cancel(staff_client, order)
    assert response.status_code == 200, response.json()
    claim.refresh_from_db()
    assert claim.status == "cancelled"
    order.refresh_from_db()
    assert order.status == "cancelled"
    assert calls["cancel"] == 1


def test_a_lost_answer_yandex_does_not_show_yet_keeps_the_stamp_and_is_never_resent(
    staff: StaffUser, staff_client: APIClient, calls: dict[str, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    order, claim = _accepted(staff)
    _move(order, claim, "performer_found")
    sent: list[str] = []

    def lost(self: Any, claim_id: str, *args: Any, **kwargs: Any) -> None:
        sent.append(claim_id)
        raise YandexUnavailable("timeout after send (test)")  # outcome unknown; claims/info still lags

    monkeypatch.setattr(fake_yandex.FakeYandexDelivery, "cancel", lost)
    response = _cancel(staff_client, order)
    assert response.status_code == 409, response.json()  # was 503 with the stamp cleared
    assert response.json()["code"] == "conflict"
    assert response.json()["message"] == services.MESSAGE_YANDEX_CANCEL_PENDING
    _assert_unchanged(order, "processing")
    claim.refresh_from_db()
    assert claim.ended_at is None and claim.cancel_sent_at is not None and claim.cancel_state == "free"
    stamped = shipment_of(order).cancel_requested_at
    assert stamped is not None
    audit = AuditEntry.objects.get(action=services.AUDIT_CARRIER_CANCEL).after or {}
    assert audit["outcome"] == "cancel_pending"

    _move(order, claim, "cancelled")  # Yandex had taken it: the writer reads it as our own cancel
    assert not _open(order, K.DISPATCH_FAILED)
    shipment = shipment_of(order)
    assert shipment.stage == G.CANCELLED and shipment.cancel_requested_at == stamped
    detail = selectors.order_detail(order.pk)
    assert detail is not None and detail.available_actions["dispatch"] is False
    retry = _cancel(staff_client, order)
    assert retry.status_code == 200, retry.json()
    order.refresh_from_db()
    assert order.status == "cancelled" and Refund.objects.get(payment__order=order).amount == order.total
    assert OrderEvent.objects.get(order=order, type="cancelled").data["bts_cancelled"] is True
    assert shipment_of(order).state == "cancelled"
    assert len(sent) == 1 and calls["cancel_terms"] == 1


def test_a_cancel_that_never_left_is_503_and_the_stamp_is_cleared(
    staff: StaffUser, staff_client: APIClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    order, claim = _accepted(staff)
    _move(order, claim, "performer_found")

    def refused(self: Any, *args: Any, **kwargs: Any) -> None:
        raise YandexUnavailable("429 (test)", outcome_unknown=False, status=429)

    monkeypatch.setattr(fake_yandex.FakeYandexDelivery, "cancel", refused)
    response = _cancel(staff_client, order)
    assert response.status_code == 503, response.json()
    _assert_unchanged(order, "processing")
    claim.refresh_from_db()
    assert claim.ended_at is None and claim.cancel_sent_at is None
    assert shipment_of(order).cancel_requested_at is None


def test_a_lost_paid_cancel_read_back_counts_its_fee(
    staff: StaffUser, staff_client: APIClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    order, claim = _accepted(staff)
    _move(order, claim, "pickup_arrived")
    claim.refresh_from_db()
    fee = claim.offer_price
    assert fee is not None

    def lost(self: Any, claim_id: str, *args: Any, **kwargs: Any) -> None:
        fake_yandex.advance(claim_id, "cancelled_with_payment")  # processed, no final price yet
        raise YandexUnavailable("timeout after send (test)")

    monkeypatch.setattr(fake_yandex.FakeYandexDelivery, "cancel", lost)
    response = _cancel(staff_client, order)
    assert response.status_code == 200, response.json()
    claim.refresh_from_db()
    assert (claim.status, claim.cancel_fee, claim.final_price) == ("cancelled_with_payment", fee, None)
    assert claim.cancel_state == "paid"
    assert shipment_of(order).carrier_cost_total == fee


def test_a_paid_cancel_yandex_shows_late_counts_its_fee_at_once_and_is_never_resent(
    staff: StaffUser, staff_client: APIClient, calls: dict[str, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    order, claim = _accepted(staff)
    _move(order, claim, "pickup_arrived")
    claim.refresh_from_db()
    fee = claim.offer_price
    assert fee is not None
    sent: list[str] = []
    monkeypatch.setattr(  # answered 200, but claims/info keeps the old status for a while
        fake_yandex.FakeYandexDelivery, "cancel", lambda self, claim_id, *a, **kw: sent.append(claim_id)
    )
    response = _cancel(staff_client, order)
    assert response.status_code == 409, response.json()
    assert response.json()["code"] == "conflict"
    assert response.json()["message"] == services.MESSAGE_YANDEX_CANCEL_PENDING
    _assert_unchanged(order, "processing")
    claim.refresh_from_db()
    # the fee Yandex took is counted at once, before any status shows it (a poll's final price may never come)
    assert (claim.cancel_fee, claim.cancel_state, claim.final_price) == (fee, "paid", None)
    assert claim.cancel_sent_at is not None and claim.ended_at is None
    shipment = shipment_of(order)
    assert (shipment.carrier_cost_total, shipment.cost_difference) == (fee, PAID_FEE - fee)
    stamped = shipment.cancel_requested_at
    assert stamped is not None

    # staff retry while Yandex still lags, then while it does not answer: only reads, the stamp stays
    retry = _cancel(staff_client, order)
    assert retry.status_code == 409 and retry.json()["code"] == "conflict", retry.json()
    with monkeypatch.context() as down:
        down.setattr(fake_yandex.FakeYandexDelivery, "claim", _unreadable)
        unanswered = _cancel(staff_client, order)
    assert unanswered.status_code == 409 and unanswered.json()["code"] == "conflict", unanswered.json()
    assert len(sent) == 1 and calls["cancel_terms"] == 1  # never a second cancel-info / claims/cancel
    assert shipment_of(order).cancel_requested_at == stamped  # the review beat's clock did not restart
    _assert_unchanged(order, "processing")

    _move(order, claim, "cancelled_with_payment")  # Yandex finally shows it
    assert not _open(order, K.DISPATCH_FAILED)
    assert shipment_of(order).stage == G.CANCELLED
    done = _cancel(staff_client, order)
    assert done.status_code == 200, done.json()
    order.refresh_from_db()
    assert order.status == "cancelled" and Refund.objects.get(payment__order=order).amount == order.total
    assert OrderEvent.objects.get(order=order, type="cancelled").data["bts_cancelled"] is True
    shipment = shipment_of(order)
    assert shipment.carrier_cost_total == fee and shipment.state == "cancelled"  # counted once
    assert len(sent) == 1 and calls["cancel_terms"] == 1


def _lost_paid_cancel(
    staff: StaffUser, staff_client: APIClient, monkeypatch: pytest.MonkeyPatch
) -> tuple[Order, CourierClaim, int, list[str]]:
    """A paid cancel Yandex took whose answer was lost while ``claims/info`` still shows the courier at the
    warehouse: 409 ``conflict``, the ``cancel-info`` price kept on the claim, no fee counted yet."""
    order, claim = _accepted(staff)
    _move(order, claim, "pickup_arrived")
    claim.refresh_from_db()
    fee = claim.offer_price
    assert fee is not None
    sent: list[str] = []

    def lost(self: Any, claim_id: str, *args: Any, **kwargs: Any) -> None:
        sent.append(claim_id)
        raise YandexUnavailable("timeout after send (test)")  # outcome unknown; claims/info lags

    monkeypatch.setattr(fake_yandex.FakeYandexDelivery, "cancel", lost)
    response = _cancel(staff_client, order)
    assert response.status_code == 409 and response.json()["code"] == "conflict", response.json()
    claim.refresh_from_db()
    assert (claim.cancel_state, claim.cancel_price, claim.cancel_fee) == ("paid", fee, None)
    assert shipment_of(order).carrier_cost_total == 0
    return order, claim, fee, sent


@pytest.mark.parametrize("shown", ["first_read", "re_read"])
def test_a_lost_paid_cancel_yandex_shows_only_on_the_retry_counts_its_fee_once(
    staff: StaffUser,
    staff_client: APIClient,
    calls: dict[str, int],
    monkeypatch: pytest.MonkeyPatch,
    shown: str,
) -> None:
    order, claim, fee, sent = _lost_paid_cancel(staff, staff_client, monkeypatch)
    if shown == "first_read":  # the retry's first read finds the claim cancelled
        fake_yandex.advance(claim.claim_id, "cancelled_with_payment")
    else:  # the retry's first read still lags; its re-read shows the cancel
        original = fake_yandex.FakeYandexDelivery.claim
        reads: list[str] = []

        def lagging(self: Any, claim_id: str, **kwargs: Any) -> Any:
            reads.append(claim_id)
            if len(reads) == 2:
                fake_yandex.advance(claim_id, "cancelled_with_payment")
            return original(self, claim_id, **kwargs)

        monkeypatch.setattr(fake_yandex.FakeYandexDelivery, "claim", lagging)
    retry = _cancel(staff_client, order)
    assert retry.status_code == 200, retry.json()
    claim.refresh_from_db()
    assert (claim.status, claim.cancel_fee, claim.final_price) == ("cancelled_with_payment", fee, None)
    shipment = shipment_of(order)
    assert (shipment.carrier_cost_total, shipment.cost_difference) == (fee, PAID_FEE - fee)
    assert shipment.state == "cancelled"
    order.refresh_from_db()
    assert order.status == "cancelled" and Refund.objects.get(payment__order=order).amount == order.total
    assert len(sent) == 1 and calls["cancel_terms"] == 1  # never a second cancel-info / claims/cancel
    audit = AuditEntry.objects.filter(action=services.AUDIT_CARRIER_CANCEL).order_by("pk").last()
    assert audit is not None and audit.after is not None
    outcome = "already_cancelled" if shown == "first_read" else "cancelled"
    assert (audit.after["outcome"], audit.after["cancel_fee"]) == (outcome, fee)


def test_a_lost_paid_cancel_the_poll_ends_first_counts_its_fee_once_and_the_final_price_replaces_it(
    staff: StaffUser, staff_client: APIClient, calls: dict[str, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    order, claim, fee, sent = _lost_paid_cancel(staff, staff_client, monkeypatch)
    _move(order, claim, "cancelled_with_payment")  # the poll sees Yandex's cancel before any retry
    claim.refresh_from_db()
    assert (claim.cancel_fee, claim.final_price, claim.ended_at is not None) == (fee, None, True)
    assert shipment_of(order).carrier_cost_total == fee
    retry = _cancel(staff_client, order)  # the claim has ended: no carrier call, the order is refunded
    assert retry.status_code == 200, retry.json()
    order.refresh_from_db()
    assert order.status == "cancelled" and Refund.objects.get(payment__order=order).amount == order.total
    assert OrderEvent.objects.get(order=order, type="cancelled").data["bts_cancelled"] is True
    assert shipment_of(order).carrier_cost_total == fee
    assert len(sent) == 1 and calls["cancel_terms"] == 1
    # Yandex's final price arrives later: it replaces the fee, never adds to it
    final = replace(
        get_yandex_delivery().claim(claim.claim_id),
        final_price=fee + 700,
        updated_ts=timeutils.now() + timedelta(minutes=1),
    )
    services.apply_yandex_claim(order.pk, final, source="poll")
    services.apply_yandex_claim(order.pk, final, source="poll")  # a duplicate changes nothing
    shipment = shipment_of(order)
    assert (shipment.carrier_cost_total, shipment.cost_difference) == (fee + 700, PAID_FEE - fee - 700)
    claim.refresh_from_db()
    assert (claim.cancel_fee, claim.final_price, claim.settled_at is not None) == (fee, fee + 700, True)


def test_a_refused_paid_cancel_of_a_claim_yandex_shows_cancelled_counts_the_quoted_fee(
    staff: StaffUser, staff_client: APIClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    order, claim = _accepted(staff)
    _move(order, claim, "pickup_arrived")
    claim.refresh_from_db()
    fee = claim.offer_price
    assert fee is not None

    def refused(self: Any, claim_id: str, *args: Any, **kwargs: Any) -> None:
        fake_yandex.advance(claim_id, "cancelled_with_payment")  # cancelled all the same (no final price yet)
        raise YandexConflict("inappropriate_status", "already cancelled (test)")

    monkeypatch.setattr(fake_yandex.FakeYandexDelivery, "cancel", refused)
    response = _cancel(staff_client, order)
    assert response.status_code == 200, response.json()
    claim.refresh_from_db()
    assert (claim.status, claim.cancel_fee, claim.cancel_sent_at) == ("cancelled_with_payment", fee, None)
    shipment = shipment_of(order)
    assert (shipment.carrier_cost_total, shipment.cost_difference) == (fee, PAID_FEE - fee)
    audit = AuditEntry.objects.get(action=services.AUDIT_CARRIER_CANCEL).after or {}
    assert (audit["outcome"], audit["cancel_fee"]) == ("already_cancelled", fee)


def test_a_claim_yandex_never_answered_is_withdrawn_without_a_call(
    staff: StaffUser, staff_client: APIClient, calls: dict[str, int]
) -> None:
    order = yandex_order()
    claim = dispatch(order, staff.pk)  # the dispatch handler has not run: no Yandex id, no accept
    response = _cancel(staff_client, order)
    assert response.status_code == 200, response.json()
    claim.refresh_from_db()
    assert claim.ended_at is not None and claim.error_code == "cancel_requested"
    assert shipment_of(order).stage == G.CANCELLED
    order.refresh_from_db()
    assert order.status == "cancelled"
    assert run(claim) is None  # the queued dispatch finds its claim ended: nothing is sent
    assert calls == {}


def test_an_unaccepted_claim_is_cancelled_at_yandex_best_effort_and_never_accepted(
    staff: StaffUser, staff_client: APIClient, calls: dict[str, int]
) -> None:
    order = yandex_order(fake={"estimate": "pending"})
    claim = dispatch(order, staff.pk)
    assert run(claim) is not None  # deferred while Yandex estimates
    claim.refresh_from_db()
    assert claim.claim_id and claim.accept_sent_at is None
    response = _cancel(staff_client, order)
    assert response.status_code == 200, response.json()
    claim.refresh_from_db()
    assert claim.ended_at is not None and claim.status == "cancelled"
    assert run(claim) is None
    assert calls.get("accept", 0) == 0 and calls["cancel"] == 1
    order.refresh_from_db()
    assert order.status == "cancelled"


# --- after pickup -------------------------------------------------------------------------------------------


def test_in_transit_a_return_is_requested_and_the_order_refunded_once_yandex_confirms_it(
    staff: StaffUser, staff_client: APIClient, calls: dict[str, int]
) -> None:
    order, claim = _accepted(staff)
    _move(order, claim, "pickuped")
    order.refresh_from_db()
    assert order.status == "handed_to_courier"
    response = _cancel(staff_client, order)
    assert response.status_code == 200, response.json()
    assert calls["request_return"] == 1 and calls.get("cancel", 0) == 0
    claim.refresh_from_db()
    assert delivery_yandex.stage_for(claim.status) == G.RETURNING
    order.refresh_from_db()
    assert order.status == "cancelled" and Refund.objects.get(payment__order=order).amount == order.total
    shipment = shipment_of(order)
    assert shipment.stage == G.RETURNING and shipment.cancel_requested_at is not None
    assert OrderEvent.objects.get(order=order, type="cancelled").data["bts_cancelled"] is False
    assert not OrderEvent.objects.filter(order=order, type="delivery_failed").exists()  # we asked for it
    assert ShipmentEvent.objects.filter(shipment=shipment, kind="return_requested").count() == 1
    pending = _open(order, K.RETURN_PENDING)
    assert pending is not None and pending.code == "returning"
    assert response.json()["available_actions"]["confirm_return"] is True

    # the parcel is back: staff confirm it
    confirmed = _confirm(staff_client, order, note="Posilka butun")
    assert confirmed.status_code == 200, confirmed.json()
    assert confirmed.json()["available_actions"]["confirm_return"] is False
    assert confirmed.json()["status"] == "cancelled"
    shipment = shipment_of(order)
    assert shipment.stage == G.RETURNED and shipment.returned_at is not None
    assert shipment.return_confirmed_by_id == staff.pk
    pending.refresh_from_db()
    assert (pending.state, pending.resolved_by_id, pending.resolution_note) == (
        "resolved",
        staff.pk,
        "Posilka butun",
    )
    returned = OrderEvent.objects.get(order=order, type="shipment_returned")
    assert returned.source == "admin" and returned.data["claim_id"] == claim.claim_id
    assert AuditEntry.objects.filter(action=services.AUDIT_RETURN_CONFIRMED, actor_id=staff.pk).count() == 1

    # Yandex reports the return afterwards: nothing is reopened, the stage stays
    _move(order, claim, "return_arrived", "returned", "returned_finish")
    assert shipment_of(order).stage == G.RETURNED
    assert not _open(order, K.RETURN_PENDING)
    assert OrderEvent.objects.filter(order=order, type="shipment_returned").count() == 1
    assert _confirm(staff_client, order).status_code == 409  # confirmed already


def test_a_refused_return_is_409_and_the_order_stays(
    staff: StaffUser, staff_client: APIClient, calls: dict[str, int]
) -> None:
    order, claim = _accepted(staff, **{"return": "refused"})
    _move(order, claim, "pickuped")
    response = _cancel(staff_client, order)
    assert response.status_code == 409, response.json()
    assert response.json()["code"] == "not_cancellable"
    assert response.json()["message"] == services.MESSAGE_YANDEX_RETURN_REFUSED
    _assert_unchanged(order, "handed_to_courier")
    assert shipment_of(order).cancel_requested_at is None
    assert not _open(order, K.RETURN_PENDING)
    assert calls["request_return"] == 1


def test_a_return_yandex_has_not_confirmed_is_409_return_requested_and_the_retry_refunds(
    staff: StaffUser, staff_client: APIClient, calls: dict[str, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    order, claim = _accepted(staff)
    _move(order, claim, "pickuped")
    returns: list[str] = []

    def request_return(self: Any, claim_id: str, *args: Any, **kwargs: Any) -> None:
        returns.append(claim_id)  # answered, but claims/info still shows the parcel in transit
        if len(returns) > 1:  # Yandex refuses a duplicate of the return it is already running
            raise YandexConflict("state_transition_forbidden", "a return is running (test)")

    monkeypatch.setattr(fake_yandex.FakeYandexDelivery, "request_return", request_return)
    response = _cancel(staff_client, order)
    assert response.status_code == 409, response.json()
    assert response.json()["code"] == "return_requested"
    assert response.json()["message"] == services.MESSAGE_YANDEX_RETURN_REQUESTED
    _assert_unchanged(order, "handed_to_courier")
    shipment = shipment_of(order)
    stamped = shipment.cancel_requested_at
    assert stamped is not None  # the return was asked: no dispatch, no failed delivery
    assert ShipmentEvent.objects.filter(shipment=shipment, kind="return_requested").count() == 1
    pending = _open(order, K.RETURN_PENDING)
    assert pending is not None and pending.code == "return_requested"
    detail = selectors.order_detail(order.pk)
    assert detail is not None and detail.available_actions["dispatch"] is False

    # staff press cancel again before Yandex moves: only a read, never a second claims/return
    again = _cancel(staff_client, order)
    assert again.status_code == 409 and again.json()["code"] == "return_requested", again.json()
    assert returns == [claim.claim_id]
    shipment = shipment_of(order)
    assert shipment.cancel_requested_at == stamped  # kept, with its first time
    assert ShipmentEvent.objects.filter(shipment=shipment, kind="return_requested").count() == 1
    _assert_unchanged(order, "handed_to_courier")

    _move(order, claim, "returning")  # Yandex confirms the return
    assert not OrderEvent.objects.filter(order=order, type="delivery_failed").exists()
    assert not DeliveryIssue.objects.filter(order=order, kind=K.DELIVERY_FAILED).exists()
    retry = _cancel(staff_client, order)
    assert retry.status_code == 200, retry.json()
    order.refresh_from_db()
    assert order.status == "cancelled" and Refund.objects.get(payment__order=order).amount == order.total
    assert _open(order, K.RETURN_PENDING) is not None  # until the parcel is confirmed back
    assert returns == [claim.claim_id]


def test_a_return_whose_answer_was_lost_keeps_the_stamp_and_is_never_resent(
    staff: StaffUser, staff_client: APIClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    order, claim = _accepted(staff)
    _move(order, claim, "pickuped")
    returns: list[str] = []

    def lost(self: Any, claim_id: str, *args: Any, **kwargs: Any) -> None:
        returns.append(claim_id)
        raise YandexUnavailable("timeout after send (test)")  # outcome unknown; claims/info still lags

    monkeypatch.setattr(fake_yandex.FakeYandexDelivery, "request_return", lost)
    response = _cancel(staff_client, order)
    assert response.status_code == 409, response.json()  # was 503 with the stamp cleared
    assert response.json()["code"] == "return_requested"
    _assert_unchanged(order, "handed_to_courier")
    shipment = shipment_of(order)
    assert shipment.cancel_requested_at is not None
    assert ShipmentEvent.objects.filter(shipment=shipment, kind="return_requested").count() == 1
    pending = _open(order, K.RETURN_PENDING)
    assert pending is not None and pending.code == "return_requested"

    _move(order, claim, "returning")  # Yandex had started it: our return, not a failed delivery
    assert not OrderEvent.objects.filter(order=order, type="delivery_failed").exists()
    assert not DeliveryIssue.objects.filter(order=order, kind=K.DELIVERY_FAILED).exists()
    retry = _cancel(staff_client, order)
    assert retry.status_code == 200, retry.json()
    order.refresh_from_db()
    assert order.status == "cancelled"
    assert returns == [claim.claim_id]


def test_a_failed_delivery_opens_an_issue_and_the_returning_parcel_is_cancelled_without_a_call(
    staff: StaffUser, staff_client: APIClient, calls: dict[str, int]
) -> None:
    order, claim = _accepted(staff)
    _move(order, claim, "pickuped", "delivery_arrived", "returning")
    failed = _open(order, K.DELIVERY_FAILED)
    assert failed is not None and failed.code == "returning"
    assert OrderEvent.objects.get(order=order, type="delivery_failed").data["carrier_status"] == "returning"
    response = _cancel(staff_client, order)
    assert response.status_code == 200, response.json()
    assert _carrier_calls(calls) == {}
    order.refresh_from_db()
    assert order.status == "cancelled" and Refund.objects.get(payment__order=order).amount == order.total
    failed.refresh_from_db()
    assert failed.state == "resolved"
    pending = _open(order, K.RETURN_PENDING)
    assert pending is not None and pending.code == "returning"
    assert shipment_of(order).cancel_requested_at is None  # no carrier call, no stamp


def test_a_returned_parcel_is_cancelled_without_a_call(
    staff: StaffUser, staff_client: APIClient, calls: dict[str, int]
) -> None:
    order, claim = _accepted(staff)
    _move(order, claim, "pickuped", "returning", "returned")
    assert _open(order, K.RETURN_PENDING) is not None
    response = _cancel(staff_client, order)
    assert response.status_code == 200, response.json()
    assert _carrier_calls(calls) == {}
    order.refresh_from_db()
    assert order.status == "cancelled"
    assert _open(order, K.RETURN_PENDING) is not None


def test_a_delivered_order_is_never_cancelled(staff: StaffUser, staff_client: APIClient) -> None:
    order, claim = _accepted(staff)
    _move(order, claim, "pickuped", "delivered")
    order.refresh_from_db()
    assert order.status == "delivered"
    response = _cancel(staff_client, order)
    assert response.status_code == 409 and response.json()["code"] == "not_cancellable"
    assert response.json()["message"] == services.MESSAGE_YANDEX_DELIVERED
    # Yandex shows the parcel delivered before our order moved: refused all the same
    other, other_claim = _accepted(staff)
    _move(other, other_claim, "pickuped")
    Shipment.objects.filter(order=other).update(stage=G.DELIVERED)
    refused = _cancel(staff_client, other)
    assert refused.status_code == 409 and refused.json()["message"] == services.MESSAGE_YANDEX_DELIVERED
    _assert_unchanged(other, "handed_to_courier")
    with pytest.raises(NotCancellable):
        services.cancel_order(other.pk, "Mijoz bekor qildi", staff.pk)


@pytest.mark.parametrize(
    ("ending", "stage"), [("cancelled_with_items_on_hands", G.CANCELLED), ("failed", G.FAILED)]
)
def test_a_parcel_gone_after_pickup_is_not_refunded_and_staff_may_mark_it_delivered(
    staff: StaffUser, staff_client: APIClient, ending: str, stage: str
) -> None:
    order, claim = _accepted(staff)
    _move(order, claim, "pickuped")
    transition = {"status": "delivered"}
    url = f"{ORDERS}{order.pk}/transition/"
    refused = staff_client.post(url, transition, format="json", HTTP_IDEMPOTENCY_KEY=_key())
    assert refused.status_code == 409 and refused.json()["code"] == "invalid_transition"
    _move(order, claim, ending)
    unaccounted = _open(order, K.PARCEL_UNACCOUNTED)
    assert unaccounted is not None and unaccounted.urgent
    response = _cancel(staff_client, order)
    assert response.status_code == 409, response.json()
    assert response.json()["message"] == services.MESSAGE_YANDEX_PARCEL_OUT
    _assert_unchanged(order, "handed_to_courier")
    detail = staff_client.get(f"{ORDERS}{order.pk}/").json()
    assert detail["available_actions"]["transitions"] == ["delivered"]
    before = shipment_of(order)
    assert before.stage == stage
    done = staff_client.post(url, transition, format="json", HTTP_IDEMPOTENCY_KEY=_key())
    assert done.status_code == 200, done.json()
    assert done.json()["delivery"]["stage"] == "delivered"  # the admin card no longer says cancelled / failed
    order.refresh_from_db()
    assert order.status == "delivered"
    unaccounted.refresh_from_db()
    assert unaccounted.state == "resolved"
    shipment = shipment_of(order)
    assert shipment.stage == G.DELIVERED
    assert (shipment.state, shipment.carrier_cost_total, shipment.cost_difference) == (
        before.state,
        before.carrier_cost_total,
        before.cost_difference,
    )
    customer_view = selectors.delivery_view(order, shipment, "uz")
    assert customer_view is not None and customer_view["stage"] == "delivered"
    event = ShipmentEvent.objects.get(shipment=shipment, kind="status_received")
    assert event.manual and event.payload == {
        "stage": "delivered",
        "claim_id": claim.claim_id,
        "claim_number": 1,
        "confirmed_by": staff.pk,
    }
    claim.refresh_from_db()
    assert claim.status == ending  # Yandex's record stays as it was


# --- crash, double cancel, a dispatch holding the lock ------------------------------------------------------


@override_settings(**ALERTS)
def test_a_cancel_that_stopped_between_its_phases_is_finished_without_a_second_carrier_call(
    staff: StaffUser, staff_client: APIClient, calls: dict[str, int]
) -> None:
    order, claim = _accepted(staff)
    _move(order, claim, "performer_found")
    outcome = services.cancel_at_carrier(order.pk, staff.pk)  # …and the process died before cancel_order
    assert outcome.outcome == services.CARRIER_CANCELLED
    order.refresh_from_db()
    assert order.status == "processing"
    claim.refresh_from_db()
    assert claim.status == "cancelled" and claim.ended_at is not None
    stamped = shipment_of(order).cancel_requested_at
    assert stamped is not None
    with pinned_clock(stamped + timedelta(minutes=11)):
        delivery_review.review_delivery_issues()
    incomplete = _open(order, K.CANCEL_INCOMPLETE)
    assert incomplete is not None and incomplete.urgent and incomplete.code == "carrier_cancelled"
    alert = OutboxMessage.objects.get(topic="alert.send", payload__event="delivery.issue_opened")
    assert alert.payload["fields"] == {
        "order_id": order.pk,
        "kind": "cancel_incomplete",
        "code": "carrier_cancelled",
        "urgent": True,
        "issue_id": incomplete.pk,
    }
    response = _cancel(staff_client, order)  # staff repeat the cancel
    assert response.status_code == 200, response.json()
    assert calls["cancel"] == 1 and calls["cancel_terms"] == 1  # no second carrier call
    order.refresh_from_db()
    assert order.status == "cancelled" and Refund.objects.get(payment__order=order).amount == order.total
    assert OrderEvent.objects.get(order=order, type="cancelled").data["bts_cancelled"] is True
    assert shipment_of(order).state == "cancelled"
    incomplete.refresh_from_db()
    assert incomplete.state == "resolved"


@override_settings(**ALERTS)
def test_a_cancel_that_stopped_before_a_waiting_dispatch_ran_again_is_flagged_and_finished_by_repeating(
    staff: StaffUser, staff_client: APIClient
) -> None:
    order = yandex_order(fake={"estimate": "pending"})
    claim = dispatch(order, staff.pk)
    assert run(claim) is not None  # deferred while Yandex estimates: an id, no accept
    plan = services._plan_carrier_cancel(order.pk, staff.pk, None)  # txn A stamps, then the process died
    assert not isinstance(plan, services.CarrierCancel)
    run(claim)  # the dispatch finds the stamp: the claim is withdrawn for the cancel, nobody is summoned
    claim.refresh_from_db()
    assert claim.ended_at is not None and claim.error_code == "cancel_requested"
    order.refresh_from_db()
    assert order.status == "processing"
    stamped = shipment_of(order).cancel_requested_at
    assert stamped is not None
    with pinned_clock(stamped + timedelta(minutes=9)):
        delivery_review.review_delivery_issues()
    assert _open(order, K.CANCEL_INCOMPLETE) is None  # not yet 10 minutes
    with pinned_clock(stamped + timedelta(minutes=11)):
        delivery_review.review_delivery_issues()
    incomplete = _open(order, K.CANCEL_INCOMPLETE)
    assert incomplete is not None and incomplete.urgent and incomplete.code == "carrier_cancelled"
    assert OutboxMessage.objects.filter(topic="alert.send", payload__event="delivery.issue_opened").exists()
    with pinned_clock(stamped + timedelta(minutes=16)):
        delivery_review.review_delivery_issues()
    incomplete.refresh_from_db()
    assert incomplete.state == "open"  # the next run keeps it
    response = _cancel(staff_client, order)  # staff repeat the cancel: no carrier call is left to make
    assert response.status_code == 200, response.json()
    order.refresh_from_db()
    assert order.status == "cancelled" and Refund.objects.get(payment__order=order).amount == order.total
    incomplete.refresh_from_db()
    assert incomplete.state == "resolved"


def test_a_double_cancel_is_replayed_or_refused_without_a_second_carrier_call(
    staff: StaffUser, staff_client: APIClient, calls: dict[str, int]
) -> None:
    order, claim = _accepted(staff)
    _move(order, claim, "performer_found")
    key = _key()
    first = _cancel(staff_client, order, key)
    assert first.status_code == 200, first.json()
    replay = _cancel(staff_client, order, key)
    assert replay.status_code == 200 and replay["Idempotent-Replayed"] == "true"
    again = _cancel(staff_client, order)
    assert again.status_code == 409 and again.json()["code"] == "not_cancellable"
    assert _carrier_calls(calls) == {"cancel_terms": 1, "cancel": 1}
    assert Refund.objects.filter(payment__order=order).count() == 1


@pytest.mark.django_db(transaction=True)
def test_a_cancel_while_a_dispatch_holds_the_advisory_lock_is_409(
    staff_client: APIClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from glowderma.core import tasks as core_tasks

    monkeypatch.setattr(core_tasks.dispatch_outbox, "delay", lambda *args, **kwargs: None)  # no eager run
    open_warehouse()
    order = yandex_order()
    held, release = threading.Event(), threading.Event()

    def dispatching_worker() -> None:
        try:
            with locks.yandex_lock(order.pk):
                held.set()
                release.wait(10)
        finally:
            connection.close()

    thread = threading.Thread(target=dispatching_worker)
    thread.start()
    try:
        assert held.wait(10)
        response = _cancel(staff_client, order)
        assert response.status_code == 409, response.json()
        assert response.json()["code"] == "conflict"
        assert response.json()["message"] == services.MESSAGE_YANDEX_BUSY
        _assert_unchanged(order, "processing")
    finally:
        release.set()
        thread.join(10)
    assert _cancel(staff_client, order).status_code == 200  # free again


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("call", ["cancel", "return", "marker_lost"])
def test_an_order_busy_once_yandex_answered_keeps_the_marker_and_the_retry_only_reads(
    staff: StaffUser,
    staff_client: APIClient,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    call: str,
) -> None:
    """Another transaction holds the Order row while ``claims/cancel`` / ``claims/return`` answers: the
    marker write waits a bounded time, then the marker alone is kept on the claim (or as its event) and the
    answer is 409 ``conflict`` with the stamp kept — the retry only reads. With the claim row held too, the
    marker is lost as in a crash: 409 all the same, logged."""
    from glowderma.core import tasks as core_tasks

    monkeypatch.setattr(core_tasks.dispatch_outbox, "delay", lambda *args, **kwargs: None)  # no eager run
    monkeypatch.setattr(services, "CARRIER_RECORD_WAIT", timedelta(milliseconds=300))
    monkeypatch.setattr(services, "CARRIER_MARK_WAIT", timedelta(milliseconds=300))
    open_warehouse()
    order, claim = _accepted(staff)
    _move(order, claim, "pickuped" if call == "return" else "pickup_arrived")
    claim.refresh_from_db()
    fee = claim.offer_price
    go, held, release, done = threading.Event(), threading.Event(), threading.Event(), threading.Event()
    sent: list[str] = []

    def busy_order() -> None:
        try:
            if go.wait(10):
                with transaction.atomic():
                    Order.objects.select_for_update().get(pk=order.pk)
                    if call == "marker_lost":
                        CourierClaim.objects.select_for_update().get(pk=claim.pk)
                    held.set()
                    release.wait(10)
            done.set()
        finally:
            connection.close()

    def answered(self: Any, claim_id: str, *args: Any, **kwargs: Any) -> None:
        sent.append(claim_id)  # Yandex took it; claims/info keeps the old status for a while
        go.set()
        assert held.wait(10)

    method = "request_return" if call == "return" else "cancel"
    monkeypatch.setattr(fake_yandex.FakeYandexDelivery, method, answered)
    thread = threading.Thread(target=busy_order)
    thread.start()
    try:
        with caplog.at_level("WARNING", logger="glowderma.commerce.services"):
            response = _cancel(staff_client, order)
        assert response.status_code == 409, response.json()
        assert response.json()["code"] == "conflict"
        assert not done.is_set()  # answered while the order was still busy: the wait is bounded
    finally:
        release.set()
        thread.join(10)
    expected = services.MESSAGE_YANDEX_RETURN_REQUESTED if call == "return" else None
    assert response.json()["message"] == (expected or services.MESSAGE_YANDEX_CANCEL_PENDING)
    logged = [record.getMessage() for record in caplog.records]
    assert any("yandex.carrier_record_busy" in line for line in logged)
    assert shipment_of(order).cancel_requested_at is not None  # the stamp is kept
    _assert_unchanged(order, "handed_to_courier" if call == "return" else "processing")
    claim.refresh_from_db()
    if call == "marker_lost":  # the documented residual window: no marker, and it says so
        assert claim.cancel_sent_at is None
        assert any("yandex.carrier_marker_lost" in line for line in logged)
        return
    assert not any("yandex.carrier_marker_lost" in line for line in logged)
    if call == "return":
        assert ShipmentEvent.objects.filter(
            shipment__order=order, kind=ShipmentEvent.Kind.RETURN_REQUESTED
        ).exists()
    else:
        assert (claim.cancel_state, claim.cancel_price, claim.cancel_fee) == ("paid", fee, None)
        assert claim.cancel_sent_at is not None

    retry = _cancel(staff_client, order)  # Yandex still lags: only reads, never a second call
    assert retry.status_code == 409, retry.json()
    assert retry.json()["code"] == ("return_requested" if call == "return" else "conflict")
    assert len(sent) == 1
    _move(order, claim, "returning" if call == "return" else "cancelled_with_payment")
    done_retry = _cancel(staff_client, order)
    assert done_retry.status_code == 200, done_retry.json()
    order.refresh_from_db()
    assert order.status == "cancelled" and Refund.objects.get(payment__order=order).amount == order.total
    assert len(sent) == 1
    if call == "cancel":  # the fee of the paid cancel is counted once Yandex shows it
        assert shipment_of(order).carrier_cost_total == fee


# --- confirm return -----------------------------------------------------------------------------------------


def test_confirm_return_of_an_open_order_keeps_it_and_allows_a_new_dispatch(
    staff: StaffUser, staff_client: APIClient
) -> None:
    order, claim = _accepted(staff)
    _move(order, claim, "pickuped", "returning")
    assert _confirm(staff_client, yandex_order()).status_code == 409  # nothing is returning there
    detail = staff_client.get(f"{ORDERS}{order.pk}/").json()
    assert detail["available_actions"]["confirm_return"] is True
    response = _confirm(staff_client, order)
    assert response.status_code == 200, response.json()
    assert response.json()["status"] == "handed_to_courier"  # staff dispatch again or cancel
    shipment = shipment_of(order)
    assert shipment.stage == G.RETURNED and shipment.return_confirmed_by_id == staff.pk
    assert ShipmentEvent.objects.filter(shipment=shipment, kind="returned").count() == 1
    assert _open(order, K.DELIVERY_FAILED) is not None  # still to decide: dispatch again or cancel
    _move(order, claim, "return_arrived")  # a lagging status never moves the parcel back on the road
    assert shipment_of(order).stage == G.RETURNED
    _move(order, claim, "returned", "returned_finish")
    assert not _open(order, K.RETURN_PENDING)
    detail = staff_client.get(f"{ORDERS}{order.pk}/").json()
    assert detail["available_actions"]["confirm_return"] is False
    assert detail["available_actions"]["dispatch"] is True
    assert _confirm(staff_client, order).status_code == 409


def _failed_re_dispatch(staff: StaffUser, order: Order) -> CourierClaim:
    """Staff dispatch the returned parcel again and Yandex finds no courier: the new claim stays current,
    the stage goes back to ``returned``."""
    second = dispatch(order, staff.pk)
    assert run(second) is None
    second.refresh_from_db()
    assert second.number == 2 and second.accepted_at is not None
    _move(order, second, "performer_not_found")
    shipment = shipment_of(order)
    assert shipment.stage == G.RETURNED and shipment.claim_id == second.pk
    return second


def test_a_failed_re_dispatch_after_a_confirmed_return_offers_no_second_confirmation(
    staff: StaffUser, staff_client: APIClient
) -> None:
    order, claim = _accepted(staff)
    _move(order, claim, "pickuped", "returning")
    assert _confirm(staff_client, order).status_code == 200
    _move(order, claim, "returned", "returned_finish")
    _failed_re_dispatch(staff, order)
    detail = staff_client.get(f"{ORDERS}{order.pk}/").json()
    assert detail["available_actions"]["confirm_return"] is False  # the parcel's return was confirmed
    again = _confirm(staff_client, order)
    assert again.status_code == 409 and again.json()["code"] == "conflict", again.json()
    assert ShipmentEvent.objects.filter(shipment__order=order, kind="returned").count() == 1
    assert OrderEvent.objects.filter(order=order, type="shipment_returned").count() == 1
    response = _cancel(staff_client, order)
    assert response.status_code == 200, response.json()
    order.refresh_from_db()
    assert order.status == "cancelled"
    assert not _open(order, K.RETURN_PENDING)  # no return left to confirm
    assert response.json()["available_actions"]["confirm_return"] is False


def test_confirming_after_a_failed_re_dispatch_records_the_claim_that_carried_the_parcel(
    staff: StaffUser, staff_client: APIClient
) -> None:
    order, claim = _accepted(staff)
    _move(order, claim, "pickuped", "returning", "returned", "returned_finish")  # nobody confirmed it
    returned_at = shipment_of(order).returned_at
    assert returned_at is not None
    _failed_re_dispatch(staff, order)
    detail = staff_client.get(f"{ORDERS}{order.pk}/").json()
    assert detail["available_actions"]["confirm_return"] is True
    response = _confirm(staff_client, order)
    assert response.status_code == 200, response.json()
    assert response.json()["available_actions"]["confirm_return"] is False
    event = ShipmentEvent.objects.get(shipment__order=order, kind="returned")
    assert (event.payload["claim_id"], event.payload["claim_number"]) == (claim.claim_id, 1)
    assert OrderEvent.objects.filter(order=order, type="shipment_returned").count() == 1  # Yandex's, kept
    shipment = shipment_of(order)
    assert shipment.returned_at == returned_at  # when the parcel came back, not when the re-dispatch failed
    assert not _open(order, K.RETURN_PENDING)
    assert _confirm(staff_client, order).status_code == 409


def test_confirm_return_refusals(staff: StaffUser, staff_client: APIClient) -> None:
    waiting = yandex_order()
    refused = _confirm(staff_client, waiting)
    assert refused.status_code == 409 and refused.json()["code"] == "invalid_transition"
    assert refused.json()["message"] == services.MESSAGE_RETURN_NOT_CONFIRMABLE
    assert (
        staff_client.post(
            f"{ORDERS}999999/confirm-return/", {}, format="json", HTTP_IDEMPOTENCY_KEY=_key()
        ).status_code
        == 404
    )
    order, claim = _accepted(staff)
    _move(order, claim, "pickuped", "returning")
    too_long = _confirm(staff_client, order, note="x" * 1001)
    assert too_long.status_code == 400
    assert _confirm(staff_client, order).status_code == 200
    again = _confirm(staff_client, order)
    assert again.status_code == 409 and again.json()["code"] == "conflict"
