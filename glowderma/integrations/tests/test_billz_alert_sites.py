"""The Billz adapter's ERROR events raise their alerts at the sites that log them (core.alerts;
docs/integrations/billz.md, docs/ops/monitoring.md): configuration errors, the open circuit, the shared cache
being down, a sale whose state is unclear and a bill that is not what the customer paid. Ids and codes only.
Recorded answers (``billz_support.BillzServer``); no network."""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from django.test import override_settings

from glowderma.core import alerts
from glowderma.core.models import OutboxMessage
from glowderma.integrations.billz.ports import (
    BillzCheckByHand,
    BillzMisconfigured,
    BillzUnavailable,
    SaleLine,
)
from glowderma.integrations.tests.billz_support import (
    FOAM_ID,
    SALE_ID,
    SERUM_ID,
    BillzServer,
    Clock,
    Journal,
    Sale,
    fixture,
    live,
)
from glowderma.integrations.tests.test_billz_live import PHONE, sale
from glowderma.integrations.tests.test_billz_live_robustness import BrokenCache, products_call

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def alerts_on() -> Any:
    with override_settings(ALERT_WEBHOOK_URL="https://alerts.example.uz/hook"):
        yield


def raised(event: str) -> list[dict[str, Any]]:
    return [
        message.payload["fields"]
        for message in OutboxMessage.objects.filter(topic=alerts.ALERT_TOPIC).order_by("id")
        if message.payload["event"] == event
    ]


def scopes(event: str) -> list[str]:
    """The scope part of each alert's dedupe key (``alert:<event>:<scope>:<window>``)."""
    return [
        message.dedupe_key.split(":")[2]
        for message in OutboxMessage.objects.filter(topic=alerts.ALERT_TOPIC).order_by("id")
        if message.payload["event"] == event
    ]


def test_the_shared_cache_being_down_alerts_once_per_window() -> None:
    adapter = live(BillzServer(), cache=BrokenCache())
    for _ in range(3):
        with pytest.raises(BillzUnavailable, match="shared cache unavailable"):
            adapter.get_product("4607012301")
    (fields,) = raised("billz.cache_unavailable")
    assert fields["error"] == "ConnectionError" and isinstance(fields["operation"], str)


def test_a_refused_key_and_a_refused_route_are_configuration_errors() -> None:
    server = BillzServer()
    server.overrides["POST /v1/auth/login"] = lambda request: httpx.Response(
        403, json=fixture("login_forbidden")
    )
    with pytest.raises(BillzMisconfigured):
        products_call(live(server))
    assert raised("billz.configuration_error") == [{"case": "key_refused", "http_status": 403}]
    OutboxMessage.objects.filter(topic=alerts.ALERT_TOPIC).delete()
    server = BillzServer()
    server.overrides["GET /v2/products"] = lambda request: httpx.Response(403, json=fixture("forbidden"))
    with pytest.raises(BillzMisconfigured):
        products_call(live(server))
    assert raised("billz.configuration_error") == [
        {"case": "refused", "http_status": 403, "error_code": None}
    ]


def test_an_open_circuit_alerts() -> None:
    clock = Clock()
    server = BillzServer(clock)
    adapter = live(server, clock)

    def hang(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    server.overrides["GET /v2/products"] = hang
    for _ in range(2):
        with pytest.raises(BillzUnavailable):
            products_call(adapter)
    assert raised("billz.circuit_open") == [{"failures": 5, "open_seconds": 90}]


def test_a_sale_with_contradicting_paid_signals_alerts() -> None:
    server = BillzServer()
    server.sales[SALE_ID] = Sale(SALE_ID, "5632631379", items={SERUM_ID: 3.0, FOAM_ID: 1.0})
    original = server._detail

    def detail(sale_: Sale) -> dict[str, Any]:
        body = original(sale_)
        body["order_detail"]["order_payments"] = [{"paid_amount": 650_000}]
        return body

    server._detail = detail  # type: ignore[method-assign,assignment]
    with pytest.raises(BillzUnavailable, match="contradicting"):
        live(server).push_sale(sale(), Journal(server, billz_order_id=SALE_ID))
    assert raised("billz.sale_state_unclear") == [{"order_id": 10248}]
    assert scopes("billz.sale_state_unclear") == ["10248"]  # one alert per order, like billz.sale_dead


def test_a_bill_other_than_what_the_customer_paid_alerts() -> None:
    server = BillzServer()

    def billz_prices(request: httpx.Request) -> httpx.Response:
        bill = fixture("recalculate")
        bill["total_price"] = 370_000
        return httpx.Response(200, json=bill)

    server.overrides["POST /v1/recalculate-order-bill/{order_id}"] = billz_prices
    with pytest.raises(BillzCheckByHand):
        live(server).push_sale(sale(PHONE, SaleLine("4607012301", 2, 150_000)), Journal(server))
    assert raised("billz.sale_total_differs") == [{"order_id": 10248}]
    assert scopes("billz.sale_total_differs") == ["10248"]
