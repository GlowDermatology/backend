"""BTS (customer contract §2.13.3, §2.14.3): the webhook (re-track, never trust the payload), the polling
fallback (single flight, round robin, per-parcel failures, run outcome) and ``delivery/regions/``."""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from datetime import datetime, timedelta
from typing import Any
from unittest import mock

import httpx
import pytest
from django.core.cache import cache
from django.test import override_settings
from rest_framework.test import APIClient

from glowderma.commerce import services, tasks
from glowderma.commerce.models import Order, OrderEvent
from glowderma.commerce.tests.factories import OrderFactory
from glowderma.commerce.tests.webhook_support import BTS_URL, real_now, rows
from glowderma.core import timeutils
from glowderma.core.timeutils import TASHKENT
from glowderma.delivery import selectors as delivery_selectors
from glowderma.delivery import tasks as delivery_tasks
from glowderma.delivery.models import Shipment, ShipmentEvent
from glowderma.delivery.tests.factories import ShipmentFactory
from glowderma.integrations import httpclient
from glowderma.integrations.bts.fake import FakeBts
from glowderma.integrations.bts.ports import BtsRejected, BtsUnavailable, Parcel
from glowderma.integrations.models import IntegrationState
from glowderma.integrations.tests.live_fixtures import Script, reply
from glowderma.reporting.selectors import bts_polling_failing

SINCE = datetime(2026, 10, 25, 12, 0, tzinfo=TASHKENT)  # the shipment row's creation

pytestmark = pytest.mark.django_db
SECRET = "local-bts-webhook-secret-at-least-32-characters"  # noqa: S105 - the local default
OK = {"ok": True}
IGNORED = {"ok": True, "ignored": True}


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
        response = hook({"clientId": shipment.order_id, "orderId": 99999999, "status": "300"})
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
    """``json.loads`` raises RecursionError (not ValueError) on ``"[" * 10000``: without the token it is a
    401 with a bounded row, never an HTTP 500 with nothing stored."""
    deep = "[" * 10_000
    response = APIClient().generic("POST", BTS_URL, deep, content_type="application/json")
    assert response.status_code == 401 and response.json() == {"ok": False}
    (row,) = rows("bts")
    assert row.event_key.startswith("rejected:") and row.raw_body == ""
    assert row.result["error_code"] == "bad_token" and len(str(row.payload)) <= 1024
    authenticated = APIClient().generic(
        "POST", BTS_URL, deep, content_type="application/json", HTTP_X_GD_WEBHOOK_TOKEN=SECRET
    )
    assert authenticated.status_code == 200 and authenticated.json() == {"ok": True, "ignored": True}


def test_an_oversized_body_or_too_many_query_fields_is_a_client_error_not_a_500(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Django's own limits (``DATA_UPLOAD_MAX_MEMORY_SIZE``, ``DATA_UPLOAD_MAX_NUMBER_FIELDS``) reach core's
    handler (413 / 400) instead of the webhook's catch-all, which would answer 500 with an ERROR."""
    with override_settings(DATA_UPLOAD_MAX_MEMORY_SIZE=64):
        big = APIClient().generic("POST", BTS_URL, "x" * 1024, content_type="application/json")
    assert big.status_code == 413
    many = "&".join(f"f{n}=1" for n in range(1200))
    crowded = APIClient().generic("POST", f"{BTS_URL}?{many}", "{}", content_type="application/json")
    assert crowded.status_code == 400
    assert "bts.webhook failed" not in caplog.text and rows("bts") == []


# --- what BTS really sends (A-M2: docs.bts.uz /page/webhook and /page/test-update-status) ---------------


def bts_document(shipment: Shipment, **changes: Any) -> dict[str, Any]:
    document: dict[str, Any] = {
        "clientId": str(shipment.order_id),
        "orderId": int(shipment.tracking_number),
        "barcode": "0199" + shipment.tracking_number,
        "status": {"id": 6, "code": 1200, "name": "Доставлен"},
        "date": "2026-10-26 15:00:00",
    }
    document.update(changes)
    return document


def post_raw(body: bytes | str, content_type: str, *, query: str = f"?token={SECRET}") -> Any:
    return APIClient().generic("POST", f"{BTS_URL}{query}", body, content_type=content_type)


def delivered(shipment: Shipment) -> None:
    Order.objects.filter(pk=shipment.order_id).update(metadata={"fake": {"bts": {"track_status": "300"}}})


def test_flat_form_fields_with_php_bracket_keys_and_the_query_token(tracked: list[str]) -> None:
    shipment = parcel()
    delivered(shipment)
    form = {
        "clientId": str(shipment.order_id),
        "orderId": shipment.tracking_number,
        "barcode": "019914293644",
        "status[id]": "6",
        "status[code]": "1200",
        "status[name]": "Доставлен",
        "date": "2026-10-26 15:00:00",
    }
    response = APIClient().post(f"{BTS_URL}?token={SECRET}", form, format="multipart")
    assert response.status_code == 200 and response.json() == {"ok": True}
    assert tracked == [shipment.tracking_number]
    assert Order.objects.get(pk=shipment.order_id).status == "delivered"
    (row,) = rows("bts")
    assert row.event_key == f"{shipment.tracking_number}:1200:2026-10-26 15:00:00" and "?" not in row.path
    assert SECRET not in str(row.headers) and SECRET not in row.raw_body


@pytest.mark.parametrize("shape", ["named_field", "unnamed_field", "urlencoded_name", "json_string"])
def test_a_form_or_body_holding_the_json_document(tracked: list[str], shape: str) -> None:
    shipment = parcel()
    document = json.dumps(bts_document(shipment, clientId=None))
    if shape == "named_field":
        response = APIClient().post(f"{BTS_URL}?token={SECRET}", {"data": document}, format="multipart")
    elif shape == "unnamed_field":  # curl --form '"{…}"' (no name=): the document is the field's name
        response = post_raw(document.encode(), "application/x-www-form-urlencoded")
    elif shape == "urlencoded_name":
        response = post_raw(json.dumps(document).encode(), "application/x-www-form-urlencoded")
    else:  # a JSON string holding the document
        response = post_raw(json.dumps(document), "application/json")
    assert response.status_code == 200 and response.json() == {"ok": True}
    assert tracked == [shipment.tracking_number]  # found by orderId == our tracking number


def test_status_as_a_json_string_and_an_id_without_code(tracked: list[str]) -> None:
    shipment = parcel()
    form = {
        "clientId": "",
        "orderId": shipment.tracking_number,
        "status": json.dumps({"id": 6, "name": "Доставлен"}),
        "date": "2022-06-20 07:55:01",
    }
    response = post_raw(
        "&".join(f"{key}={value}" for key, value in form.items()), "application/x-www-form-urlencoded"
    )
    assert response.json() == {"ok": True} and tracked == [shipment.tracking_number]
    assert rows("bts")[0].event_key == f"{shipment.tracking_number}:6:2022-06-20 07:55:01"


@pytest.mark.parametrize("client_id", [None, "", "stg-123", "abc"])
def test_identity_falls_back_to_order_id_never_the_barcode(
    tracked: list[str], client_id: object, caplog: pytest.LogCaptureFixture
) -> None:
    shipment = parcel()
    with caplog.at_level(logging.WARNING):
        assert hook(bts_document(shipment, clientId=client_id)).json() == {"ok": True}
    assert tracked == [shipment.tracking_number] and "tracking_mismatch" not in caplog.text
    other = parcel()
    response = hook(bts_document(other, clientId=None, orderId=None, barcode=other.tracking_number))
    assert response.json() == {"ok": True, "ignored": True}  # the barcode is never a lookup key


def test_a_matching_client_id_and_order_id_log_no_mismatch(
    tracked: list[str], caplog: pytest.LogCaptureFixture
) -> None:
    shipment = parcel()
    with caplog.at_level(logging.WARNING):
        assert hook(bts_document(shipment, clientId=shipment.order_id)).json() == {"ok": True}
    assert "bts.webhook_tracking_mismatch" not in caplog.text


def test_the_client_id_carries_the_environment_prefix(tracked: list[str]) -> None:
    shipment = parcel()
    with override_settings(BTS_CLIENT_ID_PREFIX="stg-"):
        assert hook(bts_document(shipment, clientId=f"stg-{shipment.order_id}", orderId=None)).json() == OK
        # another environment's (or the bare) clientId is not ours: nothing to look up without orderId
        assert hook(bts_document(shipment, clientId=str(shipment.order_id), orderId=None)).json() == IGNORED
    assert tracked == [shipment.tracking_number]


def test_a_body_larger_than_the_bound_is_refused_unread() -> None:
    response = post_raw(b"x" * (17 * 1024), "application/json")
    assert response.status_code == 413 and response.json() == {"ok": False}
    assert rows("bts") == []


def test_a_real_notification_over_the_live_adapter(monkeypatch: pytest.MonkeyPatch) -> None:
    """The A-M2 probe: BTS's form-data notification re-tracks over the live adapter and delivers."""
    scripted = Script().on("POST", "/auth/login", reply("bts", "login_ok"))
    scripted.on(
        "GET",
        "/v1/order/track",
        httpx.Response(200, json={"status": True, "data": {"orderId": 4293644, "status": {"code": 1200}}}),
    )
    real = httpclient.new_client
    monkeypatch.setattr(httpclient, "new_client", lambda transport=None: real(scripted.transport))
    order = OrderFactory.create(paid=True)
    old = real_now() - timedelta(minutes=60)
    shipment = ShipmentFactory.create(
        order=order,
        created=True,
        tracking_number="4293644",
        bts_order_id="4293644",
        bts_status="100",
        bts_created_at=old,
        last_status_at=old,
    )
    form = {
        "clientId": str(order.pk),
        "orderId": "4293644",
        "barcode": "019914293644",
        "status[id]": "6",
        "status[code]": "1200",
        "status[name]": "Доставлен",
        "date": "2026-10-26 15:00:00",
    }
    live = {
        "BTS_MODE": "live",
        "BTS_BASE_URL": "https://apitest.bts.uz:28345",
        "BTS_LOGIN": "glowderma",
        "BTS_PASSWORD": "bts-password-value",
    }
    with override_settings(**live):
        response = APIClient().post(f"{BTS_URL}?token={SECRET}", form, format="multipart")
    assert response.json() == {"ok": True}
    assert [r.url.params["orderId"] for r in scripted.to("GET", "/v1/order/track")] == ["4293644"]
    assert Order.objects.get(pk=shipment.order_id).status == "delivered"


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


def test_a_stale_region_list_is_refreshed_once_and_served_while_bts_fails(public_client: APIClient) -> None:
    """R7: single flight, the stale list served while a refresh runs elsewhere or fails, 503 only with no
    list at all; the beat refreshes it before any request has to."""
    calls: list[int] = []
    real = FakeBts.regions
    fail = {"on": False}

    def spy(self: FakeBts) -> Any:
        calls.append(1)
        if fail["on"]:
            raise BtsUnavailable("down")
        return real(self)

    url = "/api/v1/delivery/regions/"
    with mock.patch.object(FakeBts, "regions", spy):
        fail["on"] = True
        assert public_client.get(url).status_code == 503  # nothing cached and BTS down
        fail["on"] = False
        first = public_client.get(url).json()
        later = timeutils.now() + timedelta(hours=25)
        fail["on"] = True
        with mock.patch.object(timeutils, "now", lambda: later):
            stale = public_client.get(url)  # stale: one refresh attempt, BTS down → the stale list
            assert stale.status_code == 200 and stale.json() == first
            cache.add(delivery_selectors.REGIONS_LOCK_KEY, 1, 60)  # another worker is refreshing
            assert public_client.get(url).json() == first
            cache.delete(delivery_selectors.REGIONS_LOCK_KEY)
            fail["on"] = False
            assert delivery_tasks.refresh_bts_regions() == 4
            assert public_client.get(url).json()["updated_at"] != first["updated_at"]
    assert calls == [1, 1, 1, 1]
    cache.clear()
    cache.add(delivery_selectors.REGIONS_LOCK_KEY, 1, 60)
    assert public_client.get(url).status_code == 503  # nothing cached and the fetch is running elsewhere


def test_regions_empty_while_delivery_is_not_offered(public_client: APIClient) -> None:
    with override_settings(DEV_FEATURES_ALLOWED=False, BTS_MODE="fake"):
        assert public_client.get("/api/v1/delivery/regions/").json() == {"regions": [], "updated_at": None}


def test_fake_bts_in_production_makes_nothing_up() -> None:
    with override_settings(DEV_FEATURES_ALLOWED=False, BTS_MODE="fake"):
        bts = FakeBts({"tariff": 1})
        assert bts.regions() == []
        with pytest.raises(BtsRejected):
            bts.quote("10", "1001")
        assert bts.find_by_client_id("1", since=SINCE) is None
        with pytest.raises(BtsUnavailable):
            bts.track("1")
