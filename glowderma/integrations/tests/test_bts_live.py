"""The live BTS adapter (customer contract §2.14.3; docs/integrations/bts.md): token login, payloads, the
clientId lookup, the status translation and every failure mapping — against the answers docs.bts.uz shows."""

import json
import logging
import threading
import time
from datetime import datetime
from typing import Any
from urllib.parse import parse_qs

import httpx
import pytest
from django.test import override_settings

from glowderma.core import timeutils
from glowderma.integrations import httpclient
from glowderma.integrations.bts import protocol
from glowderma.integrations.bts.live import LiveBts
from glowderma.integrations.bts.ports import BtsRejected, BtsUnavailable, CancelResult, CreateRequest, Parcel
from glowderma.integrations.bts.registry import get_bts

from .live_fixtures import Script, connect_error, read_timeout, reply

BASE = "https://apitest.bts.uz:28345"
PASSWORD = "bts-password-value"  # noqa: S105 - test value
SENDER = protocol.Sender(
    name="Glow Derma", phone="+998712000000", address="Toshkent sh., Amir Temur 1", city_code="0101"
)
REQUEST = CreateRequest(
    client_id="10248",
    attempt=1,
    recipient_name="Malika Karimova",
    recipient_phone="+998901234567",
    address="Chilonzor 9, 12-uy",
    region_code="01",
    city_code="0102",
    comment="3-podyezd",
)


@pytest.fixture(autouse=True)
def fast_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(httpclient, "RETRY_BACKOFF_SECONDS", 0)


def bts(script: Script, *, pickup_type: str = "courier", sender: protocol.Sender = SENDER) -> LiveBts:
    script.routes.setdefault(("POST", "/auth/login"), [reply("bts", "login_ok")])
    return LiveBts(
        base_url=BASE + "/",
        login="glowderma",
        password=PASSWORD,
        sender=sender,
        pickup_type=pickup_type,
        weight_g=1000,
        lookup_days=30,
        transport=script.transport,
    )


def body(request: httpx.Request) -> Any:
    return json.loads(request.content)


def test_every_call_carries_the_token_of_one_login() -> None:
    script = Script().on("POST", "/v1/order-calculate/index", reply("bts", "calculate_multiple"))
    adapter = bts(script)
    assert adapter.quote("10", "1001") == 138_000  # courier_to_courier of the multiple-cost answer
    assert adapter.quote("10", "1002") == 138_000
    login, *calls = script.requests
    assert (login.method, str(login.url)) == ("POST", f"{BASE}/auth/login")
    assert body(login) == {"login": "glowderma", "password": PASSWORD}
    assert "Authorization" not in login.headers and login.headers["language"] == "uz"
    assert len(calls) == 2 and len(script.to("POST", "/auth/login")) == 1
    for call in calls:
        assert call.headers["Authorization"] == "Bearer bts-access-token-1"
        assert call.headers["Accept"] == call.headers["Content-Type"] == "application/json"
    assert body(calls[0]) == {
        "senderCityCode": "0101",
        "receiverCityCode": "1001",
        "pickup_type": "courier",
        "dropoff_type": "courier",
        "is_multiple_cost": 0,
        "weight": 1.0,
    }


def test_a_quote_reads_the_single_answer_and_maps_refusals() -> None:
    script = Script().on("POST", "/v1/order-calculate/index", reply("bts", "calculate_single"))
    assert bts(script).quote("13", "1301") == 28_000
    script = Script().on("POST", "/v1/order-calculate/index", reply("bts", "calculate_multiple"))
    assert bts(script, pickup_type="branch").quote("13", "1301") == 83_000  # branch_to_courier
    for name, status in (("calculate_unavailable", 200), ("calculate_422", 422)):
        script = Script().on("POST", "/v1/order-calculate/index", reply("bts", name, status))
        with pytest.raises(BtsRejected) as refused:
            bts(script).quote("13", "1301")
        assert refused.value.error_code == "route_not_served"
    for responder in (
        httpx.Response(502, json={}),
        httpx.Response(200, json={"status": True, "data": {"courier_to_courier": {"available": True}}}),
        httpx.Response(200, json={"status": True, "data": {}}),
        read_timeout,
    ):
        script = Script().on("POST", "/v1/order-calculate/index", responder)
        with pytest.raises(BtsUnavailable):
            bts(script).quote("13", "1301")


def test_a_refused_token_is_renewed_once() -> None:
    script = Script().on(
        "POST",
        "/v1/order-calculate/index",
        httpx.Response(401, json={"name": "Unauthorized"}),
        reply("bts", "calculate_single"),
    )
    script.on(
        "POST",
        "/auth/login",
        reply("bts", "login_ok"),
        httpx.Response(200, json={"status": True, "data": {"access_token": "bts-access-token-2"}}),
    )
    assert bts(script).quote("13", "1301") == 28_000
    first, second = script.to("POST", "/v1/order-calculate/index")
    assert first.headers["Authorization"] == "Bearer bts-access-token-1"
    assert second.headers["Authorization"] == "Bearer bts-access-token-2"
    assert len(script.to("POST", "/auth/login")) == 2


def test_refused_credentials_are_an_error_log_and_unavailable(caplog: pytest.LogCaptureFixture) -> None:
    script = Script().on("POST", "/auth/login", reply("bts", "login_unauthorized", 401))
    with caplog.at_level(logging.INFO), pytest.raises(BtsUnavailable):
        bts(script).quote("13", "1301")
    assert any(r.levelno == logging.ERROR and "bts.auth_failed" in r.getMessage() for r in caplog.records)
    script = Script().on("POST", "/v1/order-calculate/index", httpx.Response(401, json={}))
    with caplog.at_level(logging.INFO), pytest.raises(BtsUnavailable, match="fresh token"):
        bts(script).quote("13", "1301")
    assert PASSWORD not in caplog.text and "bts-access-token" not in caplog.text
    script = Script().on("POST", "/auth/login", connect_error)
    with pytest.raises(BtsUnavailable):
        bts(script).quote("13", "1301")


def test_concurrent_calls_share_one_login() -> None:
    def slow_login(request: httpx.Request) -> httpx.Response:
        time.sleep(0.1)
        return reply("bts", "login_ok")

    script = Script().on("POST", "/auth/login", slow_login)
    script.on("POST", "/v1/order-calculate/index", reply("bts", "calculate_single"))
    adapter = bts(script)
    start = threading.Event()
    prices: list[int] = []

    def worker() -> None:
        start.wait(10)
        prices.append(adapter.quote("13", "1301"))

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for thread in threads:
        thread.start()
    start.set()
    for thread in threads:
        thread.join(10)
    assert prices == [28_000] * 6
    assert len(script.to("POST", "/auth/login")) == 1


def test_regions_follow_every_page_of_every_city_list() -> None:
    def cities(request: httpx.Request) -> httpx.Response:
        query = parse_qs(request.url.query.decode())
        assert query["per-page"] == ["50"]
        if query["regionCode"] == ["01"]:
            return reply("bts", "cities_01")
        return reply("bts", "cities_10_page1" if query["page"] == ["1"] else "cities_10_page2")

    script = Script().on("GET", "/v1/directory/regions", reply("bts", "regions"))
    script.on("GET", "/v1/directory/cities", cities)
    regions = bts(script).regions()
    assert [(r.code, r.name, [c.code for c in r.cities]) for r in regions] == [
        ("01", "город Ташкент", ["0101", "0102"]),
        ("10", "Ташкентская область", ["1001", "1002"]),
    ]
    assert len(script.to("GET", "/v1/directory/cities")) == 3


@pytest.mark.parametrize(
    "responder",
    [
        httpx.Response(200, json={"status": True, "data": {"items": [], "_meta": {}}}),
        httpx.Response(403, json={"status": False, "message": "forbidden"}),
        httpx.Response(500, json={}),
        httpx.Response(200, json={"status": True, "data": {}}),
        connect_error,
    ],
)
def test_a_region_list_that_failed_is_unavailable_never_empty(responder: object) -> None:
    script = Script().on("GET", "/v1/directory/regions", responder)  # type: ignore[arg-type]
    with pytest.raises(BtsUnavailable):
        bts(script).regions()


def test_a_create_sends_our_parcel_and_reads_the_waybill_number() -> None:
    script = Script().on("POST", "/v1/order/add", reply("bts", "create_ok"))
    parcel = bts(script).create(REQUEST)
    assert parcel == Parcel(
        client_id="10248", tracking_number="4293644", bts_order_id="4293644", tariff=None, status="100"
    )
    (create,) = script.to("POST", "/v1/order/add")
    assert body(create) == {
        "clientId": "10248",
        "pickup_type": "courier",
        "dropoff_type": "courier",
        "is_sender_location": False,
        "is_receiver_location": False,
        "sender": {
            "name": "Glow Derma",
            "phone": "+998712000000",
            "address": "Toshkent sh., Amir Temur 1",
            "city_code": "0101",
        },
        "receiver": {
            "name": "Malika Karimova",
            "phone": "+998901234567",
            "address": "Chilonzor 9, 12-uy (3-podyezd)",
            "city_code": "0102",
        },
        "cargo": {"weight": 1.0, "piece": 1},
    }


def test_branch_pickup_names_the_branch() -> None:
    script = Script().on("POST", "/v1/order/add", reply("bts", "create_ok"))
    sender = protocol.Sender("Glow Derma", "+998712000000", "Amir Temur 1", "0101", branch_code="0124")
    bts(script, pickup_type="branch", sender=sender).create(REQUEST)
    sent = body(script.to("POST", "/v1/order/add")[0])
    assert sent["pickup_type"] == "branch" and sent["sender"]["branch_code"] == "0124"


def test_a_refused_create_is_a_validation_error_without_the_phone() -> None:
    script = Script().on("POST", "/v1/order/add", reply("bts", "create_422", 422))
    with pytest.raises(BtsRejected) as refused:
        bts(script).create(REQUEST)
    assert refused.value.error_code == "validation_error"
    assert "receiverCityId" in refused.value.message and "998901234567" not in refused.value.message


@pytest.mark.parametrize(
    ("responder", "sends"),
    [
        (
            reply("bts", "create_500", 500),
            1,
        ),  # BTS answers some data errors with 500 (verify M3): retried later
        (httpx.Response(200, json={"status": True, "data": {"clientId": "10248"}}), 1),
        (read_timeout, 1),
        (connect_error, httpclient.RETRY_ATTEMPTS),
    ],
)
def test_a_create_without_a_usable_answer_is_unavailable_and_never_repeated(
    responder: object, sends: int
) -> None:
    script = Script().on("POST", "/v1/order/add", responder)  # type: ignore[arg-type]
    with pytest.raises(BtsUnavailable):
        bts(script).create(REQUEST)
    assert len(script.to("POST", "/v1/order/add")) == sends


def test_the_lookup_scans_the_window_and_skips_deleted_parcels(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(timeutils, "now", lambda: datetime(2026, 10, 26, 23, 30, tzinfo=timeutils.TASHKENT))

    def pages(request: httpx.Request) -> httpx.Response:
        query = parse_qs(request.url.query.decode())
        assert (query["beginDate"], query["endDate"], query["per-page"]) == (
            ["2026-09-26"],
            ["2026-10-27"],
            ["50"],
        )
        return reply("bts", "list_page1" if query["page"] == ["1"] else "list_page2")

    script = Script().on("GET", "/v1/order/list", pages)
    adapter = bts(script)
    assert adapter.find_by_client_id("10248") == Parcel("10248", "4293644", "4293644", None, "100")
    assert adapter.find_by_client_id("10249") is None
    script = Script().on("GET", "/v1/order/list", reply("bts", "list_empty"))
    assert bts(script).find_by_client_id("10248") is None


@pytest.mark.parametrize(
    "responder",
    [httpx.Response(422, json={"status": False}), httpx.Response(500, json={}), read_timeout],
)
def test_a_lookup_that_failed_never_reads_as_absent(responder: object) -> None:
    script = Script().on("GET", "/v1/order/list", responder)  # type: ignore[arg-type]
    with pytest.raises(BtsUnavailable):
        bts(script).find_by_client_id("10248")


def test_a_lookup_beyond_the_page_cap_is_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(protocol, "MAX_PAGES", 1)
    script = Script().on("GET", "/v1/order/list", reply("bts", "list_page1"))
    with pytest.raises(BtsUnavailable, match="more than 1 pages"):
        bts(script).find_by_client_id("10248")


def test_track_asks_by_waybill_number_and_translates_the_status() -> None:
    script = Script().on("GET", "/v1/order/track", reply("bts", "track_ok"))
    parcel = bts(script).track("4293644")
    assert parcel == Parcel("", "4293644", "4293644", None, "200")  # 1100 courier delivering → handed
    (request,) = script.to("GET", "/v1/order/track")
    assert parse_qs(request.url.query.decode()) == {"orderId": ["4293644"]}


@pytest.mark.parametrize(
    ("code", "port"),
    [
        (100, "100"),
        (None, "100"),
        ("", "100"),
        (200, "200"),
        (300, "200"),  # in the sending office: never "delivered"
        ("800", "200"),
        (1100, "200"),
        (1200, "300"),
        (1300, "1300"),
        (1400, "1400"),
        (900, "900"),
        ("abc", "unknown"),
        (True, "unknown"),
    ],
)
def test_bts_codes_translate_to_the_port_vocabulary(code: object, port: str) -> None:
    assert protocol.port_status({"code": code, "name": "x"}) == port


@pytest.mark.parametrize(
    ("responder", "error", "code"),
    [
        (reply("bts", "track_404", 404), BtsRejected, "parcel_not_found"),
        (httpx.Response(400, json={"status": False}), BtsRejected, "http_400"),
        (httpx.Response(503, json={}), BtsUnavailable, None),
        (httpx.Response(200, json={"status": True, "data": {"orderId": 1}}), BtsUnavailable, None),
        (read_timeout, BtsUnavailable, None),
    ],
)
def test_track_failures(responder: object, error: type[Exception], code: str | None) -> None:
    script = Script().on("GET", "/v1/order/track", responder)  # type: ignore[arg-type]
    with pytest.raises(error) as raised:
        bts(script).track("4293644")
    if code:
        assert isinstance(raised.value, BtsRejected) and raised.value.error_code == code


def test_track_of_a_number_bts_cannot_know_sends_nothing() -> None:
    script = Script()
    with pytest.raises(BtsRejected) as refused:
        bts(script).track("BTS-10248")
    assert refused.value.error_code == "parcel_not_found" and script.requests == []


PARCEL = Parcel("10248", "4293644", "4293644", None, "100")


@pytest.mark.parametrize(
    ("responder", "result"),
    [
        (reply("bts", "cancel_ok"), CancelResult(ok=True, status="1300")),
        (reply("bts", "cancel_already", 400), CancelResult(ok=True, status="1300")),
        (
            reply("bts", "cancel_refused", 400),
            CancelResult(ok=False, status="100", reason="error.status_not_allowed"),
        ),
        (
            httpx.Response(404, json={"status": False, "message": "Ресурс не найден."}),
            CancelResult(False, "100", "Ресурс не найден."),
        ),
    ],
)
def test_cancel_outcomes(responder: httpx.Response, result: CancelResult) -> None:
    script = Script().on("GET", "/v1/order-cancel/index", responder)
    assert bts(script).cancel(PARCEL) == result
    (request,) = script.to("GET", "/v1/order-cancel/index")
    assert parse_qs(request.url.query.decode()) == {"orderId": ["4293644"]}


def test_cancel_failures_are_unavailable() -> None:
    for responder in (
        httpx.Response(502, json={}),
        httpx.Response(200, text="oops"),
        httpx.Response(404, text="<html>proxy</html>"),
        read_timeout,
    ):
        script = Script().on("GET", "/v1/order-cancel/index", responder)
        with pytest.raises(BtsUnavailable):
            bts(script).cancel(PARCEL)
    script = Script()
    result = bts(script).cancel(Parcel("10248", "", "", None, ""))
    assert result.ok is False and script.requests == []


def test_no_personal_data_or_secret_reaches_the_logs(caplog: pytest.LogCaptureFixture) -> None:
    script = Script().on("GET", "/v1/order/list", reply("bts", "list_empty"))
    script.on("POST", "/v1/order/add", reply("bts", "create_422", 422))
    with caplog.at_level(logging.DEBUG):
        adapter = bts(script)
        assert adapter.find_by_client_id("10248") is None
        with pytest.raises(BtsRejected):
            adapter.create(REQUEST)
    for secret in (PASSWORD, "bts-access-token-1", "998901234567", "Malika", "Chilonzor", "Amir Temur"):
        assert secret not in caplog.text


@override_settings(
    BTS_MODE="live",
    BTS_BASE_URL=BASE,
    BTS_LOGIN="glowderma",
    BTS_PASSWORD=PASSWORD,
    BTS_SENDER_NAME="Glow Derma",
    BTS_SENDER_PHONE="+998712000000",
    BTS_SENDER_ADDRESS="Amir Temur 1",
    BTS_SENDER_CITY_CODE="0101",
    BTS_PICKUP_TYPE="courier",
    BTS_CLIENT_LOOKUP_DAYS=14,
)
def test_the_registry_builds_the_live_adapter_from_the_settings() -> None:
    live = get_bts({"down": True})  # the scenario is ignored by the live adapter
    assert isinstance(live, LiveBts)
    assert (live.base_url, live.login, live.lookup_days, live.weight_g) == (BASE, "glowderma", 14, 1000)
    assert live.sender == protocol.Sender("Glow Derma", "+998712000000", "Amir Temur 1", "0101", "")
