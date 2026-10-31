"""commerce models (design 03 §1, §10): every CHECK / unique / partial unique through IntegrityError, the
FK ``on_delete`` rules, ordering, ``__str__`` and the indexes/constraints as PostgreSQL stores them."""

from datetime import datetime, timedelta
from typing import Any

import pytest
from django.db import IntegrityError, connection, transaction
from django.db.models import ProtectedError

from glowderma.accounts.models import Customer
from glowderma.accounts.tests.factories import CustomerFactory, StaffUserFactory
from glowderma.catalog.tests.factories import ProductFactory
from glowderma.commerce.models import (
    EVENT_SOURCES,
    EVENT_STATUS_AFTER,
    NON_STATUS_EVENT_TYPES,
    Cart,
    CartItem,
    Order,
    OrderEvent,
    OrderItem,
    StockReservation,
)
from glowderma.commerce.tests.factories import (
    DESIGN_ADDRESS,
    CartFactory,
    CartItemFactory,
    OrderEventFactory,
    OrderFactory,
    OrderItemFactory,
    StockReservationFactory,
    order_with_lines,
)
from glowderma.core import enums
from glowderma.core.timeutils import TASHKENT

pytestmark = pytest.mark.django_db

T0 = datetime(2026, 10, 23, 14, 2, tzinfo=TASHKENT)


def violates(constraint: str, create: Any, **kwargs: Any) -> None:
    with pytest.raises(IntegrityError, match=constraint), transaction.atomic():
        create(**kwargs)


def index_definitions(table: str) -> dict[str, str]:
    with connection.cursor() as cursor:
        cursor.execute("SELECT indexname, indexdef FROM pg_indexes WHERE tablename = %s", [table])
        return dict(cursor.fetchall())


def constraint_names(table: str, types: str = "cu") -> set[str]:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT conname FROM pg_constraint WHERE conrelid = %s::regclass AND contype = ANY(%s)",
            [table, list(types)],
        )
        return {row[0] for row in cursor.fetchall()}


# --- Cart / CartItem ---------------------------------------------------------------------------------------


def test_cart_one_per_customer_str_and_on_delete_rules() -> None:
    cart = CartFactory.create()
    assert str(cart) == f"Mijoz #{cart.customer_id} savatchasi"
    assert cart.last_checkout_order is None
    violates("commerce_cart_customer_id_key", Cart.objects.create, customer=cart.customer)
    order = OrderFactory.create(customer=CustomerFactory.create())
    cart.last_checkout_order = order
    cart.save()
    assert Customer.objects.get(pk=cart.customer_id).cart == cart
    Order.objects.filter(pk=order.pk).delete()  # SET_NULL: the cart survives its last checkout order
    # Re-read: mypy keeps the ``Order`` narrowing from the assignment above across refresh_from_db().
    assert Cart.objects.get(pk=cart.pk).last_checkout_order is None
    cart.customer.delete()  # A1 says this never happens; the CASCADE is the right semantics for a cart
    assert not Cart.objects.filter(pk=cart.pk).exists()


def test_cart_item_unique_quantity_range_and_ordering() -> None:
    cart = CartFactory.create()
    product = ProductFactory.create()
    first = CartItemFactory.create(cart=cart, product=product, quantity=2)
    assert str(first) == f"2 × mahsulot #{product.pk} (savatcha #{cart.pk})"
    violates("cartitem_cart_product_unique", CartItemFactory.create, cart=cart, product=product)
    violates("cartitem_quantity_1_99", CartItemFactory.create, cart=cart, quantity=0)
    violates("cartitem_quantity_1_99", CartItemFactory.create, cart=cart, quantity=100)
    second = CartItemFactory.create(cart=cart, quantity=99, added_at=first.added_at + timedelta(seconds=1))
    earlier = CartItemFactory.create(cart=cart, added_at=first.added_at - timedelta(seconds=1))
    assert list(cart.items.all()) == [earlier, first, second]
    product.delete()  # CASCADE on product is deliberate (carts are ephemeral)
    assert not CartItem.objects.filter(pk=first.pk).exists()
    cart_pk = cart.pk
    cart.delete()
    assert not CartItem.objects.filter(cart_id=cart_pk).exists()


# --- Order --------------------------------------------------------------------------------------------------


def test_order_factory_defaults_satisfy_every_check_and_str() -> None:
    order = OrderFactory.create(created_at=T0)
    assert order.status == enums.OrderStatus.AWAITING_PAYMENT
    assert order.total == order.items_subtotal + order.delivery_fee - order.cashback_used == 210_000
    assert order.reservation_expires_at == T0 + timedelta(minutes=15)
    assert order.address_text == DESIGN_ADDRESS and order.recipient_phone == order.customer.phone
    assert order.metadata == {} and order.customer_note == "" and order.cancellation_reason == ""
    assert (
        str(order)
        == f"Buyurtma №{order.pk} (To‘lov kutilmoqda, 210\u00a0000 so‘m, mijoz #{order.customer_id})"
    )
    assert [item.line_total for item in order.items.all()] == [185_000]


def test_order_traits_and_pickup() -> None:
    paid = OrderFactory.create(paid=True)
    assert paid.status == enums.OrderStatus.PROCESSING and paid.paid_at is not None
    handed = OrderFactory.create(handed=True)
    assert handed.handed_to_courier_at is not None and handed.paid_at is not None
    delivered = OrderFactory.create(delivered=True, pickup=True)
    assert delivered.delivered_at is not None and delivered.delivery_fee == 0 and delivered.address_text == ""
    assert delivered.total == delivered.items_subtotal
    cancelled = OrderFactory.create(cancelled=True)
    assert cancelled.cancelled_by is not None and cancelled.cancellation_reason == "admin"
    expired = OrderFactory.create(expired=True)
    assert expired.cancelled_at == expired.reservation_expires_at and expired.cancelled_by is None
    assert expired.cancellation_reason == enums.CancellationReason.RESERVATION_EXPIRED


def test_order_with_lines_derives_subtotals() -> None:
    # №10231: 301 ×1, 416 ×2, 417 ×1, 401 ×1 (promo) → items 554 000, promo 240 000 (03 §7.8)
    order = order_with_lines(
        [(1, 160_000, False), (2, 77_000, False), (1, 0, False), (1, 240_000, True)],
        pickup=True,
        cashback_used=20_000,
    )
    assert (order.items_subtotal, order.promo_subtotal, order.total) == (554_000, 240_000, 534_000)
    assert [(i.position, i.quantity, i.promo) for i in order.items.all()] == [
        (0, 1, False),
        (1, 2, False),
        (2, 1, False),
        (3, 1, True),
    ]


def test_order_total_formula() -> None:
    violates("order_total_formula", OrderFactory.create, total=210_001)
    violates("order_total_formula", OrderFactory.create, cashback_used=10_000, total=210_000)
    order = OrderFactory.create(cashback_used=48_600, items_subtotal=808_000, total=784_400)
    assert order.total == 784_400


def test_order_amounts_range() -> None:
    violates("order_amounts_range", OrderFactory.create, items_subtotal=-1, total=24_999)
    violates("order_amounts_range", OrderFactory.create, promo_subtotal=-1)
    violates("order_amounts_range", OrderFactory.create, delivery_fee=-1, total=184_999)
    violates("order_amounts_range", OrderFactory.create, cashback_used=-1, total=210_001)
    violates("order_amounts_range", OrderFactory.create, promo_subtotal=185_001)
    # C-4: cashback never pays the delivery fee
    violates("order_amounts_range", OrderFactory.create, cashback_used=185_001, total=24_999)
    assert OrderFactory.create(cashback_used=185_000, total=25_000).total == 25_000


def test_order_address_by_fulfilment() -> None:
    violates("order_address_by_fulfilment", OrderFactory.create, address_text="")
    violates("order_address_by_fulfilment", OrderFactory.create, pickup=True, address_text="x")
    violates("order_address_by_fulfilment", OrderFactory.create, pickup=True, address_region_code="10")
    violates("order_address_by_fulfilment", OrderFactory.create, pickup=True, address_city_code="1001")
    violates("order_address_by_fulfilment", OrderFactory.create, pickup=True, delivery_fee=1, total=185_001)
    pickup = OrderFactory.create(pickup=True, address_comment="", recipient_name="")
    assert pickup.address_text == "" and pickup.delivery_fee == 0


@pytest.mark.parametrize("phone", ["+99890123456", "998901234567", "+7 999", "+99890123456a"])
def test_order_recipient_phone_e164(phone: str) -> None:
    violates("order_recipient_phone_e164", OrderFactory.create, recipient_phone=phone)


def test_order_recipient_phone_may_be_blank() -> None:
    assert OrderFactory.create(recipient_phone="").recipient_phone == ""


def test_order_cancellation_fields() -> None:
    now = T0 + timedelta(hours=1)
    # cancelled needs cancelled_at and a reason
    violates("order_cancellation_fields", OrderFactory.create, status="cancelled")
    violates("order_cancellation_fields", OrderFactory.create, status="cancelled", cancelled_at=now)
    violates(
        "order_cancellation_fields",
        OrderFactory.create,
        status="cancelled",
        cancellation_reason="reservation_expired",
    )
    # open statuses carry no cancellation columns
    violates("order_cancellation_fields", OrderFactory.create, cancelled_at=now)
    violates("order_cancellation_fields", OrderFactory.create, cancellation_reason="reservation_expired")
    violates("order_cancellation_fields", OrderFactory.create, cancellation_note="x")
    violates("order_cancellation_fields", OrderFactory.create, cancelled_by=StaffUserFactory.create())
    violates("order_cancellation_fields", OrderFactory.create, paid=True, cancelled_at=now)


def test_order_cancellation_by_reason() -> None:
    staff = StaffUserFactory.create()
    violates("order_cancellation_by_reason", OrderFactory.create, cancelled=True, cancelled_by=None)
    violates("order_cancellation_by_reason", OrderFactory.create, cancelled=True, cancellation_note="")
    violates("order_cancellation_by_reason", OrderFactory.create, expired=True, cancelled_by=staff)
    violates(
        "order_cancellation_by_reason",
        OrderFactory.create,
        expired=True,
        cancellation_reason="customer_request",
        cancelled_by=staff,
    )
    customer_cancel = OrderFactory.create(expired=True, cancellation_reason="customer_request")
    assert customer_cancel.cancelled_by is None
    assert OrderFactory.create(expired=True, cancellation_note="free text allowed").cancellation_note


def test_order_paid_at_by_status() -> None:
    now = T0 + timedelta(minutes=3)
    violates("order_paid_at_by_status", OrderFactory.create, paid_at=now)  # awaiting_payment
    violates("order_paid_at_by_status", OrderFactory.create, paid=True, paid_at=None)
    violates("order_paid_at_by_status", OrderFactory.create, handed=True, paid_at=None)
    violates("order_paid_at_by_status", OrderFactory.create, delivered=True, paid_at=None)
    assert OrderFactory.create(cancelled=True, paid_at=now).paid_at == now  # cancelled after payment
    assert OrderFactory.create(expired=True).paid_at is None


def test_order_delivered_at_by_status() -> None:
    now = T0 + timedelta(hours=26)
    violates("order_delivered_at_by_status", OrderFactory.create, delivered=True, delivered_at=None)
    violates("order_delivered_at_by_status", OrderFactory.create, paid=True, delivered_at=now)
    violates("order_delivered_at_by_status", OrderFactory.create, cancelled=True, delivered_at=now)


def test_order_awaiting_has_expiry_and_reservation_minutes() -> None:
    violates("order_awaiting_has_expiry", OrderFactory.create, reservation_expires_at=None)
    assert OrderFactory.create(paid=True, reservation_expires_at=None).reservation_expires_at is None
    violates("order_reservation_minutes_5_60", OrderFactory.create, reservation_minutes=4)
    violates("order_reservation_minutes_5_60", OrderFactory.create, reservation_minutes=61)
    assert OrderFactory.create(reservation_minutes=60).reservation_minutes == 60


def test_order_ordering_customer_protect_and_cancelled_by_protect() -> None:
    customer = CustomerFactory.create()
    older = OrderFactory.create(customer=customer, created_at=T0)
    newer = OrderFactory.create(customer=customer, created_at=T0 + timedelta(minutes=1))
    tie = OrderFactory.create(customer=customer, created_at=T0 + timedelta(minutes=1))
    assert list(Order.objects.all()) == [tie, newer, older]
    assert list(customer.orders.all()) == [tie, newer, older]
    with pytest.raises(ProtectedError):
        customer.delete()
    cancelled = OrderFactory.create(cancelled=True)
    assert cancelled.cancelled_by is not None
    assert list(cancelled.cancelled_by.cancelled_orders.all()) == [cancelled]
    with pytest.raises(ProtectedError):
        cancelled.cancelled_by.delete()


def test_order_indexes_and_constraints_in_pg_catalog() -> None:
    indexes = index_definitions("commerce_order")
    assert indexes["order_status_idx"].endswith("(status)")
    assert indexes["order_created_idx"].endswith("(created_at, id)")
    assert indexes["order_customer_created_idx"].endswith("(customer_id, created_at DESC, id DESC)")
    assert indexes["order_awaiting_expiry_idx"].endswith(
        "(reservation_expires_at) WHERE ((status)::text = 'awaiting_payment'::text)"
    )
    assert indexes["order_provider_idx"].endswith("(payment_provider)")
    assert constraint_names("commerce_order", "c") >= {
        "order_total_formula",
        "order_amounts_range",
        "order_address_by_fulfilment",
        "order_recipient_phone_e164",
        "order_cancellation_fields",
        "order_cancellation_by_reason",
        "order_paid_at_by_status",
        "order_delivered_at_by_status",
        "order_awaiting_has_expiry",
        "order_reservation_minutes_5_60",
    }
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname = 'order_total_formula'"
        )
        assert cursor.fetchone() == ("CHECK ((total = ((items_subtotal + delivery_fee) - cashback_used)))",)


# --- OrderItem ----------------------------------------------------------------------------------------------


def test_orderitem_str_snapshot_and_ordering() -> None:
    product = ProductFactory.create(
        name_uz="Seramidli namlantiruvchi krem", billz_id="4607012345", price=312_000
    )
    order = OrderFactory.create(items=[], items_subtotal=312_000, total=337_000)
    item = OrderItemFactory.create(order=order, product=product)
    assert (item.name, item.billz_id, item.unit_price, item.line_total) == (
        "Seramidli namlantiruvchi krem",
        "4607012345",
        312_000,
        312_000,
    )
    assert str(item) == f"Seramidli namlantiruvchi krem × 1 @ 312\u00a0000 so‘m (buyurtma №{order.pk})"
    assert item.image_url == "" and item.promo is False and item.position == 0
    second = OrderItemFactory.create(order=order, position=5)
    first = OrderItemFactory.create(order=order, position=1)
    assert list(order.items.all()) == [item, first, second]


def test_orderitem_constraints() -> None:
    order = OrderFactory.create()
    product = order.items.get().product
    violates("orderitem_position_unique", OrderItemFactory.create, order=order, position=0)
    violates("orderitem_product_unique", OrderItemFactory.create, order=order, position=1, product=product)
    violates("orderitem_amounts", OrderItemFactory.create, order=order, position=1, quantity=0)
    violates("orderitem_amounts", OrderItemFactory.create, order=order, position=1, unit_price=-1)
    violates("orderitem_line_total", OrderItemFactory.create, order=order, position=1, line_total=1)
    violates(
        "orderitem_line_total",
        OrderItemFactory.create,
        order=order,
        position=1,
        quantity=2,
        unit_price=100,
        line_total=100,
    )
    violates("orderitem_name_required", OrderItemFactory.create, order=order, position=1, name="")
    free = OrderItemFactory.create(order=order, position=1, unit_price=0)
    assert free.line_total == 0


def test_orderitem_product_protect_and_order_cascade() -> None:
    order = OrderFactory.create()
    item = order.items.get()
    with pytest.raises(ProtectedError):
        item.product.delete()
    assert list(item.product.order_items.all()) == [item]
    Order.objects.filter(pk=order.pk).delete()  # orders are never deleted; CASCADE is the right semantics
    assert not OrderItem.objects.filter(pk=item.pk).exists()


def test_orderitem_constraints_in_pg_catalog() -> None:
    assert constraint_names("commerce_orderitem") >= {
        "orderitem_position_unique",
        "orderitem_product_unique",
        "orderitem_amounts",
        "orderitem_line_total",
        "orderitem_name_required",
    }


# --- OrderEvent ---------------------------------------------------------------------------------------------


def test_orderevent_str_ordering_and_defaults() -> None:
    order = OrderFactory.create(created_at=T0)
    created = OrderEventFactory.create(order=order)
    assert created.data == {"channel": "ios", "item_count": 1, "total": 210_000}
    assert created.at == T0 and created.actor is None
    assert str(created) == f"Buyurtma №{order.pk}: Buyurtma yaratildi (Tizim) · 2026-10-23 14:02"
    reservation = OrderEventFactory.create(
        order=order,
        type="reservation_created",
        status_after="",
        data={"item_count": 1, "minutes": 15, "expires_at": "2026-10-23T14:17:00+05:00"},
    )
    paid = OrderEventFactory.create(
        order=order,
        type="payment_confirmed",
        status_after="processing",
        at=T0 + timedelta(minutes=3),
        data={
            "provider": "click",
            "transaction_id": "CLK-1",
            "amount": 210_000,
            "reservation_converted": True,
        },
    )
    assert list(OrderEvent.objects.all()) == [paid, reservation, created]  # -at, -id
    assert list(order.events.all()) == [paid, reservation, created]


def test_orderevent_actor_by_source() -> None:
    staff = StaffUserFactory.create()
    violates("orderevent_actor_by_source", OrderEventFactory.create, actor=staff)  # system with actor
    violates(
        "orderevent_actor_by_source",
        OrderEventFactory.create,
        type="status_changed",
        source="admin",
        status_after="delivered",
        data={"from": "processing", "to": "delivered", "push_sent": True},
    )  # admin without actor
    event = OrderEventFactory.create(
        type="status_changed",
        source="admin",
        status_after="delivered",
        actor=staff,
        data={"from": "processing", "to": "delivered", "push_sent": True},
    )
    assert list(staff.order_events.all()) == [event]
    with pytest.raises(ProtectedError):
        staff.delete()


def test_orderevent_status_by_type() -> None:
    violates("orderevent_status_by_type", OrderEventFactory.create, type="cancelled", status_after="")
    violates("orderevent_status_by_type", OrderEventFactory.create, status_after="processing")
    violates("orderevent_status_by_type", OrderEventFactory.create, status_after="")
    violates(
        "orderevent_status_by_type",
        OrderEventFactory.create,
        type="reservation_created",
        status_after="cancelled",
    )
    violates(
        "orderevent_status_by_type",
        OrderEventFactory.create,
        type="status_changed",
        source="admin",
        actor=StaffUserFactory.create(),
        status_after="processing",
    )
    for event_type, statuses in EVENT_STATUS_AFTER.items():
        for status in statuses:
            OrderEventFactory.create(
                type=event_type,
                source=EVENT_SOURCES[event_type][0],
                status_after=status,
                actor=StaffUserFactory.create() if EVENT_SOURCES[event_type][0] == "admin" else None,
                data={},
            )
    for event_type in NON_STATUS_EVENT_TYPES:
        OrderEventFactory.create(type=event_type, source="system", status_after="", data={})
    assert set(EVENT_STATUS_AFTER) | set(NON_STATUS_EVENT_TYPES) == set(enums.OrderEventType.values)


def test_orderevent_source_by_type() -> None:
    staff = StaffUserFactory.create()
    violates(
        "orderevent_source_by_type",
        OrderEventFactory.create,
        type="status_changed",
        source="system",
        status_after="delivered",
    )
    violates(
        "orderevent_source_by_type",
        OrderEventFactory.create,
        type="handed_to_courier",
        source="admin",
        actor=staff,
        status_after="handed_to_courier",
    )
    violates("orderevent_source_by_type", OrderEventFactory.create, source="bts")
    violates(
        "orderevent_source_by_type",
        OrderEventFactory.create,
        type="cancelled",
        source="bts",
        status_after="cancelled",
    )
    for event_type, sources in EVENT_SOURCES.items():
        status_after = EVENT_STATUS_AFTER.get(event_type, ("",))[0]
        for source in sources:
            OrderEventFactory.create(
                type=event_type,
                source=source,
                status_after=status_after,
                actor=staff if source == "admin" else None,
                data={},
            )
    assert set(EVENT_SOURCES) == set(enums.OrderEventType.values)


def test_orderevent_order_protect_and_index() -> None:
    event = OrderEventFactory.create()
    with pytest.raises(ProtectedError):
        Order.objects.filter(pk=event.order_id).delete()
    indexes = index_definitions("commerce_orderevent")
    assert indexes["orderevent_order_at_idx"].endswith("(order_id, at, id)")
    assert constraint_names("commerce_orderevent", "c") >= {
        "orderevent_actor_by_source",
        "orderevent_status_by_type",
        "orderevent_source_by_type",
    }


# --- StockReservation ---------------------------------------------------------------------------------------


def test_reservation_defaults_str_and_ordering() -> None:
    order = OrderFactory.create(created_at=T0)
    reservation = StockReservationFactory.create(order=order, quantity=2)
    assert reservation.state == "active" and reservation.expires_at == T0 + timedelta(minutes=15)
    assert reservation.converted_at is None and reservation.release_reason == ""
    assert str(reservation) == (f"Band: 2 × mahsulot #{reservation.product_id} · buyurtma №{order.pk} (Faol)")
    other = StockReservationFactory.create(order=order)
    assert list(order.reservations.all()) == [reservation, other]
    assert list(reservation.product.reservations.all()) == [reservation]


def test_reservation_one_active_per_line_partial_unique() -> None:
    order = OrderFactory.create()
    product = ProductFactory.create()
    now = T0 + timedelta(minutes=3)
    active = StockReservationFactory.create(order=order, product=product)
    violates("reservation_one_active_per_line", StockReservationFactory.create, order=order, product=product)
    # a converted/released row of the same line does not collide, and neither does a second order
    StockReservationFactory.create(order=order, product=product, state="converted", converted_at=now)
    StockReservationFactory.create(
        order=order, product=product, state="released", released_at=now, release_reason="expired"
    )
    StockReservationFactory.create(product=product)
    assert StockReservation.objects.filter(order=order, product=product, state="active").get() == active


def test_reservation_quantity_positive_and_state_fields() -> None:
    now = T0 + timedelta(minutes=3)
    violates("reservation_quantity_positive", StockReservationFactory.create, quantity=0)
    create = StockReservationFactory.create
    violates("reservation_state_fields", create, state="active", converted_at=now)
    violates("reservation_state_fields", create, state="active", released_at=now)
    violates("reservation_state_fields", create, state="active", release_reason="expired")
    violates("reservation_state_fields", create, state="converted")
    violates("reservation_state_fields", create, state="converted", converted_at=now, released_at=now)
    violates(
        "reservation_state_fields", create, state="converted", converted_at=now, release_reason="expired"
    )
    violates("reservation_state_fields", create, state="released", released_at=now)
    violates("reservation_state_fields", create, state="released", released_at=now, converted_at=now)
    violates("reservation_state_fields", create, state="released", release_reason="cancelled")
    for reason in StockReservation.ReleaseReason:
        create(state="released", released_at=now, release_reason=reason)
    assert create(state="converted", converted_at=now).released_at is None


def test_reservation_product_protect_and_order_cascade() -> None:
    reservation = StockReservationFactory.create()
    with pytest.raises(ProtectedError):
        reservation.product.delete()
    Order.objects.filter(pk=reservation.order_id).delete()
    assert not StockReservation.objects.filter(pk=reservation.pk).exists()


def test_reservation_indexes_and_constraints_in_pg_catalog() -> None:
    indexes = index_definitions("commerce_stockreservation")
    assert indexes["reservation_one_active_per_line"].startswith("CREATE UNIQUE INDEX")
    assert indexes["reservation_one_active_per_line"].endswith(
        "(order_id, product_id) WHERE ((state)::text = 'active'::text)"
    )
    assert indexes["reservation_active_product_idx"].endswith(
        "(product_id) WHERE ((state)::text = 'active'::text)"
    )
    assert indexes["reservation_converted_prod_idx"].endswith(
        "(product_id, converted_at) WHERE ((state)::text = 'converted'::text)"
    )
    assert indexes["reservation_active_expires_idx"].endswith(
        "(expires_at) WHERE ((state)::text = 'active'::text)"
    )
    assert constraint_names("commerce_stockreservation", "c") >= {
        "reservation_quantity_positive",
        "reservation_state_fields",
    }
    # a conditional UniqueConstraint is a partial UNIQUE INDEX, not a table constraint
    assert "reservation_one_active_per_line" not in constraint_names("commerce_stockreservation", "u")
