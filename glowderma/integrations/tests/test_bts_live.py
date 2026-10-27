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
from django.core.cache import cache
from django.test import override_settings

from glowderma.core import timeutils
from glowderma.core.timeutils import TASHKENT
from glowderma.integrations import httpclient, tokens
from glowderma.integrations.bts import live as bts_live
from glowderma.integrations.bts import protocol
from glowderma.integrations.bts.live import LiveBts
from glowderma.integrations.bts.ports import BtsRejected, BtsUnavailable, CancelResult, CreateRequest, Parcel
from glowderma.integrations.bts.registry import get_bts

from .live_fixtures import Script, connect_error, read_timeout, reply

SINCE = datetime(2026, 10, 25, 12, 0, tzinfo=TASHKENT)  # the shipment row's creation
SEPTEMBER = datetime(2026, 9, 27, 9, 0, tzinfo=TASHKENT)

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
    tokens.reset_local()


def bts(
    script: Script, *, pickup_type: str = "courier", sender: protocol.Sender = SENDER, prefix: str = ""
) -> LiveBts:
    script.routes.setdefault(("POST", "/auth/login"), [reply("bts", "login_ok")])
    return LiveBts(
        base_url=BASE + "/",
        login="glowderma",
        password=PASSWORD,
        sender=sender,
        pickup_type=pickup_type,
        weight_g=1000,
        client_prefix=prefix,
        transport=script.transport,
    )


def listing(items: list[dict[str, Any]], **data: Any) -> httpx.Response:
    return httpx.Response(200, json={"status": True, "data": {"items": items, **data}})


def order(order_id: int, client_id: object, code: object = None) -> dict[str, Any]:
    return {"orderId": order_id, "clientId": client_id, "status": {"code": code, "info": "x"}}


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
        cache.clear()  # a quote is cached for minutes
        script = Script().on("POST", "/v1/order-calculate/index", reply("bts", name, status))
        with pytest.raises(BtsRejected) as refused:
            bts(script).quote("13", "1301")
        assert refused.value.error_code == "route_not_served"
    garbled = httpx.Response(200, headers={"Content-Encoding": "gzip"}, stream=httpx.ByteStream(b"no gzip"))
    for responder in (
        httpx.Response(502, json={}),
        httpx.Response(200, json={"status": True, "data": {"courier_to_courier": {"available": True}}}),
        httpx.Response(200, json={"status": True, "data": {}}),
        read_timeout,
        # R1: a body that does not decode, an infinite or absurd price are "unavailable", never a 500
        lambda request: garbled,
        httpx.Response(200, content=b'{"status": true, "data": {"price": 1e400, "available": true}}'),
        httpx.Response(200, json={"status": True, "data": {"price": -5}}),
        httpx.Response(200, json={"status": True, "data": {"price": "12e3"}}),
        httpx.Response(200, json={"status": True, "data": {"price": 10**12}}),
        # R5 / A-S6: answers that say "not processed" → 503 at checkout, never 409 delivery_unavailable
        httpx.Response(429, json={}),
        httpx.Response(403, json={}),
        httpx.Response(302, headers={"Location": "https://elsewhere"}),
        httpx.Response(408, json={}),
    ):
        cache.clear()
        script = Script().on("POST", "/v1/order-calculate/index", responder)
        with pytest.raises(BtsUnavailable):
            bts(script).quote("13", "1301")


def test_a_quote_is_cached_per_direction_and_bounded_for_the_request_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R7: the checkout and delivery/calculate path waits at most REQUEST_PATH_SECONDS, and an identical
    quote (sender city, city, weight, pickup) is served from the cache for minutes."""
    deadlines: list[float] = []
    real = httpclient.Deadline

    def recording(seconds: float = httpclient.TOTAL_TIMEOUT_SECONDS) -> httpclient.Deadline:
        deadlines.append(seconds)
        return real(seconds)

    monkeypatch.setattr(httpclient, "Deadline", recording)
    script = Script().on("POST", "/v1/order-calculate/index", reply("bts", "calculate_single"))
    adapter = bts(script)
    assert adapter.quote("13", "1301") == adapter.quote("13", "1301") == 28_000
    assert len(script.to("POST", "/v1/order-calculate/index")) == 1
    adapter.quote("13", "1302")  # another city: another quote
    assert len(script.to("POST", "/v1/order-calculate/index")) == 2
    assert deadlines == [bts_live.REQUEST_PATH_SECONDS] * 2 and bts_live.REQUEST_PATH_SECONDS <= 4


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
    with pytest.raises(BtsUnavailable, match="refused"):  # R10: remembered, no second login attempt
        bts(script).track("4293644")
    assert len(script.to("POST", "/auth/login")) == 1
    cache.clear()
    script = Script().on("POST", "/v1/order-calculate/index", httpx.Response(401, json={}))
    with caplog.at_level(logging.INFO), pytest.raises(BtsUnavailable, match="HTTP 401"):
        bts(script).quote("13", "1301")
    assert "after a fresh login" in caplog.text
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
        "ready_to_take": True,  # A-S8: every documented example sends it; we create only after payment
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
        # R5 / A-S6: not processed → the outbox retries, never shipment_failed
        (httpx.Response(429, json={"message": "Too many"}), 1),
        (httpx.Response(408, json={}), 1),
        (httpx.Response(425, json={}), 1),
        (httpx.Response(403, json={}), 1),
        (httpx.Response(301, headers={"Location": "https://elsewhere"}), 1),
        (httpx.Response(200, json={"status": True, "data": {"orderId": "--5"}}), 1),
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
            ["2026-11-02"],  # A-S5: a week ahead (the list's date filter is not documented)
            ["50"],
        )
        return reply("bts", "list_page1" if query["page"] == ["1"] else "list_page2")

    script = Script().on("GET", "/v1/order/list", pages)
    adapter = bts(script)
    found = adapter.find_by_client_id("10248", since=SEPTEMBER)
    assert found == Parcel("10248", "4293644", "4293644", None, "100")
    assert adapter.find_by_client_id("10249", since=SEPTEMBER) is None
    script = Script().on("GET", "/v1/order/list", reply("bts", "list_empty"))
    assert bts(script).find_by_client_id("10248", since=SINCE) is None


@pytest.mark.parametrize(
    "responder",
    [httpx.Response(422, json={"status": False}), httpx.Response(500, json={}), read_timeout],
)
def test_a_lookup_that_failed_never_reads_as_absent(responder: object) -> None:
    script = Script().on("GET", "/v1/order/list", responder)  # type: ignore[arg-type]
    with pytest.raises(BtsUnavailable):
        bts(script).find_by_client_id("10248", since=SINCE)


def test_the_lookup_starts_the_day_before_the_shipment_was_created(monkeypatch: pytest.MonkeyPatch) -> None:
    """A-S4 / R3: the window is bounded by the shipment's first create request, not 30 days."""
    monkeypatch.setattr(timeutils, "now", lambda: datetime(2026, 10, 26, 12, 0, tzinfo=timeutils.TASHKENT))
    script = Script().on("GET", "/v1/order/list", reply("bts", "list_empty"))
    since = datetime(2026, 10, 25, 0, 30, tzinfo=timeutils.TASHKENT)
    assert bts(script).find_by_client_id("10248", since=since) is None
    (request,) = script.requests[1:]
    query = parse_qs(request.url.query.decode())
    assert (query["beginDate"], query["endDate"]) == (["2026-10-24"], ["2026-11-02"])


def test_a_full_page_without_meta_follows_the_next_link_or_fails_closed() -> None:
    """A-S3: pagination data missing — follow ``_links.next``; a full page that says nothing is unprovable."""
    page1 = [order(5_000_000 + n, str(90_000 + n)) for n in range(protocol.PER_PAGE)]

    def linked(request: httpx.Request) -> httpx.Response:
        if parse_qs(request.url.query.decode())["page"] == ["1"]:
            return listing(page1, _links={"next": {"href": "/v1/order/list?page=2"}})
        return listing([order(4_293_644, "10248")], _links={"next": None})

    script = Script().on("GET", "/v1/order/list", linked)
    found = bts(script).find_by_client_id("10248", since=SINCE)
    assert found == Parcel("10248", "4293644", "4293644", None, "100")
    assert len(script.to("GET", "/v1/order/list")) == 2
    script = Script().on("GET", "/v1/order/list", listing(page1))
    with pytest.raises(BtsUnavailable, match="without pagination"):
        bts(script).find_by_client_id("10248", since=SINCE)
    script = Script().on("GET", "/v1/order/list", listing(page1[:3]))  # a short page is the last one
    assert bts(script).find_by_client_id("10248", since=SINCE) is None


def test_two_live_parcels_of_one_order_are_an_error(caplog: pytest.LogCaptureFixture) -> None:
    items = [order(4_293_640, "10248", 200), order(4_293_644, 10248), order(4_293_650, "10248", 1300)]
    script = Script().on("GET", "/v1/order/list", listing(items))
    found = bts(script).find_by_client_id("10248", since=SINCE)
    assert found == Parcel("10248", "4293644", "4293644", None, "100")
    assert "bts.duplicate_parcels" in caplog.text and "4293640" in caplog.text


def test_the_client_id_carries_the_environment_prefix() -> None:
    """R18: ``BTS_CLIENT_ID_PREFIX`` tells environments sharing one BTS account apart."""
    script = Script().on("POST", "/v1/order/add", reply("bts", "create_ok"))
    assert bts(script, prefix="stg-").create(REQUEST).client_id == "10248"
    assert body(script.to("POST", "/v1/order/add")[0])["clientId"] == "stg-10248"
    items = [order(4_293_640, "10248"), order(4_293_644, "stg-10248"), order(4_293_650, "prod-10248")]
    script = Script().on("GET", "/v1/order/list", listing(items))
    found = bts(script, prefix="stg-").find_by_client_id("10248", since=SINCE)
    assert found == Parcel("10248", "4293644", "4293644", None, "100")
    script = Script().on("GET", "/v1/order/list", listing(items))
    found = bts(script).find_by_client_id("10248", since=SINCE)
    assert found == Parcel("10248", "4293640", "4293640", None, "100")
    assert protocol.order_ref_of("stg-0", "stg-") is None and protocol.order_ref_of(None, "") is None
    assert protocol.order_ref_of(10248, "") == "10248" and protocol.order_ref_of("stg-12x", "stg-") is None


def test_a_lookup_beyond_the_page_cap_is_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(protocol, "MAX_PAGES", 1)
    script = Script().on("GET", "/v1/order/list", reply("bts", "list_page1"))
    with pytest.raises(BtsUnavailable, match="more than 1 pages"):
        bts(script).find_by_client_id("10248", since=SINCE)


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
        ("--100", "unknown"),  # R1: strict parsing, never a ValueError
        ("²", "unknown"),
        (1200.0, "unknown"),
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
        (httpx.Response(429, json={}), BtsUnavailable, None),
        (httpx.Response(200, json={"status": False, "message": "x"}), BtsRejected, "http_200"),
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
        httpx.Response(429, json={"status": False, "message": "Too many"}),  # R5: never "BTS refused"
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
        assert adapter.find_by_client_id("10248", since=SINCE) is None
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
    BTS_CLIENT_ID_PREFIX="stg-",
    BTS_READY_TO_TAKE=False,
)
def test_the_registry_builds_the_live_adapter_from_the_settings() -> None:
    live = get_bts({"down": True})  # the scenario is ignored by the live adapter
    assert isinstance(live, LiveBts)
    assert (live.base_url, live.login, live.weight_g) == (BASE, "glowderma", 1000)
    assert (live.client_prefix, live.ready_to_take) == ("stg-", False)
    assert live.sender == protocol.Sender("Glow Derma", "+998712000000", "Amir Temur 1", "0101", "")
