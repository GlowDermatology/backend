"""BTS (customer contract §2.13.3, §2.14.3): the webhook (re-track, never trust the payload), the polling
fallback (single flight, round robin, per-parcel failures, run outcome) and ``delivery/regions/``."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from datetime import datetime, timedelta
from typing import Any
from unittest import mock

import pytest
from django.core.cache import cache
from django.test import override_settings
from rest_framework.test import APIClient

from glowderma.commerce import services, tasks
from glowderma.commerce.models import Order, OrderEvent
from glowderma.commerce.tests.factories import OrderFactory
from glowderma.commerce.tests.webhook_support import BTS_URL, real_now, rows
from glowderma.core import timeutils
from glowderma.delivery.models import Shipment, ShipmentEvent
from glowderma.delivery.tests.factories import ShipmentFactory
from glowderma.integrations.bts.fake import FakeBts
from glowderma.integrations.bts.ports import BtsRejected, BtsUnavailable, Parcel
from glowderma.integrations.models import IntegrationState
from glowderma.reporting.selectors import bts_polling_failing

pytestmark = pytest.mark.django_db
SECRET = "local-bts-webhook-secret-at-least-32-characters"  # noqa: S105 - the local default


def parcel(*, status: str = "100", track: str | None = None, minutes_old: int = 60) -> Shipment:
    """A created parcel of a processing order whose last status is ``minutes_old`` old (stale by default)."""
    scenario: dict[str, Any] = {}
    if track:
        scenario["track"] = track
    order = OrderFactory.create(paid=True, metadata={"fake": {"bts": scenario}} if scenario else {})
    old = real_now() - timedelta(minutes=minutes_old)
    shipment = ShipmentFactory.create(
        order=order, created=True, bts_status=status, bts_created_at=old, last_status_at=old
    )
    ShipmentEvent.objects.create(
        shipment=shipment,
        kind=ShipmentEvent.Kind.STATUS_RECEIVED,
        bts_status=status,
        via="polling",
        payload={"tracking_number": shipment.tracking_number, "status": status},
        at=old,
    )
    return shipment


def hook(body: dict[str, Any], *, token: str | None = SECRET, query: str = "") -> Any:
    headers: dict[str, Any] = {"HTTP_X_GD_WEBHOOK_TOKEN": token} if token is not None else {}
    return APIClient().post(f"{BTS_URL}{query}", body, format="json", **headers)


def state() -> IntegrationState:
    return IntegrationState.objects.get(provider="bts")


@pytest.fixture
def tracked() -> Iterator[list[str]]:
    calls: list[str] = []
    real = FakeBts.track

    def spy(self: FakeBts, tracking_number: str) -> Parcel:
        calls.append(tracking_number)
        return real(self, tracking_number)

    with mock.patch.object(FakeBts, "track", spy):
        yield calls


# --- webhook
# ----------------------------------------------------------------------------------------------------


def test_bad_token_answers_401_with_a_bounded_row() -> None:
    shipment = parcel()
    assert hook({"clientId": shipment.order_id}, token="wrong").status_code == 401  # noqa: S106
    response = hook({"clientId": shipment.order_id}, token=None, query="?token=wrong-token-value")
    assert response.status_code == 401 and response.json() == {"ok": False}
    for row in rows("bts"):
        assert row.event_key.startswith("rejected:") and row.raw_body == "" and "?" not in row.path
        assert "X-GD-Webhook-Token" not in row.headers


def test_query_token_is_stripped_from_the_stored_path(tracked: list[str]) -> None:
    shipment = parcel()
    response = hook({"clientId": shipment.order_id}, token=None, query=f"?token={SECRET}")
    assert response.status_code == 200
    (row,) = rows("bts")
    assert (
        row.path == BTS_URL
        and SECRET not in str(row.headers)
        and row.entity_ref == f"order:{shipment.order_id}"
    )


def test_the_retrack_is_authoritative() -> None:
    shipment = parcel()
    Order.objects.filter(pk=shipment.order_id).update(metadata={"fake": {"bts": {"track_status": "200"}}})
    before = state()
    response = hook({"clientId": shipment.order_id, "status": "300", "eventTime": "2026-10-23T16:40:00"})
    assert response.json() == {"ok": True}
    order = Order.objects.get(pk=shipment.order_id)
    assert order.status == "handed_to_courier"
    event = OrderEvent.objects.get(order=order, type="handed_to_courier")
    assert event.data["via"] == "webhook" and event.data["bts_status"] == "200"
    after = state()
    assert (after.last_sync_at, after.consecutive_failures) == (
        before.last_sync_at,
        before.consecutive_failures,
    )
    key = rows("bts")[0].event_key
    assert key == f"{shipment.tracking_number}:300:2026-10-23T16:40:00"
    hook({"clientId": shipment.order_id, "status": "300", "eventTime": "2026-10-23T16:40:00"})
    assert rows("bts")[1].event_key.startswith(f"dup:{key}:")


def test_a_long_event_key_is_hashed_and_its_redelivery_is_a_duplicate() -> None:
    shipment = parcel()
    body = {"clientId": shipment.order_id, "status": "3" * 100, "eventTime": "2026-10-23T16:40:00" + "Z" * 80}
    assert hook(body).status_code == 200
    assert hook(body).status_code == 200  # was a unique violation (HTTP 500) on every retry
    first, second = rows("bts")
    assert first.event_key.startswith("sha256:") and len(first.event_key) <= 120
    assert second.event_key.startswith(f"dup:{first.event_key}:") and len(second.event_key) <= 160
    assert second.result["outcome"] == "duplicate"


def test_a_payload_number_of_another_parcel_is_ignored(
    tracked: list[str], caplog: pytest.LogCaptureFixture
) -> None:
    shipment = parcel()
    with caplog.at_level(logging.WARNING):
        response = hook({"clientId": shipment.order_id, "trackingNumber": "99999999", "status": "300"})
    assert response.status_code == 200 and tracked == [shipment.tracking_number]
    assert "bts.webhook_tracking_mismatch" in caplog.text


def test_unknown_parcel_rejected_and_down(caplog: pytest.LogCaptureFixture) -> None:
    assert hook({"clientId": 10**8}).json() == {"ok": True, "ignored": True}
    rejected = parcel(track="rejected")
    with caplog.at_level(logging.WARNING):
        assert hook({"clientId": rejected.order_id}).json() == {"ok": True, "ignored": True}
    assert "bts.track_rejected" in caplog.text
    assert Order.objects.get(pk=rejected.order_id).status == "processing"
    assert state().consecutive_failures == 0
    down = parcel(track="down")
    count = len(rows("bts"))
    response = hook({"clientId": down.order_id})
    assert response.status_code == 503 and response.json() == {"ok": False}
    assert len(rows("bts")) == count  # nothing stored


def test_a_body_nested_too_deep_is_unparsable() -> None:
    """``json.loads`` raises RecursionError (not ValueError) on ``"[" * 200000``: without the token it is a
    401 with a bounded row, never an HTTP 500 with nothing stored."""
    deep = "[" * 200_000
    response = APIClient().generic("POST", BTS_URL, deep, content_type="application/json")
    assert response.status_code == 401 and response.json() == {"ok": False}
    (row,) = rows("bts")
    assert row.event_key.startswith("rejected:") and row.raw_body == ""
    assert row.result["error_code"] == "bad_token" and len(str(row.payload)) <= 1024
    authenticated = APIClient().generic(
        "POST", BTS_URL, deep, content_type="application/json", HTTP_X_GD_WEBHOOK_TOKEN=SECRET
    )
    assert authenticated.status_code == 200 and authenticated.json() == {"ok": True, "ignored": True}


def test_fake_bts_in_production_answers_401() -> None:
    shipment = parcel()
    with override_settings(DEV_FEATURES_ALLOWED=False, BTS_MODE="fake"):
        assert hook({"clientId": shipment.order_id}).status_code == 401
    assert rows("bts")[0].result["error_code"] == "fake_mode"


# --- polling
# ------------------------------------------------------------------------------------------------------


def test_polling_picks_only_stale_parcels(tracked: list[str]) -> None:
    stale = parcel(minutes_old=60)
    parcel(minutes_old=5)
    assert tasks.poll_bts() == [stale.order_id]
    assert tracked == [stale.tracking_number]
    assert state().last_sync_at is not None and state().data["poll_cursor"] == stale.pk


def test_round_robin_over_250_parcels(tracked: list[str]) -> None:
    shipments = [parcel() for _ in range(250)]
    for _ in range(3):
        tasks.run_bts_poll()
    assert sorted(tracked) == sorted(s.tracking_number for s in shipments)  # all 250, none twice
    assert (
        ShipmentEvent.objects.filter(kind="status_received").count() == 250
    )  # unchanged statuses: nothing new
    tasks.run_bts_poll()  # wraps to the lowest id
    assert tracked[250] == shipments[0].tracking_number


def test_a_volatile_field_appends_no_event() -> None:
    shipment = parcel()

    def volatile(self: FakeBts, tracking_number: str) -> Parcel:
        return Parcel(
            client_id="x", tracking_number=tracking_number, bts_order_id="new", tariff=99, status="100"
        )

    with mock.patch.object(FakeBts, "track", volatile):
        tasks.run_bts_poll()
    assert ShipmentEvent.objects.filter(shipment=shipment, kind="status_received").count() == 1


def test_single_flight_and_the_time_budget(tracked: list[str]) -> None:
    parcel()
    cache.add(tasks.POLL_LOCK_KEY, 1, timeout=60)
    assert tasks.poll_bts() == [] and tracked == []
    cache.delete(tasks.POLL_LOCK_KEY)
    for _ in range(4):
        parcel()
    start = real_now()
    ticks = iter(range(0, 10_000, 200))

    def clock() -> datetime:
        return start + timedelta(seconds=next(ticks))

    with mock.patch.object(timeutils, "now", clock):
        tasks.run_bts_poll()
    assert len(tracked) == 1  # the second parcel would start after 300 s


def test_per_parcel_failures_are_skipped(tracked: list[str], caplog: pytest.LogCaptureFixture) -> None:
    first = parcel()
    down = parcel(track="down")
    rejected = parcel(track="rejected")
    last = parcel()
    with caplog.at_level(logging.WARNING):
        result = tasks.run_bts_poll()
    assert result.tracked == [first.order_id, last.order_id] and (result.rejected, result.unavailable) == (
        1,
        1,
    )
    assert caplog.text.count("bts.poll_parcel_failed") == 2
    assert state().data["poll_cursor"] == last.pk and state().last_sync_at is not None
    assert state().consecutive_failures == 0 and down.pk < rejected.pk


def test_a_parcel_whose_apply_fails_never_holds_the_cursor(
    tracked: list[str], caplog: pytest.LogCaptureFixture
) -> None:
    poison = parcel()
    last = parcel()
    real_apply = services.apply_bts_status

    def apply(order_id: int, *args: Any, **kwargs: Any) -> Any:
        if order_id == poison.order_id:
            raise RuntimeError("cannot apply")
        return real_apply(order_id, *args, **kwargs)

    with mock.patch.object(services, "apply_bts_status", apply), caplog.at_level(logging.ERROR):
        result = tasks.run_bts_poll()
    assert result.tracked == [last.order_id]
    assert "bts.poll_parcel_failed" in caplog.text and "'kind': 'apply'" in caplog.text
    assert state().data["poll_cursor"] == last.pk and state().last_sync_at is not None


def test_a_run_whose_every_apply_fails_is_a_failure(caplog: pytest.LogCaptureFixture) -> None:
    """Every tracked parcel's apply raised: nothing was applied, so the run is no success (no
    ``last_sync_at``) and counts a failure with the ERROR alert, like a run that tracked nothing."""
    first, second = parcel(), parcel()
    before = state()

    def apply(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("cannot apply")

    with mock.patch.object(services, "apply_bts_status", apply), caplog.at_level(logging.ERROR):
        result = tasks.run_bts_poll()
    assert result.tracked == [] and result.apply_failed == 2 and not result.stopped
    after = state()
    assert after.consecutive_failures == 1 and after.last_sync_at == before.last_sync_at
    assert after.data["poll_cursor"] == second.pk and first.pk < second.pk
    assert "bts.poll_nothing_tracked" in caplog.text and "'apply_failed': 2" in caplog.text
    assert bts_polling_failing() is True


def test_three_transport_failures_in_a_row_stop_the_run(tracked: list[str]) -> None:
    downs = [parcel(track="down") for _ in range(3)]
    after = parcel()
    result = tasks.run_bts_poll()
    assert result.stopped and result.tracked == []
    assert state().consecutive_failures == 1 and state().data["poll_cursor"] == downs[-1].pk
    assert tasks.run_bts_poll().tracked == [after.order_id]  # the next run starts after the streak
    assert state().consecutive_failures == 0


def test_a_rejection_resets_the_streak() -> None:
    parcel(track="down")
    parcel(track="down")
    parcel(track="rejected")
    parcel(track="down")
    result = tasks.run_bts_poll()
    assert not result.stopped and (result.rejected, result.unavailable) == (1, 3)


def test_every_parcel_refused_is_a_failure(caplog: pytest.LogCaptureFixture) -> None:
    parcel(track="rejected")
    parcel(track="rejected")
    with caplog.at_level(logging.ERROR):
        tasks.run_bts_poll()
    assert state().last_sync_at is None and state().consecutive_failures == 1
    assert "bts.poll_nothing_tracked" in caplog.text and bts_polling_failing() is True
    parcel()
    tasks.run_bts_poll()  # the cursor wrapped: the refused ones again, then the trackable one
    tasks.run_bts_poll()
    assert bts_polling_failing() is False


def test_refused_and_unavailable_below_the_streak_is_a_failure() -> None:
    parcel(track="rejected")
    parcel(track="down")
    tasks.run_bts_poll()
    assert state().consecutive_failures == 1 and state().last_sync_at is None


def test_no_candidate_writes_nothing() -> None:
    before = state()
    assert tasks.run_bts_poll().tracked == []
    after = state()
    assert (after.last_error_at, after.consecutive_failures, after.data) == (
        before.last_error_at,
        before.consecutive_failures,
        before.data,
    )


def test_fake_bts_in_production_polls_nothing(tracked: list[str]) -> None:
    parcel()
    with override_settings(DEV_FEATURES_ALLOWED=False, BTS_MODE="fake"):
        assert tasks.poll_bts() == []
    assert tracked == []


# --- regions
# ---------------------------------------------------------------------------------------------------


def test_regions_are_cached(public_client: APIClient) -> None:
    calls: list[int] = []
    real = FakeBts.regions

    def spy(self: FakeBts) -> Any:
        calls.append(1)
        return real(self)

    with mock.patch.object(FakeBts, "regions", spy):
        first = public_client.get("/api/v1/delivery/regions/")
        second = public_client.get("/api/v1/delivery/regions/")
    assert first.status_code == 200 and first.json() == second.json() and calls == [1]
    body = first.json()
    assert [region["code"] for region in body["regions"]] == ["10", "11", "12", "13"]
    assert body["regions"][0]["cities"][0] == {"code": "1001", "name": "Chilonzor tumani"}
    assert body["updated_at"] is not None
    assert first.headers["Cache-Control"] in ("public, max-age=60", "private, no-cache")


def test_regions_empty_while_delivery_is_not_offered(public_client: APIClient) -> None:
    with override_settings(DEV_FEATURES_ALLOWED=False, BTS_MODE="fake"):
        assert public_client.get("/api/v1/delivery/regions/").json() == {"regions": [], "updated_at": None}


def test_fake_bts_in_production_makes_nothing_up() -> None:
    with override_settings(DEV_FEATURES_ALLOWED=False, BTS_MODE="fake"):
        bts = FakeBts({"tariff": 1})
        assert bts.regions() == []
        with pytest.raises(BtsRejected):
            bts.quote("10", "1001")
        assert bts.find_by_client_id("1") is None
        with pytest.raises(BtsUnavailable):
            bts.track("1")
