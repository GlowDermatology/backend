"""The server cart (customer contract §2.5): every rule of the endpoint table, idempotent adds, statuses."""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any

import pytest
from django.test import override_settings
from rest_framework.test import APIClient

from glowderma.accounts.models import Customer
from glowderma.catalog.models import Category
from glowderma.commerce.models import CartItem
from glowderma.commerce.tests.support import credit, place, product, seed_policies, shopper
from glowderma.commerce.tests.webhook_support import real_now

pytestmark = pytest.mark.django_db
CART = "/api/v1/cart/"
ITEMS = "/api/v1/cart/items/"


def key() -> dict[str, str]:
    return {"HTTP_IDEMPOTENCY_KEY": str(uuid.uuid4())}


def add(client: APIClient, product_id: int, quantity: int = 1, **extra: Any) -> Any:
    return client.post(ITEMS, {"product_id": product_id, "quantity": quantity}, format="json", **{**key(), **extra})


def test_empty_cart(customer_client: APIClient) -> None:
    body = customer_client.get(CART).json()
    assert body == {
        "items": [],
        "item_count": 0,
        "quantity_total": 0,
        "items_subtotal": 0,
        "promo_subtotal": 0,
        "has_problems": False,
        "cashback": {"available": 0, "max_usable_pickup": 0},
        "updated_at": None,
        "last_change": None,
    }


def test_add_needs_a_key_and_a_retry_replays(customer_client: APIClient) -> None:
    item = product(stock=5)
    missing = customer_client.post(ITEMS, {"product_id": item.pk}, format="json")
    assert missing.status_code == 400 and missing.json()["code"] == "idempotency_key_required"
    headers = key()
    first = customer_client.post(ITEMS, {"product_id": item.pk, "quantity": 2}, format="json", **headers)
    again = customer_client.post(ITEMS, {"product_id": item.pk, "quantity": 2}, format="json", **headers)
    assert first.status_code == again.status_code == 200
    assert again.headers["Idempotent-Replayed"] == "true"
    assert CartItem.objects.get().quantity == 2
    assert first.json()["last_change"] == {
        "product_id": item.pk,
        "requested_quantity": 2,
        "quantity": 2,
        "max_reached": False,
    }


def test_add_clamps_to_max_quantity(customer_client: APIClient) -> None:
    item = product(price=312_000, stock=7, image=True)
    body = add(customer_client, item.pk, 9).json()
    line = body["items"][0]
    assert line["quantity"] == 7 and line["max_quantity"] == 7 and line["max_reached"] is True
    assert body["last_change"]["max_reached"] is True and body["last_change"]["requested_quantity"] == 9
    assert line["status"] == "ok" and line["line_total"] == 7 * 312_000
    assert line["main_image_url"].startswith("http://127.0.0.1:8000/media/products/")
    plenty = product(stock=50)
    assert add(customer_client, plenty.pk, 12).json()["items"][1]["quantity"] == 10  # CART_MAX_QTY_PER_LINE
    again = add(customer_client, plenty.pk, 1).json()
    assert again["items"][1]["quantity"] == 10 and again["last_change"]["max_reached"] is True


def test_add_refusals(customer_client: APIClient) -> None:
    hidden = product(stock=5)
    Category.objects.filter(pk=hidden.category_id).update(is_active=False)
    response = add(customer_client, hidden.pk)
    assert response.status_code == 409 and response.json()["code"] == "product_unavailable"
    assert response.json()["message"] == "Mahsulot hozir sotuvda yo‘q."
    empty = product(stock=0)
    response = add(customer_client, empty.pk)
    assert response.status_code == 409
    assert response.json()["code"] == "OUT_OF_STOCK" and response.json()["message"] == "Bu mahsulot qolmagan."
    assert response.json()["details"] == {"product_id": empty.pk, "available": 0}
    unknown = add(customer_client, 10**8)
    assert unknown.status_code == 400 and unknown.json()["errors"][0]["field"] == "product_id"
    assert unknown.json()["errors"][0]["code"] == "does_not_exist"
    bad = customer_client.post(ITEMS, {"product_id": empty.pk, "quantity": 100}, format="json", **key())
    assert bad.status_code == 400 and bad.json()["errors"][0]["code"] == "max_value"
    assert not CartItem.objects.exists()


def test_cart_full(customer_client: APIClient) -> None:
    first, second, third = product(), product(), product()
    with override_settings(CART_MAX_LINES=2):
        add(customer_client, first.pk)
        add(customer_client, second.pk)
        response = add(customer_client, third.pk)
        assert add(customer_client, first.pk).status_code == 200  # an existing line is not a new one
    assert response.status_code == 409 and response.json()["code"] == "cart_full"
    assert response.json()["details"] == {"max_lines": 2}
    assert response.json()["message"] == "Savatga ko‘pi bilan 2 xil mahsulot qo‘shish mumkin."


def test_patch_and_delete(customer_client: APIClient) -> None:
    item = product(stock=8)
    add(customer_client, item.pk, 2)
    patched = customer_client.patch(f"{ITEMS}{item.pk}/", {"quantity": 20}, format="json").json()
    assert patched["items"][0]["quantity"] == 8 and patched["last_change"]["max_reached"] is True
    assert customer_client.patch(f"{ITEMS}{10**8}/", {"quantity": 1}, format="json").status_code == 404
    item.stock.quantity = 0
    item.stock.save()
    refused = customer_client.patch(f"{ITEMS}{item.pk}/", {"quantity": 1}, format="json")
    assert refused.status_code == 409 and refused.json()["code"] == "OUT_OF_STOCK"
    assert CartItem.objects.get().quantity == 8
    Category.objects.filter(pk=item.category_id).update(is_active=False)
    hidden = customer_client.patch(f"{ITEMS}{item.pk}/", {"quantity": 1}, format="json")
    assert hidden.json()["code"] == "product_unavailable"
    assert customer_client.delete(f"{ITEMS}{item.pk}/").status_code == 200
    assert customer_client.delete(f"{ITEMS}{item.pk}/").status_code == 200  # idempotent
    assert not CartItem.objects.exists()


def test_bulk_delete_and_clear(customer_client: APIClient) -> None:
    items = [product() for _ in range(3)]
    for item in items:
        add(customer_client, item.pk)
    body = customer_client.delete(f"{ITEMS}?product_id={items[0].pk}&product_id={items[1].pk}&product_id=999999").json()
    assert [line["product_id"] for line in body["items"]] == [items[2].pk]
    none = customer_client.delete(ITEMS)
    assert none.status_code == 400 and none.json()["errors"][0]["code"] == "required"
    many = "&".join(f"product_id={n}" for n in range(1, 52))
    too_many = customer_client.delete(f"{ITEMS}?{many}")
    assert too_many.status_code == 400 and too_many.json()["errors"][0]["code"] == "max_items"
    assert customer_client.delete(f"{ITEMS}?product_id=abc").json()["errors"][0]["code"] == "invalid"
    assert customer_client.delete(CART).json()["items"] == []


def test_price_changed_statuses_and_totals(customer_client: APIClient, customer: Customer) -> None:
    seed_policies()
    credit(customer, 30_000, real_now())
    cheap = product(price=100_000, stock=5, promo=True)
    scarce = product(price=50_000, stock=5)
    gone = product(price=20_000, stock=5)
    hidden = product(price=10_000, stock=5)
    for item, quantity in ((cheap, 2), (scarce, 4), (gone, 1), (hidden, 1)):
        add(customer_client, item.pk, quantity)
    cheap.price = 120_000
    cheap.save()
    scarce.stock.quantity = 3
    scarce.stock.save()
    gone.stock.quantity = 0
    gone.stock.save()
    Category.objects.filter(pk=hidden.category_id).update(is_active=False)
    body = customer_client.get(CART).json()
    lines = {line["product_id"]: line for line in body["items"]}
    assert lines[cheap.pk]["price_changed"] is True and lines[cheap.pk]["previous_price"] == 100_000
    assert lines[cheap.pk]["unit_price"] == 120_000 and lines[cheap.pk]["status"] == "ok"
    assert lines[scarce.pk]["status"] == "insufficient" and lines[scarce.pk]["max_quantity"] == 3
    assert lines[gone.pk]["status"] == "out_of_stock" and lines[gone.pk]["available"] == 0
    assert lines[hidden.pk]["status"] == "unavailable"
    assert body["items_subtotal"] == 240_000 and body["promo_subtotal"] == 240_000
    assert body["has_problems"] is True and body["quantity_total"] == 8
    assert body["cashback"]["available"] > 0
    assert body["cashback"]["max_usable_pickup"] == min(body["cashback"]["available"], 240_000 - 1000)


def test_names_follow_the_language(customer_client: APIClient) -> None:
    item = product(name_uz="Krem", name_ru="Крем", name_en="", billz_name="BILLZ CREAM")
    add(customer_client, item.pk)
    assert customer_client.get(CART, HTTP_ACCEPT_LANGUAGE="ru").json()["items"][0]["name"] == "Крем"
    assert customer_client.get(CART, HTTP_ACCEPT_LANGUAGE="en").json()["items"][0]["name"] == "Krem"


def test_reorder_refills_the_cart(customer_client: APIClient, customer: Customer) -> None:
    kept = product(stock=3)
    hidden = product(stock=3)
    order = place(customer, [(kept, 2), (hidden, 1)], fulfilment="pickup")
    Category.objects.filter(pk=hidden.category_id).update(is_active=False)  # kept: 3 − 2 reserved = 1 left
    url = f"/api/v1/orders/{order.pk}/reorder/"
    assert customer_client.post(url, format="json").json()["code"] == "idempotency_key_required"
    body = customer_client.post(url, format="json", **key()).json()
    lines = {line["product_id"]: line for line in body["items"]}
    assert body["last_change"] is None
    assert lines[kept.pk]["quantity"] == 1  # clamped to max_quantity
    assert lines[hidden.pk]["status"] == "unavailable" and lines[hidden.pk]["quantity"] == 1
    other = place(shopper(), fulfilment="pickup")
    assert customer_client.post(f"/api/v1/orders/{other.pk}/reorder/", format="json", **key()).status_code == 404


def test_reorder_cart_full(customer_client: APIClient, customer: Customer) -> None:
    a, b, c = product(), product(), product(stock=0)
    order = place(customer, [(a, 1), (b, 1)], fulfilment="pickup")
    with override_settings(CART_MAX_LINES=1):
        response = customer_client.post(f"/api/v1/orders/{order.pk}/reorder/", format="json", **key())
    assert response.status_code == 409 and response.json()["code"] == "cart_full"
    assert not CartItem.objects.exists()


def test_reorder_respects_the_history_horizon(customer_client: APIClient, customer: Customer) -> None:
    order = place(customer, fulfilment="pickup")
    Customer.objects.filter(pk=customer.pk).update(history_from=order.created_at + timedelta(seconds=1))
    assert customer_client.post(f"/api/v1/orders/{order.pk}/reorder/", format="json", **key()).status_code == 404
