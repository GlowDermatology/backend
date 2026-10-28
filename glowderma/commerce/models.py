"""Models of `commerce` — docs/plans/m1-models/03-… §1 (slice B2).

``Cart``/``CartItem`` (M2 shopping cart), ``Order`` (the immutable commercial snapshot plus the mutable
lifecycle status; ``id`` is the order number, sequence from 10001), ``OrderItem`` (checkout snapshots),
``OrderEvent`` (append-only typed history, contract §4) and ``StockReservation`` (units held for an unpaid
order). Every status change happens inside ``commerce.services`` (B5) under the order lock; the Django admin
is read-only. The order state machine is data in :mod:`glowderma.commerce.state`.
"""

from django.core.serializers.json import DjangoJSONEncoder
from django.db import models
from django.db.models import F, Q

from glowderma.core import enums
from glowderma.core.models import AppendOnlyModel, TimeStampedModel, timestamp_default

PHONE_E164_REGEX = r"^\+998\d{9}$"

# Event types by the status they produce (contract §4): the two CHECKs below are generated from these tables
# so the model, the migration and ``state.py`` cannot drift apart.
EVENT_STATUS_AFTER: dict[str, tuple[str, ...]] = {
    enums.OrderEventType.ORDER_CREATED: (enums.OrderStatus.AWAITING_PAYMENT,),
    enums.OrderEventType.PAYMENT_CONFIRMED: (enums.OrderStatus.PROCESSING,),
    enums.OrderEventType.HANDED_TO_COURIER: (enums.OrderStatus.HANDED_TO_COURIER,),
    enums.OrderEventType.DELIVERED: (enums.OrderStatus.DELIVERED,),
    enums.OrderEventType.CANCELLED: (enums.OrderStatus.CANCELLED,),
    enums.OrderEventType.RESERVATION_EXPIRED: (enums.OrderStatus.CANCELLED,),
    enums.OrderEventType.STATUS_CHANGED: (enums.OrderStatus.HANDED_TO_COURIER, enums.OrderStatus.DELIVERED),
}
NON_STATUS_EVENT_TYPES: tuple[str, ...] = (
    enums.OrderEventType.RESERVATION_CREATED,
    enums.OrderEventType.BILLZ_SALE_RECORDED,
    enums.OrderEventType.CASHBACK_CREDITED,
    enums.OrderEventType.PAYMENT_FAILED,
    enums.OrderEventType.SHIPMENT_CREATED,
    enums.OrderEventType.SHIPMENT_FAILED,
    enums.OrderEventType.REFUND_REQUESTED,
    enums.OrderEventType.REFUND_SUCCEEDED,
    enums.OrderEventType.REFUND_FAILED,
)
# Allowed ``source`` per event type (contract §4 "source" column).
EVENT_SOURCES: dict[str, tuple[str, ...]] = {
    enums.OrderEventType.ORDER_CREATED: (enums.EventSource.SYSTEM,),
    enums.OrderEventType.RESERVATION_CREATED: (enums.EventSource.SYSTEM,),
    enums.OrderEventType.PAYMENT_CONFIRMED: (enums.EventSource.SYSTEM,),
    enums.OrderEventType.BILLZ_SALE_RECORDED: (enums.EventSource.SYSTEM,),
    enums.OrderEventType.CASHBACK_CREDITED: (enums.EventSource.SYSTEM,),
    enums.OrderEventType.PAYMENT_FAILED: (enums.EventSource.SYSTEM,),
    enums.OrderEventType.RESERVATION_EXPIRED: (enums.EventSource.SYSTEM,),
    enums.OrderEventType.REFUND_SUCCEEDED: (enums.EventSource.SYSTEM,),
    enums.OrderEventType.REFUND_FAILED: (enums.EventSource.SYSTEM,),
    enums.OrderEventType.HANDED_TO_COURIER: (enums.EventSource.BTS,),
    enums.OrderEventType.DELIVERED: (enums.EventSource.BTS,),
    enums.OrderEventType.STATUS_CHANGED: (enums.EventSource.ADMIN,),
    enums.OrderEventType.SHIPMENT_CREATED: (enums.EventSource.SYSTEM, enums.EventSource.ADMIN),
    enums.OrderEventType.SHIPMENT_FAILED: (enums.EventSource.SYSTEM, enums.EventSource.ADMIN),
    enums.OrderEventType.REFUND_REQUESTED: (enums.EventSource.SYSTEM, enums.EventSource.ADMIN),
    enums.OrderEventType.CANCELLED: (enums.EventSource.SYSTEM, enums.EventSource.ADMIN),
}


def _status_by_type_condition() -> Q:
    condition = Q(type__in=list(NON_STATUS_EVENT_TYPES), status_after="")
    for event_type, statuses in EVENT_STATUS_AFTER.items():
        condition |= Q(type=event_type, status_after__in=list(statuses))
    return condition


def _source_by_type_condition() -> Q:
    condition = Q()
    for event_type, sources in EVENT_SOURCES.items():
        condition |= Q(type=event_type, source__in=list(sources))
    return condition


class Cart(TimeStampedModel):
    """The customer's server-side shopping cart (M2): one row per customer, emptied by a checkout."""

    customer = models.OneToOneField("accounts.Customer", on_delete=models.CASCADE, related_name="cart")
    last_checkout_order = models.ForeignKey(
        "commerce.Order", null=True, on_delete=models.SET_NULL, related_name="+"
    )  # the order the last successful checkout produced (debug aid; the M2 checkout is @idempotent anyway)

    class Meta:
        ordering = ["id"]

    def __str__(self) -> str:
        return f"Cart of customer {self.customer_id}"


class CartItem(models.Model):
    """One product line of a cart; prices are read live at checkout, ``price_at_add`` only feeds the cart's
    "price changed since you added it" hint (customer contract §2.5, M2-C12)."""

    cart = models.ForeignKey("commerce.Cart", on_delete=models.CASCADE, related_name="items")
    product = models.ForeignKey("catalog.Product", on_delete=models.CASCADE, related_name="cart_items")
    quantity = models.PositiveSmallIntegerField()
    price_at_add = models.BigIntegerField(null=True)  # Product.price at the last write; null before 0004
    added_at = models.DateTimeField(default=timestamp_default, editable=False)
    updated_at = models.DateTimeField(default=timestamp_default)

    class Meta:
        ordering = ["cart_id", "added_at", "id"]
        constraints = [
            models.UniqueConstraint(fields=["cart", "product"], name="cartitem_cart_product_unique"),
            models.CheckConstraint(
                condition=Q(quantity__gte=1, quantity__lte=99), name="cartitem_quantity_1_99"
            ),
        ]

    def __str__(self) -> str:
        return f"{self.quantity} × product {self.product_id} (cart {self.cart_id})"


class Order(TimeStampedModel):
    """A customer's online order; ``id`` is the order number shown everywhere (№10248, from 10001)."""

    customer = models.ForeignKey("accounts.Customer", on_delete=models.PROTECT, related_name="orders")
    channel = models.CharField(max_length=8, choices=enums.OrderChannel.choices)  # ios|android|web
    fulfilment = models.CharField(max_length=16, choices=enums.FulfilmentMethod.choices)  # delivery|pickup
    status = models.CharField(
        max_length=24, choices=enums.OrderStatus.choices, default=enums.OrderStatus.AWAITING_PAYMENT
    )
    # delivery snapshot (delivery only; "" for pickup)
    address_text = models.CharField(max_length=300, blank=True)
    address_region_code = models.CharField(max_length=16, blank=True)  # BTS region id from accounts.Address
    address_city_code = models.CharField(max_length=16, blank=True)  # BTS city id
    address_comment = models.CharField(max_length=200, blank=True)
    recipient_name = models.CharField(max_length=150, blank=True)  # D-3: BTS needs a name after a scrub
    recipient_phone = models.CharField(max_length=13, blank=True)
    customer_note = models.CharField(max_length=300, blank=True)  # M2 "izoh"
    # money (so'm)
    items_subtotal = models.BigIntegerField()  # Σ OrderItem.line_total
    promo_subtotal = models.BigIntegerField(default=0)  # Σ line_total WHERE promo
    delivery_fee = models.BigIntegerField(default=0)  # quoted BTS tariff at checkout; 0 for pickup
    cashback_used = models.BigIntegerField(default=0)  # = CashbackHold.amount
    total = models.BigIntegerField()  # items_subtotal + delivery_fee − cashback_used
    payment_provider = models.CharField(
        max_length=16, choices=enums.PaymentProvider.choices
    )  # chosen at checkout
    # reservation
    reservation_minutes = models.PositiveSmallIntegerField()  # SiteSettings.reservation_minutes at checkout
    reservation_expires_at = models.DateTimeField(
        null=True
    )  # set while awaiting_payment; kept afterwards for history
    # lifecycle
    status_changed_at = models.DateTimeField(default=timestamp_default)
    paid_at = models.DateTimeField(null=True)
    handed_to_courier_at = models.DateTimeField(null=True)
    delivered_at = models.DateTimeField(null=True)
    cancelled_at = models.DateTimeField(null=True)
    cancellation_reason = models.CharField(
        max_length=24, blank=True, choices=enums.CancellationReason.choices
    )  # reservation_expired|admin|customer_request
    cancellation_note = models.CharField(max_length=500, blank=True)  # admin reason text (3..500)
    cancelled_by = models.ForeignKey(
        "accounts.StaffUser", null=True, on_delete=models.PROTECT, related_name="cancelled_orders"
    )
    metadata = models.JSONField(
        default=dict, blank=True, encoder=DjangoJSONEncoder
    )  # internal; keys in design 03 §1.3 (seed, fake.* provider knobs, checkout.* diagnostics)

    class Meta:
        ordering = ["-created_at", "-id"]
        constraints = [
            models.CheckConstraint(
                condition=Q(total=F("items_subtotal") + F("delivery_fee") - F("cashback_used")),
                name="order_total_formula",
            ),
            models.CheckConstraint(
                condition=Q(
                    items_subtotal__gte=0,
                    promo_subtotal__gte=0,
                    delivery_fee__gte=0,
                    cashback_used__gte=0,
                    total__gte=0,
                    promo_subtotal__lte=F("items_subtotal"),
                    cashback_used__lte=F("items_subtotal"),
                ),
                name="order_amounts_range",
            ),
            models.CheckConstraint(
                condition=(Q(fulfilment=enums.FulfilmentMethod.DELIVERY) & ~Q(address_text=""))
                | Q(
                    fulfilment=enums.FulfilmentMethod.PICKUP,
                    address_text="",
                    address_region_code="",
                    address_city_code="",
                    delivery_fee=0,
                ),
                name="order_address_by_fulfilment",
            ),
            models.CheckConstraint(
                condition=Q(recipient_phone="") | Q(recipient_phone__regex=PHONE_E164_REGEX),
                name="order_recipient_phone_e164",
            ),
            models.CheckConstraint(
                condition=(
                    Q(status=enums.OrderStatus.CANCELLED, cancelled_at__isnull=False)
                    & ~Q(cancellation_reason="")
                )
                | Q(
                    status__in=[
                        enums.OrderStatus.AWAITING_PAYMENT,
                        enums.OrderStatus.PROCESSING,
                        enums.OrderStatus.HANDED_TO_COURIER,
                        enums.OrderStatus.DELIVERED,
                    ],
                    cancelled_at__isnull=True,
                    cancellation_reason="",
                    cancellation_note="",
                    cancelled_by__isnull=True,
                ),
                name="order_cancellation_fields",
            ),
            models.CheckConstraint(
                condition=(
                    Q(cancellation_reason=enums.CancellationReason.ADMIN, cancelled_by__isnull=False)
                    & ~Q(cancellation_note="")
                )
                | Q(
                    cancellation_reason__in=[
                        enums.CancellationReason.RESERVATION_EXPIRED,
                        enums.CancellationReason.CUSTOMER_REQUEST,
                    ],
                    cancelled_by__isnull=True,
                )
                | Q(cancellation_reason=""),
                name="order_cancellation_by_reason",
            ),
            models.CheckConstraint(
                condition=Q(status=enums.OrderStatus.AWAITING_PAYMENT, paid_at__isnull=True)
                | Q(
                    status__in=[
                        enums.OrderStatus.PROCESSING,
                        enums.OrderStatus.HANDED_TO_COURIER,
                        enums.OrderStatus.DELIVERED,
                    ],
                    paid_at__isnull=False,
                )
                | Q(status=enums.OrderStatus.CANCELLED),
                name="order_paid_at_by_status",
            ),
            models.CheckConstraint(
                condition=Q(status=enums.OrderStatus.DELIVERED, delivered_at__isnull=False)
                | (~Q(status=enums.OrderStatus.DELIVERED) & Q(delivered_at__isnull=True)),
                name="order_delivered_at_by_status",
            ),
            models.CheckConstraint(
                condition=~Q(status=enums.OrderStatus.AWAITING_PAYMENT)
                | Q(reservation_expires_at__isnull=False),
                name="order_awaiting_has_expiry",
            ),
            models.CheckConstraint(
                condition=Q(reservation_minutes__gte=5, reservation_minutes__lte=60),
                name="order_reservation_minutes_5_60",
            ),
        ]
        indexes = [
            models.Index(fields=["status"], name="order_status_idx"),
            models.Index(fields=["created_at", "id"], name="order_created_idx"),
            models.Index(fields=["customer", "-created_at", "-id"], name="order_customer_created_idx"),
            models.Index(
                fields=["reservation_expires_at"],
                condition=Q(status=enums.OrderStatus.AWAITING_PAYMENT),
                name="order_awaiting_expiry_idx",
            ),
            models.Index(fields=["payment_provider"], name="order_provider_idx"),
        ]

    def __str__(self) -> str:
        return f"Order №{self.pk} ({self.status}, {self.total} so'm, customer {self.customer_id})"


class OrderItem(models.Model):
    """One product line of an order, fully snapshotted at checkout (name, Billz id, image, price, promo)."""

    order = models.ForeignKey("commerce.Order", on_delete=models.CASCADE, related_name="items")
    position = models.PositiveSmallIntegerField()
    product = models.ForeignKey("catalog.Product", on_delete=models.PROTECT, related_name="order_items")
    name = models.CharField(max_length=200)  # Product.name_uz, else billz_name, at checkout
    billz_id = models.CharField(max_length=14)
    image_url = models.CharField(
        max_length=500, blank=True
    )  # media storage path of images[0] at checkout ("products/1/9f2c.webp"); rendered absolute by
    # core.media.absolute_media_url at serialization time; "" → null
    quantity = models.PositiveIntegerField()
    unit_price = models.BigIntegerField()  # Product.price at checkout
    promo = models.BooleanField(default=False)
    line_total = models.BigIntegerField()  # quantity × unit_price

    class Meta:
        ordering = ["order_id", "position"]
        constraints = [
            models.UniqueConstraint(fields=["order", "position"], name="orderitem_position_unique"),
            models.UniqueConstraint(fields=["order", "product"], name="orderitem_product_unique"),
            models.CheckConstraint(condition=Q(quantity__gte=1, unit_price__gte=0), name="orderitem_amounts"),
            models.CheckConstraint(
                condition=Q(line_total=F("quantity") * F("unit_price")), name="orderitem_line_total"
            ),
            models.CheckConstraint(condition=~Q(name=""), name="orderitem_name_required"),
        ]

    def __str__(self) -> str:
        return f"{self.name} × {self.quantity} @ {self.unit_price} (order {self.order_id})"


class OrderEvent(AppendOnlyModel):
    """One immutable, typed entry of an order's history (contract §4); written only by ``record_event``."""

    order = models.ForeignKey("commerce.Order", on_delete=models.PROTECT, related_name="events")
    type = models.CharField(max_length=32, choices=enums.OrderEventType.choices)
    source = models.CharField(max_length=8, choices=enums.EventSource.choices)  # system|bts|admin
    status_after = models.CharField(max_length=24, blank=True, choices=enums.OrderStatus.choices)  # "" → null
    actor = models.ForeignKey(
        "accounts.StaffUser", null=True, on_delete=models.PROTECT, related_name="order_events"
    )
    data = models.JSONField(default=dict, encoder=DjangoJSONEncoder)
    at = models.DateTimeField(default=timestamp_default, editable=False)

    class Meta:
        ordering = ["-at", "-id"]
        indexes = [models.Index(fields=["order", "at", "id"], name="orderevent_order_at_idx")]
        constraints = [
            models.CheckConstraint(
                condition=Q(source=enums.EventSource.ADMIN, actor__isnull=False)
                | (~Q(source=enums.EventSource.ADMIN) & Q(actor__isnull=True)),
                name="orderevent_actor_by_source",
            ),
            models.CheckConstraint(condition=_status_by_type_condition(), name="orderevent_status_by_type"),
            models.CheckConstraint(condition=_source_by_type_condition(), name="orderevent_source_by_type"),
        ]

    def __str__(self) -> str:
        return f"Order {self.order_id}: {self.type} ({self.source}) at {self.at:%Y-%m-%d %H:%M}"


class StockReservation(TimeStampedModel):
    """Units of a product held for an unpaid order; converted on payment, released on expiry/cancel."""

    class State(models.TextChoices):  # internal
        ACTIVE = "active"
        CONVERTED = "converted"
        RELEASED = "released"

    class ReleaseReason(models.TextChoices):  # internal
        EXPIRED = "expired"
        CANCELLED = "cancelled"
        PAYMENT_FAILED = "payment_failed"

    order = models.ForeignKey("commerce.Order", on_delete=models.CASCADE, related_name="reservations")
    product = models.ForeignKey("catalog.Product", on_delete=models.PROTECT, related_name="reservations")
    quantity = models.PositiveIntegerField()
    state = models.CharField(max_length=16, choices=State.choices, default=State.ACTIVE)
    expires_at = models.DateTimeField()  # = Order.reservation_expires_at
    converted_at = models.DateTimeField(null=True)
    released_at = models.DateTimeField(null=True)
    release_reason = models.CharField(max_length=16, blank=True, choices=ReleaseReason.choices)
    # A converted reservation stops counting against availability for good once Billz has (or staff recorded)
    # the sale and the stock was read after it (``services.settle_billz_reservations``, NEW-2): the hot
    # availability read touches only the unsettled ones.
    billz_settled_at = models.DateTimeField(null=True)

    class Meta:
        ordering = ["order_id", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["order", "product"],
                condition=Q(state="active"),
                name="reservation_one_active_per_line",
            ),
            models.CheckConstraint(condition=Q(quantity__gte=1), name="reservation_quantity_positive"),
            models.CheckConstraint(
                condition=(
                    Q(state="active", converted_at__isnull=True, released_at__isnull=True, release_reason="")
                    | Q(
                        state="converted",
                        converted_at__isnull=False,
                        released_at__isnull=True,
                        release_reason="",
                    )
                    | (
                        Q(state="released", converted_at__isnull=True, released_at__isnull=False)
                        & ~Q(release_reason="")
                    )
                ),
                name="reservation_state_fields",
            ),
        ]
        indexes = [
            models.Index(
                fields=["product"], condition=Q(state="active"), name="reservation_active_product_idx"
            ),
            models.Index(
                fields=["product", "converted_at"],
                condition=Q(state="converted"),
                name="reservation_converted_prod_idx",
            ),
            models.Index(
                fields=["expires_at"], condition=Q(state="active"), name="reservation_active_expires_idx"
            ),
            models.Index(
                fields=["product"],
                condition=Q(state="converted", billz_settled_at__isnull=True),
                name="reservation_unsettled_prod_idx",
            ),
        ]

    def __str__(self) -> str:
        return (
            f"Reservation {self.quantity} × product {self.product_id} "
            f"for order {self.order_id} ({self.state})"
        )
