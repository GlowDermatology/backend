"""The live Billz adapter against recorded answers (``billz_support.BillzServer``; no network).

Auth and token rotation, the shared rate limiter, retries and the error mapping (``client``), the catalog
sync (pagination, shop mapping, watermark, reconciliation guards) and the idempotent sale push (``live``).
"""

from __future__ import annotations

import json
import logging
import threading
import time
from datetime import UTC, datetime, timedelta
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
    BillzCheckByHand,
    BillzListingIncomplete,
    BillzMisconfigured,
    BillzNotSupported,
    BillzPaymentPending,
    BillzProduct,
    BillzRejected,
    BillzShiftClosed,
    BillzUnavailable,
    SaleCancelled,
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
    Journal,
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
    assert login.headers["platform-id"] == PLATFORM_ID  # openapi.yaml declares it on the login too (N4)
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
    """Three threads, each with its own limiter on the shared cache: every slot is won once, slots are at
    least one interval apart, and no thread starts before its slot (a late wake-up only delays a request)."""
    limiter_rate = 20.0
    slots: list[tuple[float, float]] = []
    lock = threading.Lock()

    def worker() -> None:
        limiter = RateLimiter(limiter_rate)
        for _ in range(4):
            start = limiter.acquire()
            with lock:
                slots.append((start, time.time()))

    threads = [threading.Thread(target=worker) for _ in range(3)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)
    starts = sorted(start for start, _ in slots)
    assert len(starts) == 12
    gaps = [later - earlier for earlier, later in zip(starts, starts[1:], strict=False)]
    assert (
        min(gaps) >= 1 / limiter_rate - 1e-6
    )  # never two requests in one slot (float epsilon at epoch scale)
    assert all(woke >= start - 0.005 for start, woke in slots)  # (5 ms of clock granularity)


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
    assert server.count("GET /v2/products") == 1 + billz_client.BATCH.max_throttled + 1


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
    assert server.count("GET /v2/products") == 1 + billz_client.BATCH.max_attempts
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
        # decision D2: a closed shift waits for the next one (deferred, never dead at once)
        (400, _payment_error(20010), BillzShiftClosed, "shift closed"),
        (400, _payment_error(20082), BillzShiftClosed, "cash box is closed"),
        (400, _payment_error(20033), BillzShiftClosed, "no open shift"),
        (400, _payment_error(20016), BillzRejected, "not enough stock"),
        (400, _payment_error(20017), BillzRejected, "deleted"),
        (400, _payment_error(20036), BillzRejected, "product not found"),
        (400, _payment_error(20037), BillzRejected, "archived"),
        (400, _payment_error(20063), BillzRejected, "OFD"),
        # decision D3: configuration-type refusals are retryable (and alert) until a setting is fixed
        (400, _payment_error(20009), BillzMisconfigured, "another shop"),
        (400, _payment_error(20085), BillzMisconfigured, "no access to the shop"),
        (400, _payment_error(20999), BillzRejected, "payment is possible only"),  # unknown → permanent
        (403, fixture("forbidden"), BillzMisconfigured, "may not call this route (access denied)"),
        (
            404,
            {"code": 404, "message": "no route", "error": "", "data": None},
            BillzMisconfigured,
            "no route",
        ),
        (301, {}, BillzMisconfigured, "redirect (HTTP 301)"),
        (308, {}, BillzMisconfigured, "check BILLZ_BASE_URL"),
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
    """Review B8 (probes test_redirects_and_route_errors_are_permanent and
    test_configuration_and_shift_codes_are_permanent): only refusals staff cannot cure by a setting or by
    waiting are permanent."""
    error = classify("POST /v2/order-payment/{order_id}", status, body)
    assert type(error) is kind and text in str(error)
    if "data" in body and isinstance(body["data"], dict):
        assert error.error_code == body["data"].get("error_code")
    assert error.http_status == status


def test_a_404_of_one_resource_is_permanent_but_a_404_of_a_route_is_configuration() -> None:
    gone = classify("GET /v2/order/{id}", 404, {"message": "not found"}, resource=True)
    assert type(gone) is BillzRejected and gone.not_applied
    route = classify("POST /v2/order", 404, {"message": "not found"})
    assert type(route) is BillzMisconfigured and route.not_applied


def test_what_an_error_says_about_the_outcome() -> None:
    """``not_applied``: Billz certainly did not apply the request (the payment attempt is then forgotten)."""
    assert classify("x", 400, _payment_error(20020)).not_applied  # refused: nothing was paid
    assert classify("x", 429, {}).not_applied
    assert not classify("x", 500, {}).not_applied  # Billz may have applied it
    assert not classify("x", 408, fixture("async_timeout")).not_applied


def test_billz_free_text_is_scrubbed_before_it_is_kept_anywhere() -> None:
    """Review B13 (probe test_billz_free_text_lands_in_the_exception_text): a phone Billz echoes and control
    characters never reach last_error, the outbox.dead audit row or a log line."""
    body = {"code": 400, "message": "client with phone_number +998901234567 already exists\x00", "data": None}
    error = classify("POST /v1/client", 400, body)
    assert "+998901234567" not in str(error) and "998901234567" not in str(error)
    assert "<number>" in str(error) and "\x00" not in str(error)


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
        ("overlapping_pages", "3 distinct products for a count of 4 \\(1 repeated"),
        ("other_shop", "no listed product carries an entry for the configured shop"),
    ],
)
def test_incomplete_or_implausible_full_listing_yields_nothing(scenario: str, message: str) -> None:
    """Also review A M4 (probe test_full_listing_with_overlapping_pages_is_accepted_as_complete): page 2
    repeats a product of page 1 while the count stays, so one product was never listed — the reconciliation
    would hide it; and review B3: a shop id no product carries would zero every stock."""
    server = BillzServer()
    if scenario == "overlapping_pages":
        first, second = server.product_pages
        second["products"][0] = first["products"][1]  # unstable order: page 2 repeats page 1's last product
    elif scenario == "other_shop":
        server.product_pages = [fixture("products_page1"), fixture("products_page2")]
        adapter_shop = "6f244db4-75d6-4fac-9841-14867d45e36b"  # a valid UUID no product carries
        listing: list[BillzProduct] = []
        with pytest.raises(BillzListingIncomplete, match=message):
            listing.extend(live(server, shop_id=adapter_shop).iter_products("full"))
        assert listing == []
        return
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


def sale(phone: str = PHONE, *lines: SaleLine) -> SaleRequest:
    from uuid import UUID

    return SaleRequest(
        order_id=10248,
        order_uuid=UUID(ORDER_UUID),
        lines=lines
        or [
            SaleLine("4607012301", 2, 185_000),
            SaleLine("4607012302", 1, 95_000),
            SaleLine("4607012301", 1, 185_000),  # a second line of the same product: one upsert of 3
        ],
        customer_phone=phone,
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
    # every barcode is resolved before the sale exists (review B10), the id persisted before anything else
    assert journal.requests_before == [
        "POST /v1/auth/login",
        "GET /v2/products",
        "GET /v2/products",
        "POST /v2/order",
    ]
    assert server.labels()[3:] == [
        "POST /v2/order",
        "POST /v2/order-product/{order_id}",
        "POST /v2/order-product/{order_id}",
        "GET /v1/client",
        "PUT /v2/order-customer-new/{order_id}",
        "POST /v1/recalculate-order-bill/{order_id}",
        "POST /v2/order-payment/{order_id}",
    ]
    create = server.requests[3]
    assert create.headers["Billz-Response-Channel"] == "HTTP"
    assert create.url.params["Billz-Response-Channel"] == "HTTP"  # header and query parameter (N10)
    assert json.loads(create.content) == {"shop_id": SHOP_ID, "cashbox_id": CASHBOX_ID}
    # decision D1: each product at the price the customer was charged
    assert server.bodies("POST /v2/order-product/{order_id}") == [
        {
            "product_id": SERUM_ID,
            "sold_measurement_value": 3,
            "used_wholesale_price": False,
            "use_free_price": True,
            "free_price": 185_000,
        },
        {
            "product_id": FOAM_ID,
            "sold_measurement_value": 1,
            "used_wholesale_price": False,
            "use_free_price": True,
            "free_price": 95_000,
        },
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
                {"company_payment_type_id": PAYMENT_TYPE_ID, "paid_amount": 650_000, "returned_amount": 0}
            ],
            "comment": "Glow Derma #10248",
            "without_cashback": True,
        }
    ]
    payment = [r for r in server.requests if server.label(r) == "POST /v2/order-payment/{order_id}"][0]
    assert "Billz-Response-Channel" not in payment.headers  # a synchronous method
    assert journal.payments_announced == 1 and journal.payment_attempted_at is not None
    assert PHONE not in caplog.text and PHONE[1:] not in caplog.text
    assert server.sales[SALE_ID].payments and server.sales[SALE_ID].customer_id == CLIENT_ID
    assert cache.get(f"billz:client:{pii_ref(PHONE)}") == CLIENT_ID
    assert cache.get(billz_live.SALE_LOCK_KEY.format(order_uuid=ORDER_UUID)) is None


def test_a_bill_other_than_what_the_customer_paid_is_never_paid(caplog: pytest.LogCaptureFixture) -> None:
    """Decision D1 (review A M2, probe test_bill_total_differing_from_glowderma_total_is_paid_as_billed):
    Billz ignoring the free price (or a Billz client discount) makes the bill differ from what the customer
    paid; nothing is paid and staff check the draft."""
    server = BillzServer()

    def billz_prices(request: httpx.Request) -> httpx.Response:
        bill = fixture("recalculate")
        bill["total_price"] = 370_000  # 2 × 185 000 at Billz's own price, the customer paid 2 × 150 000
        return httpx.Response(200, json=bill)

    server.overrides["POST /v1/recalculate-order-bill/{order_id}"] = billz_prices
    journal = Journal(server)
    with caplog.at_level(logging.ERROR), pytest.raises(BillzCheckByHand, match="customer was charged 300000"):
        live(server).push_sale(sale(PHONE, SaleLine("4607012301", 2, 150_000)), journal)
    assert server.count("POST /v2/order-payment/{order_id}") == 0
    assert journal.payments_announced == 0
    assert server.bodies("POST /v2/order-product/{order_id}")[0]["free_price"] == 150_000
    assert "billz.sale_total_differs" in caplog.text and "billz_total=370000" in caplog.text


def test_a_fractional_bill_is_never_rounded_nor_paid() -> None:
    """Review A S2 (probe test_fractional_bill_total_is_rounded_before_paying): a 99 000.4 bill is not paid
    as 99 000 (Billz would answer 20020 ten times); D1 refuses it at once for staff."""
    server = BillzServer()

    def fractional(request: httpx.Request) -> httpx.Response:
        bill = fixture("recalculate")
        bill["total_price"] = 99_000.4
        return httpx.Response(200, json=bill)

    server.overrides["POST /v1/recalculate-order-bill/{order_id}"] = fractional
    with pytest.raises(BillzCheckByHand, match="Billz bills 99000.4"):
        live(server).push_sale(sale(PHONE, SaleLine("4607012302", 1, 99_000)), Journal(server))
    assert server.count("POST /v2/order-payment/{order_id}") == 0


def test_an_uncalculated_bill_is_retried_later() -> None:
    server = BillzServer()

    def uncalculated(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={**fixture("recalculate"), "is_calculated": False})

    server.overrides["POST /v1/recalculate-order-bill/{order_id}"] = uncalculated
    with pytest.raises(BillzUnavailable, match="not calculated") as caught:
        live(server).push_sale(sale(), Journal(server))
    assert not isinstance(caught.value, BillzRejected) and caught.value.error_code == 20084
    assert server.count("POST /v2/order-payment/{order_id}") == 0


def test_lines_of_one_product_at_different_prices_need_a_check_by_hand() -> None:
    server = BillzServer()
    with pytest.raises(BillzCheckByHand, match="different unit prices"):
        live(server).push_sale(
            sale(PHONE, SaleLine("4607012301", 1, 185_000), SaleLine("4607012301", 1, 180_000)),
            Journal(server),
        )
    assert server.count("POST /v2/order") == 0


def test_create_takes_the_sale_id_from_data_not_from_the_envelope() -> None:
    """Review A S1 (probe test_create_prefers_envelope_id_over_data_id): openapi post_v2_order_result names
    the reply message in the envelope's ``id`` and the sale in ``data.id``."""
    server = BillzServer()
    original = server._default

    def create(request: httpx.Request) -> httpx.Response:
        response = original("POST /v2/order", request)
        body = response.json()
        body["id"] = "99999999-0000-4000-8000-000000000099"
        return httpx.Response(200, json=body)

    server.overrides["POST /v2/order"] = create
    journal = Journal(server)
    live(server).push_sale(sale(), journal)
    assert journal.recorded == [(SALE_ID, "5632631379")]
    assert server.sales[SALE_ID].payments


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


def test_a_stale_cached_client_id_is_looked_up_again_once() -> None:
    """Review B9 / A N9 (probe test_a_stale_cached_client_id_is_never_looked_up_again): a cached client Billz
    refuses is forgotten and resolved once more instead of failing every sale of the customer for 30 days."""
    server = BillzServer()
    key = billz_live.CLIENT_ID_KEY.format(ref=pii_ref(PHONE))
    stale = "11111111-2222-4333-8444-555555555555"  # merged or deleted in Billz since it was cached
    cache.set(key, stale, timeout=3600)
    original = server._default

    def link(request: httpx.Request) -> httpx.Response:
        if json.loads(request.content)["customer_id"] == stale:
            return httpx.Response(404, json={"code": 404, "message": "customer not found", "data": None})
        return original("PUT /v2/order-customer-new/{order_id}", request)

    server.overrides["PUT /v2/order-customer-new/{order_id}"] = link
    live(server).push_sale(sale(), Journal(server))
    assert server.count("GET /v1/client") == 1
    assert cache.get(key) == CLIENT_ID and server.sales[SALE_ID].customer_id == CLIENT_ID
    assert server.sales[SALE_ID].payments


def test_a_freshly_resolved_client_billz_refuses_is_not_retried_again() -> None:
    server = BillzServer()
    server.overrides["PUT /v2/order-customer-new/{order_id}"] = lambda request: httpx.Response(
        404, json={"code": 404, "message": "customer not found", "data": None}
    )
    with pytest.raises(BillzMisconfigured):
        live(server).push_sale(sale(), Journal(server))
    assert server.count("PUT /v2/order-customer-new/{order_id}") == 1  # not from the cache: no second try


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
    """Billz applied the step, the worker died before it saw the answer; the outbox redelivers and the journal
    names the persisted Billz sale: one sale, the right quantities, one client link, one payment."""
    server = BillzServer()
    journal = Journal(server)
    server.crash_after.add(crash_after)
    with pytest.raises(WorkerCrash):
        live(server).push_sale(sale(), journal)
    assert (
        cache.get(billz_live.SALE_LOCK_KEY.format(order_uuid=ORDER_UUID)) is None
    )  # released on the way out
    assert journal.recorded == [(SALE_ID, "5632631379")]
    receipt = live(server).push_sale(sale(), journal)
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


def test_resume_reasserts_every_line_at_the_customers_price() -> None:
    """On resume every line is upserted again (absolute quantity and free price): a line Billz holds at its
    own price is corrected before the bill is read; a product the order does not hold (a cashier's) is set to
    0 (review A N5) instead of being paid."""
    server = BillzServer()
    cashier_product = "77777777-0000-4000-8000-000000000077"
    server.prices[cashier_product] = 10_000
    server.sales[SALE_ID] = billz_sale = Sale(
        SALE_ID, "5632631379", items={SERUM_ID: 3.0, cashier_product: 1.0}
    )
    live(server).push_sale(sale(), Journal(server, billz_order_id=SALE_ID))
    bodies = server.bodies("POST /v2/order-product/{order_id}")
    assert bodies[0] == {
        "product_id": cashier_product,
        "sold_measurement_value": 0,
        "used_wholesale_price": False,
    }
    assert [(body["product_id"], body["free_price"]) for body in bodies[1:]] == [
        (SERUM_ID, 185_000),
        (FOAM_ID, 95_000),
    ]
    assert billz_sale.items == {SERUM_ID: 3.0, FOAM_ID: 1.0}
    assert billz_sale.payments[0]["payments"][0]["paid_amount"] == 650_000


def test_journal_failure_stops_before_any_item_is_sent() -> None:
    server = BillzServer()
    with pytest.raises(RuntimeError, match="database unavailable"):
        live(server).push_sale(sale(), Journal(server, fail=True))
    # the documented orphan-draft window
    assert server.labels() == [
        "POST /v1/auth/login",
        "GET /v2/products",
        "GET /v2/products",
        "POST /v2/order",
    ]


def test_deleted_or_unknown_sale_is_permanent() -> None:
    server = BillzServer()
    server.sales[SALE_ID] = Sale(SALE_ID, "5632631379", deleted=True)
    with pytest.raises(BillzRejected, match="was deleted") as deleted:
        live(server).push_sale(sale(), Journal(server, billz_order_id=SALE_ID))
    assert deleted.value.error_code == 20017
    with pytest.raises(BillzRejected, match="not found"):
        live(server).push_sale(sale(), Journal(server, billz_order_id="11111111-2222-4333-8444-555555555555"))
    assert server.count("POST /v2/order-payment/{order_id}") == 0


def test_an_unknown_product_is_permanent_and_leaves_no_draft() -> None:
    """Review B10 (probe test_unknown_barcode_leaves_an_orphan_draft): every barcode is resolved before the
    sale is created, so an unknown product dead-letters without an orphan draft in Billz."""
    server = BillzServer()
    journal = Journal(server)
    with pytest.raises(BillzRejected, match="4607099999 not found") as rejected:
        live(server).push_sale(sale(PHONE, SaleLine("4607099999", 1, 1000)), journal)
    assert rejected.value.error_code == 20036
    assert server.count("POST /v2/order") == 0 and journal.recorded == []


@pytest.mark.parametrize("code", [20036, 20037])
def test_a_stale_cached_product_id_is_looked_up_again_once(code: int) -> None:
    """Also on 20037 (archived): review A N1 (probe test_cached_archived_product_id_is_not_refreshed_on_…)."""
    server = BillzServer()
    stale = "aaaaaaaa-0000-4000-8000-000000000000"
    cache.set("billz:product:4607012301", stale, timeout=60)

    def add(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        if payload["product_id"] == stale:
            return httpx.Response(400, json=_payment_error(code))
        server.apply_item(SALE_ID, payload)
        return httpx.Response(200, json=fixture("async_ok"))

    server.overrides["POST /v2/order-product/{order_id}"] = add
    live(server).push_sale(sale(), Journal(server))
    assert server.sales[SALE_ID].items == {SERUM_ID: 3.0, FOAM_ID: 1.0}
    assert cache.get("billz:product:4607012301") == SERUM_ID
    assert server.sales[SALE_ID].payments


@pytest.mark.parametrize(("code", "kind"), [(20084, BillzUnavailable), (20010, BillzShiftClosed)])
def test_payment_refusals_are_typed_and_forget_the_attempt(code: int, kind: type[Exception]) -> None:
    server = BillzServer()
    server.overrides["POST /v2/order-payment/{order_id}"] = lambda request: httpx.Response(
        400, json=_payment_error(code)
    )
    journal = Journal(server)
    with pytest.raises(kind):
        live(server).push_sale(sale(), journal)
    # Billz answered with a refusal: certainly not paid, so the next attempt may pay
    assert (journal.payments_announced, journal.forgotten, journal.payment_attempted_at) == (1, 1, None)


def test_a_lost_payment_answer_is_never_followed_by_a_second_payment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Review B2 (probe test_lost_payment_answer_is_followed_by_a_second_payment_post): the payment attempt is
    persisted before the request; while Billz still shows the sale open the next attempts wait for the quiet
    period (a deferral, not a retry) and then stop for staff — a second POST is never sent."""
    server = BillzServer()
    calls: list[int] = []

    def slow_payment(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        raise httpx.ReadTimeout("timed out", request=request)  # Billz may still be processing it

    server.overrides["POST /v2/order-payment/{order_id}"] = slow_payment
    journal = Journal(server)
    with pytest.raises(BillzUnavailable, match="ReadTimeout") as first:
        live(server).push_sale(sale(), journal)
    assert not first.value.not_applied and journal.payment_attempted_at is not None
    sent = journal.payment_attempted_at
    # the outbox retries seconds later: GET /v2/order shows no payment yet → deferred, nothing sent
    monkeypatch.setattr(timeutils, "now", lambda: sent + timedelta(seconds=10))
    with pytest.raises(BillzPaymentPending) as pending:
        live(server).push_sale(sale(), journal)
    assert pending.value.retry_after == billz_live.PAYMENT_QUIET_PERIOD - timedelta(seconds=10)
    # after the quiet period the sale is still open: stop for a check by hand, never pay again
    monkeypatch.setattr(timeutils, "now", lambda: sent + billz_live.PAYMENT_QUIET_PERIOD)
    with pytest.raises(BillzCheckByHand, match="not paying again"):
        live(server).push_sale(sale(), journal)
    assert len(calls) == 1 and server.count("POST /v2/order-payment/{order_id}") == 1


def test_a_lost_payment_that_went_through_is_found_paid() -> None:
    server = BillzServer()
    server.crash_after.add("POST /v2/order-payment/{order_id}")
    journal = Journal(server)
    with pytest.raises(WorkerCrash):
        live(server).push_sale(sale(), journal)
    assert journal.payment_attempted_at is not None  # a crash keeps the attempt
    receipt = live(server).push_sale(sale(), journal)
    assert receipt.billz_sale_id == SALE_ID and server.count("POST /v2/order-payment/{order_id}") == 1


@pytest.mark.parametrize(
    ("finished_at", "sold_at", "payments", "state"),
    [
        ("0001-01-01T00:00:00Z", "", None, "open"),
        ("2026-10-26 14:05:20", "", None, "paid"),
        ("", "2026-10-26 14:05:20", None, "paid"),  # closed reported through sold_at (review A S3)
        ("2026-10-26 14:05:20", "", [{"paid_amount": 650_000}], "paid"),
        ("2026-10-26 14:05:20", "", [], "unclear"),  # closed, yet no payment
        ("", "", [{"paid_amount": 650_000}], "unclear"),  # a payment on an open sale
        ("", "", [], "open"),
    ],
)
def test_sale_state_reads_every_paid_signal(
    finished_at: str, sold_at: str, payments: list[dict[str, Any]] | None, state: str
) -> None:
    order: dict[str, Any] = {"finished_at": finished_at, "sold_at": sold_at, "order_detail": {}}
    if payments is not None:
        order["order_detail"]["order_payments"] = payments
    assert billz_live.sale_state(order) == state


def test_resume_never_pays_a_sale_billz_reports_sold() -> None:
    """Review A S3 (probe test_resume_pays_again_when_finished_at_stays_zero_but_payments_exist)."""
    server = BillzServer()
    original = server._detail

    def detail(sale_: Sale) -> dict[str, Any]:
        body = original(sale_)
        body["finished_at"] = ""  # Billz reports the close in sold_at instead
        if sale_.payments:
            body["sold_at"] = "2026-10-26 14:05:20"
        return body

    server._detail = detail  # type: ignore[method-assign,assignment]
    journal = Journal(server)
    live(server).push_sale(sale(), journal)
    live(server).push_sale(sale(), journal)
    assert server.count("POST /v2/order-payment/{order_id}") == 1


def test_contradicting_paid_signals_retry_with_an_error_and_never_pay(
    caplog: pytest.LogCaptureFixture,
) -> None:
    server = BillzServer()
    server.sales[SALE_ID] = Sale(SALE_ID, "5632631379", items={SERUM_ID: 3.0, FOAM_ID: 1.0})
    original = server._detail

    def detail(sale_: Sale) -> dict[str, Any]:
        body = original(sale_)
        body["order_detail"]["order_payments"] = [{"paid_amount": 650_000}]  # yet finished_at is zero
        return body

    server._detail = detail  # type: ignore[method-assign,assignment]
    with caplog.at_level(logging.ERROR), pytest.raises(BillzUnavailable, match="contradicting"):
        live(server).push_sale(sale(), Journal(server, billz_order_id=SALE_ID))
    assert "billz.sale_state_unclear" in caplog.text
    assert server.count("POST /v2/order-payment/{order_id}") == 0


def test_an_order_cancelled_meanwhile_stops_before_the_payment() -> None:
    """Review B11: the journal re-checks the order right before the payment request."""
    server = BillzServer()
    journal = Journal(server, cancelled=True)
    with pytest.raises(SaleCancelled):
        live(server).push_sale(sale(), journal)
    assert server.count("POST /v2/order-payment/{order_id}") == 0
    assert journal.recorded == [(SALE_ID, "5632631379")]  # the draft the caller names for staff


def test_a_push_another_attempt_finished_returns_its_receipt() -> None:
    server = BillzServer()
    journal = Journal(server, billz_order_id=SALE_ID)
    journal.billz_sale_ref = "5632631379"
    journal.pushed = True
    receipt = live(server).push_sale(sale(), journal)
    assert (receipt.billz_sale_id, receipt.billz_sale_ref) == (SALE_ID, "5632631379")
    assert server.requests == []


def test_a_push_in_progress_elsewhere_is_retried_later() -> None:
    server = BillzServer()
    cache.add(billz_live.SALE_LOCK_KEY.format(order_uuid=ORDER_UUID), "another-worker", timeout=60)
    with pytest.raises(BillzUnavailable, match="another worker") as caught:
        live(server).push_sale(sale(), Journal(server))
    assert caught.value.not_applied and server.requests == []
    assert cache.get(billz_live.SALE_LOCK_KEY.format(order_uuid=ORDER_UUID)) == "another-worker"


def test_the_sale_lock_is_released_and_renewed_only_by_its_owner() -> None:
    """Review A S4 (probes test_sale_lock_is_deleted_even_when_another_worker_owns_it and
    test_sale_lock_release_is_not_owner_checked): a worker whose lock expired and was taken by another never
    deletes the other's lock, and stops before paying."""
    server = BillzServer()
    key = billz_live.SALE_LOCK_KEY.format(order_uuid=ORDER_UUID)
    original = server._default

    def recalc(request: httpx.Request) -> httpx.Response:
        cache.set(key, "worker-B", 600)  # our lock expired and worker B took it meanwhile
        return original("POST /v1/recalculate-order-bill/{order_id}", request)

    server.overrides["POST /v1/recalculate-order-bill/{order_id}"] = recalc
    journal = Journal(server)
    with pytest.raises(BillzUnavailable, match="expired under this worker"):
        live(server).push_sale(sale(), journal)
    assert cache.get(key) == "worker-B"
    assert server.count("POST /v2/order-payment/{order_id}") == 0 and journal.payments_announced == 0


def test_the_push_ends_within_its_deadline() -> None:
    """Review B5: one push never outlives the outbox lease; every wait honours the push deadline."""
    clock = Clock()
    server = BillzServer(clock)
    adapter = live(server, clock)

    def slow(request: httpx.Request) -> httpx.Response:
        clock.now += 130  # each answer takes 130 s
        server.apply_item(SALE_ID, json.loads(request.content))
        return httpx.Response(200, json={**fixture("async_ok"), "id": SALE_ID})

    server.overrides["POST /v2/order-product/{order_id}"] = slow
    with pytest.raises(BillzUnavailable, match="deadline") as caught:
        adapter.push_sale(sale(), Journal(server))
    assert caught.value.not_applied
    # after 260 s the next request (the client lookup) is never started, let alone the payment
    assert server.labels()[-1] == "POST /v2/order-product/{order_id}"
    assert server.count("POST /v2/order-payment/{order_id}") == 0
    assert billz_live.PUSH_DEADLINE_SECONDS < 300 < billz_live.SALE_LOCK_TTL  # below the outbox lease


def test_helpers() -> None:
    assert billz_live.barcode_of({"barcode": " 4607012301 "}) == "4607012301"
    assert billz_live.barcode_of({"barcode": "\u0664\u0666\u0660\u0667\u0660\u0661\u0662\u0663"}) is None
    assert billz_live.barcode_of({"barcode": "ABC-1"}) is None and billz_live.barcode_of({}) is None
    assert billz_live.price_of({"shop_prices": [{"shop_id": SHOP_ID, "retail_price": 5, "retail_currency": "USD"}]}, SHOP_ID) is None  # fmt: skip  # noqa: E501
    assert (
        billz_live.price_of({"shop_prices": [{"shop_id": SHOP_ID.upper(), "retail_price": 7}]}, SHOP_ID) == 7
    )
    assert billz_live.stock_of({"shop_measurement_values": [{"shop_id": SHOP_ID, "active_measurement_value": -1.5}]}, SHOP_ID) == -2  # fmt: skip  # noqa: E501
    assert billz_live.stock_of({"shop_measurement_values": "junk"}, SHOP_ID) == 0
    assert billz_live.volume_of({"custom_fields": [{"custom_field_system_name": "volume", "custom_field_value": "5 ml"}]}, "VOLUME") == "5 ml"  # fmt: skip  # noqa: E501
    assert billz_live.volume_of({"product_attributes": []}, "") is None  # no field configured: not asserted
    assert billz_live.volume_of({"product_attributes": []}, "Объём") == ""
    assert billz_live.aggregate([SaleLine("1", 1, 10), SaleLine("2", 2, 5), SaleLine("1", 2, 10)]) == [
        ("1", 3, 30),
        ("2", 2, 10),
    ]
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


@pytest.mark.redis_integration
def test_limiter_and_token_lock_on_the_real_redis_cache() -> None:
    """Two RedisCache instances (two worker processes) against the development Redis: a slot and the renewal
    lock are won by exactly one, and the token pair round-trips through Redis. Only this test's own keys (a
    unique prefix) are deleted afterwards — never ``clear()``, which would flush the shared database."""
    from uuid import uuid4

    from django.core.cache.backends.redis import RedisCache
    from redis.exceptions import RedisError

    def backend(prefix: str) -> RedisCache:
        return RedisCache(
            "redis://localhost:6380/0",
            {"KEY_PREFIX": prefix, "OPTIONS": {"socket_connect_timeout": 1, "socket_timeout": 1}},
        )

    prefix = f"billz-test-{uuid4().hex}"
    first, second = backend(prefix), backend(prefix)
    try:
        first.get("ping")
    except RedisError:
        pytest.skip("Development Redis is unavailable on localhost:6380")
    clock = Clock()
    limiters = [
        RateLimiter(2.0, cache=backend_, clock=clock, sleep=clock.sleep) for backend_ in (first, second)
    ]
    starts: list[float] = []
    try:
        starts = [limiters[index % 2].acquire() for index in range(6)]
        assert all(b - a == pytest.approx(0.5) for a, b in zip(starts, starts[1:], strict=False))
        assert first.add(billz_client.LOCK_KEY, "a", timeout=5) is True
        assert second.add(billz_client.LOCK_KEY, "b", timeout=5) is False
        first.delete(billz_client.LOCK_KEY)
        server = BillzServer()
        assert (
            BillzClient(config(), transport=server.transport, cache=first).access_token()
            == "access-from-login"
        )
        assert (
            BillzClient(config(), transport=server.transport, cache=second).access_token()
            == "access-from-login"
        )
        assert server.count("POST /v1/auth/login") == 1  # the second "process" read the pair from Redis
    finally:
        # the slot keys of the logins expire on their own within a minute; the token pair would live for days
        own = [f"{billz_client.RATE_PREFIX}:{round(start / 0.5)}" for start in starts]
        own += [billz_client.LOCK_KEY, billz_client.TOKENS_KEY, f"{billz_client.RATE_PREFIX}:pause"]
        first.delete_many(own)
