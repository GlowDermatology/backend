"""factory-boy factories for the professionals models (call them as ``XFactory.create(...)``)."""

from datetime import datetime, time, timedelta

from factory.declarations import LazyAttribute, Sequence, SubFactory
from factory.django import DjangoModelFactory

from glowderma.core import enums
from glowderma.core.timeutils import TASHKENT
from glowderma.professionals.models import (
    BlockedPeriod,
    Location,
    Professional,
    ProfessionalService,
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
    name = Sequence(lambda n: f"Mutaxassis {n + 1}")  # type: ignore[no-untyped-call]
    role = enums.SpecialistRole.DERMATOLOGIST
    position = Sequence(lambda n: n)  # type: ignore[no-untyped-call]


class ServiceFactory(DjangoModelFactory[Service]):
    class Meta:
        model = Service

    name_uz = Sequence(lambda n: f"Xizmat {n + 1}")  # type: ignore[no-untyped-call]
    short_name_uz = Sequence(lambda n: f"X{n + 1}")  # type: ignore[no-untyped-call]
    default_duration_min = 30
    default_price = 150_000
    position = Sequence(lambda n: n)  # type: ignore[no-untyped-call]


class ProfessionalServiceFactory(DjangoModelFactory[ProfessionalService]):
    class Meta:
        model = ProfessionalService

    professional = SubFactory(ProfessionalFactory)  # type: ignore[no-untyped-call]
    service = SubFactory(ServiceFactory)  # type: ignore[no-untyped-call]
    price = 150_000
    duration_min = LazyAttribute(lambda o: o.service.default_duration_min)  # type: ignore[no-untyped-call]


class WorkingHoursFactory(DjangoModelFactory[WorkingHours]):
    class Meta:
        model = WorkingHours

    professional = SubFactory(ProfessionalFactory)  # type: ignore[no-untyped-call]
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


class BlockedPeriodFactory(DjangoModelFactory[BlockedPeriod]):
    class Meta:
        model = BlockedPeriod

    professional = SubFactory(ProfessionalFactory)  # type: ignore[no-untyped-call]
    start = datetime(2026, 10, 26, 16, 0, tzinfo=TASHKENT)
    end = datetime(2026, 10, 26, 16, 30, tzinfo=TASHKENT)
    kind = enums.BlockKind.BUSY
    label = "Band qilingan"
