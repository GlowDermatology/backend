"""``BillzPort.read_stock`` (gap X-STOCK-FRESHNESS): one exact-barcode lookup per barcode within one deadline,
the interactive profile on a customer request, never an exception for a Billz failure (recorded Billz answers
over ``httpx.MockTransport``, no network)."""

from __future__ import annotations

import httpx
import pytest

from glowderma.integrations.billz.client import BATCH, INTERACTIVE
from glowderma.integrations.billz.fake import FakeBillz
from glowderma.integrations.billz.ports import BillzProduct, StockRead
from glowderma.integrations.tests.billz_support import BillzServer, Clock, fixture, live

SERUM, FOAM, CREAM = "4607012301", "4607012302", "4607012303"


def test_reads_each_barcode_with_the_interactive_profile() -> None:
    server = BillzServer()
    adapter = live(server)
    read = adapter.read_stock([SERUM, FOAM, "4607099999", "not-a-barcode", SERUM], budget_seconds=4.0)
    assert isinstance(read, StockRead)
    assert set(read.rows) == {SERUM, FOAM}
    assert (read.rows[SERUM].stock, read.rows[FOAM].stock) == (12, 0)  # BILLZ_SHOP_ID only
    assert read.rows[SERUM].billz_name == "HYALURONIC SERUM 30ML"
    assert read.unknown == {"4607099999", "not-a-barcode"} and read.unavailable == frozenset()
    searches = [r.url.params.get("search") for r in server.requests if r.url.path == "/v2/products"]
    assert searches == [SERUM, FOAM, "4607099999"]  # one lookup per distinct barcode; junk never asked
    assert adapter.interactive.profile is INTERACTIVE and adapter.client.profile is BATCH


def test_a_barcode_of_another_shop_reads_as_zero_stock() -> None:
    read = live(BillzServer()).read_stock([CREAM], budget_seconds=4.0)
    assert read.rows[CREAM].stock == 0  # listed by Billz, but no entry for our shop


@pytest.mark.parametrize("interactive", [True, False])
def test_billz_down_reports_every_barcode_unavailable_without_raising(interactive: bool) -> None:
    server = BillzServer()
    adapter = live(server)
    server.overrides["GET /v2/products"] = lambda request: httpx.Response(500, json=fixture("internal_error"))
    read = adapter.read_stock([SERUM, FOAM], budget_seconds=4.0, interactive=interactive)
    assert read.rows == {} and read.unknown == frozenset()
    assert read.unavailable == {SERUM, FOAM}


def test_a_failure_half_way_keeps_what_was_read() -> None:
    server = BillzServer()
    adapter = live(server)
    answered: list[str] = []
    original = server._products

    def first_only(request: httpx.Request) -> httpx.Response:
        if answered:
            raise httpx.ConnectError("refused", request=request)
        answered.append(request.url.params["search"])
        return original(request)

    server.overrides["GET /v2/products"] = first_only
    read = adapter.read_stock([FOAM, SERUM, CREAM], budget_seconds=4.0)
    assert set(read.rows) == {FOAM} and read.unavailable == {SERUM, CREAM}


def test_the_budget_bounds_the_whole_read() -> None:
    """One deadline for every barcode: once it passed, the rest are deferred (nothing sent; Billz is healthy,
    only the budget ran out — review fix: not unavailable)."""
    clock = Clock()
    server = BillzServer(clock)
    adapter = live(server, clock)
    original = server._products

    def slow(request: httpx.Request) -> httpx.Response:
        clock.now += 3.0  # each answer takes 3 s of the 4 s budget
        return original(request)

    server.overrides["GET /v2/products"] = slow
    read = adapter.read_stock([SERUM, FOAM, CREAM], budget_seconds=4.0)
    assert set(read.rows) == {SERUM}
    assert read.deferred == {FOAM, CREAM} and read.unavailable == frozenset()
    assert sum(1 for r in server.requests if r.url.path == "/v2/products") <= 2


def test_a_lookup_the_deadline_cuts_short_after_an_answer_is_deferred() -> None:
    """Review fix: a slow but healthy Billz (the deadline-capped read timeout of the 2nd lookup) defers the
    rest; it is not Billz failing."""
    clock = Clock()
    server = BillzServer(clock)
    adapter = live(server, clock)
    original = server._products
    started = clock.now

    def slow(request: httpx.Request) -> httpx.Response:
        if request.url.params.get("search") == SERUM:
            clock.now += 1.5
            return original(request)
        clock.now = started + 4.0  # the read timeout was capped by the 4 s deadline
        raise httpx.ReadTimeout("slow", request=request)

    server.overrides["GET /v2/products"] = slow
    read = adapter.read_stock([SERUM, FOAM, CREAM], budget_seconds=4.0)
    assert set(read.rows) == {SERUM}
    assert read.deferred == {FOAM, CREAM} and read.unavailable == frozenset()


def test_a_billz_that_answers_nothing_within_the_budget_is_unavailable() -> None:
    """A hang on the very first lookup (cut by the deadline) is Billz not answering: unavailable."""
    clock = Clock()
    server = BillzServer(clock)
    adapter = live(server, clock)
    started = clock.now

    def hang(request: httpx.Request) -> httpx.Response:
        clock.now = started + 4.0
        raise httpx.ReadTimeout("hang", request=request)

    server.overrides["GET /v2/products"] = hang
    read = adapter.read_stock([SERUM, FOAM], budget_seconds=4.0)
    assert read.unavailable == {SERUM, FOAM} and read.deferred == frozenset()


def test_an_ambiguous_barcode_is_deferred_and_the_rest_still_read() -> None:
    """Review nit: one barcode whose search fills every page without an exact match no longer ends the run."""
    server = BillzServer()
    adapter = live(server)
    original = server._products
    ambiguous = "4607012"

    def crowded(request: httpx.Request) -> httpx.Response:
        if request.url.params.get("search") == ambiguous:
            products = [{"id": f"p{i}", "barcode": f"{ambiguous}{i:05d}"} for i in range(50)]
            return httpx.Response(200, json={"count": 100_000, "products": products})
        return original(request)

    server.overrides["GET /v2/products"] = crowded
    read = adapter.read_stock([ambiguous, SERUM], budget_seconds=4.0)
    assert read.deferred == {ambiguous} and read.unavailable == frozenset()
    assert set(read.rows) == {SERUM}


def test_an_unconfigured_adapter_sends_nothing() -> None:
    server = BillzServer()
    adapter = live(server, secret_key="")
    assert not adapter.is_configured()
    read = adapter.read_stock([SERUM], budget_seconds=4.0)
    assert read.unavailable == {SERUM} and server.requests == []


def test_a_429_pause_fails_fast_on_the_interactive_profile() -> None:
    """Billz throttling us is the limiter's wait running out, not Billz failing: deferred (review fix)."""
    clock = Clock()
    server = BillzServer(clock)
    adapter = live(server, clock)
    adapter.client.limiter.pause(30)
    read = adapter.read_stock([SERUM], budget_seconds=4.0)
    assert read.deferred == {SERUM} and read.unavailable == frozenset() and sum(clock.sleeps) < 1
    assert server.requests == []


def test_the_fake_answers_like_its_lookup() -> None:
    class Known:
        def get(self, billz_id: str) -> BillzProduct | None:
            return None

        def all(self):  # type: ignore[no-untyped-def]
            return iter(())

    read = FakeBillz(Known()).read_stock(["4607012301", "4607010000"], budget_seconds=4.0)
    assert set(read.rows) == {"4607012301"} and read.unknown == {"4607010000"}
    assert read.unavailable == frozenset() and read.deferred == frozenset()
