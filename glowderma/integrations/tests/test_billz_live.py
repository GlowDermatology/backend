"""The live Billz adapter against recorded answers (``billz_support.BillzServer``; no network).

Auth and token rotation, the shared rate limiter, retries and the error mapping (``client``), the catalog
sync (pagination, shop mapping, watermark, reconciliation guards) and the idempotent sale push (``live``).
"""

from __future__ import annotations

import json
import logging
import threading
import time
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from django.core.cache import cache
from django.test import override_settings

from glowderma.core import timeutils
from glowderma.core.pii import pii_ref
from glowderma.integrations import selectors, services
from glowderma.integrations.billz import client as billz_client
from glowderma.integrations.billz import live as billz_live
from glowderma.integrations.billz.client import (
    BillzAuthError,
    BillzClient,
    BillzConfig,
    RateLimiter,
    classify,
    retry_after_seconds,
)
from glowderma.integrations.billz.fake import FakeBillz
from glowderma.integrations.billz.live import LiveBillz, is_finished, sale_ref
from glowderma.integrations.billz.ports import (
    BillzListingIncomplete,
    BillzNotSupported,
    BillzProduct,
    BillzRejected,
    BillzUnavailable,
    SaleLine,
    SaleRequest,
)
from glowderma.integrations.billz.registry import get_billz
from glowderma.integrations.models import IntegrationState
from glowderma.integrations.tests.billz_support import (
    CASHBOX_ID,
    CLIENT_ID,
    FOAM_ID,
    NEW_CLIENT_ID,
    PAYMENT_TYPE_ID,
    PLATFORM_ID,
    SALE_ID,
    SECRET,
    SERUM_ID,
    SHOP_ID,
    BillzServer,
    Clock,
    Sale,
    WorkerCrash,
    config,
    fixture,
    live,
)

ORDER_UUID = "0d6f6c1e-5a7b-4c1e-9a43-2f1d8e3b7a10"
PHONE = "+998901234567"


def tokens_in_cache() -> dict[str, Any]:
    value = cache.get(billz_client.TOKENS_KEY)
    assert isinstance(value, dict)
    return value


def products_call(adapter: LiveBillz) -> dict[str, Any]:
    return adapter.client.call("GET", "/v2/products", label="GET /v2/products", params={}, idempotent=True)


# --- auth ---------------------------------------------------------------------------------------------------


def test_first_call_logs_in_and_keeps_the_pair_only_in_the_cache(caplog: pytest.LogCaptureFixture) -> None:
    server = BillzServer()
    adapter = live(server)
    with caplog.at_level(logging.DEBUG):
        products_call(adapter)
        products_call(adapter)  # the cached access token is reused
    assert server.labels() == ["POST /v1/auth/login", "GET /v2/products", "GET /v2/products"]
    login = server.requests[0]
    assert json.loads(login.content) == {"secret_token": SECRET}
    assert "platform-id" not in login.headers  # only the refresh needs it (openapi.yaml info)
    assert "Authorization" not in login.headers
    assert all(r.headers["Authorization"] == "Bearer access-from-login" for r in server.requests[1:])
    stored = tokens_in_cache()
    assert (stored["access"], stored["refresh"]) == ("access-from-login", "refresh-from-login")
    # never logged, never in a URL
    for secret in (SECRET, "access-from-login", "refresh-from-login"):
        assert secret not in caplog.text
        assert all(secret not in str(r.url) for r in server.requests)
    assert "secret_key" not in repr(adapter.config) and SECRET not in repr(adapter.config)


@pytest.mark.django_db
def test_tokens_never_reach_the_database() -> None:
    server = BillzServer()
    products_call(live(server))
    for state in IntegrationState.objects.all():
        assert "access-from-login" not in json.dumps(state.data) and "refresh" not in json.dumps(state.data)


def test_expiring_access_token_is_refreshed_with_rotation() -> None:
    clock = Clock()
    server = BillzServer(clock)
    adapter = live(server, clock)
    products_call(adapter)
    clock.now += 1_296_000 - 200  # inside the 5-minute renewal margin of the 15-day token
    products_call(adapter)
    assert server.labels() == [
        "POST /v1/auth/login",
        "GET /v2/products",
        "POST /v2/auth/refresh",
        "GET /v2/products",
    ]
    refresh = server.requests[2]
    assert json.loads(refresh.content) == {"refresh_token": "refresh-from-login"}
    assert refresh.headers["platform-id"] == PLATFORM_ID
    assert server.requests[3].headers["Authorization"] == "Bearer access-from-refresh"
    stored = tokens_in_cache()
    assert stored["refresh"] == "refresh-from-refresh"  # rotated: the spent token is never presented again
    assert stored["access_expires_at"] == pytest.approx(clock.now + 86_400)
    assert stored["refresh_expires_at"] == pytest.approx(clock.now + 2_592_000)
    clock.now += 86_400
    products_call(adapter)
    assert json.loads(server.requests[-2].content) == {"refresh_token": "refresh-from-refresh"}


def test_refused_refresh_falls_back_to_one_login(caplog: pytest.LogCaptureFixture) -> None:
    clock = Clock()
    server = BillzServer(clock)
    adapter = live(server, clock)
    products_call(adapter)
    server.overrides["POST /v2/auth/refresh"] = lambda request: httpx.Response(
        400, json=fixture("refresh_reused")
    )
    clock.now += 1_296_000
    with caplog.at_level(logging.WARNING):
        products_call(adapter)
    assert server.labels()[2:] == ["POST /v2/auth/refresh", "POST /v1/auth/login", "GET /v2/products"]
    assert "invalid_grant" in caplog.text and "refresh-from-login" not in caplog.text


def test_expired_refresh_token_logs_in_without_trying_it() -> None:
    clock = Clock()
    server = BillzServer(clock)
    adapter = live(server, clock)
    products_call(adapter)
    clock.now += 31 * 86_400
    products_call(adapter)
    assert server.labels()[2:] == ["POST /v1/auth/login", "GET /v2/products"]


def test_401_renews_once_and_repeats_the_call_once() -> None:
    server = BillzServer()
    adapter = live(server)
    products_call(adapter)
    server.access_tokens = {"access-from-refresh"}  # Billz revoked the login token
    products_call(adapter)
    assert server.labels()[1:] == [
        "GET /v2/products",
        "GET /v2/products",
        "POST /v2/auth/refresh",
        "GET /v2/products",
    ]


def test_a_second_401_is_unavailable_never_a_loop() -> None:
    server = BillzServer()
    adapter = live(server)
    server.access_tokens = set()
    with pytest.raises(BillzUnavailable, match="freshly renewed token"):
        products_call(adapter)
    assert server.labels() == [
        "POST /v1/auth/login",
        "GET /v2/products",
        "POST /v2/auth/refresh",
        "GET /v2/products",
    ]


def test_refused_key_is_an_auth_error() -> None:
    server = BillzServer()
    server.overrides["POST /v1/auth/login"] = lambda request: httpx.Response(
        403, json=fixture("login_forbidden")
    )
    with pytest.raises(BillzAuthError, match="refused the integration key"):
        products_call(live(server))
    assert issubclass(BillzAuthError, BillzUnavailable)
    with pytest.raises(BillzAuthError, match="BILLZ_SECRET_KEY"):
        products_call(live(BillzServer(), secret_key=""))


def test_single_flight_refresh_under_thread_contention() -> None:
    """Eight workers find the access token expired at once: exactly one refresh reaches Billz, the others
    wait for the cache and use the pair it stored (no reuse of the spent refresh token)."""
    server = BillzServer()
    refreshes: list[str] = []
    entered = threading.Event()

    def slow_refresh(request: httpx.Request) -> httpx.Response:
        refreshes.append(json.loads(request.content)["refresh_token"])
        entered.set()
        time.sleep(0.3)
        return httpx.Response(200, json=fixture("refresh_ok"))

    server.overrides["POST /v2/auth/refresh"] = slow_refresh
    cache.set(
        billz_client.TOKENS_KEY,
        {
            "access": "access-from-login",
            "access_expires_at": time.time() - 1,
            "refresh": "refresh-from-login",
            "refresh_expires_at": time.time() + 3600,
        },
        timeout=3600,
    )
    tokens: list[str] = []
    errors: list[BaseException] = []

    def worker() -> None:
        try:
            tokens.append(BillzClient(config(), transport=server.transport).access_token())
        except BaseException as exc:  # pragma: no cover - surfaced by the assertion below
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)
    assert entered.is_set() and not errors
    assert refreshes == ["refresh-from-login"]
    assert server.count("POST /v1/auth/login") == 0
    assert tokens == ["access-from-refresh"] * 8
    assert cache.get(billz_client.LOCK_KEY) is None


# --- rate limiter, 429, retries -----------------------------------------------------------------------------


def test_rate_limiter_spaces_requests_across_two_clients() -> None:
    """Two clients (two workers) share one limiter through the cache: request starts are ≥ 1/rate apart."""
    clock = Clock()
    server = BillzServer(clock)
    first = live(server, clock, rate_limit_per_second=2.0)
    second = live(server, clock, rate_limit_per_second=2.0)
    for _ in range(3):
        products_call(first)
        products_call(second)
    starts = server.times
    assert len(starts) == 7  # one login + six calls, every one through the limiter
    assert all(later - earlier >= 0.5 - 1e-9 for earlier, later in zip(starts, starts[1:], strict=False))
    assert starts[-1] - starts[0] == pytest.approx(3.0)


def test_rate_limiter_holds_across_threads_with_the_real_clock() -> None:
    limiter_rate = 20.0
    server = BillzServer()
    starts: list[float] = []
    lock = threading.Lock()

    def worker() -> None:
        limiter = RateLimiter(limiter_rate)
        for _ in range(4):
            limiter.acquire()
            with lock:
                starts.append(time.time())

    threads = [threading.Thread(target=worker) for _ in range(3)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)
    starts.sort()
    gaps = [later - earlier for earlier, later in zip(starts, starts[1:], strict=False)]
    assert len(starts) == 12 and not server.requests
    assert min(gaps) >= 0.8 / limiter_rate  # one slot apart (minus scheduler wake-up jitter)


def test_rate_limiter_refuses_to_queue_beyond_its_wait() -> None:
    clock = Clock()
    limiter = RateLimiter(1.0, clock=clock, sleep=clock.sleep, max_wait=2.0)
    limiter.acquire()
    limiter.acquire()
    clock.now -= 2.0  # both future slots are taken from the point of view of a third caller
    with pytest.raises(BillzUnavailable, match="slot"):
        limiter.acquire()
    with pytest.raises(ValueError, match="positive"):
        RateLimiter(0)


def test_429_honours_retry_after_and_pauses_every_worker() -> None:
    clock = Clock()
    server = BillzServer(clock)
    first = live(server, clock)
    second = live(server, clock)
    products_call(first)
    answers = iter([httpx.Response(429, headers={"Retry-After": "3"}, json=fixture("rate_limited"))])
    server.overrides["GET /v2/products"] = lambda request: next(
        answers, httpx.Response(200, json=fixture("products_page1"))
    )
    throttled_at = clock.now
    products_call(first)
    retried_at = server.times[-1]
    assert retried_at - throttled_at >= 3.0
    assert cache.get(f"{billz_client.RATE_PREFIX}:pause") == pytest.approx(throttled_at + 3.0, abs=0.01)
    products_call(second)
    assert server.count("GET /v2/products") == 4


def test_429_without_retry_after_backs_off_and_gives_up() -> None:
    clock = Clock()
    server = BillzServer(clock)
    adapter = live(server, clock)
    products_call(adapter)
    server.overrides["GET /v2/products"] = lambda request: httpx.Response(429, json=fixture("rate_limited"))
    with pytest.raises(BillzUnavailable, match="429"):
        products_call(adapter)
    assert server.count("GET /v2/products") == 1 + billz_client.MAX_THROTTLED + 1


def test_retry_after_parsing() -> None:
    now = datetime(2026, 10, 26, 12, 0, tzinfo=UTC).timestamp()
    response = httpx.Response(429, headers={"Retry-After": "Mon, 26 Oct 2026 12:00:05 GMT"})
    assert retry_after_seconds(response, now=now) == pytest.approx(5.0)
    assert retry_after_seconds(httpx.Response(429, headers={"Retry-After": "600"}), now=now) == 60.0
    assert retry_after_seconds(httpx.Response(429, headers={"Retry-After": "soon"}), now=now) is None
    assert retry_after_seconds(httpx.Response(429), now=now) is None


def test_5xx_on_a_read_is_retried_with_jittered_backoff_then_unavailable() -> None:
    clock = Clock()
    server = BillzServer(clock)
    adapter = live(server, clock)
    products_call(adapter)
    server.overrides["GET /v2/products"] = lambda request: httpx.Response(500, json=fixture("internal_error"))
    clock.sleeps.clear()
    with pytest.raises(BillzUnavailable, match="HTTP 500"):
        products_call(adapter)
    assert server.count("GET /v2/products") == 1 + billz_client.MAX_ATTEMPTS
    backoffs = [s for s in clock.sleeps if s >= 0.3]  # the limiter's own waits are slot-sized (≤ 1 ms here)
    assert backoffs == [0.375, 0.75, 1.5]  # base·2^(n-1)·(0.5 + jitter/2) with jitter 0.5


def test_a_write_with_an_unknown_outcome_is_never_repeated_blindly() -> None:
    server = BillzServer()
    adapter = live(server, Clock())
    products_call(adapter)
    server.overrides["POST /v2/order"] = lambda request: httpx.Response(502, json=fixture("internal_error"))
    with pytest.raises(BillzUnavailable, match="HTTP 502"):
        adapter._create_order()

    def read_timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    server.overrides["POST /v2/order"] = read_timeout
    with pytest.raises(BillzUnavailable, match="ReadTimeout"):
        adapter._create_order()
    assert server.count("POST /v2/order") == 2  # one each: Billz may have created the sale

    refused = [True]

    def connect_then_ok(request: httpx.Request) -> httpx.Response:
        if refused.pop() if refused else False:
            raise httpx.ConnectError("refused", request=request)
        return httpx.Response(200, json=fixture("order_created"))

    server.overrides["POST /v2/order"] = connect_then_ok
    assert adapter._create_order() == (SALE_ID, "5632631379")  # never reached Billz: safe to resend


def test_async_answers_without_the_result_or_timed_out_are_unavailable() -> None:
    server = BillzServer()
    adapter = live(server)
    products_call(adapter)
    server.overrides["POST /v2/order"] = lambda request: httpx.Response(
        200, json=fixture("async_accepted_only")
    )
    with pytest.raises(BillzUnavailable, match="correlation id only"):
        adapter._create_order()
    server.overrides["POST /v2/order"] = lambda request: httpx.Response(408, json=fixture("async_timeout"))
    with pytest.raises(BillzUnavailable, match="408 TIMEOUT"):
        adapter._create_order()
    assert server.requests[-1].headers["Billz-Response-Channel"] == "HTTP"


def _payment_error(code: int) -> dict[str, Any]:
    body = fixture("payment_error")
    body["data"]["error_code"] = code
    return body


@pytest.mark.parametrize(
    ("status", "body", "kind", "text"),
    [
        (400, _payment_error(20084), BillzUnavailable, "not calculated"),
        (400, _payment_error(20020), BillzUnavailable, "do not match"),
        (400, _payment_error(20015), BillzUnavailable, "no items"),
        (400, _payment_error(20010), BillzRejected, "shift closed"),
        (400, _payment_error(20082), BillzRejected, "cash box is closed"),
        (400, _payment_error(20033), BillzRejected, "no open shift"),
        (400, _payment_error(20016), BillzRejected, "not enough stock"),
        (400, _payment_error(20017), BillzRejected, "deleted"),
        (400, _payment_error(20036), BillzRejected, "product not found"),
        (400, _payment_error(20037), BillzRejected, "archived"),
        (400, _payment_error(20063), BillzRejected, "OFD"),
        (400, _payment_error(20009), BillzRejected, "another shop"),
        (400, _payment_error(20999), BillzRejected, "payment is possible only"),  # unknown → permanent
        (403, fixture("forbidden"), BillzRejected, "may not call this route (access denied)"),
        (404, {"code": 404, "message": "not found", "error": "", "data": None}, BillzRejected, "not found"),
        (400, {"error": {"code": "BAD_REQUEST", "message": "shop_id is required"}}, BillzRejected, "shop_id"),
        (
            422,
            {"code": 422, "message": "invalid", "error": "invalid", "data": None},
            BillzRejected,
            "invalid",
        ),
        (408, fixture("async_timeout"), BillzUnavailable, "TIMEOUT"),
        (503, {}, BillzUnavailable, "HTTP 503"),
    ],
)
def test_error_mapping(status: int, body: dict[str, Any], kind: type[Exception], text: str) -> None:
    error = classify("POST /v2/order-payment/{order_id}", status, body)
    assert type(error) is kind and text in str(error)
    if isinstance(error, BillzRejected) and "data" in body and isinstance(body["data"], dict):
        assert error.error_code == body["data"].get("error_code")


# --- catalog sync -------------------------------------------------------------------------------------------


@pytest.mark.django_db
def test_full_sync_pages_and_maps_the_configured_shop() -> None:
    server = BillzServer()
    adapter = live(server)
    rows = list(adapter.iter_products("full"))
    listing = [r for r in server.requests if server.label(r) == "GET /v2/products"]
    assert [dict(r.url.params) for r in listing] == [
        {"limit": "2", "include_empty_stocks": "true", "page": "1"},
        {"limit": "2", "include_empty_stocks": "true", "page": "2"},
    ]
    assert [(r.billz_id, r.billz_name, r.volume, r.price, r.stock) for r in rows] == [
        ("4607012301", "HYALURONIC SERUM 30ML", "30 ml", 185_000, 12),  # our shop, not the warehouse's 40
        ("4607012302", "GENTLE FOAM 150ML", "150 ml", 99_000, 0),  # retail price (promo is ours to set)
        ("4607012303", "WAREHOUSE ONLY CREAM", "", None, 0),  # not stocked or priced in our shop
    ]  # the gift box without a barcode is skipped
    assert all(isinstance(r, BillzProduct) and r.synced_at.tzinfo is not None for r in rows)
    assert cache.get("billz:product:4607012301") == SERUM_ID
    assert cache.get("billz:product:4607012302") == FOAM_ID


def test_page_size_defaults_to_the_documented_maximum() -> None:
    with override_settings(BILLZ_SECRET_KEY="k", BILLZ_SHOP_ID=SHOP_ID):  # noqa: S106
        assert BillzConfig.from_settings().page_size == 300


@pytest.mark.django_db
def test_delta_watermark_moves_only_after_a_successful_run() -> None:
    server = BillzServer()
    adapter = live(server, Clock())
    first = timeutils.now()
    list(adapter.iter_products("delta"))
    assert "last_updated_date" not in server.requests[-1].url.params  # no cursor yet: everything
    assert selectors.provider_data("billz") == {}  # not applied yet → not committed
    adapter.sync_succeeded("delta")
    since = datetime.fromisoformat(selectors.provider_data("billz")["delta_since"])
    assert since >= first and "full_count" not in selectors.provider_data("billz")
    adapter.sync_succeeded("delta")  # nothing pending: no-op
    list(adapter.iter_products("delta"))
    expected = (since - billz_live.DELTA_OVERLAP).astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S")
    assert server.requests[-1].url.params["last_updated_date"] == expected
    # a run that fails on page 2 raises and leaves the cursor where it was
    before = selectors.provider_data("billz")
    server.overrides["GET /v2/products"] = lambda request: (
        httpx.Response(500, json=fixture("internal_error"))
        if request.url.params["page"] == "2"
        else httpx.Response(200, json=fixture("products_page1"))
    )
    with pytest.raises(BillzUnavailable):
        list(adapter.iter_products("delta"))
    adapter.sync_succeeded("delta")
    assert selectors.provider_data("billz") == before


@pytest.mark.django_db
def test_full_sync_records_its_count_for_the_plausibility_guard() -> None:
    adapter = live(BillzServer())
    list(adapter.iter_products("full"))
    adapter.sync_succeeded("full")
    data = selectors.provider_data("billz")
    assert data["full_count"] == 4 and data["delta_since"] == data["full_synced_at"]


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("scenario", "message"),
    [
        ("short_page", "only 2 of 4"),
        ("count_changed", "count changed while paging"),
        ("empty", "no product at all"),
        ("implausible", "fewer than 80"),
    ],
)
def test_incomplete_or_implausible_full_listing_yields_nothing(scenario: str, message: str) -> None:
    server = BillzServer()
    if scenario == "short_page":
        server.product_pages = [fixture("products_page1")]
    elif scenario == "count_changed":
        server.product_pages[1]["count"] = 5
    elif scenario == "empty":
        server.product_pages = [{"count": 0, "products": None}]
    else:
        services.update_provider_data("billz", {"full_count": 100})
    adapter = live(server)
    yielded: list[BillzProduct] = []
    with pytest.raises(BillzListingIncomplete, match=message):
        for row in adapter.iter_products("full"):
            yielded.append(row)
    assert yielded == []
    adapter.sync_succeeded("full")
    assert selectors.provider_data("billz").get("full_count") in (None, 100)
    assert BillzListingIncomplete.code == "billz_listing_incomplete"


@pytest.mark.django_db
def test_delta_tolerates_a_count_change_while_paging() -> None:
    server = BillzServer()
    server.product_pages[1]["count"] = 4
    server.product_pages[0]["count"] = 3
    rows = list(live(server).iter_products("delta"))
    assert len(rows) == 3


def test_get_product_matches_the_barcode_exactly() -> None:
    server = BillzServer()
    adapter = live(server)
    found = adapter.get_product("4607012302")
    assert found is not None and (found.billz_name, found.price, found.stock) == (
        "GENTLE FOAM 150ML",
        99_000,
        0,
    )
    search = server.requests[-1].url.params
    assert (search["search"], search["fields"], search["include_empty_stocks"]) == (
        "4607012302",
        "barcode",
        "true",
    )
    assert adapter.get_product("46070123") is None  # a substring of three barcodes, none equal
    calls = len(server.requests)
    assert adapter.get_product("not-digits") is None and len(server.requests) == calls


def test_unconfigured_adapter_refuses_every_call() -> None:
    adapter = live(BillzServer(), shop_id="")
    assert adapter.is_configured() is False
    with pytest.raises(BillzUnavailable, match="not configured"):
        adapter.get_product("4607012301")
    with pytest.raises(BillzUnavailable, match="not configured"):
        list(adapter.iter_products("delta"))


# --- sale push ----------------------------------------------------------------------------------------------


class Journal:
    def __init__(self, server: BillzServer, fail: bool = False) -> None:
        self.server = server
        self.fail = fail
        self.recorded: list[tuple[str, str]] = []
        self.requests_before: list[str] = []

    def order_created(self, billz_order_id: str, billz_sale_ref: str) -> None:
        self.requests_before = self.server.labels()
        if self.fail:
            raise RuntimeError("database unavailable")
        self.recorded.append((billz_order_id, billz_sale_ref))


def sale(billz_order_id: str = "", phone: str = PHONE) -> SaleRequest:
    from uuid import UUID

    return SaleRequest(
        order_id=10248,
        order_uuid=UUID(ORDER_UUID),
        lines=[
            SaleLine("4607012301", 2, 185_000),
            SaleLine("4607012302", 1, 95_000),
            SaleLine("4607012301", 1, 185_000),  # a second line of the same product: one upsert of 3
        ],
        customer_phone=phone,
        billz_order_id=billz_order_id,
    )


def test_sale_push_happy_path(caplog: pytest.LogCaptureFixture) -> None:
    server = BillzServer()
    adapter = live(server)
    journal = Journal(server)
    with caplog.at_level(logging.DEBUG):
        receipt = adapter.push_sale(sale(), journal)
    assert (receipt.billz_order_id, receipt.billz_sale_id, receipt.billz_sale_ref) == (
        SALE_ID,
        SALE_ID,
        "5632631379",
    )
    assert journal.recorded == [(SALE_ID, "5632631379")]
    assert journal.requests_before == [
        "POST /v1/auth/login",
        "POST /v2/order",
    ]  # persisted before anything else
    assert server.labels()[1:] == [
        "POST /v2/order",
        "GET /v2/products",  # barcode → product id (then cached)
        "POST /v2/order-product/{order_id}",
        "GET /v2/products",
        "POST /v2/order-product/{order_id}",
        "GET /v1/client",
        "PUT /v2/order-customer-new/{order_id}",
        "POST /v1/recalculate-order-bill/{order_id}",
        "POST /v2/order-payment/{order_id}",
    ]
    create = server.requests[1]
    assert create.headers["Billz-Response-Channel"] == "HTTP"
    assert json.loads(create.content) == {"shop_id": SHOP_ID, "cashbox_id": CASHBOX_ID}
    assert server.bodies("POST /v2/order-product/{order_id}") == [
        {"product_id": SERUM_ID, "sold_measurement_value": 3, "used_wholesale_price": False},
        {"product_id": FOAM_ID, "sold_measurement_value": 1, "used_wholesale_price": False},
    ]
    assert all(
        r.headers["Billz-Response-Channel"] == "HTTP"
        for r in server.requests
        if server.label(r) in ("POST /v2/order-product/{order_id}", "PUT /v2/order-customer-new/{order_id}")
    )
    assert server.bodies("PUT /v2/order-customer-new/{order_id}") == [
        {"customer_id": CLIENT_ID, "check_auth_code": False}
    ]
    assert server.bodies("POST /v2/order-payment/{order_id}") == [
        {
            "payments": [
                {"company_payment_type_id": PAYMENT_TYPE_ID, "paid_amount": 654_000, "returned_amount": 0}
            ],
            "comment": "Glow Derma #10248",
            "without_cashback": True,
        }
    ]
    payment = [r for r in server.requests if server.label(r) == "POST /v2/order-payment/{order_id}"][0]
    assert "Billz-Response-Channel" not in payment.headers  # a synchronous method
    # Billz's bill (654 000 at Billz prices) differs from ours (650 000): paid as billed, logged without PII
    assert "billz.sale_total_differs" in caplog.text and "billz_total=654000" in caplog.text
    assert PHONE not in caplog.text and PHONE[1:] not in caplog.text
    assert server.sales[SALE_ID].payments and server.sales[SALE_ID].customer_id == CLIENT_ID
    assert cache.get(f"billz:client:{pii_ref(PHONE)}") == CLIENT_ID
    assert cache.get(billz_live.SALE_LOCK_KEY.format(order_uuid=ORDER_UUID)) is None


def test_sale_push_creates_a_missing_client_once() -> None:
    server = BillzServer()
    adapter = live(server)
    adapter.push_sale(sale(phone="+998935550011"), Journal(server))
    assert server.created_clients == 1
    assert server.bodies("POST /v1/client") == [{"phone_number": "+998935550011"}]
    assert server.sales[SALE_ID].customer_id == NEW_CLIENT_ID
    # Billz's search lags behind a creation: the next sale finds the id in the cache, not by a second create
    server.clients.pop("+998935550011")
    server.sales.clear()
    adapter.push_sale(sale(phone="+998935550011"), Journal(server))
    assert server.created_clients == 1


def test_sale_without_a_phone_links_no_client() -> None:
    server = BillzServer()
    live(server).push_sale(sale(phone=""), Journal(server))
    assert server.count("GET /v1/client") == 0 and server.count("PUT /v2/order-customer-new/{order_id}") == 0


@pytest.mark.parametrize(
    "crash_after",
    [
        "POST /v2/order-product/{order_id}",
        "PUT /v2/order-customer-new/{order_id}",
        "POST /v1/recalculate-order-bill/{order_id}",
        "POST /v2/order-payment/{order_id}",
    ],
)
def test_a_crash_at_any_step_resumes_without_duplicates(crash_after: str) -> None:
    """Billz applied the step, the worker died before it saw the answer; the outbox redelivers with the
    persisted Billz sale id: one sale, the right quantities, one client link, one payment."""
    server = BillzServer()
    journal = Journal(server)
    server.crash_after.add(crash_after)
    with pytest.raises(WorkerCrash):
        live(server).push_sale(sale(), journal)
    assert (
        cache.get(billz_live.SALE_LOCK_KEY.format(order_uuid=ORDER_UUID)) is None
    )  # released on the way out
    ((billz_order_id, _),) = journal.recorded
    receipt = live(server).push_sale(sale(billz_order_id), Journal(server))
    assert receipt.billz_order_id == SALE_ID
    assert list(server.sales) == [SALE_ID]
    assert server.sales[SALE_ID].items == {SERUM_ID: 3.0, FOAM_ID: 1.0}
    assert server.sales[SALE_ID].customer_id == CLIENT_ID
    assert len(server.sales[SALE_ID].payments) == 1
    assert server.count("POST /v2/order") == 1
    assert server.count("POST /v2/order-payment/{order_id}") == 1
    assert server.count("PUT /v2/order-customer-new/{order_id}") == 1
    if crash_after == "POST /v2/order-payment/{order_id}":
        # paid before the crash: the retry only looks the sale up
        assert server.labels()[-1] == "GET /v2/order/{id}"


def test_resume_tops_up_only_the_missing_items() -> None:
    server = BillzServer()
    server.sales[SALE_ID] = billz_sale = Sale(SALE_ID, "5632631379", items={SERUM_ID: 3.0})
    live(server).push_sale(sale(SALE_ID), Journal(server))
    assert server.bodies("POST /v2/order-product/{order_id}") == [
        {"product_id": FOAM_ID, "sold_measurement_value": 1, "used_wholesale_price": False}
    ]
    assert billz_sale.items == {SERUM_ID: 3.0, FOAM_ID: 1.0}


def test_journal_failure_stops_before_any_item_is_sent() -> None:
    server = BillzServer()
    with pytest.raises(RuntimeError, match="database unavailable"):
        live(server).push_sale(sale(), Journal(server, fail=True))
    assert server.labels() == ["POST /v1/auth/login", "POST /v2/order"]  # the documented orphan draft window


def test_deleted_or_unknown_sale_is_permanent() -> None:
    server = BillzServer()
    server.sales[SALE_ID] = Sale(SALE_ID, "5632631379", deleted=True)
    with pytest.raises(BillzRejected, match="was deleted") as deleted:
        live(server).push_sale(sale(SALE_ID), Journal(server))
    assert deleted.value.error_code == 20017
    with pytest.raises(BillzRejected, match="not found"):
        live(server).push_sale(sale("11111111-2222-4333-8444-555555555555"), Journal(server))
    assert server.count("POST /v2/order-payment/{order_id}") == 0


def test_unknown_product_is_permanent_and_stops_before_payment() -> None:
    server = BillzServer()
    request = SaleRequest(
        order_id=1, order_uuid=sale().order_uuid, lines=[SaleLine("4607099999", 1, 1000)], customer_phone=""
    )
    with pytest.raises(BillzRejected, match="4607099999 not found") as rejected:
        live(server).push_sale(request, Journal(server))
    assert rejected.value.error_code == 20036
    assert server.count("POST /v2/order-payment/{order_id}") == 0


def test_a_stale_cached_product_id_is_looked_up_again_once() -> None:
    server = BillzServer()
    cache.set("billz:product:4607012301", "aaaaaaaa-0000-4000-8000-000000000000", timeout=60)

    def add(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        if payload["product_id"].startswith("aaaaaaaa"):
            return httpx.Response(400, json=_payment_error(20036))
        server.sales[SALE_ID].items[payload["product_id"]] = float(payload["sold_measurement_value"])
        return httpx.Response(200, json=fixture("async_ok"))

    server.overrides["POST /v2/order-product/{order_id}"] = add
    live(server).push_sale(sale(), Journal(server))
    assert server.sales[SALE_ID].items == {SERUM_ID: 3.0, FOAM_ID: 1.0}
    assert cache.get("billz:product:4607012301") == SERUM_ID


@pytest.mark.parametrize(("code", "kind"), [(20084, BillzUnavailable), (20010, BillzRejected)])
def test_payment_refusals_are_typed(code: int, kind: type[Exception]) -> None:
    server = BillzServer()
    server.overrides["POST /v2/order-payment/{order_id}"] = lambda request: httpx.Response(
        400, json=_payment_error(code)
    )
    with pytest.raises(kind):
        live(server).push_sale(sale(), Journal(server))


def test_a_push_in_progress_elsewhere_is_retried_later() -> None:
    server = BillzServer()
    cache.add(billz_live.SALE_LOCK_KEY.format(order_uuid=ORDER_UUID), 1, timeout=60)
    with pytest.raises(BillzUnavailable, match="another worker"):
        live(server).push_sale(sale(), Journal(server))
    assert server.requests == []


def test_helpers() -> None:
    assert is_finished({"finished_at": "2026-10-26 14:05:20"}) is True
    assert is_finished({"finished_at": "0001-01-01T00:00:00Z"}) is False  # Go's zero time: a draft
    assert is_finished({"finished_at": ""}) is False and is_finished({}) is False
    assert sale_ref("5632631379", SALE_ID) == "5632631379"
    assert sale_ref("", SALE_ID) == SALE_ID.replace("-", "")[:32]
    assert sale_ref("x" * 40, SALE_ID) == SALE_ID.replace("-", "")[:32]


# --- stubs and registry -------------------------------------------------------------------------------------


@pytest.mark.parametrize("kind", ["live", "fake"])
def test_explicit_stubs_refuse_loudly(kind: str) -> None:
    adapter: Any = live(BillzServer()) if kind == "live" else FakeBillz()
    with pytest.raises(BillzNotSupported, match="refunds of paid sales") as refund:
        adapter.refund_sale(SALE_ID)
    assert refund.value.feature == "refunds of paid sales" and refund.value.code == "billz_not_supported"
    with pytest.raises(BillzNotSupported, match="L6/L7"):
        adapter.find_store_sale("S-88213")
    with pytest.raises(BillzNotSupported, match="no webhooks"):
        adapter.parse_webhook(b"{}")


@override_settings(
    BILLZ_MODE="live",
    BILLZ_SECRET_KEY="secret",  # noqa: S106
    BILLZ_SHOP_ID=SHOP_ID,
    BILLZ_CASHBOX_ID=CASHBOX_ID,
    BILLZ_PAYMENT_TYPE_ID=PAYMENT_TYPE_ID,
    BILLZ_RATE_LIMIT_PER_SECOND=1.5,
    BILLZ_PAGE_SIZE=250,
)
def test_registry_builds_the_live_adapter_from_settings() -> None:
    adapter = get_billz()
    assert isinstance(adapter, LiveBillz) and adapter.is_configured() is True
    assert (adapter.config.shop_id, adapter.config.page_size) == (SHOP_ID, 250)
    assert adapter.client.limiter.interval == pytest.approx(1 / 1.5)
    with override_settings(BILLZ_PAYMENT_TYPE_ID=""):
        assert get_billz().is_configured() is False
