"""Yandex status sync (plan §3.7): the single writer ``services.apply_yandex_claim`` (every documented
status, duplicate / stale / out-of-order snapshots, old claims, unknown statuses, order effects and issues,
final price and ``cost_unknown``), the ``yandex.sync_claim`` handler, the ``commerce.poll_yandex`` beat, the
courier-details retention and the dev route that drives the fake courier."""

# ruff: noqa: F811 - the fixtures come from yandex_dispatch_support and are requested by name

from __future__ import annotations

import importlib
from collections.abc import Iterator
from dataclasses import replace
from datetime import timedelta
from typing import Any

import pytest
from django.core.cache import cache
from django.test import override_settings
from django.urls import clear_url_caches
from rest_framework.test import APIClient

from glowderma.accounts.models import StaffUser
from glowderma.commerce import services, tasks, topics, yandex_flow
from glowderma.commerce.models import Order, OrderEvent
from glowderma.commerce.tests.yandex_dispatch_support import (  # noqa: F401 - fixtures
    PAID_FEE,
    calls,
    dispatch,
    fresh_fake_yandex,
    no_pauses,
    open_warehouse,
    run,
    shipment_of,
    yandex_order,
)
from glowderma.core import enums, integration_modes, timeutils
from glowderma.core.models import OutboxMessage
from glowderma.delivery import services as delivery_services
from glowderma.delivery import tasks as delivery_tasks
from glowderma.delivery import yandex as delivery_yandex
from glowderma.delivery.models import CourierClaim, DeliveryIssue, DeliverySettings, ShipmentEvent
from glowderma.integrations import selectors as integration_selectors
from glowderma.integrations.yandex_delivery import fake as fake_yandex
from glowderma.integrations.yandex_delivery.ports import Claim, YandexUnavailable
from glowderma.integrations.yandex_delivery.registry import get_yandex_delivery

pytestmark = pytest.mark.django_db

K = enums.DeliveryIssueKind
G = enums.ShipmentStage


@pytest.fixture(autouse=True)
def warehouse() -> DeliverySettings:
    return open_warehouse()


@pytest.fixture(autouse=True)
def no_poll_lock() -> Iterator[None]:
    cache.delete(yandex_flow.POLL_LOCK_KEY)
    yield
    cache.delete(yandex_flow.POLL_LOCK_KEY)


def _accepted(staff: StaffUser, **fake: Any) -> tuple[Order, CourierClaim]:
    """A paid Yandex order whose courier claim was accepted (status ``performer_lookup``)."""
    order = yandex_order(fake=fake or None)
    claim = dispatch(order, staff.pk)
    assert run(claim) is None
    claim.refresh_from_db()
    assert claim.accepted_at is not None and claim.status == "performer_lookup"
    return order, claim


def _advance(
    order: Order, claim: CourierClaim, status: str, *, source: str = "poll"
) -> services.ClaimApplied:
    """The fake courier moves the claim to ``status``; its snapshot goes through the single writer."""
    fake_yandex.advance(claim.claim_id, status)
    snapshot = get_yandex_delivery().claim(claim.claim_id)
    return services.apply_yandex_claim(order.pk, snapshot, source=source)


def _open(order: Order, kind: str) -> DeliveryIssue | None:
    return DeliveryIssue.objects.filter(order=order, kind=kind, state="open").first()


def _expected_stage(status: str) -> str:
    if status in delivery_yandex.FAILURE_STATUSES and status not in delivery_yandex.PICKED_UP:
        return G.FAILED  # no courier came: dispatch again
    stage = delivery_yandex.stage_for(status)
    assert stage is not None
    return stage


def _expected_order_status(status: str) -> str:
    if status in delivery_yandex.DELIVERED_STATUSES:
        return "delivered"
    if status in delivery_yandex.PICKED_UP:
        return "handed_to_courier"
    return "processing"


# --- every documented status ----------------------------------------------------------------------------


@pytest.mark.parametrize("status", [s for s in delivery_yandex.STATUSES if s != "performer_lookup"])
def test_every_documented_status_maps_to_its_stage_and_order_effect(staff: StaffUser, status: str) -> None:
    order, claim = _accepted(staff)
    result = _advance(order, claim, status)
    assert result.applied
    shipment = shipment_of(order)
    order.refresh_from_db()
    claim.refresh_from_db()
    assert claim.status == status
    assert shipment.stage == _expected_stage(status)
    assert shipment.state == "created"  # a claim was accepted
    assert order.status == _expected_order_status(status)
    assert (claim.ended_at is not None) is delivery_yandex.is_finished(status)
    latest = (
        ShipmentEvent.objects.filter(shipment=shipment, kind="claim_status").order_by("-at", "-id").first()
    )
    assert latest is not None and latest.carrier_status == status and latest.via == "polling"
    payload = latest.payload
    assert set(payload) == {"claim_id", "claim_number", "status", "version", "offer_price", "final_price"}
    if status in delivery_yandex.DELIVERED_STATUSES:
        event = OrderEvent.objects.get(order=order, type="delivered")
        assert event.source == "yandex" and event.data["bts_status"] == status
        assert OutboxMessage.objects.filter(topic="push.order_status", payload__status="delivered").exists()
    if status in delivery_yandex.PICKED_UP and status not in delivery_yandex.DELIVERED_STATUSES:
        assert OrderEvent.objects.filter(order=order, type="handed_to_courier", source="yandex").count() == 1
    stage = delivery_yandex.stage_for(status)
    if stage == G.RETURNING:
        assert OrderEvent.objects.get(order=order, type="delivery_failed").data == {
            "provider": "yandex",
            "carrier_status": status,
            "reason": "",
            "claim_id": claim.claim_id,
        }
        issue = _open(order, K.DELIVERY_FAILED)
        assert issue is not None and issue.code == status
    if status in delivery_yandex.RETURNED_STATUSES:
        assert OrderEvent.objects.filter(order=order, type="shipment_returned").count() == 1
        assert _open(order, K.RETURN_PENDING) is not None and shipment.returned_at is not None
    if status in delivery_yandex.FAILURE_STATUSES:
        if status in delivery_yandex.PICKED_UP:
            issue = _open(order, K.PARCEL_UNACCOUNTED)
            assert issue is not None and issue.urgent and issue.code == status
        else:
            issue = _open(order, K.DISPATCH_FAILED)
            assert issue is not None and issue.code == status
            assert not DeliveryIssue.objects.filter(order=order, kind=K.PARCEL_UNACCOUNTED).exists()


def test_an_unknown_status_is_stored_and_logged_but_moves_nothing(
    staff: StaffUser, caplog: pytest.LogCaptureFixture
) -> None:
    order, claim = _accepted(staff)
    result = _advance(order, claim, "pay_waiting")
    assert result.applied
    claim.refresh_from_db()
    assert claim.status == "pay_waiting"
    assert shipment_of(order).stage == G.COURIER_REQUESTED
    order.refresh_from_db()
    assert order.status == "processing"
    assert "yandex.unknown_status" in caplog.text


def test_the_poll_may_skip_pickuped_delivered_moves_a_processing_order(staff: StaffUser) -> None:
    order, claim = _accepted(staff)
    _advance(order, claim, "delivered_finish")
    order.refresh_from_db()
    assert order.status == "delivered"
    assert not OrderEvent.objects.filter(order=order, type="handed_to_courier").exists()


def test_terminal_statuses_never_regress(staff: StaffUser) -> None:
    order, claim = _accepted(staff)
    _advance(order, claim, "delivered")
    _advance(order, claim, "pickuped")  # a later snapshot claims an earlier status
    order.refresh_from_db()
    assert order.status == "delivered"
    assert OrderEvent.objects.filter(order=order, type="delivered").count() == 1


def test_delivered_on_a_cancelled_order_opens_an_urgent_issue(staff: StaffUser) -> None:
    order, claim = _accepted(staff)
    Order.objects.filter(pk=order.pk).update(
        status="cancelled",
        cancelled_at=timeutils.now(),
        cancellation_reason="admin",
        cancellation_note="test",
        cancelled_by_id=staff.pk,
    )
    _advance(order, claim, "delivered")
    order.refresh_from_db()
    assert order.status == "cancelled"
    issue = _open(order, K.PARCEL_UNACCOUNTED)
    assert issue is not None and issue.urgent and issue.code == "delivered_after_cancel"


def test_a_failure_after_pickup_is_an_urgent_parcel_unaccounted(staff: StaffUser) -> None:
    order, claim = _accepted(staff)
    _advance(order, claim, "pickuped")
    _advance(order, claim, "failed")
    issue = _open(order, K.PARCEL_UNACCOUNTED)
    assert issue is not None and issue.urgent and issue.code == "failed"
    assert _open(order, K.DISPATCH_FAILED) is None
    order.refresh_from_db()
    assert order.status == "handed_to_courier"


def test_returning_with_a_reason_then_returned(staff: StaffUser, monkeypatch: pytest.MonkeyPatch) -> None:
    order, claim = _accepted(staff)
    _advance(order, claim, "pickuped")
    fake_yandex.advance(claim.claim_id, "returning")
    snapshot = replace(get_yandex_delivery().claim(claim.claim_id), return_reasons=("recipient_unavailable",))
    services.apply_yandex_claim(order.pk, snapshot, source="poll")
    event = OrderEvent.objects.get(order=order, type="delivery_failed")
    assert event.data["reason"] == "recipient_unavailable" and event.source == "yandex"
    issue = _open(order, K.DELIVERY_FAILED)
    assert issue is not None and issue.code == "recipient_unavailable"
    _advance(order, claim, "return_arrived")
    assert OrderEvent.objects.filter(order=order, type="delivery_failed").count() == 1  # once per claim
    _advance(order, claim, "returned")
    _advance(order, claim, "returned_finish")
    assert OrderEvent.objects.filter(order=order, type="shipment_returned").count() == 1
    shipment = shipment_of(order)
    assert shipment.stage == G.RETURNED and shipment.returned_at is not None
    pending = DeliveryIssue.objects.filter(order=order, kind=K.RETURN_PENDING, state="open")
    assert pending.count() == 1
    order.refresh_from_db()
    assert order.status == "handed_to_courier"  # never refunded automatically here


def test_a_returned_parcel_can_be_dispatched_again(staff: StaffUser, calls: dict[str, int]) -> None:
    order, claim = _accepted(staff)
    _advance(order, claim, "pickuped")
    _advance(order, claim, "returning")
    _advance(order, claim, "returned_finish")
    second = dispatch(order, staff.pk)
    assert second.number == 2
    assert run(second) is None
    second.refresh_from_db()
    assert second.accepted_at is not None
    assert calls["accept"] == 2
    order.refresh_from_db()
    assert order.status == "handed_to_courier"
    _advance(order, second, "delivered")
    order.refresh_from_db()
    assert order.status == "delivered"
    assert _open(order, K.DELIVERY_FAILED) is None  # resolved by the delivery
    assert _open(order, K.RETURN_PENDING) is None


def test_a_failed_re_dispatch_of_a_returned_parcel_leaves_it_returned(staff: StaffUser) -> None:
    order, first = _accepted(staff)
    for status in ("pickuped", "returning", "returned_finish"):
        _advance(order, first, status)
    second = dispatch(order, staff.pk)
    run(second)
    second.refresh_from_db()
    _advance(order, second, "performer_not_found")
    shipment = shipment_of(order)
    assert shipment.stage == G.RETURNED  # the parcel is still at the warehouse
    issue = _open(order, K.DISPATCH_FAILED)
    assert issue is not None and issue.code == "performer_not_found"
    order.refresh_from_db()
    from glowderma.commerce import selectors

    assert order.status == "handed_to_courier" and selectors.dispatch_blocker(order, shipment) is None
    third = dispatch(order, staff.pk)
    assert third.number == 3


def test_an_order_whose_return_was_requested_gets_no_delivery_failed_issue(staff: StaffUser) -> None:
    order, claim = _accepted(staff)
    _advance(order, claim, "pickuped")
    shipment = shipment_of(order)
    shipment.cancel_requested_at = timeutils.now()
    shipment.save(update_fields=["cancel_requested_at"])
    _advance(order, claim, "returning")
    assert _open(order, K.DELIVERY_FAILED) is None
    assert not OrderEvent.objects.filter(order=order, type="delivery_failed").exists()


def test_performer_not_found_before_pickup_fails_the_dispatch(staff: StaffUser) -> None:
    order, claim = _accepted(staff)
    _advance(order, claim, "performer_not_found")
    shipment = shipment_of(order)
    assert shipment.stage == G.FAILED
    issue = _open(order, K.DISPATCH_FAILED)
    assert issue is not None and issue.code == "performer_not_found"
    claim.refresh_from_db()
    assert claim.ended_at is not None and claim.settled_at is not None  # no cost for a courier never found
    order.refresh_from_db()
    from glowderma.commerce import selectors

    assert selectors.dispatch_blocker(order, shipment) is None


# --- duplicate, stale, out-of-order, old claims ----------------------------------------------------------


def test_duplicate_stale_and_out_of_order_snapshots(staff: StaffUser) -> None:
    order, claim = _accepted(staff)
    base = get_yandex_delivery().claim(claim.claim_id)
    now = timeutils.now()
    newer = replace(base, status="pickuped", updated_ts=now + timedelta(minutes=2))
    older = replace(base, status="performer_found", updated_ts=now + timedelta(minutes=1))
    assert services.apply_yandex_claim(order.pk, newer, source="poll").applied
    count = ShipmentEvent.objects.filter(kind="claim_status").count()
    stale = services.apply_yandex_claim(order.pk, older, source="callback")
    assert stale.outcome == delivery_services.SNAPSHOT_STALE
    duplicate = services.apply_yandex_claim(order.pk, newer, source="callback")
    assert duplicate.outcome == delivery_services.SNAPSHOT_DUPLICATE
    assert ShipmentEvent.objects.filter(kind="claim_status").count() == count
    claim.refresh_from_db()
    assert claim.status == "pickuped" and claim.updated_ts == newer.updated_ts
    assert shipment_of(order).stage == G.IN_TRANSIT
    assert OrderEvent.objects.filter(order=order, type="handed_to_courier").count() == 1


def test_an_old_claim_updates_its_row_only(staff: StaffUser) -> None:
    order, first = _accepted(staff)
    _advance(order, first, "pickuped")
    _advance(order, first, "returning")
    _advance(order, first, "returned_finish")
    second = dispatch(order, staff.pk)
    run(second)
    stage = shipment_of(order).stage
    # a late snapshot of the first claim (a callback, a lagging poll) changes neither stage nor order
    late = replace(
        get_yandex_delivery().claim(first.claim_id),
        status="delivered",
        updated_ts=timeutils.now() + timedelta(hours=1),
    )
    result = services.apply_yandex_claim(order.pk, late, source="callback")
    assert result.applied
    assert shipment_of(order).stage == stage
    order.refresh_from_db()
    assert order.status == "handed_to_courier"
    first.refresh_from_db()
    assert first.status == "delivered"


def test_a_claim_of_another_order_is_ignored(staff: StaffUser) -> None:
    order, claim = _accepted(staff)
    other = yandex_order()
    snapshot = get_yandex_delivery().claim(claim.claim_id)
    result = services.apply_yandex_claim(other.pk, snapshot, source="callback")
    assert result.outcome == delivery_services.SNAPSHOT_UNKNOWN_CLAIM


def test_an_acceptance_of_a_claim_we_closed_is_an_urgent_unknown(staff: StaffUser) -> None:
    order = yandex_order(fake={"warnings": ["address_too_far"]})
    claim = dispatch(order, staff.pk)
    run(claim)  # held for review: closed, never accepted
    claim.refresh_from_db()
    assert claim.ended_at is not None
    snapshot = replace(
        get_yandex_delivery().claim(claim.claim_id),
        status="performer_found",
        updated_ts=timeutils.now() + timedelta(minutes=1),
    )
    services.apply_yandex_claim(order.pk, snapshot, source="poll")
    issue = _open(order, K.DISPATCH_UNKNOWN)
    assert issue is not None and issue.urgent and issue.code == "unexpected_acceptance"


# --- cost -------------------------------------------------------------------------------------------------


def test_final_price_is_added_once(staff: StaffUser) -> None:
    order, claim = _accepted(staff, offer_price=20_000, final_price=23_000)
    _advance(order, claim, "delivered")
    _advance(order, claim, "delivered_finish")
    shipment = shipment_of(order)
    claim.refresh_from_db()
    assert (claim.final_price, shipment.carrier_cost_total, shipment.cost_difference) == (
        23_000,
        23_000,
        PAID_FEE - 23_000,
    )
    assert claim.settled_at is not None
    again = replace(get_yandex_delivery().claim(claim.claim_id), version=claim.version + 1)  # type: ignore[operator]
    services.apply_yandex_claim(order.pk, again, source="callback")
    shipment = shipment_of(order)
    assert shipment.carrier_cost_total == 23_000


def test_a_finished_claim_without_a_price_is_settled_cost_unknown_after_24_hours(staff: StaffUser) -> None:
    order, claim = _accepted(staff)
    snapshot = replace(
        get_yandex_delivery().claim(claim.claim_id),
        status="delivered_finish",
        final_price=None,
        updated_ts=timeutils.now() + timedelta(minutes=1),
    )
    services.apply_yandex_claim(order.pk, snapshot, source="poll")
    claim.refresh_from_db()
    assert claim.ended_at is not None and claim.settled_at is None
    tasks.poll_yandex()  # within 24 h: still waiting for the price
    claim.refresh_from_db()
    assert claim.settled_at is None
    CourierClaim.objects.filter(pk=claim.pk).update(ended_at=timeutils.now() - timedelta(hours=25))
    result = yandex_flow.run_poll()
    assert result.settled_unknown == 1
    claim = CourierClaim.objects.get(pk=claim.pk)
    shipment = shipment_of(order)
    assert claim.settled_at is not None and claim.error_code == "cost_unknown"
    assert shipment.cost_difference == PAID_FEE and shipment.carrier_cost_total == 0


# --- the poll ----------------------------------------------------------------------------------------------


def test_the_poll_reads_every_unsettled_claim_with_bulk_info(staff: StaffUser, calls: dict[str, int]) -> None:
    first, _ = _accepted(staff)
    second, _ = _accepted(staff)
    before = calls.get("claims", 0)
    result = yandex_flow.run_poll()
    assert sorted(result.applied) == sorted([first.pk, second.pk])
    assert calls["claims"] == before + 1  # one bulk_info for both
    assert shipment_of(first).stage == G.COURIER_ASSIGNED  # performer_found
    state = integration_selectors.provider_state("yandex")
    assert state is not None and state.last_success_at is not None and state.consecutive_failures == 0


def test_a_failed_poll_records_the_failure(staff: StaffUser, monkeypatch: pytest.MonkeyPatch) -> None:
    _accepted(staff)

    def down(self: Any, claim_ids: list[str], **kwargs: Any) -> list[Claim]:
        raise YandexUnavailable("down")

    monkeypatch.setattr(fake_yandex.FakeYandexDelivery, "claims", down)
    result = yandex_flow.run_poll()
    assert result.failed
    state = integration_selectors.provider_state("yandex")
    assert state is not None and state.consecutive_failures == 1


def test_the_poll_is_single_flight_and_off_for_a_fake_under_production(
    staff: StaffUser, calls: dict[str, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    _accepted(staff)
    before = calls.get("claims", 0)
    cache.add(yandex_flow.POLL_LOCK_KEY, 1, timeout=60)
    assert yandex_flow.run_poll().applied == []
    cache.delete(yandex_flow.POLL_LOCK_KEY)
    monkeypatch.setattr(integration_modes, "fake_in_production", lambda provider: provider == "yandex")
    assert yandex_flow.run_poll().applied == []
    assert calls.get("claims", 0) == before


def test_no_candidate_writes_no_health(staff: StaffUser) -> None:
    assert yandex_flow.run_poll().polled == 0
    state = integration_selectors.provider_state("yandex")
    assert state is not None and state.last_success_at is None


def test_the_beat_runs_the_poll_every_minute_and_the_retention_daily() -> None:
    from config.celery import app

    assert app.conf.beat_schedule["commerce-poll-yandex"] == {
        "task": "commerce.poll_yandex",
        "schedule": 60.0,
    }
    assert (
        app.conf.beat_schedule["delivery-blank-courier-details"]["task"] == "delivery.blank_courier_details"
    )


# --- the sync handler ----------------------------------------------------------------------------------


def test_sync_claim_re_reads_and_applies(staff: StaffUser) -> None:
    order, claim = _accepted(staff)
    fake_yandex.advance(claim.claim_id, "pickup_arrived")
    message = OutboxMessage.objects.create(
        topic=topics.YANDEX_SYNC_CLAIM,
        payload={"order_id": order.pk, "claim_id": claim.claim_id, "updated_ts": ""},
        dedupe_key="test-sync",
    )
    tasks.sync_yandex_claim(message)
    shipment = shipment_of(order)
    assert shipment.stage == G.COURIER_ARRIVED
    event = ShipmentEvent.objects.filter(shipment=shipment, carrier_status="pickup_arrived").get()
    assert event.via == "webhook"


def test_sync_claim_waits_for_a_lagging_claims_info(staff: StaffUser) -> None:
    order, claim = _accepted(staff)
    ahead = (timeutils.now() + timedelta(minutes=5)).isoformat()
    payload = {"order_id": order.pk, "claim_id": claim.claim_id, "updated_ts": ahead}
    from glowderma.core import outbox

    with pytest.raises(outbox.RetryLater):
        yandex_flow.sync_claim(payload, received_at=timeutils.now())
    # long after the callback: apply what Yandex says (the poll keeps it current anyway)
    assert yandex_flow.sync_claim(payload, received_at=timeutils.now() - timedelta(minutes=10)) is not None


def test_sync_claim_of_an_unknown_claim_does_nothing(staff: StaffUser, calls: dict[str, int]) -> None:
    order = yandex_order()
    before = calls.get("claim", 0)
    assert yandex_flow.sync_claim({"order_id": order.pk, "claim_id": "x" * 32, "updated_ts": ""}) is None
    assert calls.get("claim", 0) == before


# --- courier details retention ---------------------------------------------------------------------------


def test_courier_details_are_blanked_seven_days_after_the_end(staff: StaffUser) -> None:
    order, claim = _accepted(staff)
    _advance(order, claim, "performer_found")
    _advance(order, claim, "delivered_finish")
    claim.refresh_from_db()
    assert claim.courier_name and claim.courier_vehicle
    assert delivery_tasks.blank_courier_details() == 0
    CourierClaim.objects.filter(pk=claim.pk).update(ended_at=timeutils.now() - timedelta(days=8))
    assert delivery_tasks.blank_courier_details() == 1
    claim.refresh_from_db()
    assert (claim.courier_name, claim.courier_vehicle, claim.tracking_url) == ("", "", "")
    assert delivery_tasks.blank_courier_details() == 0


# --- the dev route -----------------------------------------------------------------------------------------


@pytest.fixture
def reload_urls() -> Iterator[None]:
    import config.urls

    yield
    importlib.reload(importlib.import_module("glowderma.commerce.api.admin.urls"))
    importlib.reload(config.urls)
    clear_url_caches()


def test_the_dev_route_drives_the_fake_courier(staff_client: APIClient, staff: StaffUser) -> None:
    order, claim = _accepted(staff)
    url = f"/api/v1/admin/dev/yandex/{order.pk}/advance/"
    response = staff_client.post(url, {"status": "pickuped"}, format="json")
    assert response.status_code == 200, response.json()
    assert response.json()["status"] == "handed_to_courier"
    assert response.json()["delivery"]["stage"] == "in_transit"
    assert staff_client.post(url, {"status": "teleported"}, format="json").status_code == 400
    empty = yandex_order()
    assert (
        staff_client.post(
            f"/api/v1/admin/dev/yandex/{empty.pk}/advance/", {"status": "delivered"}, format="json"
        ).status_code
        == 409
    )


def test_the_dev_route_exists_only_with_dev_features_and_the_fake(reload_urls: None) -> None:
    import config.urls
    from glowderma.commerce.api.dev import yandex as dev_yandex

    assert dev_yandex.enabled()
    with override_settings(DEV_FEATURES_ALLOWED=False):
        assert not dev_yandex.enabled()
        importlib.reload(importlib.import_module("glowderma.commerce.api.admin.urls"))
        importlib.reload(config.urls)
        clear_url_caches()
        from django.urls import NoReverseMatch, reverse

        with pytest.raises(NoReverseMatch):
            reverse("admin-dev-yandex-advance", kwargs={"pk": 1})
    with override_settings(YANDEX_DELIVERY_MODE="live"):
        assert not dev_yandex.enabled()
