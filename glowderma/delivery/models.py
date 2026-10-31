"""Models of `delivery` — docs/plans/m1-models/02-… §3 (slice B2).

``Shipment`` is the BTS parcel record of a delivery order (one row per order, created at checkout in state
``not_created``); ``ShipmentEvent`` is its append-only protocol trail. The API's ``pending`` flag is not a
column: it is derived from the newest ``bts.create_shipment`` outbox message the row points at
(:meth:`ShipmentQuerySet.with_pending`). Every write happens inside ``delivery.services`` (B5) under the Order
lock held by ``commerce``; the Django admin is read-only.
"""

from typing import Self

from django.core.serializers.json import DjangoJSONEncoder
from django.db import models
from django.db.models import BooleanField, Case, Q, Value, When

from glowderma.core import enums
from glowderma.core.models import AppendOnlyModel, TimeStampedModel, timestamp_default

# Outbox statuses under which a BTS create message is still going to be delivered: ``pending`` and ``failed``
# are re-claimable by the dispatcher (backoff retry), ``in_flight`` is leased; ``done``/``dead`` are final.
PENDING_MESSAGE_STATUSES: tuple[str, ...] = ("pending", "failed", "in_flight")


class ShipmentQuerySet(models.QuerySet["Shipment"]):
    def with_pending(self) -> Self:
        """Annotate the API's derived ``pending`` ("a BTS create message is queued or in flight")."""
        return self.annotate(pending=Shipment.pending_expression())


class Shipment(TimeStampedModel):
    """The BTS parcel of a delivery order; ``state`` is internal (the API shows ``cancelled`` as created)."""

    class State(models.TextChoices):  # internal; API ShipmentState adds `not_required` and hides `cancelled`
        NOT_CREATED = "not_created"
        FAILED = "failed"
        CREATED = "created"
        CANCELLED = "cancelled"

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
    create_requests = models.PositiveIntegerField(default=0)
    last_attempt_at = models.DateTimeField(null=True)
    last_error_code = models.CharField(max_length=32, blank=True)
    last_error = models.TextField(blank=True, verbose_name="oxirgi xato")
    # the newest `bts.create_shipment` message; `pending` is derived from its status
    create_message = models.ForeignKey(
        "core.OutboxMessage", null=True, on_delete=models.SET_NULL, related_name="+"
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
                condition=(
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
                ),
                name="shipment_state_fields",
            ),
        ]
        indexes = [
            models.Index(fields=["state"], name="shipment_state_idx"),
            models.Index(
                fields=["tracking_number"],
                condition=~Q(tracking_number=""),
                name="shipment_tracking_idx",
            ),
        ]

    def __str__(self) -> str:
        suffix = f" {self.tracking_number}" if self.tracking_number else ""
        return f"Shipment of order {self.order_id}: {self.state}{suffix}"

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


class ShipmentEvent(AppendOnlyModel):
    """One immutable BTS protocol record of a shipment (creation answer, failure, status, cancellation)."""

    class Kind(models.TextChoices):  # internal
        CREATED = "created"
        CREATE_FAILED = "create_failed"
        STATUS_RECEIVED = "status_received"
        CANCELLED = "cancelled"
        CANCEL_REFUSED = "cancel_refused"

    shipment = models.ForeignKey(
        "delivery.Shipment", on_delete=models.PROTECT, related_name="events", verbose_name="jo‘natma"
    )
    kind = models.CharField(max_length=16, choices=Kind.choices, verbose_name="turi")
    bts_status = models.CharField(max_length=16, blank=True, verbose_name="BTS holati")
    via = models.CharField(max_length=16, blank=True, choices=enums.ShipmentVia.choices, verbose_name="manba")
    # the triggering create was a manual retry
    manual = models.BooleanField(default=False, verbose_name="qo‘lda")
    error_code = models.CharField(max_length=32, blank=True, verbose_name="xato kodi")
    # BTS response / webhook body (PII-light: no customer name)
    payload = models.JSONField(default=dict, blank=True, encoder=DjangoJSONEncoder, verbose_name="ma’lumot")
    at = models.DateTimeField(default=timestamp_default, editable=False, verbose_name="vaqt")

    class Meta:
        verbose_name = "yetkazish hodisasi"
        verbose_name_plural = "yetkazish tarixi"
        ordering = ["shipment_id", "at", "id"]
        indexes = [models.Index(fields=["shipment", "at", "id"], name="shipmentevent_shipment_at_idx")]

    def __str__(self) -> str:
        return f"Shipment {self.shipment_id}: {self.kind} {self.bts_status} ({self.at:%Y-%m-%d %H:%M})"
