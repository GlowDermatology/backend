"""professionals models: every CHECK / unique constraint, on_delete behaviour and __str__ (design 01 §5)."""

from datetime import datetime, time
from decimal import Decimal
from typing import Any

import pytest
from django.db import IntegrityError, transaction
from django.db.models import ProtectedError

from glowderma.appointments.tests.factories import AppointmentFactory
from glowderma.core import enums
from glowderma.core.timeutils import TASHKENT
from glowderma.professionals.models import (
    BlockedPeriod,
    Location,
    Professional,
    Service,
    WorkingHours,
    WorkingHoursOverride,
)
from glowderma.professionals.tests.factories import (
    BlockedPeriodFactory,
    LocationFactory,
    ProfessionalFactory,
    ServiceFactory,
    WorkingHoursFactory,
    WorkingHoursOverrideFactory,
)

pytestmark = pytest.mark.django_db


def violates(constraint: str, create: Any, **kwargs: Any) -> None:
    with pytest.raises(IntegrityError, match=constraint), transaction.atomic():
        create(**kwargs)


# --- Location -----------------------------------------------------------------------------------------------


def test_location_str_defaults_and_checks() -> None:
    location = LocationFactory.create(name_uz="Glow Derma klinikasi")
    assert str(location) == "Glow Derma klinikasi"
    assert location.is_active and location.latitude is None and location.position == 0
    violates("location_name_uz_required", LocationFactory.create, name_uz="")
    violates("location_phone_e164", LocationFactory.create, phone="+99871200000")
    violates("location_phone_e164", LocationFactory.create, phone="998712000000")
    assert LocationFactory.create(phone="").phone == ""
    violates("location_latlng_pair", LocationFactory.create, latitude=Decimal("41.311081"))
    violates("location_latlng_pair", LocationFactory.create, longitude=Decimal("69.240562"))
    both = LocationFactory.create(latitude=Decimal("41.311081"), longitude=Decimal("69.240562"))
    assert both.longitude == Decimal("69.240562")


def test_location_with_professionals_is_protected() -> None:
    professional = ProfessionalFactory.create()
    location = professional.location
    assert location is not None
    with pytest.raises(ProtectedError):
        location.delete()
    professional.delete()
    location.delete()
    assert not Location.objects.exists()


# --- Professional -------------------------------------------------------------------------------------------


def test_professional_str_ordering_and_checks() -> None:
    later = ProfessionalFactory.create(name="Zafar", position=1)
    second = ProfessionalFactory.create(name="Nodira Rahimova", position=0)
    first = ProfessionalFactory.create(name="Aziz", position=0, role=enums.SpecialistRole.COSMETOLOGIST)
    assert str(second) == "Nodira Rahimova (Dermatolog)"
    assert list(Professional.objects.all()) == [first, second, later]  # position, name, id
    assert first.slot_step_min == 30 and first.photo.name == "" and first.experience_years is None
    violates("professional_name_required", ProfessionalFactory.create, name="")
    violates("professional_slot_step_allowed", ProfessionalFactory.create, slot_step_min=25)
    violates("professional_slot_step_allowed", ProfessionalFactory.create, slot_step_min=0)
    for allowed in (10, 15, 20, 30, 60):
        assert ProfessionalFactory.create(slot_step_min=allowed).slot_step_min == allowed


def test_professional_delete_cascades_services_hours_and_blocks() -> None:
    professional = ProfessionalFactory.create()
    ServiceFactory.create(professional=professional)
    WorkingHoursFactory.create(professional=professional)
    WorkingHoursOverrideFactory.create(professional=professional)
    BlockedPeriodFactory.create(professional=professional)
    other = ServiceFactory.create()
    professional.delete()
    assert list(Service.objects.all()) == [other] and not WorkingHours.objects.exists()
    assert not WorkingHoursOverride.objects.exists() and not BlockedPeriod.objects.exists()


# --- Service ------------------------------------------------------------------------------------------------


def test_service_str_and_checks() -> None:
    service = ServiceFactory.create(name_uz="Dermatolog konsultatsiyasi", short_name_uz="Konsultatsiya")
    assert str(service) == "Dermatolog konsultatsiyasi"
    assert service.is_active and service.created_at is not None and service.updated_at is not None
    violates("service_names_uz_required", ServiceFactory.create, name_uz="")
    violates("service_names_uz_required", ServiceFactory.create, short_name_uz="")
    violates("service_duration_5_480", ServiceFactory.create, duration_min=4)
    violates("service_duration_5_480", ServiceFactory.create, duration_min=481)
    violates("service_price_non_negative", ServiceFactory.create, price=-1)
    assert ServiceFactory.create(duration_min=5, price=0).price == 0
    assert ServiceFactory.create(duration_min=480).duration_min == 480


def test_a_service_name_is_unique_within_one_specialist() -> None:
    service = ServiceFactory.create(name_uz="Konsultatsiya")
    violates(
        "service_professional_name_unique",
        ServiceFactory.create,
        professional=service.professional,
        name_uz="Konsultatsiya",
    )
    other = ServiceFactory.create(name_uz="Konsultatsiya")  # another specialist may use the same name
    assert other.professional_id != service.professional_id


def test_services_order_by_specialist_then_position() -> None:
    first = ProfessionalFactory.create()
    second = ProfessionalFactory.create()
    b = ServiceFactory.create(professional=second, position=0)
    a2 = ServiceFactory.create(professional=first, position=2)
    a1 = ServiceFactory.create(professional=first, position=1)
    assert list(Service.objects.all()) == sorted([a1, a2, b], key=lambda s: (s.professional_id, s.position))
    assert list(first.services.all()) == [a1, a2]  # the reverse accessor of the owner


def test_a_booked_service_is_protected() -> None:
    appointment = AppointmentFactory.create()
    assert appointment.service.professional_id == appointment.professional_id
    with pytest.raises(ProtectedError):
        appointment.service.delete()


# --- WorkingHours -------------------------------------------------------------------------------------------


def test_working_hours_str_unique_and_checks() -> None:
    hours = WorkingHoursFactory.create(weekday=0)
    assert str(hours) == f"Mutaxassis #{hours.professional_id} · Dushanba 09:00–18:00"
    violates(
        "workinghours_professional_weekday_unique",
        WorkingHoursFactory.create,
        professional=hours.professional,
        weekday=0,
    )
    violates("workinghours_weekday_0_6", WorkingHoursFactory.create, weekday=7)
    violates("workinghours_start_before_end", WorkingHoursFactory.create, start=time(18, 0), end=time(9, 0))
    violates("workinghours_start_before_end", WorkingHoursFactory.create, start=time(9, 0), end=time(9, 0))


@pytest.mark.parametrize(
    ("break_start", "break_end"),
    [
        (time(13, 0), None),
        (None, time(14, 0)),
        (time(14, 0), time(13, 0)),
        (time(13, 0), time(13, 0)),
        (time(8, 30), time(9, 30)),
        (time(17, 30), time(18, 30)),
    ],
)
def test_working_hours_break_inside_day(break_start: time | None, break_end: time | None) -> None:
    violates(
        "workinghours_break_inside_day",
        WorkingHoursFactory.create,
        break_start=break_start,
        break_end=break_end,
    )


def test_working_hours_break_edges_allowed() -> None:
    lunch = WorkingHoursFactory.create(break_start=time(13, 0), break_end=time(14, 0))
    edge = WorkingHoursFactory.create(break_start=time(9, 0), break_end=time(18, 0))
    assert (lunch.break_start, edge.break_end) == (time(13, 0), time(18, 0))


# --- WorkingHoursOverride -----------------------------------------------------------------------------------


def test_override_str_unique_and_checks() -> None:
    override = WorkingHoursOverrideFactory.create(date=datetime(2026, 11, 2, tzinfo=TASHKENT).date())
    assert str(override) == f"Mutaxassis #{override.professional_id} · 2026-11-02: 10:00–16:00"
    off = WorkingHoursOverrideFactory.create(professional=override.professional, start=None, end=None)
    assert str(off).endswith(": dam olish") and off.created_by is None
    violates(
        "hoursoverride_professional_date_unique",
        WorkingHoursOverrideFactory.create,
        professional=override.professional,
        date=override.date,
    )
    violates("hoursoverride_both_or_none", WorkingHoursOverrideFactory.create, start=None)
    violates("hoursoverride_both_or_none", WorkingHoursOverrideFactory.create, end=None)
    violates(
        "hoursoverride_both_or_none", WorkingHoursOverrideFactory.create, start=time(16, 0), end=time(10, 0)
    )
    violates(
        "hoursoverride_both_or_none", WorkingHoursOverrideFactory.create, start=time(10, 0), end=time(10, 0)
    )


# --- BlockedPeriod ------------------------------------------------------------------------------------------


def test_blocked_period_str_ordering_and_check() -> None:
    later = BlockedPeriodFactory.create(
        start=datetime(2026, 10, 27, 9, 0, tzinfo=TASHKENT),
        end=datetime(2026, 10, 27, 10, 0, tzinfo=TASHKENT),
    )
    block = BlockedPeriodFactory.create()
    assert str(block) == f"Band 2026-10-26 16:00–16:30 (mutaxassis #{block.professional_id})"
    assert list(BlockedPeriod.objects.all()) == [block, later]
    violates("blockedperiod_end_after_start", BlockedPeriodFactory.create, end=block.start)
    violates(
        "blockedperiod_end_after_start",
        BlockedPeriodFactory.create,
        start=block.end,
        end=block.start,
    )
    off = BlockedPeriodFactory.create(kind=enums.BlockKind.OFF, label="")
    assert off.label == "" and off.created_by is None


def test_photo_upload_path_prefixes_the_professional_directory() -> None:
    from glowderma.professionals import media

    professional = ProfessionalFactory.create()
    assert (
        media.professional_photo_path(professional, "x/9f2c.webp")
        == f"professionals/{professional.pk}/9f2c.webp"
    )
