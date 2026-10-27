"""Models of `professionals` — docs/plans/m1-models/01-… §5 (slice B2).

All times in this module are Tashkent wall-clock: ``WorkingHours``/``WorkingHoursOverride`` keep naive
``TimeField``s interpreted in ``timeutils.TASHKENT``; ``BlockedPeriod`` keeps aware instants.
"""

from typing import Any

from django.db import models
from django.db.models import F, Q

from glowderma.core import enums, slugs
from glowderma.core.models import TimeStampedModel, timestamp_default
from glowderma.professionals import media

SLUG_MAX_LENGTH = 160
PROFESSIONAL_SLUG_CONSTRAINT = "professional_slug_unique"


def professional_slug_base(name: str) -> str:
    """The derived slug of a specialist (``Professional.save`` and the ``professionals.0003`` backfill)."""
    return slugs.derive_slug(name, prefix="mutaxassis", max_length=SLUG_MAX_LENGTH)


class Location(TimeStampedModel):
    """A branch/clinic address (one row today, several supported)."""

    name_uz = models.CharField(max_length=120)
    name_ru = models.CharField(max_length=120, blank=True)
    name_en = models.CharField(max_length=120, blank=True)
    address_uz = models.CharField(max_length=300, blank=True)
    address_ru = models.CharField(max_length=300, blank=True)
    address_en = models.CharField(max_length=300, blank=True)
    phone = models.CharField(max_length=13, blank=True)
    latitude = models.DecimalField(max_digits=9, decimal_places=6, null=True)
    longitude = models.DecimalField(max_digits=9, decimal_places=6, null=True)
    is_active = models.BooleanField(default=True)
    position = models.PositiveIntegerField(default=0)

    class Meta:
        constraints = [
            models.CheckConstraint(condition=~Q(name_uz=""), name="location_name_uz_required"),
            models.CheckConstraint(
                condition=Q(phone="") | Q(phone__regex=r"^\+998\d{9}$"), name="location_phone_e164"
            ),
            models.CheckConstraint(
                condition=Q(latitude__isnull=True, longitude__isnull=True)
                | Q(latitude__isnull=False, longitude__isnull=False),
                name="location_latlng_pair",
            ),
        ]
        ordering = ["position", "id"]

    def __str__(self) -> str:
        return self.name_uz


class Professional(TimeStampedModel):
    """A bookable specialist."""

    location = models.ForeignKey(
        "professionals.Location", on_delete=models.PROTECT, related_name="professionals"
    )
    name = models.CharField(max_length=150)  # shown as-is in every language
    role = models.CharField(
        max_length=16, choices=enums.SpecialistRole.choices
    )  # dermatologist | cosmetologist
    photo = models.ImageField(upload_to=media.professional_photo_path, blank=True, max_length=255)
    bio_uz = models.TextField(blank=True)
    bio_ru = models.TextField(blank=True)
    bio_en = models.TextField(blank=True)
    # Diplomas, certificates, memberships (FND-7), free text per language; "" = not filled.
    # ``db_default``: a process still running the previous release inserts rows without these columns.
    qualifications_uz = models.TextField(blank=True, default="", db_default="")
    qualifications_ru = models.TextField(blank=True, default="", db_default="")
    qualifications_en = models.TextField(blank=True, default="", db_default="")
    experience_years = models.PositiveSmallIntegerField(null=True)
    # The specialist's own staff account, if any (FND-7): informational, grants nothing by itself.
    staff_user = models.OneToOneField(
        "accounts.StaffUser", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    slot_step_min = models.PositiveSmallIntegerField(default=30)  # slot grid used by M2 /slots
    position = models.PositiveIntegerField(default=0)
    is_active = models.BooleanField(default=True)
    # B-W1 SEO (admin contract §2.17): the website's URL segment, set once from the name when the row is
    # created, unique, never only digits, edited only by staff (stable URLs).
    slug = models.CharField(max_length=SLUG_MAX_LENGTH, blank=True, default="", db_default="")

    class Meta:
        constraints = [
            # "" = a row written by the previous release during a deploy; the next save derives one
            models.UniqueConstraint(
                fields=["slug"], condition=~Q(slug=""), name=PROFESSIONAL_SLUG_CONSTRAINT
            ),
            models.CheckConstraint(
                condition=Q(slug="")
                | (Q(slug__regex=slugs.SLUG_PATTERN) & ~Q(slug__regex=slugs.DIGITS_ONLY_PATTERN)),
                name="professional_slug_format",
            ),
            models.CheckConstraint(condition=~Q(name=""), name="professional_name_required"),
            models.CheckConstraint(
                condition=Q(slot_step_min__in=[10, 15, 20, 30, 60]), name="professional_slot_step_allowed"
            ),
        ]
        indexes = [
            models.Index(fields=["is_active", "position", "name"], name="prof_professional_active_pos")
        ]
        ordering = ["position", "name", "id"]

    def __str__(self) -> str:
        return f"{self.name} ({self.role})"

    def slug_base(self) -> str:
        return professional_slug_base(self.name)

    def save(self, *args: Any, **kwargs: Any) -> None:
        """A new row without a slug gets the first free one derived from the name (B-W1)."""
        if self.slug:
            super().save(*args, **kwargs)
            return
        if kwargs.get("update_fields") is not None:
            kwargs["update_fields"] = {*kwargs["update_fields"], "slug"}  # the derived slug is written too
        slugs.save_with_unique_slug(
            self,
            lambda: super(Professional, self).save(*args, **kwargs),
            base=self.slug_base(),
            constraint=PROFESSIONAL_SLUG_CONSTRAINT,
            max_length=SLUG_MAX_LENGTH,
        )


class Service(TimeStampedModel):
    """A bookable service type with a default duration; prices live on the professional link."""

    name_uz = models.CharField(max_length=120)
    name_ru = models.CharField(max_length=120, blank=True)
    name_en = models.CharField(max_length=120, blank=True)
    short_name_uz = models.CharField(max_length=40)
    short_name_ru = models.CharField(max_length=40, blank=True)
    short_name_en = models.CharField(max_length=40, blank=True)
    description_uz = models.TextField(blank=True)
    description_ru = models.TextField(blank=True)
    description_en = models.TextField(blank=True)
    # The discipline (FND-7); the admin API requires it on create, the default keeps older writers working.
    category = models.CharField(
        max_length=16,
        choices=enums.ServiceCategory.choices,
        default=enums.ServiceCategory.DERMATOLOGY,
        db_default=enums.ServiceCategory.DERMATOLOGY,
    )
    default_duration_min = models.PositiveSmallIntegerField()
    default_price = models.BigIntegerField(null=True)  # suggested when assigning to a professional
    position = models.PositiveIntegerField(default=0)
    is_active = models.BooleanField(default=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=~Q(name_uz="") & ~Q(short_name_uz=""), name="service_names_uz_required"
            ),
            models.CheckConstraint(
                condition=Q(default_duration_min__gte=5, default_duration_min__lte=480),
                name="service_duration_5_480",
            ),
            models.CheckConstraint(
                condition=Q(default_price__isnull=True) | Q(default_price__gte=0),
                name="service_default_price_non_negative",
            ),
        ]
        ordering = ["position", "id"]

    def __str__(self) -> str:
        return self.name_uz


class ProfessionalService(models.Model):
    """Which services a professional performs, at what price and duration (snapshotted at booking)."""

    professional = models.ForeignKey(
        "professionals.Professional", on_delete=models.CASCADE, related_name="services"
    )
    service = models.ForeignKey(
        "professionals.Service", on_delete=models.PROTECT, related_name="professional_links"
    )
    price = models.BigIntegerField()
    duration_min = models.PositiveSmallIntegerField()
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(default=timestamp_default, editable=False)
    updated_at = models.DateTimeField(default=timestamp_default)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["professional", "service"], name="professionalservice_unique"),
            models.CheckConstraint(condition=Q(price__gte=0), name="professionalservice_price_non_negative"),
            models.CheckConstraint(
                condition=Q(duration_min__gte=5, duration_min__lte=480),
                name="professionalservice_duration_5_480",
            ),
        ]
        ordering = ["professional_id", "service_id"]

    def __str__(self) -> str:
        return f"{self.professional_id}/{self.service_id}: {self.price} so'm, {self.duration_min} min"


class WorkingHours(models.Model):
    """Weekly recurring hours per professional; no row for a weekday means "not working"."""

    professional = models.ForeignKey(
        "professionals.Professional", on_delete=models.CASCADE, related_name="working_hours"
    )
    weekday = models.PositiveSmallIntegerField()  # 0 = Monday … 6 = Sunday (Python date.weekday())
    start = models.TimeField()  # Tashkent wall-clock
    end = models.TimeField()
    break_start = models.TimeField(null=True)  # optional recurring break → block kind "off"
    break_end = models.TimeField(null=True)

    class Meta:
        verbose_name_plural = "working hours"
        constraints = [
            models.UniqueConstraint(
                fields=["professional", "weekday"], name="workinghours_professional_weekday_unique"
            ),
            models.CheckConstraint(
                condition=Q(weekday__gte=0, weekday__lte=6), name="workinghours_weekday_0_6"
            ),
            models.CheckConstraint(condition=Q(start__lt=F("end")), name="workinghours_start_before_end"),
            models.CheckConstraint(
                condition=Q(break_start__isnull=True, break_end__isnull=True)
                | (
                    Q(break_start__isnull=False, break_end__isnull=False)
                    & Q(break_start__lt=F("break_end"), break_start__gte=F("start"), break_end__lte=F("end"))
                ),
                name="workinghours_break_inside_day",
            ),
        ]
        ordering = ["professional_id", "weekday"]

    def __str__(self) -> str:
        return f"{self.professional_id} wd{self.weekday} {self.start:%H:%M}-{self.end:%H:%M}"


class WorkingHoursOverride(models.Model):
    """Date-specific hours for one professional; both times null = day off."""

    professional = models.ForeignKey(
        "professionals.Professional", on_delete=models.CASCADE, related_name="hours_overrides"
    )
    date = models.DateField()  # Tashkent calendar day
    start = models.TimeField(null=True)
    end = models.TimeField(null=True)
    note = models.CharField(max_length=120, blank=True)
    created_by = models.ForeignKey(
        "accounts.StaffUser", null=True, on_delete=models.SET_NULL, related_name="+"
    )
    created_at = models.DateTimeField(default=timestamp_default, editable=False)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["professional", "date"], name="hoursoverride_professional_date_unique"
            ),
            models.CheckConstraint(
                condition=Q(start__isnull=True, end__isnull=True)
                | Q(start__isnull=False, end__isnull=False, start__lt=F("end")),
                name="hoursoverride_both_or_none",
            ),
        ]
        ordering = ["date"]

    def __str__(self) -> str:
        hours = "off" if self.start is None or self.end is None else f"{self.start:%H:%M}-{self.end:%H:%M}"
        return f"{self.professional_id} {self.date}: {hours}"


class BlockedPeriod(models.Model):
    """An absolute-time block (busy or off) in a professional's calendar."""

    professional = models.ForeignKey(
        "professionals.Professional", on_delete=models.CASCADE, related_name="blocked_periods"
    )
    start = models.DateTimeField()
    end = models.DateTimeField()
    kind = models.CharField(max_length=8, choices=enums.BlockKind.choices)  # busy | off
    label = models.CharField(max_length=80, blank=True)  # "" ↔ null in the API
    created_by = models.ForeignKey(
        "accounts.StaffUser", null=True, on_delete=models.SET_NULL, related_name="+"
    )
    created_at = models.DateTimeField(default=timestamp_default, editable=False)

    class Meta:
        constraints = [
            models.CheckConstraint(condition=Q(end__gt=F("start")), name="blockedperiod_end_after_start")
        ]
        indexes = [models.Index(fields=["professional", "start", "end"], name="prof_blocked_prof_range")]
        ordering = ["start", "id"]

    def __str__(self) -> str:
        return f"{self.kind} {self.start:%Y-%m-%d %H:%M}–{self.end:%H:%M} ({self.professional_id})"
