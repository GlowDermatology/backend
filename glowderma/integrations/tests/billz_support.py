"""A stateful Billz stand-in for the live-adapter tests, served through ``httpx.MockTransport`` (no network).

Every answer is built from the recorded fixtures in ``fixtures/billz/`` (openapi.yaml schemas and examples);
the server only keeps the state a real Billz would (tokens, sales, their items, clients, payments) so the
tests can crash a push at any step and check that the retry resumes instead of duplicating.
"""

from __future__ import annotations

import copy
import json
import re
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx

from glowderma.core import timeutils
from glowderma.integrations.billz.client import BillzClient, BillzConfig
from glowderma.integrations.billz.live import LiveBillz
from glowderma.integrations.billz.ports import SaleCancelled, SaleProgress

FIXTURES = Path(__file__).parent / "fixtures" / "billz"
SHOP_ID = "c1056fcc-539b-47ea-8bc4-d9e337c433f0"
OTHER_SHOP_ID = "6f244db4-75d6-4fac-9841-14867d45e36a"
CASHBOX_ID = "9b4a7f8c-a8ef-4870-8a1b-89869fdd4141"
PAYMENT_TYPE_ID = "c6e84d9b-0ef0-4d5a-886d-147c74a9b051"
PLATFORM_ID = "7d4a4c38-dd84-4902-b744-0488b80a4c01"
SECRET = "billz-integration-secret-for-tests"  # noqa: S105 - a test value
SALE_ID = "38c3920a-ffdd-4233-a823-8af3266f5f4f"
SERUM_ID = "2ce40c63-4527-4898-a975-7c30c255fd50"  # barcode 4607012301
FOAM_ID = "1b5e3f0d-ad00-44e5-981a-2c88af848ba3"  # barcode 4607012302
CLIENT_ID = "622c8993-9f82-45ae-a69d-63103d8506d5"  # phone +998901234567
NEW_CLIENT_ID = "8f7e6d5c-0000-4000-8000-000000000001"


def fixture(name: str) -> dict[str, Any]:
    data = json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))
    data.pop("_source", None)
    return data  # type: ignore[no-any-return]


def config(**overrides: Any) -> BillzConfig:
    values: dict[str, Any] = {
        "base_url": "https://billz.test",
        "secret_key": SECRET,
        "platform_id": PLATFORM_ID,
        "shop_id": SHOP_ID,
        "cashbox_id": CASHBOX_ID,
        "payment_type_id": PAYMENT_TYPE_ID,
        "rate_limit_per_second": 1000.0,
        "page_size": 2,
        "volume_field": "Объём",
    }
    values.update(overrides)
    return BillzConfig(**values)


class Clock:
    """A fake wall clock: ``sleep`` advances it (and records how long each sleep was)."""

    def __init__(self, start: float = 1_800_000_000.0) -> None:
        self.now = start
        self.sleeps: list[float] = []
        self.lock = threading.Lock()

    def __call__(self) -> float:
        with self.lock:
            return self.now

    def sleep(self, seconds: float) -> None:
        with self.lock:
            self.sleeps.append(seconds)
            self.now += max(seconds, 0.0)


class WorkerCrash(Exception):
    """Raised by the server after it applied a request: the worker died before it saw the answer."""


@dataclass
class Sale:
    id: str
    number: str
    items: dict[str, float] = field(default_factory=dict)
    customer_id: str = ""
    payments: list[dict[str, Any]] = field(default_factory=list)
    deleted: bool = False
    free_prices: dict[str, float] = field(default_factory=dict)  # product id → use_free_price's free_price


class Journal:
    """An in-memory ``SaleJournal``: what the purchase row would persist. A test resumes a push by passing the
    same journal again (``billz_order_id`` / ``payment_attempted_at`` survive like committed columns)."""

    def __init__(
        self,
        server: BillzServer | None = None,
        *,
        billz_order_id: str = "",
        fail: bool = False,
        cancelled: bool = False,
    ) -> None:
        self.server = server
        self.fail = fail
        self.cancelled = cancelled
        self.billz_order_id = billz_order_id
        self.billz_sale_ref = ""
        self.payment_attempted_at: datetime | None = None
        self.pushed = False
        self.recorded: list[tuple[str, str]] = []
        self.requests_before: list[str] = []
        self.payments_announced = 0
        self.forgotten = 0

    def progress(self) -> SaleProgress:
        return SaleProgress(
            billz_order_id=self.billz_order_id,
            billz_sale_ref=self.billz_sale_ref,
            payment_attempted_at=self.payment_attempted_at,
            pushed=self.pushed,
        )

    def order_created(self, billz_order_id: str, billz_sale_ref: str) -> None:
        if self.server is not None:
            self.requests_before = self.server.labels()
        if self.fail:
            raise RuntimeError("database unavailable")
        self.recorded.append((billz_order_id, billz_sale_ref))
        self.billz_order_id, self.billz_sale_ref = billz_order_id, billz_sale_ref

    def before_payment(self) -> None:
        if self.cancelled:
            raise SaleCancelled("order cancelled")
        self.payments_announced += 1
        self.payment_attempted_at = timeutils.now()

    def payment_not_applied(self) -> None:
        self.forgotten += 1
        self.payment_attempted_at = None


Handler = Callable[[httpx.Request], httpx.Response]


class BillzServer:
    """Routes ``(METHOD, path template)`` like Billz; ``override`` scripts one route, ``crash_after`` kills
    the worker right after Billz applied a request with that label."""

    ROUTES: tuple[tuple[str, re.Pattern[str], str], ...] = (
        ("POST", re.compile(r"^/v1/auth/login$"), "POST /v1/auth/login"),
        ("POST", re.compile(r"^/v2/auth/refresh$"), "POST /v2/auth/refresh"),
        ("GET", re.compile(r"^/v2/products$"), "GET /v2/products"),
        ("POST", re.compile(r"^/v2/order$"), "POST /v2/order"),
        ("GET", re.compile(r"^/v2/order/(?P<id>[^/]+)$"), "GET /v2/order/{id}"),
        ("POST", re.compile(r"^/v2/order-product/(?P<id>[^/]+)$"), "POST /v2/order-product/{order_id}"),
        ("GET", re.compile(r"^/v1/client$"), "GET /v1/client"),
        ("POST", re.compile(r"^/v1/client$"), "POST /v1/client"),
        (
            "PUT",
            re.compile(r"^/v2/order-customer-new/(?P<id>[^/]+)$"),
            "PUT /v2/order-customer-new/{order_id}",
        ),
        (
            "POST",
            re.compile(r"^/v1/recalculate-order-bill/(?P<id>[^/]+)$"),
            "POST /v1/recalculate-order-bill/{order_id}",
        ),
        ("POST", re.compile(r"^/v2/order-payment/(?P<id>[^/]+)$"), "POST /v2/order-payment/{order_id}"),
    )

    def __init__(self, clock: Clock | None = None) -> None:
        self.clock = clock
        self.lock = threading.Lock()
        self.requests: list[httpx.Request] = []
        self.times: list[float] = []
        self.overrides: dict[str, Handler] = {}
        self.crash_after: set[str] = set()
        self.access_tokens: set[str] = {"access-from-login", "access-from-refresh"}
        self.sales: dict[str, Sale] = {}
        self.clients: dict[str, str] = {"+998901234567": CLIENT_ID}
        self.product_pages: list[dict[str, Any]] = [fixture("products_page1"), fixture("products_page2")]
        self.prices = {SERUM_ID: 185_000, FOAM_ID: 99_000}
        self.created_clients = 0

    # --- helpers for the tests ---

    def labels(self) -> list[str]:
        return [self.label(request) for request in self.requests]

    def count(self, label: str) -> int:
        return self.labels().count(label)

    def label(self, request: httpx.Request) -> str:
        for method, pattern, label in self.ROUTES:
            if request.method == method and pattern.match(request.url.path):
                return label
        return f"{request.method} {request.url.path}"

    def bodies(self, label: str) -> list[Any]:
        return [json.loads(r.content or b"null") for r in self.requests if self.label(r) == label]

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self)

    # --- dispatch ---

    def __call__(self, request: httpx.Request) -> httpx.Response:
        with self.lock:
            self.requests.append(request)
            self.times.append(self.clock() if self.clock is not None else 0.0)
        label = self.label(request)
        handler = self.overrides.get(label)
        response = handler(request) if handler is not None else self._default(label, request)
        if label in self.crash_after:
            self.crash_after.discard(label)
            raise WorkerCrash(label)
        return response

    def _authorized(self, request: httpx.Request) -> bool:
        header = request.headers.get("Authorization", "")
        return header.startswith("Bearer ") and header[7:] in self.access_tokens

    def _default(self, label: str, request: httpx.Request) -> httpx.Response:
        if label == "POST /v1/auth/login":
            return httpx.Response(200, json=fixture("login_ok"))
        if label == "POST /v2/auth/refresh":
            return httpx.Response(200, json=fixture("refresh_ok"))
        if not self._authorized(request):
            return httpx.Response(401, json=fixture("unauthorized_expired"))
        sale_id = request.url.path.rsplit("/", 1)[-1]
        if label == "GET /v2/products":
            return self._products(request)
        if label == "POST /v2/order":
            body = fixture("order_created")
            new_id = (
                SALE_ID if SALE_ID not in self.sales else f"{len(self.sales):08d}-0000-4000-8000-000000000000"
            )
            body["id"] = body["data"]["id"] = new_id
            self.sales[new_id] = Sale(new_id, body["data"]["order_number"])
            return httpx.Response(200, json=body)
        if label == "GET /v2/order/{id}":
            sale = self.sales.get(sale_id)
            if sale is None:
                return httpx.Response(
                    404, json={"code": 404, "message": "not found", "error": "", "data": None}
                )
            return httpx.Response(200, json=self._detail(sale))
        if label == "POST /v2/order-product/{order_id}":
            self.apply_item(sale_id, json.loads(request.content))
            return httpx.Response(200, json={**fixture("async_ok"), "id": sale_id})
        if label == "GET /v1/client":
            phone = request.url.params.get("phone_number", "")
            client_id = self.clients.get(phone)
            if client_id is None:
                return httpx.Response(200, json=fixture("clients_empty"))
            clients = fixture("clients")
            clients["clients"][0].update(id=client_id, phone_numbers=[phone])
            return httpx.Response(200, json=clients)
        if label == "POST /v1/client":
            payload = json.loads(request.content)
            self.created_clients += 1
            client_id = fixture("client_created")["id"]
            self.clients[payload["phone_number"]] = client_id
            return httpx.Response(200, json={"id": client_id})
        if label == "PUT /v2/order-customer-new/{order_id}":
            self.sales[sale_id].customer_id = json.loads(request.content)["customer_id"]
            return httpx.Response(200, json={**fixture("async_ok"), "id": sale_id})
        if label == "POST /v1/recalculate-order-bill/{order_id}":
            sale = self.sales[sale_id]
            bill = fixture("recalculate")
            bill["order_id"] = sale_id
            bill["total_price"] = self.bill_total(sale)
            bill["items"] = [{"product_id": pid, "measurement_value": qty} for pid, qty in sale.items.items()]
            return httpx.Response(200, json=bill)
        if label == "POST /v2/order-payment/{order_id}":
            sale = self.sales[sale_id]
            payload = json.loads(request.content)
            if sale.payments:
                error = fixture("payment_error")
                error["data"].update(error_code=20999, message="order already finished")
                return httpx.Response(400, json=error)
            sale.payments.append(payload)
            return httpx.Response(200, json=fixture("payment_ok"))
        return httpx.Response(404, json={"code": 404, "message": "no route", "error": "", "data": None})

    def apply_item(self, sale_id: str, payload: dict[str, Any]) -> None:
        """Billz's ``POST /v2/order-product``: an upsert with the absolute quantity (and the free price)."""
        sale = self.sales[sale_id]
        product_id = payload["product_id"]
        quantity = float(payload["sold_measurement_value"])
        if quantity == 0:
            sale.items.pop(product_id, None)
            sale.free_prices.pop(product_id, None)
            return
        sale.items[product_id] = quantity
        if payload.get("use_free_price"):
            sale.free_prices[product_id] = float(payload["free_price"])
        else:
            sale.free_prices.pop(product_id, None)

    def bill_total(self, sale: Sale) -> float:
        """Σ quantity × (the item's free price, else Billz's retail price)."""
        return sum(
            qty * sale.free_prices.get(pid, self.prices.get(pid, 0)) for pid, qty in sale.items.items()
        )

    def _products(self, request: httpx.Request) -> httpx.Response:
        params = request.url.params
        if "search" in params:
            wanted = params["search"]
            found = [
                product
                for page in self.product_pages
                for product in page["products"]
                if wanted in str(product.get("barcode", ""))
            ]
            return httpx.Response(200, json={"count": len(found), "products": found})
        page = int(params.get("page", "1"))
        if page > len(self.product_pages):
            return httpx.Response(200, json={"count": self.product_pages[0]["count"], "products": []})
        return httpx.Response(200, json=copy.deepcopy(self.product_pages[page - 1]))

    def _detail(self, sale: Sale) -> dict[str, Any]:
        detail = fixture("order_detail")
        detail["id"] = detail["order_detail"]["order_id"] = sale.id
        detail["order_number"] = sale.number
        detail["order_detail"]["order_items"] = [
            {"product_id": pid, "measurement_value": qty, "is_returned": False}
            for pid, qty in sale.items.items()
        ]
        detail["customer_id"] = sale.customer_id
        detail["deleted"] = sale.deleted
        if sale.payments:
            detail["finished_at"] = "2026-10-26 14:05:20"
        return detail


def live(
    server: BillzServer, clock: Clock | None = None, *, cache: Any = None, **overrides: Any
) -> LiveBillz:
    """A ``LiveBillz`` against ``server`` (fake clock and sleeps when ``clock`` is given; ``cache`` replaces
    the Django cache everywhere the adapter uses one)."""
    cfg = config(**overrides)
    kwargs: dict[str, Any] = {"transport": server.transport, "jitter": lambda: 0.5}
    if clock is not None:
        kwargs.update(clock=clock, sleep=clock.sleep)
    if cache is not None:
        kwargs["cache"] = cache
    return LiveBillz(cfg, client=BillzClient(cfg, **kwargs), cache=cache)
