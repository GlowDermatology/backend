"""``POST delivery/calculate/`` with the plan §3.4 keys: Yandex Standard / Express options next to BTS, every
refusal reason, the stored and reused quotes, the fee policy, and the released answer for requests without
the new keys."""

# ruff: noqa: F811 - the fixtures come from yandex_checkout_support and are requested by name

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from django.test import override_settings
from rest_framework.test import APIClient

from glowderma.accounts.models import Customer
from glowderma.commerce import quoting
from glowderma.commerce.tests.support import product
from glowderma.commerce.tests.yandex_checkout_support import (  # noqa: F401 - fixtures
    CALCULATE,
    LEGACY_KEYS,
    NEW_KEYS,
    OPTION_KEYS,
    calculate,
    configure_warehouse,
    expected_fee,
    expected_window,
    fill,
    fresh_fake_yandex,
    frozen,
    key,
    option,
    pinned_address,
    price_calls,
    scenario_header,
)
from glowderma.core import timeutils
from glowderma.delivery.models import DeliveryQuote, DeliverySettings, WarehouseClosure
from glowderma.integrations.bts.fake import FakeBts

pytestmark = pytest.mark.django_db
BTS_FEE = FakeBts().quote("10", "1001")


def _ready(customer: Customer, *, quantity: int = 1, price: int = 300_000, **warehouse: Any) -> Any:
    configure_warehouse(**warehouse)
    fill(customer, (product(price=price), quantity))
    return pinned_address(customer)


def test_standard_and_express_options_next_to_bts(
    customer_client: APIClient, customer: Customer, frozen: Any, price_calls: list[str]
) -> None:
    address = _ready(customer, quantity=2)
    response = calculate(customer_client, address)
    assert response.status_code == 200, response.json()
    body = response.json()
    assert set(body) == LEGACY_KEYS | NEW_KEYS
    assert [(o["provider"], o["method"]) for o in body["options"]] == [
        ("yandex", "standard"),
        ("yandex", "express"),
        ("bts", None),
    ]
    assert all(set(o) == OPTION_KEYS for o in body["options"])
    assert price_calls == ["courier", "express"]  # standard → courier class, express → express class
    standard, express = option(body, "yandex", "standard"), option(body, "yandex", "express")
    assert standard["fee"] == expected_fee("courier") and express["fee"] == expected_fee("express")
    assert standard["fee"] % 500 == 0 and standard["available"] and standard["unavailable_reason"] is None
    assert standard["label"] == "Yandex Standart" and express["label"] == "Yandex Ekspress"
    window = expected_window("standard", frozen)
    assert standard["eta_from"] == timeutils.isoformat(window.eta_from)
    assert standard["eta_to"] == timeutils.isoformat(window.eta_to)
    assert standard["scheduled"] is window.scheduled
    assert standard["expected_total"] == 600_000 + standard["fee"]
    bts = option(body, "bts")
    assert bts["fee"] == BTS_FEE and bts["quote_id"] is None and bts["eta_from"] is None
    # default: Yandex Standard; the released keys describe the selected option
    assert (body["provider"], body["method"], body["quote_id"]) == (
        "yandex",
        "standard",
        standard["quote_id"],
    )
    assert body["delivery_fee"] == standard["fee"] and body["expected_total"] == 600_000 + standard["fee"]
    assert body["weight_g"] == 600 and body["region_code"] == "10" and body["quote_expires_at"]
    quotes = {str(q.pk): q for q in DeliveryQuote.objects.filter(customer=customer)}
    assert set(quotes) == {standard["quote_id"], express["quote_id"]}
    stored = quotes[standard["quote_id"]]
    assert (stored.provider, stored.method, stored.weight_g, stored.customer_fee) == (
        "yandex",
        "standard",
        600,
        standard["fee"],
    )
    assert stored.address_id == address.pk and stored.address_text == address.text
    assert (stored.latitude, stored.longitude) == (address.latitude, address.longitude)
    assert stored.expires_at == frozen + timedelta(minutes=15) and stored.used_at is None
    assert stored.meta["zone_id"] == "tashkent" and stored.meta["taxi_class"] == "courier"


def test_preferred_method_and_provider(customer_client: APIClient, customer: Customer, frozen: Any) -> None:
    address = _ready(customer)
    express = calculate(customer_client, address, delivery_method="express").json()
    assert (express["provider"], express["method"]) == ("yandex", "express")
    assert express["delivery_fee"] == option(express, "yandex", "express")["fee"]
    bts = calculate(customer_client, address, delivery_provider="bts").json()
    assert (bts["provider"], bts["method"], bts["quote_id"]) == ("bts", None, None)
    assert bts["delivery_fee"] == BTS_FEE and bts["weight_g"] == 1000
    # a preference without with_options still gets the options answer
    only = customer_client.post(
        CALCULATE,
        {"fulfilment": "delivery", "address_id": address.pk, "delivery_provider": "bts"},
        format="json",
    ).json()
    assert set(only) == LEGACY_KEYS | NEW_KEYS and only["provider"] == "bts"


def test_a_repeated_calculate_reuses_the_stored_quote(
    customer_client: APIClient, customer: Customer, frozen: Any, price_calls: list[str]
) -> None:
    address = _ready(customer)
    first = calculate(customer_client, address).json()
    second = calculate(customer_client, address).json()
    assert price_calls == ["courier", "express"]  # no second Yandex call
    assert [o["quote_id"] for o in first["options"]] == [o["quote_id"] for o in second["options"]]
    assert DeliveryQuote.objects.count() == 2
    # another weight is another quote
    fill(customer, (product(), 1))
    third = calculate(customer_client, address).json()
    assert price_calls == ["courier", "express"] * 2
    assert third["quote_id"] != first["quote_id"] and third["weight_g"] == 600


def test_an_expired_or_used_quote_is_not_reused(
    customer_client: APIClient, customer: Customer, frozen: Any, price_calls: list[str]
) -> None:
    address = _ready(customer)
    first = calculate(customer_client, address).json()
    DeliveryQuote.objects.filter(pk=first["quote_id"]).update(
        created_at=frozen - timedelta(minutes=20), expires_at=frozen
    )
    second = calculate(customer_client, address).json()
    assert second["quote_id"] != first["quote_id"] and price_calls == ["courier", "express", "courier"]


def test_a_replayed_key_asks_nobody(
    customer_client: APIClient, customer: Customer, frozen: Any, price_calls: list[str]
) -> None:
    address = _ready(customer)
    headers = key()
    body = {"fulfilment": "delivery", "address_id": address.pk, "with_options": True}
    first = customer_client.post(CALCULATE, body, format="json", **headers)
    DeliveryQuote.objects.all().delete()
    again = customer_client.post(CALCULATE, body, format="json", **headers)
    assert again.status_code == 200 and again["Idempotent-Replayed"] == "true"
    assert again.json() == first.json() and price_calls == ["courier", "express"]
    assert not DeliveryQuote.objects.exists()


def test_missing_pin_needs_coordinates_and_bts_is_selected(
    customer_client: APIClient, customer: Customer, price_calls: list[str]
) -> None:
    configure_warehouse()
    fill(customer, (product(), 1))
    address = pinned_address(customer, pin=False)
    body = calculate(customer_client, address).json()
    for method in ("standard", "express"):
        entry = option(body, "yandex", method)
        assert entry["available"] is False and entry["unavailable_reason"] == "coordinates_required"
        assert entry["fee"] is None and entry["quote_id"] is None and entry["expected_total"] is None
    assert body["provider"] == "bts" and body["delivery_fee"] == BTS_FEE
    assert price_calls == [] and not DeliveryQuote.objects.exists()


def test_a_region_pair_is_bts_only(customer_client: APIClient, customer: Customer) -> None:
    configure_warehouse()
    fill(customer, (product(), 1))
    body = calculate(customer_client, region_code="10", city_code="1001").json()
    assert option(body, "yandex", "standard")["unavailable_reason"] == "coordinates_required"
    assert body["provider"] == "bts" and body["region_code"] == "10"


def test_yandex_down_offers_no_price_and_bts_stays(customer_client: APIClient, customer: Customer) -> None:
    address = _ready(customer)
    body = calculate(customer_client, address, header=scenario_header({"check_price": "down"})).json()
    for method in ("standard", "express"):
        entry = option(body, "yandex", method)
        assert (entry["available"], entry["unavailable_reason"], entry["fee"]) == (
            False,
            "provider_unavailable",
            None,
        )
    assert body["provider"] == "bts" and option(body, "bts")["available"] is True
    assert not DeliveryQuote.objects.exists()


@pytest.mark.parametrize("answer", ["rejected", "no_tariff"])
def test_out_of_zone(customer_client: APIClient, customer: Customer, answer: str) -> None:
    address = _ready(customer)
    body = calculate(customer_client, address, header=scenario_header({"check_price": answer})).json()
    assert option(body, "yandex", "standard")["unavailable_reason"] == "out_of_zone"
    assert option(body, "yandex", "express")["unavailable_reason"] == "out_of_zone"


def test_too_heavy_for_the_courier_class(customer_client: APIClient, customer: Customer) -> None:
    address = _ready(customer, quantity=3, default_item_weight_g=4_000)  # 12 kg
    body = calculate(customer_client, address).json()
    assert option(body, "yandex", "standard")["unavailable_reason"] == "too_heavy"
    assert option(body, "yandex", "express")["available"] is True  # ≤ 20 kg
    assert (body["provider"], body["method"], body["weight_g"]) == ("yandex", "express", 12_000)
    DeliverySettings.objects.filter(pk=1).update(default_item_weight_g=8_000)  # 24 kg
    heavier = calculate(customer_client, address).json()
    assert option(heavier, "yandex", "express")["unavailable_reason"] == "too_heavy"
    assert heavier["provider"] == "bts"


def test_suspended_dispatch_and_unconfigured_warehouse(
    customer_client: APIClient, customer: Customer
) -> None:
    address = _ready(customer, dispatch_suspended=True)
    body = calculate(customer_client, address).json()
    assert option(body, "yandex", "standard")["unavailable_reason"] == "dispatch_suspended"
    DeliverySettings.objects.filter(pk=1).update(dispatch_suspended=False, contact_name="")
    unconfigured = calculate(customer_client, address).json()
    assert option(unconfigured, "yandex", "express")["unavailable_reason"] == "not_configured"


def test_a_closed_warehouse_schedules_the_window(
    customer_client: APIClient, customer: Customer, frozen: Any
) -> None:
    address = _ready(customer)
    WarehouseClosure.objects.create(date=timeutils.local_date(frozen), note="Bayram")
    body = calculate(customer_client, address).json()
    standard = option(body, "yandex", "standard")
    assert standard["scheduled"] is True
    window = expected_window("standard", frozen)
    assert window.scheduled and standard["eta_from"] == timeutils.isoformat(window.eta_from)
    assert timeutils.local_date(window.eta_from) > timeutils.local_date(frozen)


def test_a_disabled_method_is_omitted(customer_client: APIClient, customer: Customer) -> None:
    address = _ready(customer, express=False)
    body = calculate(customer_client, address).json()
    assert [(o["provider"], o["method"]) for o in body["options"]] == [("yandex", "standard"), ("bts", None)]
    DeliverySettings.objects.filter(pk=1).update(standard_enabled=False)
    assert [o["provider"] for o in calculate(customer_client, address).json()["options"]] == ["bts"]


def test_the_pilot_list(customer_client: APIClient, customer: Customer) -> None:
    address = _ready(customer)
    with override_settings(YANDEX_DELIVERY_PILOT_CUSTOMER_IDS=[customer.pk + 1000]):
        outside = calculate(customer_client, address).json()
    assert [o["provider"] for o in outside["options"]] == ["bts"]
    with override_settings(YANDEX_DELIVERY_PILOT_CUSTOMER_IDS=[customer.pk]):
        inside = calculate(customer_client, address).json()
    assert inside["provider"] == "yandex"


def test_yandex_is_not_offered_when_fake_under_production(
    customer_client: APIClient, customer: Customer
) -> None:
    address = _ready(customer)
    with override_settings(DEV_FEATURES_ALLOWED=False, YANDEX_DELIVERY_MODE="fake", BTS_MODE="fake"):
        assert quoting.yandex_offered(customer.pk) is False
        response = calculate(customer_client, address)
    assert response.status_code == 409 and response.json()["code"] == "delivery_unavailable"
    assert not DeliveryQuote.objects.exists()


def test_fee_policy_markup_vat_and_rounding(customer_client: APIClient, customer: Customer) -> None:
    address = _ready(customer, markup_percent="10.00", estimate_vat_percent="12.00")
    body = calculate(customer_client, address, header=scenario_header({"price": 20_001})).json()
    # 20 001 × 1.12 × 1.10 = 24 641.232 → rounded up to 500 so'm
    assert option(body, "yandex", "standard")["fee"] == 25_000
    stored = DeliveryQuote.objects.get(method="standard")
    assert (stored.carrier_price, stored.customer_fee) == (20_001, 25_000)
    DeliverySettings.objects.filter(pk=1).update(markup_percent="0.00", estimate_vat_percent="0.00")
    DeliveryQuote.objects.all().delete()  # an unexpired quote would be reused at its stored fee
    exact = calculate(
        customer_client, address, delivery_method="express", header=scenario_header({"price": 20_000})
    ).json()
    assert option(exact, "yandex", "express")["fee"] == 20_000


def test_cashback_follows_the_selected_fee(customer_client: APIClient, customer: Customer) -> None:
    from glowderma.commerce.tests.support import credit, seed_policies
    from glowderma.commerce.tests.webhook_support import real_now

    seed_policies()
    credit(customer, 100_000, real_now())
    address = _ready(customer)
    body = calculate(customer_client, address, cashback_to_use=10**6).json()
    standard = option(body, "yandex", "standard")
    assert body["cashback_to_use"] == body["cashback_max"] > 0
    assert body["expected_total"] == 300_000 + standard["fee"] - body["cashback_max"]
    assert standard["expected_total"] == body["expected_total"]


def test_nothing_available_is_409_with_the_options(customer_client: APIClient, customer: Customer) -> None:
    configure_warehouse()
    fill(customer, (product(), 1))
    address = pinned_address(customer, region="", city="")
    response = calculate(customer_client, address, header=scenario_header({"check_price": "down"}))
    assert response.status_code == 409 and response.json()["code"] == "delivery_unavailable"
    reasons = [o["unavailable_reason"] for o in response.json()["details"]["options"]]
    assert reasons == ["provider_unavailable", "provider_unavailable", "out_of_zone"]


def test_options_keep_the_released_address_and_cart_checks(
    customer_client: APIClient, customer: Customer
) -> None:
    configure_warehouse()
    missing = calculate(customer_client).json()
    assert missing["errors"][0]["code"] == "required"
    empty = calculate(customer_client, pinned_address(customer))
    assert empty.status_code == 409 and empty.json()["code"] == "cart_empty"


def test_pickup_with_options(customer_client: APIClient, customer: Customer) -> None:
    configure_warehouse()
    fill(customer, (product(price=300_000), 1))
    body = calculate(customer_client, fulfilment="pickup").json()
    assert set(body) == LEGACY_KEYS | NEW_KEYS
    assert body["options"] == [] and body["provider"] is None and body["quote_id"] is None
    assert body["delivery_fee"] == 0 and body["expected_total"] == 300_000 and body["weight_g"] is None


def test_legacy_request_gets_the_released_answer(
    customer_client: APIClient, customer: Customer, price_calls: list[str]
) -> None:
    """A request with the released keys only: exactly the released keys, BTS fee, no Yandex call, no quote —
    and the BTS values equal the ``bts`` option of the options answer (a superset)."""
    address = _ready(customer)
    legacy = customer_client.post(
        CALCULATE, {"fulfilment": "delivery", "address_id": address.pk}, format="json"
    ).json()
    assert set(legacy) == LEGACY_KEYS
    assert legacy["delivery_fee"] == BTS_FEE and legacy["weight_g"] == 1000
    assert price_calls == [] and not DeliveryQuote.objects.exists()
    explicit = customer_client.post(
        CALCULATE, {"fulfilment": "delivery", "address_id": address.pk, "with_options": False}, format="json"
    ).json()
    assert set(explicit) == LEGACY_KEYS
    superset = calculate(customer_client, address, delivery_provider="bts").json()
    assert {k: v for k, v in superset.items() if k in LEGACY_KEYS and k != "quoted_at"} == {
        k: v for k, v in legacy.items() if k != "quoted_at"
    }
