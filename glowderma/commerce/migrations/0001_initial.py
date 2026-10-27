"""commerce.0001_initial — Cart, CartItem, Order, OrderItem, OrderEvent, StockReservation with every CHECK,
partial unique and index of design 03 §1 (the sequence restart is 0002, the append-only trigger 0003)."""

import django.core.serializers.json
import django.db.models.deletion
import django.db.models.expressions
from django.db import migrations, models

import glowderma.core.models


class Migration(migrations.Migration):
    initial = True

    # accounts.0004 holds Customer (StaffUser since 0001); catalog.0001 holds Product; core.0007 is the
    # extension/collation base every M1 table sits on (design 03 §9 row 9). Never a later accounts migration:
    # accounts.0005 belongs to a parallel package's slice.
    dependencies = [
        ("accounts", "0004_customer_device_address_smscode_social"),
        ("catalog", "0001_initial"),
        ("core", "0007_db_extensions"),
    ]

    operations = [
        migrations.CreateModel(
            name="Order",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "created_at",
                    models.DateTimeField(default=glowderma.core.models.timestamp_default, editable=False),
                ),
                (
                    "updated_at",
                    models.DateTimeField(default=glowderma.core.models.timestamp_default),
                ),
                (
                    "channel",
                    models.CharField(choices=[("ios", "Ios"), ("android", "Android")], max_length=8),
                ),
                (
                    "fulfilment",
                    models.CharField(
                        choices=[("delivery", "Delivery"), ("pickup", "Pickup")],
                        max_length=16,
                    ),
                ),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("awaiting_payment", "Awaiting Payment"),
                            ("processing", "Processing"),
                            ("handed_to_courier", "Handed To Courier"),
                            ("delivered", "Delivered"),
                            ("cancelled", "Cancelled"),
                        ],
                        default="awaiting_payment",
                        max_length=24,
                    ),
                ),
                ("address_text", models.CharField(blank=True, max_length=300)),
                ("address_region_code", models.CharField(blank=True, max_length=16)),
                ("address_city_code", models.CharField(blank=True, max_length=16)),
                ("address_comment", models.CharField(blank=True, max_length=200)),
                ("recipient_name", models.CharField(blank=True, max_length=150)),
                ("recipient_phone", models.CharField(blank=True, max_length=13)),
                ("customer_note", models.CharField(blank=True, max_length=300)),
                ("items_subtotal", models.BigIntegerField()),
                ("promo_subtotal", models.BigIntegerField(default=0)),
                ("delivery_fee", models.BigIntegerField(default=0)),
                ("cashback_used", models.BigIntegerField(default=0)),
                ("total", models.BigIntegerField()),
                (
                    "payment_provider",
                    models.CharField(choices=[("click", "Click"), ("payme", "Payme")], max_length=16),
                ),
                ("reservation_minutes", models.PositiveSmallIntegerField()),
                ("reservation_expires_at", models.DateTimeField(null=True)),
                (
                    "status_changed_at",
                    models.DateTimeField(default=glowderma.core.models.timestamp_default),
                ),
                ("paid_at", models.DateTimeField(null=True)),
                ("handed_to_courier_at", models.DateTimeField(null=True)),
                ("delivered_at", models.DateTimeField(null=True)),
                ("cancelled_at", models.DateTimeField(null=True)),
                (
                    "cancellation_reason",
                    models.CharField(
                        blank=True,
                        choices=[
                            ("reservation_expired", "Reservation Expired"),
                            ("admin", "Admin"),
                            ("customer_request", "Customer Request"),
                        ],
                        max_length=24,
                    ),
                ),
                ("cancellation_note", models.CharField(blank=True, max_length=500)),
                (
                    "metadata",
                    models.JSONField(
                        blank=True,
                        default=dict,
                        encoder=django.core.serializers.json.DjangoJSONEncoder,
                    ),
                ),
                (
                    "cancelled_by",
                    models.ForeignKey(
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="cancelled_orders",
                        to="accounts.staffuser",
                    ),
                ),
                (
                    "customer",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="orders",
                        to="accounts.customer",
                    ),
                ),
            ],
            options={
                "ordering": ["-created_at", "-id"],
            },
        ),
        migrations.CreateModel(
            name="Cart",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "created_at",
                    models.DateTimeField(default=glowderma.core.models.timestamp_default, editable=False),
                ),
                (
                    "updated_at",
                    models.DateTimeField(default=glowderma.core.models.timestamp_default),
                ),
                (
                    "customer",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="cart",
                        to="accounts.customer",
                    ),
                ),
                (
                    "last_checkout_order",
                    models.ForeignKey(
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="+",
                        to="commerce.order",
                    ),
                ),
            ],
            options={
                "ordering": ["id"],
            },
        ),
        migrations.CreateModel(
            name="OrderEvent",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "type",
                    models.CharField(
                        choices=[
                            ("order_created", "Order Created"),
                            ("reservation_created", "Reservation Created"),
                            ("payment_confirmed", "Payment Confirmed"),
                            ("billz_sale_recorded", "Billz Sale Recorded"),
                            ("cashback_credited", "Cashback Credited"),
                            ("payment_failed", "Payment Failed"),
                            ("shipment_created", "Shipment Created"),
                            ("shipment_failed", "Shipment Failed"),
                            ("status_changed", "Status Changed"),
                            ("handed_to_courier", "Handed To Courier"),
                            ("delivered", "Delivered"),
                            ("cancelled", "Cancelled"),
                            ("reservation_expired", "Reservation Expired"),
                            ("refund_requested", "Refund Requested"),
                            ("refund_succeeded", "Refund Succeeded"),
                            ("refund_failed", "Refund Failed"),
                        ],
                        max_length=32,
                    ),
                ),
                (
                    "source",
                    models.CharField(
                        choices=[
                            ("system", "System"),
                            ("bts", "Bts"),
                            ("admin", "Admin"),
                        ],
                        max_length=8,
                    ),
                ),
                (
                    "status_after",
                    models.CharField(
                        blank=True,
                        choices=[
                            ("awaiting_payment", "Awaiting Payment"),
                            ("processing", "Processing"),
                            ("handed_to_courier", "Handed To Courier"),
                            ("delivered", "Delivered"),
                            ("cancelled", "Cancelled"),
                        ],
                        max_length=24,
                    ),
                ),
                (
                    "data",
                    models.JSONField(
                        default=dict,
                        encoder=django.core.serializers.json.DjangoJSONEncoder,
                    ),
                ),
                (
                    "at",
                    models.DateTimeField(default=glowderma.core.models.timestamp_default, editable=False),
                ),
                (
                    "actor",
                    models.ForeignKey(
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="order_events",
                        to="accounts.staffuser",
                    ),
                ),
                (
                    "order",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="events",
                        to="commerce.order",
                    ),
                ),
            ],
            options={
                "ordering": ["-at", "-id"],
            },
        ),
        migrations.CreateModel(
            name="OrderItem",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("position", models.PositiveSmallIntegerField()),
                ("name", models.CharField(max_length=200)),
                ("billz_id", models.CharField(max_length=14)),
                ("image_url", models.CharField(blank=True, max_length=500)),
                ("quantity", models.PositiveIntegerField()),
                ("unit_price", models.BigIntegerField()),
                ("promo", models.BooleanField(default=False)),
                ("line_total", models.BigIntegerField()),
                (
                    "order",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="items",
                        to="commerce.order",
                    ),
                ),
                (
                    "product",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="order_items",
                        to="catalog.product",
                    ),
                ),
            ],
            options={
                "ordering": ["order_id", "position"],
            },
        ),
        migrations.CreateModel(
            name="StockReservation",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "created_at",
                    models.DateTimeField(default=glowderma.core.models.timestamp_default, editable=False),
                ),
                (
                    "updated_at",
                    models.DateTimeField(default=glowderma.core.models.timestamp_default),
                ),
                ("quantity", models.PositiveIntegerField()),
                (
                    "state",
                    models.CharField(
                        choices=[
                            ("active", "Active"),
                            ("converted", "Converted"),
                            ("released", "Released"),
                        ],
                        default="active",
                        max_length=16,
                    ),
                ),
                ("expires_at", models.DateTimeField()),
                ("converted_at", models.DateTimeField(null=True)),
                ("released_at", models.DateTimeField(null=True)),
                (
                    "release_reason",
                    models.CharField(
                        blank=True,
                        choices=[
                            ("expired", "Expired"),
                            ("cancelled", "Cancelled"),
                            ("payment_failed", "Payment Failed"),
                        ],
                        max_length=16,
                    ),
                ),
                (
                    "order",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="reservations",
                        to="commerce.order",
                    ),
                ),
                (
                    "product",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="reservations",
                        to="catalog.product",
                    ),
                ),
            ],
            options={
                "ordering": ["order_id", "id"],
            },
        ),
        migrations.CreateModel(
            name="CartItem",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("quantity", models.PositiveSmallIntegerField()),
                (
                    "added_at",
                    models.DateTimeField(default=glowderma.core.models.timestamp_default, editable=False),
                ),
                (
                    "updated_at",
                    models.DateTimeField(default=glowderma.core.models.timestamp_default),
                ),
                (
                    "cart",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="items",
                        to="commerce.cart",
                    ),
                ),
                (
                    "product",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="cart_items",
                        to="catalog.product",
                    ),
                ),
            ],
            options={
                "ordering": ["cart_id", "added_at", "id"],
                "constraints": [
                    models.UniqueConstraint(fields=("cart", "product"), name="cartitem_cart_product_unique"),
                    models.CheckConstraint(
                        condition=models.Q(("quantity__gte", 1), ("quantity__lte", 99)),
                        name="cartitem_quantity_1_99",
                    ),
                ],
            },
        ),
        migrations.AddIndex(
            model_name="order",
            index=models.Index(fields=["status"], name="order_status_idx"),
        ),
        migrations.AddIndex(
            model_name="order",
            index=models.Index(fields=["created_at", "id"], name="order_created_idx"),
        ),
        migrations.AddIndex(
            model_name="order",
            index=models.Index(
                fields=["customer", "-created_at", "-id"],
                name="order_customer_created_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="order",
            index=models.Index(
                condition=models.Q(("status", "awaiting_payment")),
                fields=["reservation_expires_at"],
                name="order_awaiting_expiry_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="order",
            index=models.Index(fields=["payment_provider"], name="order_provider_idx"),
        ),
        migrations.AddConstraint(
            model_name="order",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    (
                        "total",
                        django.db.models.expressions.CombinedExpression(
                            django.db.models.expressions.CombinedExpression(
                                models.F("items_subtotal"),
                                "+",
                                models.F("delivery_fee"),
                            ),
                            "-",
                            models.F("cashback_used"),
                        ),
                    )
                ),
                name="order_total_formula",
            ),
        ),
        migrations.AddConstraint(
            model_name="order",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("cashback_used__gte", 0),
                    ("cashback_used__lte", models.F("items_subtotal")),
                    ("delivery_fee__gte", 0),
                    ("items_subtotal__gte", 0),
                    ("promo_subtotal__gte", 0),
                    ("promo_subtotal__lte", models.F("items_subtotal")),
                    ("total__gte", 0),
                ),
                name="order_amounts_range",
            ),
        ),
        migrations.AddConstraint(
            model_name="order",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(
                        ("fulfilment", "delivery"),
                        models.Q(("address_text", ""), _negated=True),
                    ),
                    models.Q(
                        ("address_city_code", ""),
                        ("address_region_code", ""),
                        ("address_text", ""),
                        ("delivery_fee", 0),
                        ("fulfilment", "pickup"),
                    ),
                    _connector="OR",
                ),
                name="order_address_by_fulfilment",
            ),
        ),
        migrations.AddConstraint(
            model_name="order",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("recipient_phone", ""),
                    ("recipient_phone__regex", "^\\+998\\d{9}$"),
                    _connector="OR",
                ),
                name="order_recipient_phone_e164",
            ),
        ),
        migrations.AddConstraint(
            model_name="order",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(
                        ("cancelled_at__isnull", False),
                        ("status", "cancelled"),
                        models.Q(("cancellation_reason", ""), _negated=True),
                    ),
                    models.Q(
                        ("cancellation_note", ""),
                        ("cancellation_reason", ""),
                        ("cancelled_at__isnull", True),
                        ("cancelled_by__isnull", True),
                        (
                            "status__in",
                            [
                                "awaiting_payment",
                                "processing",
                                "handed_to_courier",
                                "delivered",
                            ],
                        ),
                    ),
                    _connector="OR",
                ),
                name="order_cancellation_fields",
            ),
        ),
        migrations.AddConstraint(
            model_name="order",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(
                        ("cancellation_reason", "admin"),
                        ("cancelled_by__isnull", False),
                        models.Q(("cancellation_note", ""), _negated=True),
                    ),
                    models.Q(
                        (
                            "cancellation_reason__in",
                            ["reservation_expired", "customer_request"],
                        ),
                        ("cancelled_by__isnull", True),
                    ),
                    ("cancellation_reason", ""),
                    _connector="OR",
                ),
                name="order_cancellation_by_reason",
            ),
        ),
        migrations.AddConstraint(
            model_name="order",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(("paid_at__isnull", True), ("status", "awaiting_payment")),
                    models.Q(
                        ("paid_at__isnull", False),
                        (
                            "status__in",
                            ["processing", "handed_to_courier", "delivered"],
                        ),
                    ),
                    ("status", "cancelled"),
                    _connector="OR",
                ),
                name="order_paid_at_by_status",
            ),
        ),
        migrations.AddConstraint(
            model_name="order",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(("delivered_at__isnull", False), ("status", "delivered")),
                    models.Q(
                        models.Q(("status", "delivered"), _negated=True),
                        ("delivered_at__isnull", True),
                    ),
                    _connector="OR",
                ),
                name="order_delivered_at_by_status",
            ),
        ),
        migrations.AddConstraint(
            model_name="order",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(("status", "awaiting_payment"), _negated=True),
                    ("reservation_expires_at__isnull", False),
                    _connector="OR",
                ),
                name="order_awaiting_has_expiry",
            ),
        ),
        migrations.AddConstraint(
            model_name="order",
            constraint=models.CheckConstraint(
                condition=models.Q(("reservation_minutes__gte", 5), ("reservation_minutes__lte", 60)),
                name="order_reservation_minutes_5_60",
            ),
        ),
        migrations.AddIndex(
            model_name="orderevent",
            index=models.Index(fields=["order", "at", "id"], name="orderevent_order_at_idx"),
        ),
        migrations.AddConstraint(
            model_name="orderevent",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(("actor__isnull", False), ("source", "admin")),
                    models.Q(
                        models.Q(("source", "admin"), _negated=True),
                        ("actor__isnull", True),
                    ),
                    _connector="OR",
                ),
                name="orderevent_actor_by_source",
            ),
        ),
        migrations.AddConstraint(
            model_name="orderevent",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(
                        ("status_after", ""),
                        (
                            "type__in",
                            [
                                "reservation_created",
                                "billz_sale_recorded",
                                "cashback_credited",
                                "payment_failed",
                                "shipment_created",
                                "shipment_failed",
                                "refund_requested",
                                "refund_succeeded",
                                "refund_failed",
                            ],
                        ),
                    ),
                    models.Q(
                        ("status_after__in", ["awaiting_payment"]),
                        ("type", "order_created"),
                    ),
                    models.Q(
                        ("status_after__in", ["processing"]),
                        ("type", "payment_confirmed"),
                    ),
                    models.Q(
                        ("status_after__in", ["handed_to_courier"]),
                        ("type", "handed_to_courier"),
                    ),
                    models.Q(("status_after__in", ["delivered"]), ("type", "delivered")),
                    models.Q(("status_after__in", ["cancelled"]), ("type", "cancelled")),
                    models.Q(
                        ("status_after__in", ["cancelled"]),
                        ("type", "reservation_expired"),
                    ),
                    models.Q(
                        ("status_after__in", ["handed_to_courier", "delivered"]),
                        ("type", "status_changed"),
                    ),
                    _connector="OR",
                ),
                name="orderevent_status_by_type",
            ),
        ),
        migrations.AddConstraint(
            model_name="orderevent",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(("source__in", ["system"]), ("type", "order_created")),
                    models.Q(("source__in", ["system"]), ("type", "reservation_created")),
                    models.Q(("source__in", ["system"]), ("type", "payment_confirmed")),
                    models.Q(("source__in", ["system"]), ("type", "billz_sale_recorded")),
                    models.Q(("source__in", ["system"]), ("type", "cashback_credited")),
                    models.Q(("source__in", ["system"]), ("type", "payment_failed")),
                    models.Q(("source__in", ["system"]), ("type", "reservation_expired")),
                    models.Q(("source__in", ["system"]), ("type", "refund_succeeded")),
                    models.Q(("source__in", ["system"]), ("type", "refund_failed")),
                    models.Q(("source__in", ["bts"]), ("type", "handed_to_courier")),
                    models.Q(("source__in", ["bts"]), ("type", "delivered")),
                    models.Q(("source__in", ["admin"]), ("type", "status_changed")),
                    models.Q(
                        ("source__in", ["system", "admin"]),
                        ("type", "shipment_created"),
                    ),
                    models.Q(("source__in", ["system", "admin"]), ("type", "shipment_failed")),
                    models.Q(
                        ("source__in", ["system", "admin"]),
                        ("type", "refund_requested"),
                    ),
                    models.Q(("source__in", ["system", "admin"]), ("type", "cancelled")),
                    _connector="OR",
                ),
                name="orderevent_source_by_type",
            ),
        ),
        migrations.AddConstraint(
            model_name="orderitem",
            constraint=models.UniqueConstraint(
                fields=("order", "position"), name="orderitem_position_unique"
            ),
        ),
        migrations.AddConstraint(
            model_name="orderitem",
            constraint=models.UniqueConstraint(fields=("order", "product"), name="orderitem_product_unique"),
        ),
        migrations.AddConstraint(
            model_name="orderitem",
            constraint=models.CheckConstraint(
                condition=models.Q(("quantity__gte", 1), ("unit_price__gte", 0)),
                name="orderitem_amounts",
            ),
        ),
        migrations.AddConstraint(
            model_name="orderitem",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    (
                        "line_total",
                        django.db.models.expressions.CombinedExpression(
                            models.F("quantity"), "*", models.F("unit_price")
                        ),
                    )
                ),
                name="orderitem_line_total",
            ),
        ),
        migrations.AddConstraint(
            model_name="orderitem",
            constraint=models.CheckConstraint(
                condition=models.Q(("name", ""), _negated=True),
                name="orderitem_name_required",
            ),
        ),
        migrations.AddIndex(
            model_name="stockreservation",
            index=models.Index(
                condition=models.Q(("state", "active")),
                fields=["product"],
                name="reservation_active_product_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="stockreservation",
            index=models.Index(
                condition=models.Q(("state", "converted")),
                fields=["product", "converted_at"],
                name="reservation_converted_prod_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="stockreservation",
            index=models.Index(
                condition=models.Q(("state", "active")),
                fields=["expires_at"],
                name="reservation_active_expires_idx",
            ),
        ),
        migrations.AddConstraint(
            model_name="stockreservation",
            constraint=models.UniqueConstraint(
                condition=models.Q(("state", "active")),
                fields=("order", "product"),
                name="reservation_one_active_per_line",
            ),
        ),
        migrations.AddConstraint(
            model_name="stockreservation",
            constraint=models.CheckConstraint(
                condition=models.Q(("quantity__gte", 1)),
                name="reservation_quantity_positive",
            ),
        ),
        migrations.AddConstraint(
            model_name="stockreservation",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(
                        ("converted_at__isnull", True),
                        ("release_reason", ""),
                        ("released_at__isnull", True),
                        ("state", "active"),
                    ),
                    models.Q(
                        ("converted_at__isnull", False),
                        ("release_reason", ""),
                        ("released_at__isnull", True),
                        ("state", "converted"),
                    ),
                    models.Q(
                        ("converted_at__isnull", True),
                        ("released_at__isnull", False),
                        ("state", "released"),
                        models.Q(("release_reason", ""), _negated=True),
                    ),
                    _connector="OR",
                ),
                name="reservation_state_fields",
            ),
        ),
    ]
