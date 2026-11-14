"""Payment abandonment recovery from the cart; no provider network calls."""

import uuid
from datetime import timedelta
from typing import Any

import pytest
from django.test import override_settings
from rest_framework.test import APIClient

from glowderma.accounts.models import Customer
from glowderma.commerce import services
from glowderma.commerce.models import Cart, CartItem, Order, OrderEvent, StockReservation
from glowderma.commerce.tests.support import credit, pay, place, product, seed_policies, shopper
from glowderma.commerce.tests.webhook_support import at, prepare, real_now
from glowderma.loyalty.models import CashbackHold
from glowderma.payments.models import Payment

pytestmark = pytest.mark.django_db


def post(client: APIClient, path: str, body: dict[str, Any] | None = None, **extra: Any) -> Any:
    return client.post(
        f"/api/v1/{path}/",
        body or {},
        format="json",
        **{"HTTP_IDEMPOTENCY_KEY": str(uuid.uuid4()), **extra},
    )


def checkout(client: APIClient, *, cashback: int = 0) -> Order:
    cart = client.get("/api/v1/cart/").json()
    response = post(
        client,
        "orders/checkout",
        {
            "fulfilment": "pickup",
            "payment_provider": "click",
            "cashback_to_use": cashback,
            "expected_total": cart["items_subtotal"] - cashback,
        },
    )
    assert response.status_code == 201, response.json()
    return Order.objects.get(pk=response.json()["id"])


def restore(client: APIClient, order: Order, **extra: Any) -> Any:
    return post(client, f"orders/{order.pk}/reorder", {"cancel_unpaid": True}, **extra)


@pytest.fixture(autouse=True)
def policies() -> None:
    seed_policies()


def test_abandoned_payment_stays_visible_and_retries_the_same_order(customer_client: APIClient) -> None:
    item = product(stock=10)
    assert post(customer_client, "cart/items", {"product_id": item.pk, "quantity": 2}).status_code == 200
    order = checkout(customer_client)
    for _ in range(2):
        assert post(customer_client, f"orders/{order.pk}/pay", {"provider": "click"}).status_code == 200
        body = customer_client.get("/api/v1/cart/").json()
        assert body["items"] == [] and body["quantity_total"] == 0
        assert body["checkout_order"]["id"] == order.pk
        assert body["checkout_order"]["items"][0]["quantity"] == 2
        assert body["checkout_order"]["actions"]["pay"] is True
    assert Order.objects.count() == Payment.objects.count() == 1
    assert not CartItem.objects.exists()


def test_edit_merges_new_cart_and_releases_reservations_and_cashback(
    customer_client: APIClient,
    customer: Customer,
) -> None:
    credit(customer, 10_000, real_now())
    saved, new = product(stock=10), product(stock=10)
    post(customer_client, "cart/items", {"product_id": saved.pk, "quantity": 2})
    order = checkout(customer_client, cashback=5_000)
    post(customer_client, "cart/items", {"product_id": saved.pk, "quantity": 3})
    post(customer_client, "cart/items", {"product_id": new.pk})
    headers = {"HTTP_IDEMPOTENCY_KEY": str(uuid.uuid4())}
    first = restore(customer_client, order, **headers)
    assert first.status_code == 200, first.json()
    assert {line["product_id"]: line["quantity"] for line in first.json()["items"]} == {
        saved.pk: 3,
        new.pk: 1,
    }
    assert first.json()["checkout_order"] is None
    order.refresh_from_db()
    assert order.status == "cancelled"
    assert not StockReservation.objects.filter(order=order, state="active").exists()
    assert not CashbackHold.objects.filter(order=order, state="active").exists()
    assert Payment.objects.get(order=order).status == "failed"
    assert Cart.objects.get(customer=customer).last_checkout_order_id is None
    replay = restore(customer_client, order, **headers)
    assert replay.status_code == 200 and replay.headers["Idempotent-Replayed"] == "true"
    assert replay.json() == first.json()
    assert OrderEvent.objects.filter(order=order, type="cancelled").count() == 1
    assert checkout(customer_client).status == "awaiting_payment"


@pytest.mark.parametrize("saved_stock", [0, 10])
def test_failed_restore_rolls_back_cancellation_and_holds(
    customer_client: APIClient, customer: Customer, saved_stock: int
) -> None:
    credit(customer, 10_000, real_now())
    saved, new = product(stock=10), product(stock=10)
    post(customer_client, "cart/items", {"product_id": saved.pk})
    order = checkout(customer_client, cashback=5_000)
    post(customer_client, "cart/items", {"product_id": new.pk})
    saved.stock.quantity = saved_stock
    saved.stock.save(update_fields=["quantity"])
    with override_settings(CART_MAX_LINES=1):
        response = restore(customer_client, order)
    assert response.status_code == 409 and response.json()["code"] == "cart_full"
    order.refresh_from_db()
    assert order.status == "awaiting_payment"
    assert Payment.objects.get(order=order).status == "pending"
    assert StockReservation.objects.filter(order=order, state="active").exists()
    assert CashbackHold.objects.filter(order=order, state="active").exists()
    assert not OrderEvent.objects.filter(order=order, type="cancelled").exists()
    assert list(CartItem.objects.values_list("product_id", flat=True)) == [new.pk]


def test_click_in_flight_cannot_be_edited_and_paid_order_cannot_be_restored(
    customer_client: APIClient,
) -> None:
    post(customer_client, "cart/items", {"product_id": product(stock=10).pk})
    order = checkout(customer_client)
    with at(real_now()):
        assert prepare(order)["error"] == 0
        response = restore(customer_client, order)
    assert response.status_code == 409 and response.json()["code"] == "payment_in_progress"
    assert not CartItem.objects.exists()
    pay(order, real_now())
    paid = restore(customer_client, order)
    assert paid.status_code == 409 and paid.json()["code"] == "not_cancellable"
    assert not CartItem.objects.exists()
    assert customer_client.get("/api/v1/cart/").json()["checkout_order"] is None


def test_expired_checkout_remains_recoverable_with_current_prices(customer_client: APIClient) -> None:
    item = product(stock=10)
    post(customer_client, "cart/items", {"product_id": item.pk})
    order = checkout(customer_client)
    assert order.reservation_expires_at is not None
    later = order.reservation_expires_at + timedelta(minutes=5)
    with at(later):
        assert services.expire_reservation(order.pk)
        item.price = 123_000
        item.save(update_fields=["price"])
        body = customer_client.get("/api/v1/cart/").json()
        assert body["checkout_order"]["cancellation"]["reason"] == "reservation_expired"
        response = restore(customer_client, order)
    assert response.status_code == 200, response.json()
    assert response.json()["items"][0]["unit_price"] == 123_000
    assert response.json()["checkout_order"] is None


def test_restore_obeys_ownership_and_cart_read_obeys_history_horizon(
    customer_client: APIClient,
    customer: Customer,
) -> None:
    post(customer_client, "cart/items", {"product_id": product(stock=10).pk})
    order = checkout(customer_client)
    foreign = place(shopper(), fulfilment="pickup", when=real_now())
    assert restore(customer_client, foreign).status_code == 404
    Customer.objects.filter(pk=customer.pk).update(history_from=order.created_at + timedelta(seconds=1))
    assert customer_client.get("/api/v1/cart/").json()["checkout_order"] is None
    assert restore(customer_client, order).status_code == 404
