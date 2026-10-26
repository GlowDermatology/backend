"""Regression tests of the two adversarial reviews of the live Billz adapter (client and catalog side).

Each test names the finding and the reviewer's probe it replaces; the probe asserted the defect, the test
asserts the fixed behaviour. Recorded answers only (``billz_support.BillzServer``); no network.
"""

from __future__ import annotations

import contextlib
import copy
import json
import threading
import time
import uuid
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from celery.exceptions import SoftTimeLimitExceeded  # type: ignore[import-untyped]
from django.core.cache import cache
from redis.exceptions import ConnectionError as RedisConnectionError

from glowderma.core import timeutils
from glowderma.integrations import selectors
from glowderma.integrations.billz import client as billz_client
from glowderma.integrations.billz import live as billz_live
from glowderma.integrations.billz.client import INTERACTIVE, BillzClient, SafeCache
from glowderma.integrations.billz.ports import (
    BillzListingIncomplete,
    BillzUnavailable,
    SaleLine,
    SaleRequest,
)
from glowderma.integrations.tests.billz_support import (
    SERUM_ID,
    BillzServer,
    Clock,
    Journal,
    config,
    fixture,
    live,
)

PHONE = "+998901234567"


class BrokenCache:
    """Redis is down: every cache operation raises like Django's RedisCache does."""

    def __getattr__(self, name: str) -> Any:
        def fail(*args: Any, **kwargs: Any) -> Any:
            raise RedisConnectionError("Error 111 connecting to localhost:6380. Connection refused.")

        return fail


def product(pid: str, barcode: str, stock: float = 1) -> dict[str, Any]:
    base: dict[str, Any] = copy.deepcopy(fixture("products_page1")["products"][0])
    base["id"] = pid
    base["barcode"] = barcode
    base["shop_measurement_values"][0]["active_measurement_value"] = stock
    return base


A = ("aaaaaaaa-0000-4000-8000-000000000001", "4600000000001")
B = ("bbbbbbbb-0000-4000-8000-000000000002", "4600000000002")
D = ("dddddddd-0000-4000-8000-000000000004", "4600000000004")


def products_call(adapter: billz_live.LiveBillz) -> dict[str, Any]:
    body: dict[str, Any] = adapter.client.call(
        "GET", "/v2/products", label="GET /v2/products", params={}, idempotent=True
    )
    return body


# --- B1: Redis down -----------------------------------------------------------------------------------------


def test_redis_down_is_billz_unavailable_and_sends_nothing() -> None:
    """Review B1 (probes test_redis_down_get_product_raises_a_non_billz_error_and_sends_nothing,
    test_redis_down_push_sale_raises_non_billz_error_before_any_request): a typed, retryable error, so the
    admin answers 503 and the outbox retries; nothing reaches Billz."""
    server = BillzServer()
    adapter = live(server, cache=BrokenCache())
    with pytest.raises(BillzUnavailable, match="shared cache unavailable") as lookup:
        adapter.get_product("4607012301")
    assert lookup.value.not_applied
    request = SaleRequest(order_id=1, order_uuid=uuid.uuid4(), lines=[], customer_phone="")
    with pytest.raises(BillzUnavailable, match="shared cache unavailable"):
        adapter.push_sale(request, Journal(server))
    assert server.requests == []


def test_safe_cache_never_double_wraps() -> None:
    wrapped = SafeCache(cache)
    assert SafeCache(wrapped).backend is cache


# --- B7: non-transport httpx errors -------------------------------------------------------------------------


def test_a_corrupt_gzip_body_is_billz_unavailable() -> None:
    """Review B7 (probe test_a_corrupt_gzip_body_escapes_as_httpx_decoding_error)."""
    server = BillzServer()
    server.overrides["GET /v2/products"] = lambda request: httpx.Response(
        200, headers={"Content-Encoding": "gzip"}, content=b"not gzip at all"
    )
    with pytest.raises(BillzUnavailable, match="unreadable answer \\(DecodingError\\)"):
        live(server).get_product("4607012301")


# --- B4 / N6: token renewal ---------------------------------------------------------------------------------


def test_a_slow_renewal_never_lets_another_worker_present_the_same_refresh_token() -> None:
    """Review B4 (probe test_renewal_lock_expiry_presents_the_same_refresh_token_twice): even when the lock
    expires under a slow refresh, the token was taken out of the cache before it was presented, so the second
    worker logs in instead of presenting it again (Billz would revoke the session family); the slow worker,
    no longer owning the lock, does not overwrite the second worker's pair."""
    server = BillzServer()
    presented: list[str] = []
    first_entered = threading.Event()
    lock = threading.Lock()

    def refresh(request: httpx.Request) -> httpx.Response:
        token = json.loads(request.content)["refresh_token"]
        with lock:
            presented.append(token)
        first_entered.set()
        time.sleep(1.6)  # slower than the (patched) lock TTL
        return httpx.Response(200, json=fixture("refresh_ok"))

    server.overrides["POST /v2/auth/refresh"] = refresh
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
    results: dict[str, str] = {}
    errors: list[BaseException] = []

    def worker(name: str) -> None:
        try:
            results[name] = BillzClient(config(), transport=server.transport).access_token()
        except BaseException as exc:  # pragma: no cover - surfaced by the assertion below
            errors.append(exc)

    @contextlib.contextmanager
    def no_deadline(self: BillzClient, seconds: float) -> Iterator[None]:
        yield

    original_ttl = billz_client.LOCK_TTL_SECONDS
    billz_client.LOCK_TTL_SECONDS = 1
    # The renewal's own deadline (half the lock's TTL) makes this impossible; the test switches it off to
    # check the defence behind it: a renewal that outlives its lock never stores its pair.
    patch = pytest.MonkeyPatch()
    patch.setattr(BillzClient, "deadline", no_deadline)
    try:
        first = threading.Thread(target=worker, args=("a",))
        first.start()
        assert first_entered.wait(10)
        time.sleep(1.1)  # the first worker's lock has expired
        second = threading.Thread(target=worker, args=("b",))
        second.start()
        first.join(10)
        second.join(10)
    finally:
        billz_client.LOCK_TTL_SECONDS = original_ttl
        patch.undo()
    assert not errors
    assert presented == ["refresh-from-login"]  # never twice
    assert server.count("POST /v1/auth/login") == 1  # the second worker logged in
    assert results == {"a": "access-from-refresh", "b": "access-from-login"}
    assert cache.get(billz_client.TOKENS_KEY)["refresh"] == "refresh-from-login"  # the owner's pair stays


def test_a_refresh_that_times_out_falls_back_to_a_login_never_to_a_second_refresh() -> None:
    clock = Clock()
    server = BillzServer(clock)
    adapter = live(server, clock)
    products_call(adapter)

    def timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    server.overrides["POST /v2/auth/refresh"] = timeout
    clock.now += 1_296_000
    products_call(adapter)
    assert server.labels()[2:] == ["POST /v2/auth/refresh", "POST /v1/auth/login", "GET /v2/products"]


def test_a_login_answer_without_expires_in_gets_a_default_lifetime() -> None:
    """Review A N6 / B nit (probe test_a_login_answer_without_expires_in_refreshes_before_every_call)."""
    server = BillzServer()
    login = fixture("login_ok")
    del login["data"]["expires_in"]
    server.overrides["POST /v1/auth/login"] = lambda request: httpx.Response(200, json=login)
    adapter = live(server)
    for _ in range(5):
        products_call(adapter)
    assert server.count("POST /v1/auth/login") == 1
    assert server.count("POST /v2/auth/refresh") == 0
    stored = cache.get(billz_client.TOKENS_KEY)
    lifetime = stored["access_expires_at"] - time.time()
    assert billz_client.DEFAULT_ACCESS_TTL_SECONDS - 5 < lifetime <= billz_client.DEFAULT_ACCESS_TTL_SECONDS


# --- B5: the interactive profile ----------------------------------------------------------------------------


def test_the_admin_lookup_uses_the_interactive_profile() -> None:
    """Review B5 (probe test_billz_lookup_runs_inside_a_transaction_holding_an_advisory_lock): the lookup of
    ``POST products/`` runs inside the idempotency transaction, so it is bounded to seconds: one retry, a
    10 s read timeout, no waiting behind a 429 pause, at most 25 s in total."""
    clock = Clock()
    server = BillzServer(clock)
    adapter = live(server, clock)
    assert adapter.interactive.profile is INTERACTIVE
    assert INTERACTIVE.max_attempts == 2 and INTERACTIVE.read_timeout_cap == 10.0
    assert INTERACTIVE.call_budget is not None and INTERACTIVE.call_budget <= 25
    products_call(adapter)  # log in
    server.overrides["GET /v2/products"] = lambda request: httpx.Response(500, json=fixture("internal_error"))
    with pytest.raises(BillzUnavailable, match="HTTP 500"):
        adapter.get_product("4607012301")
    assert server.count("GET /v2/products") == 1 + 2  # one retry only
    timeouts: list[httpx.Timeout] = []
    real_client = httpx.Client

    def spy(*args: Any, **kwargs: Any) -> httpx.Client:
        timeouts.append(kwargs["timeout"])
        return real_client(*args, **kwargs)

    server.overrides.pop("GET /v2/products")
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(httpx, "Client", spy)
        adapter.get_product("4607012301")
    assert timeouts and timeouts[-1].read is not None and timeouts[-1].read <= 10.0
    # a 429 pause in force: the interactive lookup fails fast instead of sleeping behind it
    adapter.client.limiter.pause(30)
    clock.sleeps.clear()
    with pytest.raises(BillzUnavailable, match="paused"):
        adapter.get_product("4607012301")
    assert sum(clock.sleeps) < 1


# --- B6: circuit breaker ------------------------------------------------------------------------------------


def test_consecutive_failures_open_the_circuit_and_an_answer_closes_it(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Review B6: a hung Billz no longer holds every worker of the shared pool for minutes."""
    clock = Clock()
    server = BillzServer(clock)
    adapter = live(server, clock)
    products_call(adapter)

    def hang(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    server.overrides["GET /v2/products"] = hang
    with caplog.at_level("ERROR"), pytest.raises(BillzUnavailable):
        products_call(adapter)  # 4 attempts
    with pytest.raises(BillzUnavailable):
        products_call(adapter)  # the 5th failure opens the circuit
    assert "billz.circuit_open" in caplog.text
    sent = len(server.requests)
    with pytest.raises(BillzUnavailable, match="calls are paused") as fast:
        products_call(adapter)
    assert fast.value.not_applied and len(server.requests) == sent  # nothing sent while open
    server.overrides.pop("GET /v2/products")
    clock.now += billz_client.CIRCUIT_OPEN_SECONDS + 1
    products_call(adapter)  # half-open: one answer closes it
    assert cache.get(billz_client.CIRCUIT_FAILURES_KEY) is None


# --- N3: barcode search paging ------------------------------------------------------------------------------


def test_the_barcode_search_pages_until_the_exact_barcode() -> None:
    """Review A N3: ``search`` is a substring match limited to 50 a page; the exact barcode may be later."""
    server = BillzServer()
    wanted = product(SERUM_ID, "4607012301")
    others = [
        product(f"{index:08d}-0000-4000-8000-000000000000", f"84607012301{index:03d}") for index in range(50)
    ]
    pages = {"1": others, "2": [wanted]}

    def search(request: httpx.Request) -> httpx.Response:
        page = request.url.params["page"]
        return httpx.Response(200, json={"count": 51, "products": pages.get(page, [])})

    server.overrides["GET /v2/products"] = search
    found = live(server).get_product("4607012301")
    assert found is not None and found.stock == 1
    assert [r.url.params["page"] for r in server.requests if server.label(r) == "GET /v2/products"] == [
        "1",
        "2",
    ]
    assert cache.get("billz:product:4607012301") == SERUM_ID


# --- M4 / S7: listing consistency ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_duplicate_barcodes_keep_the_first_product_for_stock_and_sale_alike() -> None:
    """Review A S7 (probe test_duplicate_barcode_stock_from_first_sale_against_last)."""
    server = BillzServer()
    first = product("11111111-0000-4000-8000-000000000001", "4600000000009", stock=7)
    second = product("22222222-0000-4000-8000-000000000002", "4600000000009", stock=0)
    server.product_pages = [{"count": 2, "products": [first, second]}]
    rows = list(live(server).iter_products("delta"))
    assert [row.stock for row in rows] == [7]
    assert cache.get(billz_live.PRODUCT_ID_KEY.format(barcode="4600000000009")) == first["id"]


@pytest.mark.django_db
def test_a_delta_with_overlapping_pages_keeps_its_watermark() -> None:
    """Review A M4: a delta tolerates a shifted page but does not move the watermark past what it missed."""
    server = BillzServer()
    server.product_pages = [
        {"count": 4, "products": [product(*A), product(*B)]},
        {"count": 4, "products": [product(*B), product(*D)]},
    ]
    adapter = live(server)
    rows = list(adapter.iter_products("delta"))
    assert {row.billz_id for row in rows} == {A[1], B[1], D[1]}
    adapter.sync_succeeded("delta")
    assert selectors.provider_data("billz") == {}  # next delta re-reads from the old watermark


@pytest.mark.django_db
def test_a_full_listing_with_overlapping_pages_is_refused() -> None:
    """Review A M4 (probe test_full_listing_with_overlapping_pages_is_accepted_as_complete)."""
    server = BillzServer()
    server.product_pages = [
        {"count": 4, "products": [product(*A), product(*B)]},
        {"count": 4, "products": [product(*B), product(*D)]},
    ]
    with pytest.raises(BillzListingIncomplete, match="3 distinct products for a count of 4"):
        list(live(server).iter_products("full"))


@pytest.mark.django_db
def test_a_full_listing_repeating_a_product_without_an_id_on_another_page_is_refused() -> None:
    """NEW-6 (probe test_m4_variation_products_without_id_overlap_by_barcode): without Billz ids the overlap
    check fell back to positions, so a shifted page repeating B (and never listing C) was accepted."""
    server = BillzServer()
    a, b, b_again, d = product(*A), product(*B), product(*B), product(*D)
    for item in (a, b, b_again, d):
        item["id"] = ""
    server.product_pages = [{"count": 4, "products": [a, b]}, {"count": 4, "products": [b_again, d]}]
    with pytest.raises(BillzListingIncomplete, match="1 repeated across pages"):
        list(live(server).iter_products("full"))
    adapter = live(server)
    assert {row.billz_id for row in adapter.iter_products("delta")} == {A[1], B[1], D[1]}
    adapter.sync_succeeded("delta")
    assert selectors.provider_data("billz") == {}  # the delta keeps its watermark


@pytest.mark.django_db
def test_a_duplicate_barcode_on_one_page_without_ids_is_not_an_overlap() -> None:
    """NEW-6: two id-less products sharing a barcode on the same page are a duplicate (first kept, S7), not a
    shifted page: the full listing is accepted."""
    server = BillzServer()
    first, second, d = product(*A, stock=7), product(*A, stock=0), product(*D)
    for item in (first, second, d):
        item["id"] = ""
    server.product_pages = [{"count": 3, "products": [first, second, d]}]
    rows = list(live(server).iter_products("full"))
    assert [(row.billz_id, row.stock) for row in rows] == [(A[1], 7), (D[1], 1)]


def test_the_live_volume_is_not_asserted_without_a_volume_field() -> None:
    """Review A S6 (the adapter side): no ``BILLZ_VOLUME_FIELD`` → ``volume=None`` (not "")."""
    row = live(BillzServer(), volume_field="")._row(product(*A), A[1], timeutils.now())
    assert row.volume is None


# --- NEW-1: a cache failure after the payment went out never says "not applied" -----------------------------


def _request() -> SaleRequest:
    return SaleRequest(
        order_id=1, order_uuid=uuid.uuid4(), lines=[SaleLine("4607012301", 1, 185_000)], customer_phone=""
    )


class FlakyCache:
    """The Django cache, whose operations named in ``fail_ops`` raise like a Redis blip once ``armed``."""

    def __init__(self) -> None:
        self.armed = False
        self.fail_ops: set[str] = set()

    def __getattr__(self, name: str) -> Any:
        real = getattr(cache, name)

        def call(*args: Any, **kwargs: Any) -> Any:
            if self.armed and name in self.fail_ops:
                raise RedisConnectionError("Redis blip")
            return real(*args, **kwargs)

        return call


def test_a_cache_blip_after_a_paid_answer_keeps_the_payment_attempt() -> None:
    """NEW-1 (probe test_defect_cache_blip_after_successful_payment_forgets_the_attempt): Billz paid; the
    circuit bookkeeping right after the answer meets a Redis blip. The push succeeds (the bookkeeping is
    best effort) and the attempt stamp is never forgotten."""
    server = BillzServer()
    flaky = FlakyCache()
    original = server._default

    def payment(request: httpx.Request) -> httpx.Response:
        response = original("POST /v2/order-payment/{order_id}", request)
        flaky.armed, flaky.fail_ops = True, {"delete", "add", "incr", "set"}
        return response

    server.overrides["POST /v2/order-payment/{order_id}"] = payment
    journal = Journal()
    receipt = live(server, cache=flaky).push_sale(_request(), journal)
    assert receipt.billz_sale_id
    assert server.sales[receipt.billz_sale_id].payments
    assert journal.forgotten == 0 and journal.payment_attempted_at is not None


def test_a_cache_blip_after_a_payment_timeout_keeps_the_payment_attempt() -> None:
    """NEW-1 (probe test_defect_cache_blip_after_payment_timeout_forgets_the_attempt): the payment times out
    (outcome unknown) and counting the failure meets a Redis blip: still unknown, the stamp stays."""
    server = BillzServer()
    flaky = FlakyCache()

    def payment(request: httpx.Request) -> httpx.Response:
        flaky.armed, flaky.fail_ops = True, {"add", "incr"}
        raise httpx.ReadTimeout("timed out", request=request)

    server.overrides["POST /v2/order-payment/{order_id}"] = payment
    journal = Journal()
    with pytest.raises(BillzUnavailable) as caught:
        live(server, cache=flaky).push_sale(_request(), journal)
    assert not caught.value.not_applied
    assert journal.forgotten == 0 and journal.payment_attempted_at is not None


def test_no_error_after_an_answer_that_may_have_applied_says_not_applied() -> None:
    """NEW-1: a 5xx of a repeatable call is retried; when the retry cannot start (the circuit or the cache
    stops it), the error must not claim that nothing reached Billz."""
    server = BillzServer()
    flaky = FlakyCache()
    adapter = live(server, cache=flaky)
    products_call(adapter)  # log in

    def failing(request: httpx.Request) -> httpx.Response:
        flaky.armed, flaky.fail_ops = True, {"get"}  # the retry's circuit check cannot read the cache
        return httpx.Response(502, json={})

    server.overrides["GET /v2/products"] = failing
    with pytest.raises(BillzUnavailable, match="shared cache unavailable") as caught:
        products_call(adapter)
    assert not caught.value.not_applied


def test_safe_cache_lets_a_celery_soft_time_limit_through() -> None:
    """NEW-1 (probe test_defect_safecache_turns_celery_soft_time_limit_into_not_applied): only backend
    errors are "cache unavailable"; Celery's soft time limit (and any bug) propagates as itself."""

    class Slow:
        def __getattr__(self, name: str) -> Any:
            def call(*args: Any, **kwargs: Any) -> Any:
                raise SoftTimeLimitExceeded()

            return call

    with pytest.raises(SoftTimeLimitExceeded):
        SafeCache(Slow()).delete(billz_client.CIRCUIT_FAILURES_KEY)  # type: ignore[arg-type]
    for error in (RedisConnectionError("down"), ConnectionRefusedError(), TimeoutError()):

        class Down:
            def __getattr__(self, name: str, error: BaseException = error) -> Any:
                def call(*args: Any, **kwargs: Any) -> Any:
                    raise error

                return call

        with pytest.raises(BillzUnavailable, match="shared cache unavailable"):
            SafeCache(Down()).get("key")  # type: ignore[arg-type]


# --- NEW-3: one budget for the whole admin lookup -----------------------------------------------------------


def test_the_admin_lookup_ends_within_its_budget_over_every_search_page() -> None:
    """NEW-3 (probe test_defect_admin_lookup_total_time_exceeds_its_25_s_bound): up to ten search pages, each
    just under the 10 s read cap, used to take 90 s inside the idempotency transaction; the whole lookup now
    ends within the interactive budget (each read is cut to what is left, like a real socket timeout)."""
    clock = Clock()
    server = BillzServer(clock)
    others = [
        product(f"aaaaaaaa-0000-4000-8000-{index:012d}", f"9{index:03d}4607012301") for index in range(50)
    ]

    def search(request: httpx.Request) -> httpx.Response:
        read = float(request.extensions["timeout"]["read"])
        if read < 9.0:
            clock.now += read
            raise httpx.ReadTimeout("timed out", request=request)
        clock.now += 9.0  # just under the interactive read cap of 10 s
        return httpx.Response(200, json={"count": 500, "products": others})

    server.overrides["GET /v2/products"] = search
    adapter = live(server, clock)
    start = clock()
    with pytest.raises(BillzUnavailable):
        adapter.get_product("4607012301")
    assert clock() - start <= billz_live.LOOKUP_BUDGET_SECONDS + 0.01
    assert billz_live.LOOKUP_BUDGET_SECONDS == INTERACTIVE.call_budget == 25.0


# --- NEW-9: an answer that drips in is cut at the deadline --------------------------------------------------


class Drip(httpx.SyncByteStream):
    """A body sent a few bytes at a time, each chunk ``every`` seconds of the fake clock apart."""

    def __init__(self, clock: Clock, body: bytes, every: float) -> None:
        self.clock, self.body, self.every = clock, body, every

    def __iter__(self) -> Iterator[bytes]:
        for start in range(0, len(self.body), 4):
            self.clock.now += self.every
            yield self.body[start : start + 4]


def test_an_answer_dripping_past_the_push_deadline_is_abandoned() -> None:
    """NEW-9: the read timeout bounds each chunk only; a body dripping in under it is abandoned once the
    deadline passed — an unknown outcome (a payment keeps its attempt), never a push that outlives the
    outbox lease."""
    clock = Clock()
    server = BillzServer(clock)
    original = server._default

    def payment(request: httpx.Request) -> httpx.Response:
        answer = original("POST /v2/order-payment/{order_id}", request)
        return httpx.Response(200, stream=Drip(clock, answer.content, every=20.0))

    server.overrides["POST /v2/order-payment/{order_id}"] = payment
    journal = Journal()
    start = clock()
    with pytest.raises(BillzUnavailable, match="ReadTimeout") as caught:
        live(server, clock).push_sale(_request(), journal)
    assert not caught.value.not_applied
    assert journal.payment_attempted_at is not None and journal.forgotten == 0
    assert clock() - start <= billz_live.PUSH_DEADLINE_SECONDS + 20.0  # at most one chunk past it
