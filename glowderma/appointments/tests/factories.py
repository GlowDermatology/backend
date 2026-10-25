"""factory-boy factories for the appointments models (call them as ``XFactory.create(...)``).

Every appointment gets its own professional by default, so factory-made rows never trip the overlap
exclusion; tests that want a collision pass the same ``professional`` and ``start``.
"""

from datetime import datetime, timedelta

from factory.declarations import LazyAttribute, SubFactory
from factory.django import DjangoModelFactory

from glowderma.accounts.tests.factories import CustomerFactory
from glowderma.appointments.models import Appointment, AppointmentEvent
from glowderma.core import enums
from glowderma.core.timeutils import TASHKENT
from glowderma.professionals.tests.factories import ProfessionalFactory, ServiceFactory

DESIGN_DAY_START = datetime(2026, 10, 26, 9, 0, tzinfo=TASHKENT)


class AppointmentFactory(DjangoModelFactory[Appointment]):
    class Meta:
        model = Appointment

    professional = SubFactory(ProfessionalFactory)  # type: ignore[no-untyped-call]
    service = SubFactory(ServiceFactory)  # type: ignore[no-untyped-call]
    customer = SubFactory(CustomerFactory)  # type: ignore[no-untyped-call]
    start = DESIGN_DAY_START
    duration_min = 30
    end = LazyAttribute(lambda o: o.start + timedelta(minutes=o.duration_min))  # type: ignore[no-untyped-call]
    price = 150_000
    status = enums.AppointmentStatus.REQUESTED
    requested_via = enums.RequestedVia.APP
    requested_at = datetime(2026, 10, 24, 8, 41, tzinfo=TASHKENT)


class AppointmentEventFactory(DjangoModelFactory[AppointmentEvent]):
    class Meta:
        model = AppointmentEvent

    appointment = SubFactory(AppointmentFactory)  # type: ignore[no-untyped-call]
    type = AppointmentEvent.Type.REQUESTED
    source = AppointmentEvent.Source.CUSTOMER
    data = LazyAttribute(lambda o: {"via": "app", "price": o.appointment.price})  # type: ignore[no-untyped-call]
