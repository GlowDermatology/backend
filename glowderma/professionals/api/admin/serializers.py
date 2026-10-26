"""Admin serializers of `professionals` (contract "Mutaxassislar va xizmatlar", FND-1 / FND-7).

Write serializers validate shapes and the value rules of the contract tables and resolve references
(``location_id``, ``staff_user_id``, ``service_id``); the services own the state rules (uniqueness against
stored rows, overlaps, the deactivation guard). Read serializers render instances of the selectors'
querysets (``specialist_queryset`` prefetches ``offered_links`` and ``week_rows``).

OpenAPI components are prefixed ``Clinic`` (``ClinicSpecialist``, ``ClinicService``, ``ClinicLocation``)
because the customer API already owns ``Specialist`` and ``Location``.
"""

from __future__ import annotations

import datetime as dt
import re
from typing import Any, cast

from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from glowderma.accounts.models import StaffUser
from glowderma.core import enums, timeutils
from glowderma.core.api.exceptions import field_error
from glowderma.core.api.fields import (
    AwareDateTimeField,
    BoundedListField,
    PhoneField,
    QueryBooleanField,
    i18n_dict,
    i18n_text,
)
from glowderma.core.api.serializers import ActorSerializer
from glowderma.core.media import absolute_media_url
from glowderma.professionals.models import (
    BlockedPeriod,
    Location,
    Professional,
    ProfessionalService,
    Service,
    WorkingHours,
    WorkingHoursOverride,
)
from glowderma.professionals.services import (
    BLOCK_MAX_DAYS,
    DURATION_STEP_MIN,
    MAX_PRICE,
    OVERRIDE_HORIZON_DAYS,
)

SLOT_STEPS = (10, 15, 20, 30, 60)  # the CHECK ``professional_slot_step_allowed``
MAX_POSITION = 100_000
MAX_OFFERED_SERVICES = 50
DB_INT_MAX = 2_147_483_647
RANGE_LIMIT_DAYS = 3660  # schedule list filters: today ± ~10 years (keeps day_bounds away from date.max)
CLOCK = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")
MESSAGES = {
    "clock": "Vaqtni SS:DD ko‘rinishida kiriting.",
    "grid": "Vaqt 5 daqiqalik qadamda bo‘lsin.",
    "duration_grid": "Davomiylik 5 daqiqaga karrali bo‘lsin.",
    "end_before_start": "Tugash vaqti boshlanishidan keyin bo‘lsin.",
    "break_pair": "Tanaffusning boshlanishi va tugashini birga kiriting.",
    "break_outside": "Tanaffus ish vaqti ichida bo‘lsin.",
    "hours_pair": "Boshlanish va tugash vaqtini birga kiriting (ikkalasi bo‘sh bo‘lsa, dam olish kuni).",
    "weekday_twice": "Bu hafta kuni ikki marta berilgan.",
    "service_twice": "Bu xizmat ro‘yxatda ikki marta berilgan.",
    "service_not_found": "Xizmat topilmadi.",
    "location_not_found": "Manzil topilmadi.",
    "staff_not_found": "Xodim topilmadi.",
    "staff_taken": "Bu xodim boshqa mutaxassisga bog‘langan.",
    "slot_step": "Ruxsat etilgan qadamlar: 10, 15, 20, 30 yoki 60 daqiqa.",
    "price_max": "Narx juda katta.",
    "override_past": "O‘tgan sanani tanlab bo‘lmaydi.",
    "override_far": "Ko‘pi bilan bir yil oldinga kiritish mumkin.",
    "block_past": "Tugagan vaqtni band qilib bo‘lmaydi.",
    "block_long": "Ko‘pi bilan 366 kunlik davr.",
    "range": "Oxirgi sana boshlang‘ich sanadan oldin bo‘lmasin.",
    "range_far": "Sana bugundan ko‘pi bilan 10 yil uzoqlikda bo‘lsin.",
    "decimals": "Ko‘pi bilan 6 ta kasr raqam.",
}


def _int(**kwargs: Any) -> serializers.IntegerField:
    return serializers.IntegerField(min_value=1, max_value=DB_INT_MAX, **kwargs)


def _error(field: str, code: str, message: str | None = None) -> serializers.ValidationError:
    return serializers.ValidationError({field: [field_error(code, message)]})


class ClockField(serializers.TimeField):
    """A Tashkent wall-clock ``"HH:MM"`` on the 5-minute grid (the admin booking grid); rendered the same."""

    def __init__(self, **kwargs: Any) -> None:
        kwargs.setdefault("help_text", '"HH:MM", Tashkent, minutes a multiple of 5.')
        super().__init__(format="%H:%M", input_formats=["%H:%M"], **kwargs)

    def to_internal_value(self, value: Any) -> dt.time:
        if not isinstance(value, str) or not CLOCK.match(value):
            raise serializers.ValidationError(field_error("invalid", MESSAGES["clock"]))
        parsed = super().to_internal_value(value)
        if parsed.minute % DURATION_STEP_MIN:
            raise serializers.ValidationError(field_error("invalid", MESSAGES["grid"]))
        return parsed


def _validate_duration(value: int | None) -> int | None:
    if value is not None and value % DURATION_STEP_MIN:
        raise serializers.ValidationError(field_error("invalid", MESSAGES["duration_grid"]))
    return value


def _price_field(**kwargs: Any) -> serializers.IntegerField:
    return serializers.IntegerField(
        min_value=0, max_value=MAX_PRICE, error_messages={"max_value": MESSAGES["price_max"]}, **kwargs
    )


# --- shared i18n write shapes (Uzbek required on create where the model requires it) ---------------------


class SpecialistTextSerializer(serializers.Serializer[dict[str, str]]):
    """``qualifications`` / ``bio``: ``{uz, ru, en}``, each ≤ 2000, every language optional (missing on
    create → ``""``; a PATCH changes only the languages sent)."""

    uz = serializers.CharField(max_length=2000, allow_blank=True, required=False, default="")
    ru = serializers.CharField(max_length=2000, allow_blank=True, required=False, default="")
    en = serializers.CharField(max_length=2000, allow_blank=True, required=False, default="")


class ServiceNameSerializer(serializers.Serializer[dict[str, str]]):
    """``name``: ``uz`` trimmed 2..120 required on create; ``ru`` / ``en`` ≤ 120 optional."""

    uz = serializers.CharField(min_length=2, max_length=120)
    ru = serializers.CharField(max_length=120, allow_blank=True, required=False, default="")
    en = serializers.CharField(max_length=120, allow_blank=True, required=False, default="")


class ServiceShortNameSerializer(serializers.Serializer[dict[str, str]]):
    """``short_name``: ``uz`` 1..40 (defaults to the first 40 characters of ``name.uz`` on create); ``ru`` /
    ``en`` ≤ 40 optional."""

    uz = serializers.CharField(max_length=40)
    ru = serializers.CharField(max_length=40, allow_blank=True, required=False, default="")
    en = serializers.CharField(max_length=40, allow_blank=True, required=False, default="")


class ServiceDescriptionSerializer(serializers.Serializer[dict[str, str]]):
    """``description``: ``{uz, ru, en}``, each ≤ 2000, optional."""

    uz = serializers.CharField(max_length=2000, allow_blank=True, required=False, default="")
    ru = serializers.CharField(max_length=2000, allow_blank=True, required=False, default="")
    en = serializers.CharField(max_length=2000, allow_blank=True, required=False, default="")


class LocationNameSerializer(serializers.Serializer[dict[str, str]]):
    """``name``: ``uz`` trimmed 2..120 (required by the first PATCH); ``ru`` / ``en`` ≤ 120."""

    uz = serializers.CharField(min_length=2, max_length=120, required=False)
    ru = serializers.CharField(max_length=120, allow_blank=True, required=False)
    en = serializers.CharField(max_length=120, allow_blank=True, required=False)


class LocationAddressSerializer(serializers.Serializer[dict[str, str]]):
    """``address``: ``{uz, ru, en}``, each ≤ 300, optional."""

    uz = serializers.CharField(max_length=300, allow_blank=True, required=False)
    ru = serializers.CharField(max_length=300, allow_blank=True, required=False)
    en = serializers.CharField(max_length=300, allow_blank=True, required=False)


# --- specialists --------------------------------------------------------------------------------------------


class OfferedServiceSerializer(serializers.Serializer[ProfessionalService]):
    """One service the specialist offers: the price and duration a booking snapshots (``is_active`` = the
    link; ``service_is_active`` = the service itself; bookable only when both are true)."""

    service_id = serializers.IntegerField()
    name = serializers.CharField(source="service.name_uz")
    short_name = serializers.CharField(source="service.short_name_uz")
    category = serializers.ChoiceField(choices=enums.ServiceCategory.values, source="service.category")
    price = serializers.IntegerField()
    duration_min = serializers.IntegerField()
    is_active = serializers.BooleanField()
    service_is_active = serializers.BooleanField(source="service.is_active")


class WorkingDaySerializer(serializers.Serializer[WorkingHours]):
    """One working weekday (0 = Monday … 6 = Sunday) with its optional recurring break."""

    weekday = serializers.IntegerField()
    start = serializers.TimeField(format="%H:%M")
    end = serializers.TimeField(format="%H:%M")
    break_start = serializers.TimeField(format="%H:%M", allow_null=True)
    break_end = serializers.TimeField(format="%H:%M", allow_null=True)


class ClinicSpecialistSerializer(serializers.Serializer[Professional]):
    """The admin ``ClinicSpecialist``; instances come from ``selectors.specialist_queryset()``.

    ``upcoming_appointments`` (``requested`` / ``confirmed`` appointments starting now or later) comes from
    the view's context (``upcoming``), counted once per page.
    """

    id = serializers.IntegerField()
    name = serializers.CharField()
    role = serializers.ChoiceField(choices=enums.SpecialistRole.values)
    qualifications = serializers.SerializerMethodField()
    bio = serializers.SerializerMethodField()
    experience_years = serializers.IntegerField(allow_null=True)
    photo_url = serializers.SerializerMethodField()
    location_id = serializers.IntegerField()
    staff_user = ActorSerializer(allow_null=True)
    slot_step_min = serializers.IntegerField()
    position = serializers.IntegerField()
    is_active = serializers.BooleanField()
    services = OfferedServiceSerializer(many=True, source="offered_links")
    working_hours = WorkingDaySerializer(many=True, source="week_rows")
    upcoming_appointments = serializers.SerializerMethodField()
    created_at = serializers.DateTimeField()
    updated_at = serializers.DateTimeField()

    @extend_schema_field(i18n_text(2000))
    def get_qualifications(self, obj: Professional) -> dict[str, str]:
        return i18n_dict(obj, "qualifications")

    @extend_schema_field(i18n_text(2000))
    def get_bio(self, obj: Professional) -> dict[str, str]:
        return i18n_dict(obj, "bio")

    @extend_schema_field(serializers.URLField(allow_null=True))
    def get_photo_url(self, obj: Professional) -> str | None:
        return absolute_media_url(obj.photo.name or "") if obj.photo else None

    def get_upcoming_appointments(self, obj: Professional) -> int:
        return int(self.context.get("upcoming", {}).get(obj.pk, 0))


class SpecialistListQuerySerializer(serializers.Serializer[dict[str, Any]]):
    """Query of ``GET specialists/``; ``search`` is trimmed and cut to 64 characters."""

    search = serializers.CharField(required=False, allow_blank=True, trim_whitespace=False)
    is_active = QueryBooleanField(required=False)
    role = serializers.ChoiceField(choices=enums.SpecialistRole.values, required=False)
    ordering = serializers.ChoiceField(
        choices=enums.DirectoryOrdering.values, required=False, default=enums.DirectoryOrdering.POSITION
    )


class ClinicSpecialistWriteSerializer(serializers.Serializer[dict[str, Any]]):
    """Body of ``POST specialists/`` and (``partial``, see the subclass) ``PATCH specialists/{id}/``.

    ``context["professional_id"]`` is the edited specialist (a staff account already linked to it is not a
    conflict with itself). The validated data carries ``location`` and ``staff_user`` instances.
    """

    name = serializers.CharField(min_length=2, max_length=150)
    role = serializers.ChoiceField(choices=enums.SpecialistRole.values)
    qualifications = SpecialistTextSerializer(required=False)
    bio = SpecialistTextSerializer(required=False)
    experience_years = serializers.IntegerField(min_value=0, max_value=80, allow_null=True, required=False)
    slot_step_min = serializers.IntegerField(
        required=False, help_text="The customer slot grid in minutes: 10, 15, 20, 30 (default) or 60."
    )
    location_id = _int(required=False, help_text="Default: the clinic (GET location/).")
    staff_user_id = _int(required=False, allow_null=True, help_text="An active staff account, or null.")
    position = serializers.IntegerField(min_value=0, max_value=MAX_POSITION, required=False)
    is_active = serializers.BooleanField(required=False, default=False)

    def validate_slot_step_min(self, value: int) -> int:
        if value not in SLOT_STEPS:
            raise serializers.ValidationError(field_error("invalid_choice", MESSAGES["slot_step"]))
        return value

    def validate_location_id(self, value: int) -> Location:
        location = Location.objects.filter(pk=value).first()
        if location is None:
            raise serializers.ValidationError(field_error("does_not_exist", MESSAGES["location_not_found"]))
        return location

    def validate_staff_user_id(self, value: int | None) -> StaffUser | None:
        if value is None:
            return None
        editing = self.context.get("professional_id")
        if editing is not None:
            # The link already stored on this specialist is accepted back unchanged, even once that account
            # is deactivated: a client resending the whole form must not be refused for a field it kept.
            current = (
                Professional.objects.select_related("staff_user")
                .filter(pk=editing, staff_user_id=value)
                .first()
            )
            if current is not None and current.staff_user is not None:
                return current.staff_user
        staff = StaffUser.objects.filter(pk=value, is_active=True).first()
        if staff is None:
            raise serializers.ValidationError(field_error("does_not_exist", MESSAGES["staff_not_found"]))
        taken = Professional.objects.filter(staff_user=staff)
        if editing is not None:
            taken = taken.exclude(pk=editing)
        if taken.exists():
            raise serializers.ValidationError(field_error("unique", MESSAGES["staff_taken"]))
        return staff

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        if "location_id" in attrs:
            attrs["location"] = attrs.pop("location_id")
        if "staff_user_id" in attrs:
            attrs["staff_user"] = attrs.pop("staff_user_id")
        return attrs


class ClinicSpecialistPatchSerializer(ClinicSpecialistWriteSerializer):
    """``PATCH specialists/{id}/`` (validated with ``partial=True``): every field optional; ``qualifications``
    and ``bio`` merge leaf by leaf; ``force`` confirms a deactivation despite upcoming appointments."""

    force = serializers.BooleanField(
        required=False,
        default=False,
        help_text="With is_active=false: deactivate even with requested/confirmed appointments ahead.",
    )


class OfferedServiceInputSerializer(serializers.Serializer[dict[str, Any]]):
    service_id = _int()
    price = _price_field(help_text="So'm, 0 … 100 000 000.")
    duration_min = serializers.IntegerField(
        min_value=5,
        max_value=480,
        allow_null=True,
        required=False,
        help_text="Minutes, a multiple of 5; omitted or null = the service's default duration.",
    )
    is_active = serializers.BooleanField(required=False, default=True)

    def validate_duration_min(self, value: int | None) -> int | None:
        return _validate_duration(value)


class OfferedServicesSerializer(serializers.Serializer[dict[str, Any]]):
    """Body of ``PUT specialists/{id}/services/``: the exact set (``[]`` = offers nothing)."""

    services = BoundedListField(child=OfferedServiceInputSerializer(), max_items=MAX_OFFERED_SERVICES)

    def validate_services(self, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        errors: dict[int, dict[str, list[Any]]] = {}
        seen: set[int] = set()
        for index, item in enumerate(items):
            if item["service_id"] in seen:
                errors[index] = {"service_id": [field_error("unique", MESSAGES["service_twice"])]}
            seen.add(item["service_id"])
        found = {service.pk: service for service in Service.objects.filter(pk__in=seen)}
        for index, item in enumerate(items):
            if item["service_id"] not in found and index not in errors:
                errors[index] = {"service_id": [field_error("does_not_exist", MESSAGES["service_not_found"])]}
        if errors:
            raise serializers.ValidationError(cast(Any, errors))
        return [{**item, "service": found[item["service_id"]]} for item in items]


class WorkingDayInputSerializer(serializers.Serializer[dict[str, Any]]):
    weekday = serializers.IntegerField(min_value=0, max_value=6, help_text="0 = Monday … 6 = Sunday.")
    start = ClockField()
    end = ClockField()
    break_start = ClockField(allow_null=True, required=False)
    break_end = ClockField(allow_null=True, required=False)

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        start, end = attrs["start"], attrs["end"]
        if end <= start:
            raise _error("end", "invalid", MESSAGES["end_before_start"])
        break_start, break_end = attrs.get("break_start"), attrs.get("break_end")
        if (break_start is None) != (break_end is None):
            missing = "break_start" if break_start is None else "break_end"
            raise _error(missing, "required", MESSAGES["break_pair"])
        if break_start is not None and break_end is not None:
            if break_end <= break_start:
                raise _error("break_end", "invalid", MESSAGES["end_before_start"])
            if break_start < start or break_end > end:
                raise _error("break_start", "invalid", MESSAGES["break_outside"])
        return {**attrs, "break_start": break_start, "break_end": break_end}


class WorkingWeekSerializer(serializers.Serializer[dict[str, Any]]):
    """Body of ``PUT specialists/{id}/working-hours/``: the working weekdays (a weekday left out is a day
    off every week)."""

    days = BoundedListField(child=WorkingDayInputSerializer(), max_items=7)

    def validate_days(self, days: list[dict[str, Any]]) -> list[dict[str, Any]]:
        seen: set[int] = set()
        for index, day in enumerate(days):
            if day["weekday"] in seen:
                raise serializers.ValidationError(
                    cast(Any, {index: {"weekday": [field_error("unique", MESSAGES["weekday_twice"])]}})
                )
            seen.add(day["weekday"])
        return days


class HoursOverrideSerializer(serializers.Serializer[WorkingHoursOverride]):
    """One date's own hours; ``day_off`` = no hours that day (``start`` / ``end`` null)."""

    id = serializers.IntegerField()
    date = serializers.DateField()
    start = serializers.TimeField(format="%H:%M", allow_null=True)
    end = serializers.TimeField(format="%H:%M", allow_null=True)
    day_off = serializers.SerializerMethodField()
    note = serializers.CharField(allow_blank=True)
    created_by = ActorSerializer(allow_null=True)
    created_at = serializers.DateTimeField()

    def get_day_off(self, obj: WorkingHoursOverride) -> bool:
        return obj.start is None or obj.end is None


class HoursOverrideCreateSerializer(serializers.Serializer[dict[str, Any]]):
    """Body of ``POST specialists/{id}/overrides/``: ``start`` and ``end`` both set (that date's hours) or
    both null / omitted (a day off)."""

    date = serializers.DateField(help_text="Tashkent calendar day, today … today + 365.")
    start = ClockField(allow_null=True, required=False)
    end = ClockField(allow_null=True, required=False)
    note = serializers.CharField(max_length=120, allow_blank=True, required=False)

    def validate_date(self, value: dt.date) -> dt.date:
        today = timeutils.local_date(timeutils.now())
        if value < today:
            raise serializers.ValidationError(field_error("in_past", MESSAGES["override_past"]))
        if value > today + dt.timedelta(days=OVERRIDE_HORIZON_DAYS):
            raise serializers.ValidationError(field_error("out_of_range", MESSAGES["override_far"]))
        return value

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        start, end = attrs.get("start"), attrs.get("end")
        if (start is None) != (end is None):
            raise _error("start" if start is None else "end", "required", MESSAGES["hours_pair"])
        if start is not None and end is not None and end <= start:
            raise _error("end", "invalid", MESSAGES["end_before_start"])
        return {**attrs, "start": start, "end": end, "note": attrs.get("note", "")}


class BlockedPeriodSerializer(serializers.Serializer[BlockedPeriod]):
    """An absolute busy / off range of the specialist (``label`` null = none)."""

    id = serializers.IntegerField()
    start = serializers.DateTimeField()
    end = serializers.DateTimeField()
    kind = serializers.ChoiceField(choices=enums.BlockKind.values)
    label = serializers.SerializerMethodField()  # type: ignore[assignment]  # a field, not Field.label
    created_by = ActorSerializer(allow_null=True)
    created_at = serializers.DateTimeField()

    @extend_schema_field(serializers.CharField(allow_null=True))
    def get_label(self, obj: BlockedPeriod) -> str | None:
        return obj.label or None


class BlockedPeriodCreateSerializer(serializers.Serializer[dict[str, Any]]):
    """Body of ``POST specialists/{id}/blocks/``."""

    start = AwareDateTimeField()
    end = AwareDateTimeField()
    kind = serializers.ChoiceField(choices=enums.BlockKind.values)
    label = serializers.CharField(  # type: ignore[assignment]  # a field, not Field.label
        max_length=80, allow_blank=True, allow_null=True, required=False
    )

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        start: dt.datetime = attrs["start"]
        end: dt.datetime = attrs["end"]
        for name, value in (("start", start), ("end", end)):
            # The slot engine reads blocks as Tashkent "HH:MM"; an off-grid edge would leave a bookable
            # slot overlapping the block, so both edges sit on the 5-minute grid like the weekly hours.
            local = value.astimezone(timeutils.TASHKENT)
            if local.second or local.microsecond or local.minute % DURATION_STEP_MIN:
                raise _error(name, "invalid", MESSAGES["grid"])
        if end <= start:
            raise _error("end", "invalid", MESSAGES["end_before_start"])
        if end <= timeutils.now():
            raise _error("end", "in_past", MESSAGES["block_past"])
        if end - start > dt.timedelta(days=BLOCK_MAX_DAYS):
            raise _error("end", "out_of_range", MESSAGES["block_long"])
        return {**attrs, "label": (attrs.get("label") or "").strip()}


class ScheduleRangeQuerySerializer(serializers.Serializer[dict[str, Any]]):
    """``date_from`` (default today, Tashkent) … ``date_to`` (optional, inclusive)."""

    date_from = serializers.DateField(required=False)
    date_to = serializers.DateField(required=False)

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        today = timeutils.local_date(timeutils.now())
        limit = dt.timedelta(days=RANGE_LIMIT_DAYS)
        for name in ("date_from", "date_to"):
            value = attrs.get(name)
            if value is not None and not today - limit <= value <= today + limit:
                raise _error(name, "out_of_range", MESSAGES["range_far"])
        date_from = attrs.get("date_from") or today
        date_to = attrs.get("date_to")
        if date_to is not None and date_to < date_from:
            raise _error("date_to", "invalid", MESSAGES["range"])
        return {"date_from": date_from, "date_to": date_to}


# --- services -----------------------------------------------------------------------------------------------


class ClinicServiceSerializer(serializers.Serializer[Service]):
    """The admin ``ClinicService``; instances come from ``selectors.service_queryset()``
    (``specialist_count`` = active specialists offering it through an active link)."""

    id = serializers.IntegerField()
    name = serializers.SerializerMethodField()
    short_name = serializers.SerializerMethodField()
    description = serializers.SerializerMethodField()
    category = serializers.ChoiceField(choices=enums.ServiceCategory.values)
    default_duration_min = serializers.IntegerField()
    default_price = serializers.IntegerField(allow_null=True)
    position = serializers.IntegerField()
    is_active = serializers.BooleanField()
    specialist_count = serializers.IntegerField()
    created_at = serializers.DateTimeField()
    updated_at = serializers.DateTimeField()

    @extend_schema_field(i18n_text(120))
    def get_name(self, obj: Service) -> dict[str, str]:
        return i18n_dict(obj, "name")

    @extend_schema_field(i18n_text(40))
    def get_short_name(self, obj: Service) -> dict[str, str]:
        return i18n_dict(obj, "short_name")

    @extend_schema_field(i18n_text(2000))
    def get_description(self, obj: Service) -> dict[str, str]:
        return i18n_dict(obj, "description")


class ServiceListQuerySerializer(serializers.Serializer[dict[str, Any]]):
    """Query of ``GET services/``; ``search`` is trimmed and cut to 64 characters."""

    search = serializers.CharField(required=False, allow_blank=True, trim_whitespace=False)
    is_active = QueryBooleanField(required=False)
    category = serializers.ChoiceField(choices=enums.ServiceCategory.values, required=False)
    ordering = serializers.ChoiceField(
        choices=enums.DirectoryOrdering.values, required=False, default=enums.DirectoryOrdering.POSITION
    )


class ClinicServiceWriteSerializer(serializers.Serializer[dict[str, Any]]):
    """Body of ``POST services/`` and (``partial``) ``PATCH services/{id}/`` (texts merge leaf by leaf)."""

    name = ServiceNameSerializer()
    short_name = ServiceShortNameSerializer(required=False)
    description = ServiceDescriptionSerializer(required=False)
    category = serializers.ChoiceField(choices=enums.ServiceCategory.values)
    default_duration_min = serializers.IntegerField(
        min_value=5, max_value=480, help_text="Minutes, a multiple of 5 (5 … 480)."
    )
    default_price = _price_field(
        allow_null=True, required=False, help_text="Suggested price when offering it; null = none."
    )
    position = serializers.IntegerField(min_value=0, max_value=MAX_POSITION, required=False)
    is_active = serializers.BooleanField(required=False, default=True)

    def validate_default_duration_min(self, value: int) -> int:
        return cast(int, _validate_duration(value))

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        if self.partial:
            return attrs
        short = dict(attrs.get("short_name") or {})
        if not short.get("uz"):
            short["uz"] = attrs["name"]["uz"][:40].strip()
        attrs["short_name"] = {"ru": "", "en": "", **short}
        return attrs


# --- the location -------------------------------------------------------------------------------------------


class ClinicLocationSerializer(serializers.Serializer[Location]):
    """The clinic (decision P4: one location), shown to customers with every specialist."""

    id = serializers.IntegerField()
    name = serializers.SerializerMethodField()
    address = serializers.SerializerMethodField()
    phone = serializers.SerializerMethodField()
    latitude = serializers.SerializerMethodField()
    longitude = serializers.SerializerMethodField()
    updated_at = serializers.DateTimeField()

    @extend_schema_field(i18n_text(120))
    def get_name(self, obj: Location) -> dict[str, str]:
        return i18n_dict(obj, "name")

    @extend_schema_field(i18n_text(300))
    def get_address(self, obj: Location) -> dict[str, str]:
        return i18n_dict(obj, "address")

    @extend_schema_field(serializers.CharField(allow_null=True))
    def get_phone(self, obj: Location) -> str | None:
        return obj.phone or None

    @extend_schema_field(serializers.FloatField(allow_null=True))
    def get_latitude(self, obj: Location) -> float | None:
        return None if obj.latitude is None else round(float(obj.latitude), 6)

    @extend_schema_field(serializers.FloatField(allow_null=True))
    def get_longitude(self, obj: Location) -> float | None:
        return None if obj.longitude is None else round(float(obj.longitude), 6)


def _coordinate(limit: int) -> serializers.DecimalField:
    return serializers.DecimalField(
        max_digits=9,
        decimal_places=6,
        min_value=-limit,
        max_value=limit,
        allow_null=True,
        required=False,
        coerce_to_string=False,
        error_messages={"max_decimal_places": MESSAGES["decimals"]},
    )


class ClinicLocationPatchSerializer(serializers.Serializer[dict[str, Any]]):
    """Body of ``PATCH location/`` (validated with ``partial=True``; texts merge leaf by leaf). The first
    PATCH creates the clinic and then needs ``name.uz``; ``latitude`` and ``longitude`` are a pair."""

    name = LocationNameSerializer(required=False)
    address = LocationAddressSerializer(required=False)
    phone = PhoneField(allow_null=True, required=False)
    latitude = _coordinate(90)
    longitude = _coordinate(180)
