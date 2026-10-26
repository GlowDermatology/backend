"""commerce.services.create_order (03 §4.1 row 1): snapshots, amounts, reservation, hold, payment, events."""

from datetime import timedelta

import pytest

from glowderma.accounts import services as account_services
from glowderma.commerce import selectors, services
from glowderma.commerce.models import Order, OrderEvent, StockReservation
from glowderma.commerce.tests.support import AT, credit, pay, place, product, seed_policies, shopper
from glowderma.core import enums
from glowderma.core.seeding import pinned_clock
from glowderma.delivery.models import Shipment
from glowderma.loyalty import selectors as loyalty_selectors
from glowderma.loyalty.models import CashbackHold
from glowderma.payments.models import Payment
from glowderma.site_config.models import SiteSettings

pytestmark = pytest.mark.django_db


def test_checkout_snapshots_lines_amounts_and_writes_the_two_events() -> None:
    cream = product(312_000, image=True)
    spf = product(240_000, promo=True)
    foam = product(128_000)
    order = place(lines=[(cream, 1), (spf, 1), (foam, 2)])
    assert order.status == enums.OrderStatus.AWAITING_PAYMENT
    assert (order.items_subtotal, order.promo_subtotal, order.delivery_fee, order.total) == (
        808_000,
        240_000,
        25_000,
        833_000,
    )
    items = list(order.items.order_by("position"))
    assert [(i.product_id, i.quantity, i.unit_price, i.promo, i.line_total) for i in items] == [
        (cream.pk, 1, 312_000, False, 312_000),
        (spf.pk, 1, 240_000, True, 240_000),
        (foam.pk, 2, 128_000, False, 256_000),
    ]
    assert (
        items[0].image_url == cream.images.get().file.name
    )  # the storage path; rendered absolute by the API
    assert items[1].image_url == "" and items[0].name == cream.name_uz and items[0].billz_id == cream.billz_id
    assert order.reservation_minutes == 15 and order.reservation_expires_at == AT + timedelta(minutes=15)
    assert order.recipient_phone == order.customer.phone
    reservations = StockReservation.objects.filter(order=order)
    assert {(r.product_id, r.quantity, r.state) for r in reservations} == {
        (cream.pk, 1, "active"),
        (spf.pk, 1, "active"),
        (foam.pk, 2, "active"),
    }
    payment = Payment.objects.get(order=order)
    assert (payment.status, payment.amount, payment.provider) == ("pending", 833_000, "click")
    assert Shipment.objects.get(order=order).state == "not_created"
    events = list(OrderEvent.objects.filter(order=order).order_by("id"))
    assert [(e.type, e.status_after, e.source, e.at) for e in events] == [
        ("order_created", "awaiting_payment", "system", AT),
        ("reservation_created", "", "system", AT),
    ]
    assert events[0].data == {"channel": "ios", "item_count": 3, "total": 833_000}
    assert events[1].data == {"item_count": 3, "minutes": 15, "expires_at": "2026-10-20T12:15:00+05:00"}


def test_pickup_has_no_fee_no_address_and_no_shipment() -> None:
    order = place(fulfilment=enums.FulfilmentMethod.PICKUP)
    assert (order.delivery_fee, order.address_text) == (0, "")
    assert not Shipment.objects.filter(order=order).exists()


@pytest.mark.parametrize(
    ("region", "fee"), [("10", 25_000), ("11", 26_000), ("12", 27_000), ("13", 28_000), ("99", 25_000)]
)
def test_delivery_fee_is_the_bts_tariff_quote(region: str, fee: int) -> None:
    assert place(region_code=region).delivery_fee == fee


def test_tariff_override_from_the_fake_scenario() -> None:
    assert place(fake={"bts": {"tariff": 31_000}}).delivery_fee == 31_000


def test_reservation_minutes_follow_the_site_settings() -> None:
    SiteSettings.objects.filter(pk=1).update(reservation_minutes=30)
    order = place()
    assert order.reservation_minutes == 30 and order.reservation_expires_at == AT + timedelta(minutes=30)


def test_out_of_stock_counts_active_reservations() -> None:
    item = product(stock=2)
    place(lines=[(item, 2)])
    with pytest.raises(services.OutOfStock) as caught:
        place(lines=[(item, 1)])
    assert caught.value.product_ids == (item.pk,)
    assert selectors.available_stock([item.pk]) == {item.pk: 0}


def test_hidden_products_and_converted_sales_after_the_sync_are_unavailable() -> None:
    hidden = product(status=enums.ProductStatus.HIDDEN)
    with pytest.raises(services.OutOfStock):
        place(lines=[(hidden, 1)])
    item = product(stock=3)
    item.stock.synced_at = AT - timedelta(days=1)
    item.stock.save()
    order = place(lines=[(item, 2)])
    pay(order)
    assert selectors.available_stock([item.pk, 10**9]) == {item.pk: 1, 10**9: 0}
    # the seeder replays history against today's stock
    order_id = None
    with pinned_clock(AT):
        order_id = services.create_order(
            shopper().pk,
            [(item.pk, 5)],
            enums.FulfilmentMethod.PICKUP,
            payment_provider="click",
            channel="android",
            delivery_fee=0,
            enforce_stock=False,
        )
    assert StockReservation.objects.get(order_id=order_id).quantity == 5


def test_cashback_becomes_a_hold_until_the_reservation_expires() -> None:
    seed_policies()
    customer = shopper()
    credit(customer, 30_000, AT - timedelta(days=2))
    order = place(customer, cashback=20_000)
    hold = CashbackHold.objects.get(order_id=order.pk)
    assert (hold.amount, hold.state, hold.expires_at) == (20_000, "active", order.reservation_expires_at)
    assert order.total == order.items_subtotal + 25_000 - 20_000
    assert loyalty_selectors.account_summary(customer.pk).available == 10_000
    with pytest.raises(services.InsufficientCashback) as caught:
        place(customer, cashback=10_001)
    assert caught.value.available == 10_000


@pytest.mark.parametrize(
    "kwargs",
    [
        {"items": []},
        {"fulfilment": "drone"},
        {"channel": "web"},
        {"payment_provider": "cash"},
        {"cashback_used": 10**9},
        {"address_text": ""},
        {"address_id": 10**9},
    ],
)
def test_checkout_input_errors(kwargs: dict[str, object]) -> None:
    item = product()
    params: dict[str, object] = {
        "items": [(item.pk, 1)],
        "fulfilment": "delivery",
        "payment_provider": "click",
        "channel": "ios",
        "delivery_fee": 25_000,
        "address_text": "Toshkent",
        "region_code": "10",
        "city_code": "1001",
    }
    params.update(kwargs)
    items = params.pop("items")
    fulfilment = params.pop("fulfilment")
    with pytest.raises((services.CheckoutError, services.AddressNotFound, services.CashbackAboveMax)):
        services.create_order(shopper().pk, items, fulfilment, **params)  # type: ignore[arg-type]


def test_checkout_refuses_unknown_products_duplicates_and_deleted_customers() -> None:
    item = product()
    with pytest.raises(services.CheckoutError):
        services.create_order(
            shopper().pk, [(10**9, 1)], "pickup", payment_provider="click", channel="ios", delivery_fee=0
        )
    with pytest.raises(services.CheckoutError):
        services.create_order(
            shopper().pk,
            [(item.pk, 1), (item.pk, 2)],
            "pickup",
            payment_provider="click",
            channel="ios",
            delivery_fee=0,
        )
    with pytest.raises(services.CheckoutError):
        services.create_order(
            shopper().pk, [(item.pk, 0)], "pickup", payment_provider="click", channel="ios", delivery_fee=0
        )
    gone = account_services.delete_customer(shopper())
    with pytest.raises(services.CheckoutError):
        services.create_order(
            gone.pk, [(item.pk, 1)], "pickup", payment_provider="click", channel="ios", delivery_fee=0
        )


def test_saved_address_is_snapshotted() -> None:
    customer = shopper()
    address = account_services.add_address(
        customer.pk, text="Samarqand, Registon", region_code="13", city_code="1301"
    )
    with pinned_clock(AT):
        order_id = services.create_order(
            customer.pk,
            [(product().pk, 1)],
            "delivery",
            payment_provider="payme",
            channel="android",
            delivery_fee=28_000,
            address_id=address.pk,
        )
    saved = Order.objects.get(pk=order_id)
    assert (saved.address_text, saved.address_region_code, saved.delivery_fee) == (
        "Samarqand, Registon",
        "13",
        28_000,
    )
