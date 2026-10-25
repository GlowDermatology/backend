"""loyalty.0001_initial — CashbackPolicy, CashbackTier, LoyaltyAccount, LedgerEntry, CashbackHold, Purchase,
PurchaseItem, PosDevice with every CHECK, partial unique and index of design 02 §1 (the append-only triggers
are 0002). ``Purchase`` ↔ ``LedgerEntry`` is a circular FK: ``LedgerEntry.purchase`` is added after both
tables exist (``makemigrations`` emits it so; no manual work)."""

import django.db.models.deletion
from django.db import migrations, models

import glowderma.core.models


class Migration(migrations.Migration):
    initial = True

    # accounts.0004 holds Customer (StaffUser since 0001); catalog.0001 holds Product; commerce.0001 holds
    # Order (never 0002/0003: the sequence restart and the order-event trigger are not needed here); core.0007
    # is the extension/collation base every M1 table sits on (design 02 §0.2, 03 §9 row 12).
    dependencies = [
        ("accounts", "0004_customer_device_address_smscode_social"),
        ("catalog", "0001_initial"),
        ("commerce", "0001_initial"),
        ("core", "0007_db_extensions"),
    ]

    operations = [
        migrations.CreateModel(
            name="CashbackPolicy",
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
                ("version", models.PositiveIntegerField(unique=True)),
                ("effective_from", models.DateTimeField()),
                (
                    "basis",
                    models.CharField(
                        choices=[
                            ("before_purchase", "Before Purchase"),
                            ("with_purchase", "With Purchase"),
                        ],
                        max_length=16,
                    ),
                ),
                ("earn_on_cashback_paid", models.BooleanField()),
                (
                    "created_at",
                    models.DateTimeField(default=glowderma.core.models.timestamp_default, editable=False),
                ),
                (
                    "author",
                    models.ForeignKey(
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="cashback_policies",
                        to="accounts.staffuser",
                    ),
                ),
            ],
            options={
                "ordering": ["-version"],
            },
        ),
        migrations.CreateModel(
            name="CashbackTier",
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
                ("from_amount", models.BigIntegerField()),
                ("to_amount", models.BigIntegerField(null=True)),
                ("percent", models.DecimalField(decimal_places=2, max_digits=5)),
                (
                    "created_at",
                    models.DateTimeField(default=glowderma.core.models.timestamp_default, editable=False),
                ),
                (
                    "policy",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="tiers",
                        to="loyalty.cashbackpolicy",
                    ),
                ),
            ],
            options={
                "ordering": ["policy_id", "from_amount"],
            },
        ),
        migrations.CreateModel(
            name="LoyaltyAccount",
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
                ("balance", models.BigIntegerField(default=0)),
                (
                    "customer",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="loyalty_account",
                        to="accounts.customer",
                    ),
                ),
            ],
            options={
                "abstract": False,
            },
        ),
        migrations.CreateModel(
            name="LedgerEntry",
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
                    "kind",
                    models.CharField(
                        choices=[
                            ("credit", "Credit"),
                            ("debit", "Debit"),
                            ("adjustment", "Adjustment"),
                            ("reversal", "Reversal"),
                        ],
                        max_length=16,
                    ),
                ),
                ("amount", models.BigIntegerField()),
                ("balance_after", models.BigIntegerField()),
                (
                    "source_type",
                    models.CharField(
                        choices=[
                            ("order", "Order"),
                            ("billz_sale", "Billz Sale"),
                            ("admin", "Admin"),
                        ],
                        max_length=16,
                    ),
                ),
                ("source_ref", models.CharField(blank=True, max_length=64)),
                ("base", models.BigIntegerField(null=True)),
                (
                    "percent",
                    models.DecimalField(decimal_places=2, max_digits=5, null=True),
                ),
                ("promo_excluded", models.BooleanField(default=False)),
                ("reason", models.TextField(blank=True)),
                (
                    "at",
                    models.DateTimeField(default=glowderma.core.models.timestamp_default, editable=False),
                ),
                (
                    "actor",
                    models.ForeignKey(
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="ledger_adjustments",
                        to="accounts.staffuser",
                    ),
                ),
                (
                    "order",
                    models.ForeignKey(
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="ledger_entries",
                        to="commerce.order",
                    ),
                ),
                (
                    "policy",
                    models.ForeignKey(
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="ledger_entries",
                        to="loyalty.cashbackpolicy",
                    ),
                ),
                (
                    "reverses",
                    models.OneToOneField(
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="reversed_by",
                        to="loyalty.ledgerentry",
                    ),
                ),
                (
                    "account",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="entries",
                        to="loyalty.loyaltyaccount",
                    ),
                ),
            ],
            options={
                "ordering": ["-at", "-id"],
            },
        ),
        migrations.CreateModel(
            name="CashbackHold",
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
                ("amount", models.BigIntegerField()),
                (
                    "state",
                    models.CharField(
                        choices=[
                            ("active", "Active"),
                            ("captured", "Captured"),
                            ("released", "Released"),
                        ],
                        default="active",
                        max_length=16,
                    ),
                ),
                ("expires_at", models.DateTimeField()),
                ("captured_at", models.DateTimeField(null=True)),
                ("released_at", models.DateTimeField(null=True)),
                (
                    "release_reason",
                    models.CharField(
                        blank=True,
                        choices=[
                            ("reservation_expired", "Reservation Expired"),
                            ("order_cancelled", "Order Cancelled"),
                            ("payment_failed", "Payment Failed"),
                        ],
                        max_length=24,
                    ),
                ),
                (
                    "order",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="cashback_holds",
                        to="commerce.order",
                    ),
                ),
                (
                    "captured_entry",
                    models.OneToOneField(
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="captured_hold",
                        to="loyalty.ledgerentry",
                    ),
                ),
                (
                    "account",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="holds",
                        to="loyalty.loyaltyaccount",
                    ),
                ),
            ],
        ),
        migrations.CreateModel(
            name="PosDevice",
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
                ("name", models.CharField(max_length=100)),
                ("location_label", models.CharField(blank=True, max_length=100)),
                ("key_prefix", models.CharField(max_length=12)),
                ("key_hash", models.CharField(max_length=64, unique=True)),
                ("is_active", models.BooleanField(default=True)),
                ("last_used_at", models.DateTimeField(null=True)),
                (
                    "created_by",
                    models.ForeignKey(
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="pos_devices",
                        to="accounts.staffuser",
                    ),
                ),
            ],
            options={
                "ordering": ["name"],
            },
        ),
        migrations.CreateModel(
            name="Purchase",
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
                    "source",
                    models.CharField(
                        choices=[("online", "Online"), ("store_qr", "Store Qr")],
                        max_length=16,
                    ),
                ),
                ("billz_sale_id", models.CharField(blank=True, max_length=64)),
                ("billz_sale_ref", models.CharField(blank=True, max_length=32)),
                ("billz_order_uuid", models.UUIDField(null=True)),
                ("billz_pushed_at", models.DateTimeField(null=True)),
                ("amount", models.BigIntegerField()),
                ("promo_amount", models.BigIntegerField(default=0)),
                ("cashback_paid", models.BigIntegerField(default=0)),
                ("lifetime_before", models.BigIntegerField()),
                ("cashback_amount", models.BigIntegerField(default=0)),
                ("reversed", models.BooleanField(default=False)),
                ("reversed_at", models.DateTimeField(null=True)),
                (
                    "at",
                    models.DateTimeField(default=glowderma.core.models.timestamp_default),
                ),
                (
                    "credited_entry",
                    models.OneToOneField(
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="credited_purchase",
                        to="loyalty.ledgerentry",
                    ),
                ),
                (
                    "customer",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="purchases",
                        to="accounts.customer",
                    ),
                ),
                (
                    "order",
                    models.OneToOneField(
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="purchase",
                        to="commerce.order",
                    ),
                ),
                (
                    "policy",
                    models.ForeignKey(
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="purchases",
                        to="loyalty.cashbackpolicy",
                    ),
                ),
                (
                    "reversal_entry",
                    models.OneToOneField(
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="reversed_purchase",
                        to="loyalty.ledgerentry",
                    ),
                ),
            ],
            options={
                "ordering": ["-at", "-id"],
            },
        ),
        migrations.AddField(
            model_name="ledgerentry",
            name="purchase",
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="ledger_entries",
                to="loyalty.purchase",
            ),
        ),
        migrations.CreateModel(
            name="PurchaseItem",
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
                ("billz_product_id", models.CharField(blank=True, max_length=32)),
                ("name", models.CharField(max_length=200)),
                ("quantity", models.PositiveIntegerField()),
                ("unit_price", models.BigIntegerField()),
                ("promo", models.BooleanField(default=False)),
                ("line_total", models.BigIntegerField()),
                (
                    "product",
                    models.ForeignKey(
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="purchase_items",
                        to="catalog.product",
                    ),
                ),
                (
                    "purchase",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="items",
                        to="loyalty.purchase",
                    ),
                ),
            ],
            options={
                "ordering": ["purchase_id", "position"],
            },
        ),
        migrations.AddIndex(
            model_name="cashbackpolicy",
            index=models.Index(fields=["effective_from"], name="policy_effective_from_idx"),
        ),
        migrations.AddConstraint(
            model_name="cashbackpolicy",
            constraint=models.CheckConstraint(
                condition=models.Q(("effective_from__gt", models.F("created_at"))),
                name="policy_effective_after_created",
            ),
        ),
        migrations.AddConstraint(
            model_name="cashbacktier",
            constraint=models.UniqueConstraint(
                fields=("policy", "from_amount"), name="tier_policy_from_unique"
            ),
        ),
        migrations.AddConstraint(
            model_name="cashbacktier",
            constraint=models.CheckConstraint(
                condition=models.Q(("from_amount__gte", 0)), name="tier_from_nonneg"
            ),
        ),
        migrations.AddConstraint(
            model_name="cashbacktier",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("to_amount__isnull", True),
                    ("to_amount__gt", models.F("from_amount")),
                    _connector="OR",
                ),
                name="tier_to_above_from",
            ),
        ),
        migrations.AddConstraint(
            model_name="cashbacktier",
            constraint=models.CheckConstraint(
                condition=models.Q(("percent__gt", 0), ("percent__lte", 100)),
                name="tier_percent_range",
            ),
        ),
        migrations.AddIndex(
            model_name="cashbackhold",
            index=models.Index(
                condition=models.Q(("state", "active")),
                fields=["account"],
                name="hold_active_account_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="cashbackhold",
            index=models.Index(
                condition=models.Q(("state", "active")),
                fields=["expires_at"],
                name="hold_active_expires_idx",
            ),
        ),
        migrations.AddConstraint(
            model_name="cashbackhold",
            constraint=models.UniqueConstraint(
                condition=models.Q(("state", "active")),
                fields=("order",),
                name="hold_one_active_per_order",
            ),
        ),
        migrations.AddConstraint(
            model_name="cashbackhold",
            constraint=models.CheckConstraint(
                condition=models.Q(("amount__gt", 0)), name="hold_amount_positive"
            ),
        ),
        migrations.AddConstraint(
            model_name="cashbackhold",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(
                        ("captured_at__isnull", True),
                        ("captured_entry__isnull", True),
                        ("release_reason", ""),
                        ("released_at__isnull", True),
                        ("state", "active"),
                    ),
                    models.Q(
                        ("captured_at__isnull", False),
                        ("captured_entry__isnull", False),
                        ("release_reason", ""),
                        ("released_at__isnull", True),
                        ("state", "captured"),
                    ),
                    models.Q(
                        ("captured_at__isnull", True),
                        ("captured_entry__isnull", True),
                        ("released_at__isnull", False),
                        ("state", "released"),
                        models.Q(("release_reason", ""), _negated=True),
                    ),
                    _connector="OR",
                ),
                name="hold_state_fields",
            ),
        ),
        migrations.AddIndex(
            model_name="purchase",
            index=models.Index(fields=["customer", "at", "id"], name="purchase_customer_at_idx"),
        ),
        migrations.AddIndex(
            model_name="purchase",
            index=models.Index(
                condition=models.Q(("reversed", False)),
                fields=["customer", "source"],
                name="purchase_live_cust_src_idx",
            ),
        ),
        migrations.AddConstraint(
            model_name="purchase",
            constraint=models.UniqueConstraint(
                condition=models.Q(("billz_sale_id", ""), _negated=True),
                fields=("billz_sale_id",),
                name="purchase_billz_sale_unique",
            ),
        ),
        migrations.AddConstraint(
            model_name="purchase",
            constraint=models.UniqueConstraint(
                fields=("billz_order_uuid",), name="purchase_billz_uuid_unique"
            ),
        ),
        migrations.AddConstraint(
            model_name="purchase",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("amount__gte", 0),
                    ("cashback_amount__gte", 0),
                    ("cashback_paid__gte", 0),
                    ("cashback_paid__lte", models.F("amount")),
                    ("lifetime_before__gte", 0),
                    ("promo_amount__gte", 0),
                    ("promo_amount__lte", models.F("amount")),
                ),
                name="purchase_amounts",
            ),
        ),
        migrations.AddConstraint(
            model_name="purchase",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(
                        ("billz_order_uuid__isnull", False),
                        ("order__isnull", False),
                        ("source", "online"),
                    ),
                    models.Q(
                        ("billz_order_uuid__isnull", True),
                        ("order__isnull", True),
                        ("source", "store_qr"),
                        models.Q(("billz_sale_id", ""), _negated=True),
                    ),
                    _connector="OR",
                ),
                name="purchase_source_refs",
            ),
        ),
        migrations.AddConstraint(
            model_name="purchase",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(("reversed", True), ("reversed_at__isnull", False)),
                    models.Q(
                        ("reversal_entry__isnull", True),
                        ("reversed", False),
                        ("reversed_at__isnull", True),
                    ),
                    _connector="OR",
                ),
                name="purchase_reversed_fields",
            ),
        ),
        migrations.AddConstraint(
            model_name="purchase",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(("cashback_amount__gt", 0), ("credited_entry__isnull", False)),
                    models.Q(
                        ("cashback_amount", 0),
                        ("credited_entry__isnull", True),
                        ("reversal_entry__isnull", True),
                    ),
                    _connector="OR",
                ),
                name="purchase_credit_link",
            ),
        ),
        migrations.AddIndex(
            model_name="ledgerentry",
            index=models.Index(fields=["account", "at", "id"], name="ledger_account_at_idx"),
        ),
        migrations.AddIndex(
            model_name="ledgerentry",
            index=models.Index(fields=["order"], name="ledger_order_idx"),
        ),
        migrations.AddIndex(
            model_name="ledgerentry",
            index=models.Index(fields=["purchase"], name="ledger_purchase_idx"),
        ),
        migrations.AddConstraint(
            model_name="ledgerentry",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(("amount__gt", 0), ("kind", "credit")),
                    models.Q(("amount__lt", 0), ("kind", "debit")),
                    models.Q(
                        ("kind__in", ["adjustment", "reversal"]),
                        models.Q(("amount", 0), _negated=True),
                    ),
                    _connector="OR",
                ),
                name="ledger_amount_sign",
            ),
        ),
        migrations.AddConstraint(
            model_name="ledgerentry",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(("kind", "reversal"), ("reverses__isnull", False)),
                    models.Q(
                        models.Q(("kind", "reversal"), _negated=True),
                        ("reverses__isnull", True),
                    ),
                    _connector="OR",
                ),
                name="ledger_reversal_link",
            ),
        ),
        migrations.AddConstraint(
            model_name="ledgerentry",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(
                        ("base__gte", 0),
                        ("base__isnull", False),
                        ("kind", "credit"),
                        ("percent__isnull", False),
                        ("policy__isnull", False),
                    ),
                    models.Q(
                        models.Q(("kind", "credit"), _negated=True),
                        ("base__isnull", True),
                        ("percent__isnull", True),
                        ("policy__isnull", True),
                        ("promo_excluded", False),
                    ),
                    _connector="OR",
                ),
                name="ledger_credit_fields",
            ),
        ),
        migrations.AddConstraint(
            model_name="ledgerentry",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(("kind", "adjustment"), ("source_type", "admin")),
                    models.Q(
                        models.Q(("source_type", "admin"), _negated=True),
                        models.Q(("kind", "adjustment"), _negated=True),
                    ),
                    _connector="OR",
                ),
                name="ledger_admin_is_adjustment",
            ),
        ),
        migrations.AddConstraint(
            model_name="ledgerentry",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(
                        ("order__isnull", False),
                        ("source_type", "order"),
                        models.Q(("source_ref", ""), _negated=True),
                    ),
                    models.Q(
                        ("order__isnull", True),
                        ("purchase__isnull", False),
                        ("source_type", "billz_sale"),
                        models.Q(("source_ref", ""), _negated=True),
                    ),
                    models.Q(
                        ("order__isnull", True),
                        ("purchase__isnull", True),
                        ("source_ref", ""),
                        ("source_type", "admin"),
                    ),
                    _connector="OR",
                ),
                name="ledger_source_refs",
            ),
        ),
        migrations.AddConstraint(
            model_name="ledgerentry",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(
                        ("actor__isnull", False),
                        ("kind", "adjustment"),
                        models.Q(("reason", ""), _negated=True),
                    ),
                    models.Q(
                        models.Q(("kind", "adjustment"), _negated=True),
                        ("actor__isnull", True),
                        ("reason", ""),
                    ),
                    _connector="OR",
                ),
                name="ledger_adjustment_fields",
            ),
        ),
        migrations.AddConstraint(
            model_name="ledgerentry",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("percent__isnull", True),
                    models.Q(("percent__gt", 0), ("percent__lte", 100)),
                    _connector="OR",
                ),
                name="ledger_percent_range",
            ),
        ),
        migrations.AddConstraint(
            model_name="purchaseitem",
            constraint=models.UniqueConstraint(
                fields=("purchase", "position"), name="purchaseitem_position_unique"
            ),
        ),
        migrations.AddConstraint(
            model_name="purchaseitem",
            constraint=models.CheckConstraint(
                condition=models.Q(("line_total__gte", 0), ("quantity__gt", 0), ("unit_price__gte", 0)),
                name="purchaseitem_amounts",
            ),
        ),
    ]
