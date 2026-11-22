"""factory-boy factories for the professionals models (call them as ``XFactory.create(...)``)."""

from datetime import datetime, time, timedelta

from factory.declarations import LazyAttribute, Sequence, SubFactory
from factory.django import DjangoModelFactory

from glowderma.accounts.tests.factories import StaffUserFactory
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


class LocationFactory(DjangoModelFactory[Location]):
    class Meta:
        model = Location

    name_uz = Sequence(lambda n: f"Filial {n + 1}")  # type: ignore[no-untyped-call]
    address_uz = "Toshkent, Chilonzor tumani"
    phone = "+998712000000"


class ProfessionalFactory(DjangoModelFactory[Professional]):
    class Meta:
        model = Professional

    location = SubFactory(LocationFactory)  # type: ignore[no-untyped-call]
    staff_user = SubFactory(StaffUserFactory, role=enums.StaffRole.SPECIALIST)  # type: ignore[no-untyped-call]
    name = Sequence(lambda n: f"Mutaxassis {n + 1}")  # type: ignore[no-untyped-call]
    role = enums.SpecialistRole.DERMATOLOGIST
    position = Sequence(lambda n: n)  # type: ignore[no-untyped-call]
    is_active = True  # most tests exercise a published profile


class ServiceFactory(DjangoModelFactory[Service]):
    """One service of one specialist (a new specialist unless ``professional`` is given)."""

    class Meta:
        model = Service

    professional = SubFactory(ProfessionalFactory)  # type: ignore[no-untyped-call]
    name_uz = Sequence(lambda n: f"Xizmat {n + 1}")  # type: ignore[no-untyped-call]
    short_name_uz = Sequence(lambda n: f"X{n + 1}")  # type: ignore[no-untyped-call]
    duration_min = 30
    price = 150_000
    position = Sequence(lambda n: n)  # type: ignore[no-untyped-call]


class WorkingHoursFactory(DjangoModelFactory[WorkingHours]):
    class Meta:
        model = WorkingHours

    professional = SubFactory(ProfessionalFactory)  # type: ignore[no-untyped-call]
    location = LazyAttribute(lambda o: o.professional.location)  # type: ignore[no-untyped-call]
    weekday = LazyAttribute(lambda o: o.professional.working_hours.count())  # type: ignore[no-untyped-call]
    start = time(9, 0)
    end = time(18, 0)


class WorkingHoursOverrideFactory(DjangoModelFactory[WorkingHoursOverride]):
    class Meta:
        model = WorkingHoursOverride

    professional = SubFactory(ProfessionalFactory)  # type: ignore[no-untyped-call]
    date = Sequence(lambda n: datetime(2026, 11, 2, tzinfo=TASHKENT).date() + timedelta(days=n))  # type: ignore[no-untyped-call]
    start = time(10, 0)
    end = time(16, 0)
    location = LazyAttribute(lambda o: o.professional.location if o.start is not None else None)  # type: ignore[no-untyped-call]


class BlockedPeriodFactory(DjangoModelFactory[BlockedPeriod]):
    class Meta:
        model = BlockedPeriod

    professional = SubFactory(ProfessionalFactory)  # type: ignore[no-untyped-call]
    start = datetime(2026, 10, 26, 16, 0, tzinfo=TASHKENT)
    end = datetime(2026, 10, 26, 16, 30, tzinfo=TASHKENT)
    kind = enums.BlockKind.BUSY
    label = "Band qilingan"
