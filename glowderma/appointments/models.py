"""Models of `appointments` — docs/plans/m1-models/03-… §2 (slice B2).

``Appointment`` snapshots price and duration at booking; pending requests hold the slot (P1), enforced by a
``btree_gist`` exclusion constraint over ``tstzrange(start, "end", '[)')`` per professional for the open
statuses. ``AppointmentEvent`` is the append-only trail (trigger in ``0002_appointmentevent_append_only``).
"""

from django.contrib.postgres.constraints import ExclusionConstraint
from django.contrib.postgres.fields import DateTimeRangeField, RangeBoundary, RangeOperators
from django.core.serializers.json import DjangoJSONEncoder
from django.db import models
from django.db.models import F, Q

from glowderma.core import enums
from glowderma.core.labels import stamp
from glowderma.core.models import AppendOnlyModel, TimeStampedModel, timestamp_default


class TsTzRange(models.Func):
    """``TSTZRANGE(start, "end", '[)')`` — as in the Django docs' ``ExclusionConstraint`` example."""

    function = "TSTZRANGE"
    output_field = DateTimeRangeField()


OPEN_STATUSES = (enums.AppointmentStatus.REQUESTED, enums.AppointmentStatus.CONFIRMED)


class Appointment(TimeStampedModel):
    """A booked visit of one customer with one professional for one service in one time slot."""

    class CancelledBy(models.TextChoices):  # internal, M2
        CUSTOMER = "customer", "Mijoz"
        ADMIN = "admin", "Admin"

    professional = models.ForeignKey(
        "professionals.Professional",
        on_delete=models.PROTECT,
        related_name="appointments",
        verbose_name="mutaxassis",
    )
    service = models.ForeignKey(
        "professionals.Service", on_delete=models.PROTECT, related_name="appointments", verbose_name="xizmat"
    )
    customer = models.ForeignKey(
        "accounts.Customer", on_delete=models.PROTECT, related_name="appointments", verbose_name="mijoz"
    )
    start = models.DateTimeField(verbose_name="boshlanishi")
    end = models.DateTimeField(verbose_name="tugashi")
    # snapshot of ProfessionalService.duration_min
    duration_min = models.PositiveSmallIntegerField(verbose_name="davomiyligi (daq.)")
    price = models.BigIntegerField(verbose_name="narx")  # snapshot of ProfessionalService.price
    # requested|confirmed|rejected|attended|cancelled (T3)
    status = models.CharField(
        max_length=16,
        choices=enums.AppointmentStatus.choices,
        default=enums.AppointmentStatus.REQUESTED,
        verbose_name="holat",
    )
    # app|admin
    requested_via = models.CharField(
        max_length=8, choices=enums.RequestedVia.choices, verbose_name="so‘rov manbasi"
    )
    requested_at = models.DateTimeField(default=timestamp_default, verbose_name="so‘ralgan")
    # set iff requested_via = admin
    created_by = models.ForeignKey(
        "accounts.StaffUser",
        null=True,
        on_delete=models.PROTECT,
        related_name="created_appointments",
        verbose_name="yaratgan",
    )
    # M2 booking comment
    customer_note = models.CharField(max_length=300, blank=True, verbose_name="mijoz izohi")
    # admin-only remark (reschedule reason, …)
    staff_note = models.CharField(max_length=300, blank=True, verbose_name="xodim izohi")
    confirmed_at = models.DateTimeField(null=True, verbose_name="tasdiqlangan")
    rejected_at = models.DateTimeField(null=True, verbose_name="rad etilgan")
    attended_at = models.DateTimeField(null=True, verbose_name="kelgan")
    cancelled_at = models.DateTimeField(null=True, verbose_name="bekor qilingan")
    cancelled_by = models.CharField(max_length=8, blank=True, choices=CancelledBy.choices)
    reschedule_count = models.PositiveSmallIntegerField(default=0, verbose_name="ko‘chirishlar soni")

    class Meta:
        verbose_name = "qabul"
        verbose_name_plural = "qabullar"
        ordering = ["start", "id"]
        constraints = [
            models.CheckConstraint(condition=Q(end__gt=F("start")), name="appointment_end_after_start"),
            models.CheckConstraint(
                condition=Q(duration_min__gte=5, duration_min__lte=480), name="appointment_duration_5_480"
            ),
            models.CheckConstraint(condition=Q(price__gte=0), name="appointment_price_non_negative"),
            models.CheckConstraint(
                condition=Q(requested_via="admin", created_by__isnull=False)
                | Q(requested_via="app", created_by__isnull=True),
                name="appointment_created_by_via",
            ),
            models.CheckConstraint(
                condition=(
                    Q(
                        status="requested",
                        confirmed_at__isnull=True,
                        rejected_at__isnull=True,
                        attended_at__isnull=True,
                        cancelled_at__isnull=True,
                        cancelled_by="",
                    )
                    | Q(
                        status="confirmed",
                        confirmed_at__isnull=False,
                        rejected_at__isnull=True,
                        attended_at__isnull=True,
                        cancelled_at__isnull=True,
                        cancelled_by="",
                    )
                    | Q(
                        status="rejected",
                        rejected_at__isnull=False,
                        confirmed_at__isnull=True,
                        attended_at__isnull=True,
                        cancelled_at__isnull=True,
                        cancelled_by="",
                    )
                    | Q(
                        status="attended",
                        confirmed_at__isnull=False,
                        attended_at__isnull=False,
                        rejected_at__isnull=True,
                        cancelled_at__isnull=True,
                        cancelled_by="",
                    )
                    | Q(
                        status="cancelled",
                        cancelled_at__isnull=False,
                        rejected_at__isnull=True,
                        attended_at__isnull=True,
                    )
                    & ~Q(cancelled_by="")
                ),
                name="appointment_status_fields",
            ),
            ExclusionConstraint(
                name="appointment_no_overlap",
                expressions=[
                    (TsTzRange("start", "end", RangeBoundary()), RangeOperators.OVERLAPS),  # '[)'
                    ("professional", RangeOperators.EQUAL),
                ],
                condition=Q(status__in=["requested", "confirmed"]),
            ),
        ]
        indexes = [
            models.Index(fields=["professional", "start"], name="appointment_prof_start_idx"),
            models.Index(fields=["start"], name="appointment_start_idx"),
            models.Index(fields=["start"], condition=Q(status="requested"), name="appointment_req_start_idx"),
            models.Index(
                fields=["customer", "start"],
                condition=Q(status__in=["requested", "confirmed"]),
                name="appointment_customer_open_idx",
            ),
        ]

    def __str__(self) -> str:
        return (
            f"Qabul #{self.pk} · {self.get_status_display()}: mutaxassis #{self.professional_id} "
            f"{stamp(self.start)} ({self.duration_min} daqiqa)"
        )


class AppointmentEvent(AppendOnlyModel):
    """Immutable trail of an appointment (who did what, when, from where), mirroring ``OrderEvent``."""

    class Type(models.TextChoices):  # internal
        REQUESTED = "requested", "So‘raldi"
        CONFIRMED = "confirmed", "Tasdiqlandi"
        REJECTED = "rejected", "Rad etildi"
        ATTENDED = "attended", "Keldi"
        CANCELLED = "cancelled", "Bekor qilindi"
        RESCHEDULED = "rescheduled", "Vaqti ko‘chirildi"

    class Source(models.TextChoices):  # internal
        SYSTEM = "system", "Tizim"
        ADMIN = "admin", "Admin"
        CUSTOMER = "customer", "Mijoz"

    appointment = models.ForeignKey(
        "appointments.Appointment", on_delete=models.PROTECT, related_name="events", verbose_name="qabul"
    )
    type = models.CharField(max_length=16, choices=Type.choices, verbose_name="turi")
    source = models.CharField(max_length=8, choices=Source.choices, verbose_name="manba")
    actor = models.ForeignKey(
        "accounts.StaffUser",
        null=True,
        on_delete=models.PROTECT,
        related_name="appointment_events",
        verbose_name="bajaruvchi",
    )
    data = models.JSONField(default=dict, encoder=DjangoJSONEncoder, verbose_name="ma’lumotlar")
    at = models.DateTimeField(default=timestamp_default, editable=False, verbose_name="vaqt")

    class Meta:
        verbose_name = "qabul hodisasi"
        verbose_name_plural = "qabul tarixi"
        ordering = ["-at", "-id"]
        indexes = [models.Index(fields=["appointment", "at", "id"], name="apptevent_appointment_at_idx")]
        constraints = [
            models.CheckConstraint(
                condition=Q(source="admin", actor__isnull=False)
                | (~Q(source="admin") & Q(actor__isnull=True)),
                name="apptevent_actor_by_source",
            ),
            models.CheckConstraint(
                condition=Q(type__in=["confirmed", "rejected", "attended", "rescheduled"], source="admin")
                | Q(type__in=["requested", "cancelled"]),
                name="apptevent_source_by_type",
            ),
        ]

    def __str__(self) -> str:
        return (
            f"Qabul #{self.appointment_id}: {self.get_type_display()} ({self.get_source_display()}) · "
            f"{stamp(self.at)}"
        )
