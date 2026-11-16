"""Models of `delivery` — docs/plans/m1-models/02-… §3 (slice B2); Yandex Delivery (plan §3.1).

``Shipment`` is the parcel record of a delivery order (one row per order, created at checkout in state
``not_created``); ``provider`` names its carrier (``bts`` for every row before Yandex, decision 3).
``ShipmentEvent`` is its append-only protocol trail. The API's ``pending`` flag is not a column: it is
derived from the newest ``bts.create_shipment`` outbox message the row points at
(:meth:`ShipmentQuerySet.with_pending`).

Yandex Delivery adds the warehouse and policy singleton (``DeliverySettings`` + ``WarehouseHours`` /
``WarehouseClosure``), the stored checkout quote (``DeliveryQuote``), one ``CourierClaim`` per Yandex request
(at most one open per shipment) and the staff exception queue (``DeliveryIssue``). Every write happens inside
``delivery.services`` under the Order lock held by ``commerce``; the Django admin is read-only.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Self

from django.core.serializers.json import DjangoJSONEncoder
from django.db import models
from django.db.models import BooleanField, Case, F, Q, Value, When
from django.utils.translation import gettext

from glowderma.core import enums
from glowderma.core.labels import stamp
from glowderma.core.models import AppendOnlyModel, TimeStampedModel, timestamp_default

# Outbox statuses under which a BTS create message is still going to be delivered: ``pending`` and ``failed``
# are re-claimable by the dispatcher (backoff retry), ``in_flight`` is leased; ``done``/``dead`` are final.
PENDING_MESSAGE_STATUSES: tuple[str, ...] = ("pending", "failed", "in_flight")

PHONE_E164_REGEX = r"^\+998\d{9}$"

# Uzbekistan's bounding box (rounded outwards): a warehouse pin outside it is a typo (swapped lat/lng).
UZ_LATITUDE = (Decimal(37), Decimal(46))
UZ_LONGITUDE = (Decimal(55), Decimal(74))

_P = enums.DeliveryProvider


def _latlng_pair(prefix: str) -> Q:
    return Q(**{f"{prefix}latitude__isnull": True, f"{prefix}longitude__isnull": True}) | Q(
        **{f"{prefix}latitude__isnull": False, f"{prefix}longitude__isnull": False}
    )


# --- warehouse, hours, policy -------------------------------------------------------------------------------


class DeliverySettings(models.Model):
    """The warehouse (Yandex point A), its dispatch rules and the courier pricing policy; one row forever
    (pk 1, created by ``delivery.0007`` and re-ensured after ``post_migrate``). Reads never create it."""

    id = models.PositiveSmallIntegerField(primary_key=True, default=1, editable=False)
    # warehouse (point A of every claim)
    warehouse_name = models.CharField(max_length=150, blank=True, verbose_name="ombor nomi")
    warehouse_address = models.CharField(max_length=300, blank=True, verbose_name="ombor manzili")
    warehouse_latitude = models.DecimalField(
        max_digits=9, decimal_places=6, null=True, verbose_name="ombor kengligi"
    )
    warehouse_longitude = models.DecimalField(
        max_digits=9, decimal_places=6, null=True, verbose_name="ombor uzunligi"
    )
    contact_name = models.CharField(max_length=150, blank=True, verbose_name="mas’ul shaxs")
    contact_phone = models.CharField(max_length=13, blank=True, verbose_name="mas’ul telefoni")
    contact_email = models.EmailField(max_length=254, blank=True, verbose_name="mas’ul e-pochtasi")
    courier_comment = models.CharField(max_length=300, blank=True, verbose_name="kuryer uchun izoh")
    # dispatch rules (minutes)
    prep_minutes = models.PositiveSmallIntegerField(default=30, verbose_name="tayyorlash vaqti (daqiqa)")
    # a failed hand-over must still be able to come back before closing
    dispatch_cutoff_minutes = models.PositiveSmallIntegerField(
        default=90, verbose_name="yopilishdan oldin oxirgi chaqiruv (daqiqa)"
    )
    returns_accepted = models.BooleanField(default=True, verbose_name="qaytgan jo‘natmalar qabul qilinadi")
    dispatch_suspended = models.BooleanField(default=False, verbose_name="jo‘natish to‘xtatilgan")
    suspended_until = models.DateTimeField(null=True, blank=True, verbose_name="to‘xtatish muddati")
    suspension_note = models.CharField(max_length=300, blank=True, verbose_name="to‘xtatish izohi")
    # methods (off until staff enable them)
    standard_enabled = models.BooleanField(default=False, verbose_name="Standart yoqilgan")
    standard_taxi_class = models.CharField(
        max_length=16, default="courier", verbose_name="Standart: Yandex klassi"
    )
    standard_eta_min_minutes = models.PositiveSmallIntegerField(
        default=60, verbose_name="Standart: eng tez (daqiqa)"
    )
    standard_eta_max_minutes = models.PositiveSmallIntegerField(
        default=180, verbose_name="Standart: eng kech (daqiqa)"
    )
    express_enabled = models.BooleanField(default=False, verbose_name="Ekspress yoqilgan")
    express_taxi_class = models.CharField(
        max_length=16, default="express", verbose_name="Ekspress: Yandex klassi"
    )
    express_eta_min_minutes = models.PositiveSmallIntegerField(
        default=30, verbose_name="Ekspress: eng tez (daqiqa)"
    )
    express_eta_max_minutes = models.PositiveSmallIntegerField(
        default=90, verbose_name="Ekspress: eng kech (daqiqa)"
    )
    # product weights are not modelled: a parcel weighs this × the item count until staff weigh it
    default_item_weight_g = models.PositiveIntegerField(
        default=300, verbose_name="bitta mahsulot og‘irligi (g)"
    )
    # pricing policy (so'm; percent as DecimalField(5, 2))
    markup_percent = models.DecimalField(
        max_digits=5, decimal_places=2, default=Decimal("10.00"), verbose_name="ustama (%)"
    )
    estimate_vat_percent = models.DecimalField(
        max_digits=5, decimal_places=2, default=Decimal("0.00"), verbose_name="taxminga QQS (%)"
    )
    max_auto_increase = models.BigIntegerField(
        default=10_000, verbose_name="avtomatik qabul qilinadigan oshish (so‘m)"
    )
    max_loss_per_shipment = models.BigIntegerField(
        default=15_000, verbose_name="bitta jo‘natmadagi eng katta zarar (so‘m)"
    )
    prep_deadline_minutes = models.PositiveSmallIntegerField(
        default=120, verbose_name="tayyorlash muddati (ish daqiqasi)"
    )
    # bookkeeping
    updated_at = models.DateTimeField(default=timestamp_default, verbose_name="yangilangan")
    updated_by = models.ForeignKey(
        "accounts.StaffUser",
        null=True,
        on_delete=models.SET_NULL,
        related_name="+",
        verbose_name="yangilagan",
    )

    class Meta:
        verbose_name = "yetkazish sozlamalari"
        verbose_name_plural = "yetkazish sozlamalari"
        constraints = [
            models.CheckConstraint(condition=Q(id=1), name="deliverysettings_singleton"),
            models.CheckConstraint(condition=_latlng_pair("warehouse_"), name="deliverysettings_latlng_pair"),
            models.CheckConstraint(
                condition=Q(warehouse_latitude__isnull=True)
                | Q(
                    warehouse_latitude__gte=UZ_LATITUDE[0],
                    warehouse_latitude__lte=UZ_LATITUDE[1],
                    warehouse_longitude__gte=UZ_LONGITUDE[0],
                    warehouse_longitude__lte=UZ_LONGITUDE[1],
                ),
                name="deliverysettings_latlng_uzbekistan",
            ),
            models.CheckConstraint(
                condition=Q(contact_phone="") | Q(contact_phone__regex=PHONE_E164_REGEX),
                name="deliverysettings_contact_phone_e164",
            ),
            models.CheckConstraint(
                condition=Q(prep_minutes__gte=0, prep_minutes__lte=480)
                & Q(dispatch_cutoff_minutes__gte=0, dispatch_cutoff_minutes__lte=720)
                & Q(prep_deadline_minutes__gte=15, prep_deadline_minutes__lte=2880),
                name="deliverysettings_minutes_range",
            ),
            models.CheckConstraint(
                condition=Q(
                    standard_eta_min_minutes__gte=1,
                    standard_eta_max_minutes__gte=F("standard_eta_min_minutes"),
                    standard_eta_max_minutes__lte=1440,
                    express_eta_min_minutes__gte=1,
                    express_eta_max_minutes__gte=F("express_eta_min_minutes"),
                    express_eta_max_minutes__lte=1440,
                ),
                name="deliverysettings_eta_range",
            ),
            models.CheckConstraint(
                condition=~Q(standard_taxi_class="") & ~Q(express_taxi_class=""),
                name="deliverysettings_taxi_class_required",
            ),
            models.CheckConstraint(
                condition=Q(default_item_weight_g__gte=1, default_item_weight_g__lte=20_000),
                name="deliverysettings_item_weight_range",
            ),
            models.CheckConstraint(
                condition=Q(
                    markup_percent__gte=0,
                    markup_percent__lte=100,
                    estimate_vat_percent__gte=0,
                    estimate_vat_percent__lte=100,
                    max_auto_increase__gte=0,
                    max_loss_per_shipment__gte=0,
                ),
                name="deliverysettings_policy_range",
            ),
        ]

    def __str__(self) -> str:
        return gettext("Yetkazish sozlamalari")

    @classmethod
    def load(cls) -> DeliverySettings:
        """The singleton (``delivery.0007`` / ``post_migrate`` keep it; this never runs a second insert)."""
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj


class WarehouseHours(models.Model):
    """The warehouse's regular opening hours of one weekday (Asia/Tashkent); no row = closed that day."""

    weekday = models.PositiveSmallIntegerField(unique=True, verbose_name="hafta kuni")  # 0 = Monday
    opens_at = models.TimeField(verbose_name="ochiladi")
    closes_at = models.TimeField(verbose_name="yopiladi")

    class Meta:
        verbose_name = "ombor ish vaqti"
        verbose_name_plural = "ombor ish vaqti"
        ordering = ["weekday"]
        constraints = [
            models.CheckConstraint(
                condition=Q(weekday__gte=0, weekday__lte=6), name="warehousehours_weekday"
            ),
            models.CheckConstraint(condition=Q(opens_at__lt=F("closes_at")), name="warehousehours_order"),
        ]

    def __str__(self) -> str:
        return f"{self.weekday}: {self.opens_at:%H:%M}–{self.closes_at:%H:%M}"


class WarehouseClosure(models.Model):
    """One date that differs from the weekly hours: closed (both times null) or special hours."""

    date = models.DateField(unique=True, verbose_name="sana")
    opens_at = models.TimeField(null=True, blank=True, verbose_name="ochiladi")
    closes_at = models.TimeField(null=True, blank=True, verbose_name="yopiladi")
    note = models.CharField(max_length=200, blank=True, verbose_name="izoh")
    created_by = models.ForeignKey(
        "accounts.StaffUser", null=True, on_delete=models.SET_NULL, related_name="+", verbose_name="kiritgan"
    )
    created_at = models.DateTimeField(default=timestamp_default, editable=False, verbose_name="yaratilgan")

    class Meta:
        verbose_name = "ombor maxsus kuni"
        verbose_name_plural = "ombor maxsus kunlari"
        ordering = ["date"]
        constraints = [
            models.CheckConstraint(
                condition=Q(opens_at__isnull=True, closes_at__isnull=True)
                | Q(opens_at__isnull=False, closes_at__isnull=False, opens_at__lt=F("closes_at")),
                name="warehouseclosure_times",
            ),
        ]

    def __str__(self) -> str:
        if self.opens_at is None or self.closes_at is None:
            return gettext("%(date)s: yopiq") % {"date": self.date.isoformat()}
        return f"{self.date.isoformat()}: {self.opens_at:%H:%M}–{self.closes_at:%H:%M}"


# --- checkout quote -----------------------------------------------------------------------------------------


class DeliveryQuote(models.Model):
    """A delivery price shown to a customer (decision 4): checkout accepts its id, never a client price.

    The address is a snapshot (the customer may edit the saved address afterwards); ``carrier_price`` is the
    carrier's estimate in so'm, ``customer_fee`` what the order charges. Unused rows are purged after
    expiry (:func:`glowderma.delivery.services.purge_quotes`).
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    customer = models.ForeignKey(
        "accounts.Customer", on_delete=models.CASCADE, related_name="delivery_quotes", verbose_name="mijoz"
    )
    address_id = models.BigIntegerField(null=True, verbose_name="manzil ID")  # accounts.Address, no FK
    address_text = models.CharField(max_length=300, blank=True, verbose_name="manzil")
    latitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, verbose_name="kenglik")
    longitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, verbose_name="uzunlik")
    region_code = models.CharField(max_length=16, blank=True, verbose_name="viloyat kodi")
    city_code = models.CharField(max_length=16, blank=True, verbose_name="shahar kodi")
    provider = models.CharField(max_length=16, choices=_P.choices, verbose_name="yetkazib beruvchi")
    method = models.CharField(
        max_length=16, blank=True, choices=enums.DeliveryMethod.choices, verbose_name="usul"
    )
    carrier_price = models.BigIntegerField(verbose_name="yetkazib beruvchi narxi")  # so'm
    customer_fee = models.BigIntegerField(verbose_name="mijoz to‘lovi")  # so'm, the order's delivery_fee
    weight_g = models.PositiveIntegerField(verbose_name="og‘irlik (g)")
    eta_from = models.DateTimeField(null=True, verbose_name="yetkazish (dan)")
    eta_to = models.DateTimeField(null=True, verbose_name="yetkazish (gacha)")
    # the window starts at a later opening (the warehouse is closed or near closing now)
    scheduled = models.BooleanField(default=False, verbose_name="rejalashtirilgan")
    # ids and codes only (zone id, taxi class, …): never contacts
    meta = models.JSONField(default=dict, blank=True, encoder=DjangoJSONEncoder, verbose_name="ma’lumot")
    created_at = models.DateTimeField(default=timestamp_default, editable=False, verbose_name="yaratilgan")
    expires_at = models.DateTimeField(verbose_name="amal qilish muddati")
    used_at = models.DateTimeField(null=True, verbose_name="ishlatilgan")
    order = models.ForeignKey(
        "commerce.Order", null=True, on_delete=models.SET_NULL, related_name="+", verbose_name="buyurtma"
    )

    class Meta:
        verbose_name = "yetkazish narxi taklifi"
        verbose_name_plural = "yetkazish narxi takliflari"
        ordering = ["-created_at"]
        constraints = [
            models.CheckConstraint(
                condition=Q(carrier_price__gte=0, customer_fee__gte=0, weight_g__gte=1),
                name="deliveryquote_amounts",
            ),
            models.CheckConstraint(condition=_latlng_pair(""), name="deliveryquote_latlng_pair"),
            models.CheckConstraint(
                condition=Q(eta_from__isnull=True, eta_to__isnull=True)
                | Q(eta_from__isnull=False, eta_to__isnull=False, eta_from__lte=F("eta_to")),
                name="deliveryquote_eta_window",
            ),
            models.CheckConstraint(condition=Q(expires_at__gt=F("created_at")), name="deliveryquote_expiry"),
            models.CheckConstraint(
                condition=Q(provider=_P.BTS, method="")
                | (Q(provider=_P.YANDEX, latitude__isnull=False) & ~Q(method="")),
                name="deliveryquote_provider_fields",
            ),
            models.CheckConstraint(
                condition=Q(used_at__isnull=True, order__isnull=True) | Q(used_at__isnull=False),
                name="deliveryquote_order_when_used",
            ),
        ]
        indexes = [
            models.Index(fields=["customer", "created_at"], name="deliveryquote_customer_idx"),
            models.Index(
                fields=["expires_at"], condition=Q(used_at__isnull=True), name="deliveryquote_unused_idx"
            ),
        ]

    def __str__(self) -> str:
        return gettext("Narx taklifi %(id)s: %(provider)s %(method)s, %(fee)s so‘m") % {
            "id": str(self.pk)[:8],
            "provider": self.get_provider_display(),
            "method": self.get_method_display() if self.method else "",
            "fee": self.customer_fee,
        }


# --- shipment ----------------------------------------------------------------------------------------------


class ShipmentQuerySet(models.QuerySet["Shipment"]):
    def with_pending(self) -> Self:
        """Annotate the API's derived ``pending`` ("a BTS create message is queued or in flight")."""
        return self.annotate(pending=Shipment.pending_expression())

    def bts(self) -> Self:
        """BTS parcels only: every BTS path reads through this (a Yandex shipment is invisible to them)."""
        return self.filter(provider=_P.BTS)


# ``shipment_state_fields`` for BTS: exactly the pre-Yandex condition.
_BTS_STATE_FIELDS = (
    Q(
        state="not_created",
        tracking_number="",
        bts_created_at__isnull=True,
        cancelled_at__isnull=True,
    )
    | Q(
        state="failed",
        tracking_number="",
        bts_created_at__isnull=True,
        cancelled_at__isnull=True,
        attempts__gte=1,
        last_attempt_at__isnull=False,
    )
    | (
        Q(
            state="created",
            bts_created_at__isnull=False,
            cancelled_at__isnull=True,
            tariff__isnull=False,
        )
        & ~Q(tracking_number="")
    )
    | (
        Q(
            state="cancelled",
            bts_created_at__isnull=False,
            cancelled_at__isnull=False,
            tariff__isnull=False,
        )
        & ~Q(tracking_number="")
    )
)
# For Yandex the BTS columns stay empty in every state (the claim id lives on CourierClaim, never in
# ``tracking_number``, so a BTS webhook or poll can never match a Yandex row); ``created`` / ``cancelled``
# (a claim was accepted) need the current claim.
_YANDEX_STATE_FIELDS = Q(tracking_number="", bts_order_id="", bts_status="", bts_created_at__isnull=True) & (
    Q(state__in=["not_created", "failed"], cancelled_at__isnull=True)
    | Q(state="created", claim__isnull=False, cancelled_at__isnull=True)
    | Q(state="cancelled", claim__isnull=False, cancelled_at__isnull=False)
)


class Shipment(TimeStampedModel):
    """The parcel of a delivery order; ``state`` is internal (the API shows ``cancelled`` as created).

    BTS: ``state`` follows the BTS create / cancel answers. Yandex: ``state`` is derived from the claim
    history only (``not_created`` / ``failed`` before any acceptance, ``created`` once a claim was accepted,
    ``cancelled``) and ``stage`` carries the progress; ``claim`` is the current :class:`CourierClaim`.
    """

    class State(models.TextChoices):  # internal; API ShipmentState adds `not_required` and hides `cancelled`
        NOT_CREATED = "not_created", "Yaratilmagan"
        FAILED = "failed", "Yaratib bo‘lmadi"
        CREATED = "created", "Yaratilgan"
        CANCELLED = "cancelled", "Bekor qilingan"

    order = models.OneToOneField(
        "commerce.Order", on_delete=models.PROTECT, related_name="shipment", verbose_name="buyurtma"
    )
    state = models.CharField(
        max_length=16, choices=State.choices, default=State.NOT_CREATED, verbose_name="holat"
    )
    tracking_number = models.CharField(max_length=32, blank=True, verbose_name="trek raqami")
    # so'm; NULL until created; from the BTS create response, else Order.delivery_fee (D-2)
    tariff = models.BigIntegerField(null=True, verbose_name="tarif")
    # BTS's own id of the parcel
    bts_order_id = models.CharField(max_length=64, blank=True, verbose_name="BTS buyurtma ID")
    # raw BTS status code as text ("100", "200", …)
    bts_status = models.CharField(max_length=16, blank=True, verbose_name="BTS holati")
    # source of the last status
    via = models.CharField(max_length=16, blank=True, choices=enums.ShipmentVia.choices, verbose_name="manba")
    # API `updated_at` of the created state
    last_status_at = models.DateTimeField(null=True, verbose_name="oxirgi holat vaqti")
    bts_created_at = models.DateTimeField(null=True, verbose_name="BTS’da yaratilgan")
    cancelled_at = models.DateTimeField(null=True, verbose_name="bekor qilingan")
    # BTS create attempts that got an answer (failed or created)
    attempts = models.PositiveIntegerField(default=0, verbose_name="urinishlar")
    # outbox enqueues (automatic + manual retries); part of the dedupe key
    create_requests = models.PositiveIntegerField(default=0, verbose_name="yaratish so‘rovlari")
    last_attempt_at = models.DateTimeField(null=True, verbose_name="oxirgi urinish")
    last_error_code = models.CharField(max_length=32, blank=True, verbose_name="oxirgi xato kodi")
    last_error = models.TextField(blank=True, verbose_name="oxirgi xato")
    # the newest `bts.create_shipment` message; `pending` is derived from its status
    create_message = models.ForeignKey(
        "core.OutboxMessage",
        null=True,
        on_delete=models.SET_NULL,
        related_name="+",
        verbose_name="yaratish xabari (outbox)",
    )
    # --- Yandex Delivery (plan §3.1); a BTS row keeps the defaults
    provider = models.CharField(
        max_length=16,
        choices=_P.choices,
        default=_P.BTS,
        db_default=_P.BTS,
        verbose_name="yetkazib beruvchi",
    )
    method = models.CharField(
        max_length=16,
        blank=True,
        choices=enums.DeliveryMethod.choices,
        default="",
        db_default="",
        verbose_name="usul",
    )
    stage = models.CharField(
        max_length=24,
        blank=True,
        choices=enums.ShipmentStage.choices,
        default="",
        db_default="",
        verbose_name="bosqich",
    )
    quote = models.ForeignKey(
        "delivery.DeliveryQuote",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
        verbose_name="narx taklifi",
    )
    # the current courier claim (the newest one; history in ``claims``)
    claim = models.ForeignKey(
        "delivery.CourierClaim",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
        verbose_name="joriy kuryer buyurtmasi",
    )
    parcel_weight_g = models.PositiveIntegerField(
        null=True, blank=True, verbose_name="jo‘natma og‘irligi (g)"
    )
    prepared_at = models.DateTimeField(null=True, blank=True, verbose_name="tayyorlangan")
    prepared_by = models.ForeignKey(
        "accounts.StaffUser",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="+",
        verbose_name="tayyorlagan",
    )
    # the delivery window promised to the customer (recomputed from paid_at)
    promised_from = models.DateTimeField(null=True, blank=True, verbose_name="va’da qilingan (dan)")
    promised_to = models.DateTimeField(null=True, blank=True, verbose_name="va’da qilingan (gacha)")
    # so'm: Σ final_price (+ cancel fees) of every claim; difference = paid fee − total once settled
    carrier_cost_total = models.BigIntegerField(
        default=0, db_default=0, verbose_name="yetkazib beruvchi xarajati (so‘m)"
    )
    cost_difference = models.BigIntegerField(null=True, blank=True, verbose_name="farq (so‘m)")
    cancel_requested_at = models.DateTimeField(null=True, blank=True, verbose_name="bekor qilish so‘ralgan")
    returned_at = models.DateTimeField(null=True, blank=True, verbose_name="omborga qaytgan")
    return_confirmed_by = models.ForeignKey(
        "accounts.StaffUser",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="+",
        verbose_name="qaytishni tasdiqlagan",
    )

    objects = ShipmentQuerySet.as_manager()

    class Meta:
        verbose_name = "jo‘natma"
        verbose_name_plural = "jo‘natmalar"
        constraints = [
            models.CheckConstraint(
                condition=Q(tariff__isnull=True) | Q(tariff__gte=0),
                name="shipment_tariff_nonneg",
            ),
            models.CheckConstraint(
                condition=(Q(provider=_P.BTS) & _BTS_STATE_FIELDS)
                | (Q(provider=_P.YANDEX) & _YANDEX_STATE_FIELDS),
                name="shipment_state_fields",
            ),
            models.CheckConstraint(
                condition=Q(provider=_P.BTS, method="", stage="", claim__isnull=True)
                | (Q(provider=_P.YANDEX) & ~Q(method="")),
                name="shipment_provider_fields",
            ),
            models.CheckConstraint(
                condition=Q(carrier_cost_total__gte=0)
                & (Q(parcel_weight_g__isnull=True) | Q(parcel_weight_g__gte=1)),
                name="shipment_yandex_amounts",
            ),
            models.CheckConstraint(
                condition=Q(promised_from__isnull=True, promised_to__isnull=True)
                | Q(
                    promised_from__isnull=False,
                    promised_to__isnull=False,
                    promised_from__lte=F("promised_to"),
                ),
                name="shipment_promised_window",
            ),
        ]
        indexes = [
            models.Index(fields=["state"], name="shipment_state_idx"),
            models.Index(
                fields=["tracking_number"],
                condition=~Q(tracking_number=""),
                name="shipment_tracking_idx",
            ),
            models.Index(fields=["stage"], condition=Q(provider=_P.YANDEX), name="shipment_yandex_stage_idx"),
        ]

    def __str__(self) -> str:
        suffix = f" {self.tracking_number}" if self.tracking_number else ""
        text = gettext("Buyurtma №%(order)s jo‘natmasi: %(state)s") % {
            "order": self.order_id,
            "state": self.get_state_display(),
        }
        return f"{text}{suffix}"

    @staticmethod
    def pending_q(prefix: str = "") -> Q:
        """The ``pending`` condition as a ``Q`` (``prefix="shipment__"`` evaluates it from an order)."""
        return Q(**{f"{prefix}create_message__status__in": list(PENDING_MESSAGE_STATUSES)})

    @classmethod
    def pending_expression(cls, prefix: str = "") -> Case:
        """``pending`` as a boolean expression (``COALESCE(m.status IN (…), false)`` of design 02 §3.1)."""
        return Case(
            When(cls.pending_q(prefix), then=Value(True)),
            default=Value(False),
            output_field=BooleanField(),
        )

    @property
    def is_pending(self) -> bool:
        """The same flag for one loaded row (one query unless ``create_message`` is already cached)."""
        message = self.create_message
        return message is not None and message.status in PENDING_MESSAGE_STATUSES

    @property
    def is_bts(self) -> bool:
        return self.provider == _P.BTS


class CourierClaim(TimeStampedModel):
    """One Yandex claim request of a shipment (decision 2): created with our UUID ``request_id`` (Yandex's
    idempotency key of ``claims/create``), then its claim id, status and prices as Yandex reports them.

    ``ended_at`` closes it (finished at Yandex, or superseded); at most one open claim per shipment, so a
    second courier is never summoned while the first may still come. ``settled_at`` = its cost is final.
    Courier name / vehicle / tracking link are blanked 7 days after the end (plan §3.1).
    """

    shipment = models.ForeignKey(
        "delivery.Shipment", on_delete=models.PROTECT, related_name="claims", verbose_name="jo‘natma"
    )
    number = models.PositiveSmallIntegerField(verbose_name="tartib raqami")  # 1-based per shipment
    request_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False, verbose_name="so‘rov ID")
    claim_id = models.CharField(max_length=64, blank=True, verbose_name="Yandex buyurtma ID")
    status = models.CharField(max_length=40, blank=True, verbose_name="Yandex holati")  # verbatim
    version = models.PositiveIntegerField(null=True, blank=True, verbose_name="versiya")
    updated_ts = models.DateTimeField(null=True, blank=True, verbose_name="Yandex yangilagan")
    taxi_class = models.CharField(max_length=16, verbose_name="Yandex klassi")
    method = models.CharField(max_length=16, choices=enums.DeliveryMethod.choices, verbose_name="usul")
    # so'm: the fee the customer paid, the checkout estimate, the binding offer, the approved and final price
    quoted_fee = models.BigIntegerField(null=True, blank=True, verbose_name="to‘langan narx")
    estimate_price = models.BigIntegerField(null=True, blank=True, verbose_name="taxminiy narx")
    offer_price = models.BigIntegerField(null=True, blank=True, verbose_name="taklif narxi")
    offer_valid_until = models.DateTimeField(null=True, blank=True, verbose_name="taklif muddati")
    approved_price = models.BigIntegerField(null=True, blank=True, verbose_name="tasdiqlangan narx")
    final_price = models.BigIntegerField(null=True, blank=True, verbose_name="yakuniy narx")
    cancel_fee = models.BigIntegerField(null=True, blank=True, verbose_name="bekor qilish to‘lovi")
    # Yandex warning codes of the estimate (a list of strings)
    warnings = models.JSONField(
        default=list, blank=True, encoder=DjangoJSONEncoder, verbose_name="ogohlantirishlar"
    )
    error_code = models.CharField(max_length=64, blank=True, verbose_name="xato kodi")
    accept_sent_at = models.DateTimeField(null=True, blank=True, verbose_name="tasdiq yuborilgan")
    accepted_at = models.DateTimeField(null=True, blank=True, verbose_name="tasdiqlangan")
    ended_at = models.DateTimeField(null=True, blank=True, verbose_name="yakunlangan")
    settled_at = models.DateTimeField(null=True, blank=True, verbose_name="hisob-kitob yopilgan")
    courier_name = models.CharField(max_length=150, blank=True, verbose_name="kuryer")
    courier_vehicle = models.CharField(max_length=150, blank=True, verbose_name="transport")
    tracking_url = models.CharField(max_length=500, blank=True, verbose_name="kuzatish havolasi")
    # Yandex's id of point B (claims/return names it)
    destination_point_id = models.BigIntegerField(null=True, blank=True, verbose_name="B nuqta ID")
    requested_by = models.ForeignKey(
        "accounts.StaffUser",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="+",
        verbose_name="chaqirgan",
    )

    class Meta:
        verbose_name = "kuryer buyurtmasi"
        verbose_name_plural = "kuryer buyurtmalari"
        ordering = ["shipment_id", "number"]
        constraints = [
            models.UniqueConstraint(fields=["shipment", "number"], name="courierclaim_number_unique"),
            models.UniqueConstraint(
                fields=["claim_id"], condition=~Q(claim_id=""), name="courierclaim_claim_id_unique"
            ),
            models.UniqueConstraint(
                fields=["shipment"],
                condition=Q(ended_at__isnull=True),
                name="courierclaim_one_open_per_shipment",
            ),
            models.CheckConstraint(condition=Q(number__gte=1), name="courierclaim_number_positive"),
            models.CheckConstraint(
                condition=(Q(quoted_fee__isnull=True) | Q(quoted_fee__gte=0))
                & (Q(estimate_price__isnull=True) | Q(estimate_price__gte=0))
                & (Q(offer_price__isnull=True) | Q(offer_price__gte=0))
                & (Q(approved_price__isnull=True) | Q(approved_price__gte=0))
                & (Q(final_price__isnull=True) | Q(final_price__gte=0))
                & (Q(cancel_fee__isnull=True) | Q(cancel_fee__gte=0)),
                name="courierclaim_amounts_nonneg",
            ),
            models.CheckConstraint(
                condition=Q(accepted_at__isnull=True) | Q(accept_sent_at__isnull=False),
                name="courierclaim_accepted_after_sent",
            ),
            models.CheckConstraint(
                condition=Q(settled_at__isnull=True) | Q(ended_at__isnull=False),
                name="courierclaim_settled_after_end",
            ),
        ]
        indexes = [
            models.Index(
                fields=["id"],
                condition=Q(settled_at__isnull=True) & ~Q(claim_id=""),
                name="courierclaim_unsettled_idx",
            ),
        ]

    def __str__(self) -> str:
        return gettext("Jo‘natma #%(shipment)s, kuryer buyurtmasi %(number)s: %(status)s") % {
            "shipment": self.shipment_id,
            "number": self.number,
            "status": self.status or "—",
        }

    @property
    def is_open(self) -> bool:
        return self.ended_at is None


class ShipmentEvent(AppendOnlyModel):
    """One immutable carrier record of a shipment (creation answer, failure, status, cancellation)."""

    class Kind(models.TextChoices):  # internal
        CREATED = "created", "Yaratildi"
        CREATE_FAILED = "create_failed", "Yaratib bo‘lmadi"
        STATUS_RECEIVED = "status_received", "Holat keldi"
        CANCELLED = "cancelled", "Bekor qilindi"
        CANCEL_REFUSED = "cancel_refused", "Bekor qilish rad etildi"
        # Yandex Delivery
        CLAIM_REQUESTED = "claim_requested", "Kuryer so‘raldi"
        CLAIM_STATUS = "claim_status", "Yandex holati keldi"
        PRICE_REVIEW = "price_review", "Narx tekshiruvga yuborildi"
        RETURN_REQUESTED = "return_requested", "Qaytarish so‘raldi"
        RETURNED = "returned", "Omborga qaytdi"

    shipment = models.ForeignKey(
        "delivery.Shipment", on_delete=models.PROTECT, related_name="events", verbose_name="jo‘natma"
    )
    kind = models.CharField(max_length=16, choices=Kind.choices, verbose_name="turi")
    bts_status = models.CharField(max_length=16, blank=True, verbose_name="BTS holati")
    via = models.CharField(max_length=16, blank=True, choices=enums.ShipmentVia.choices, verbose_name="manba")
    # the triggering create was a manual retry
    manual = models.BooleanField(default=False, verbose_name="qo‘lda")
    error_code = models.CharField(max_length=32, blank=True, verbose_name="xato kodi")
    # BTS response / webhook body (PII-light: no customer name); for Yandex an allow-list of status, version,
    # prices and ids — never contacts
    payload = models.JSONField(default=dict, blank=True, encoder=DjangoJSONEncoder, verbose_name="ma’lumot")
    at = models.DateTimeField(default=timestamp_default, editable=False, verbose_name="vaqt")
    # the Yandex claim status verbatim ("" for BTS rows, which keep ``bts_status``)
    carrier_status = models.CharField(
        max_length=40, blank=True, default="", db_default="", verbose_name="yetkazib beruvchi holati"
    )

    class Meta:
        verbose_name = "yetkazish hodisasi"
        verbose_name_plural = "yetkazish tarixi"
        ordering = ["shipment_id", "at", "id"]
        indexes = [models.Index(fields=["shipment", "at", "id"], name="shipmentevent_shipment_at_idx")]

    def __str__(self) -> str:
        code = self.bts_status or self.carrier_status
        status = f" {code}" if code else ""
        return gettext("Jo‘natma #%(shipment)s: %(event)s · %(at)s") % {
            "shipment": self.shipment_id,
            "event": f"{self.get_kind_display()}{status}",
            "at": stamp(self.at),
        }


# --- exception queue ---------------------------------------------------------------------------------------


class DeliveryIssue(models.Model):
    """A delivery case staff must look at (plan §3.9): one open row per (order, kind), opened idempotently
    and mostly closed by the code that opened it. ``details`` holds ids and amounts only (shown to staff in
    the admin API; alerts carry ``delivery.services.issue_alert_fields`` instead, never ``details``),
    ``note`` is the staff text."""

    order = models.ForeignKey(
        "commerce.Order", on_delete=models.PROTECT, related_name="delivery_issues", verbose_name="buyurtma"
    )
    kind = models.CharField(max_length=24, choices=enums.DeliveryIssueKind.choices, verbose_name="turi")
    code = models.CharField(max_length=64, blank=True, verbose_name="kod")
    urgent = models.BooleanField(default=False, verbose_name="shoshilinch")
    details = models.JSONField(
        default=dict, blank=True, encoder=DjangoJSONEncoder, verbose_name="tafsilotlar"
    )
    note = models.CharField(max_length=1000, blank=True, verbose_name="izoh")
    state = models.CharField(
        max_length=16,
        choices=enums.DeliveryIssueState.choices,
        default=enums.DeliveryIssueState.OPEN,
        verbose_name="holat",
    )
    opened_at = models.DateTimeField(default=timestamp_default, editable=False, verbose_name="ochilgan")
    updated_at = models.DateTimeField(default=timestamp_default, verbose_name="yangilangan")
    reminded_at = models.DateTimeField(null=True, blank=True, verbose_name="eslatilgan")
    reminders = models.PositiveIntegerField(default=0, verbose_name="eslatmalar")
    resolved_at = models.DateTimeField(null=True, blank=True, verbose_name="hal qilingan")
    resolved_by = models.ForeignKey(
        "accounts.StaffUser",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="+",
        verbose_name="hal qilgan",
    )
    resolution_note = models.CharField(max_length=1000, blank=True, verbose_name="yechim izohi")
    created_by = models.ForeignKey(
        "accounts.StaffUser",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="+",
        verbose_name="ochgan",
    )

    class Meta:
        verbose_name = "yetkazish muammosi"
        verbose_name_plural = "yetkazish muammolari"
        ordering = ["-opened_at", "-id"]
        constraints = [
            models.UniqueConstraint(
                fields=["order", "kind"],
                condition=Q(state=enums.DeliveryIssueState.OPEN),
                name="deliveryissue_one_open_per_kind",
            ),
            models.CheckConstraint(
                condition=Q(
                    state=enums.DeliveryIssueState.OPEN, resolved_at__isnull=True, resolved_by__isnull=True
                )
                | Q(state=enums.DeliveryIssueState.RESOLVED, resolved_at__isnull=False),
                name="deliveryissue_resolution_fields",
            ),
        ]
        indexes = [
            models.Index(fields=["state", "urgent", "opened_at"], name="deliveryissue_queue_idx"),
        ]

    def __str__(self) -> str:
        return gettext("Buyurtma №%(order)s: %(kind)s (%(state)s)") % {
            "order": self.order_id,
            "kind": self.get_kind_display(),
            "state": self.get_state_display(),
        }


__all__ = [
    "PENDING_MESSAGE_STATUSES",
    "CourierClaim",
    "DeliveryIssue",
    "DeliveryQuote",
    "DeliverySettings",
    "Shipment",
    "ShipmentEvent",
    "WarehouseClosure",
    "WarehouseHours",
]
