"""The clinic on a visit is validated against the schedule and kept as part of its history."""

from datetime import datetime, time
from typing import Any

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from rest_framework.exceptions import ValidationError

from glowderma.accounts.tests.factories import CustomerFactory, StaffUserFactory
from glowderma.appointments import selectors, services
from glowderma.appointments.models import Appointment
from glowderma.appointments.tests.factories import AppointmentFactory
from glowderma.core import enums
from glowderma.core.api.exceptions import Conflict
from glowderma.core.timeutils import TASHKENT
from glowderma.professionals import services as professional_services
from glowderma.professionals.models import WorkingHours, WorkingHoursOverride
from glowderma.professionals.tests.factories import (
    LocationFactory,
    ServiceFactory,
    WorkingHoursFactory,
)

pytestmark = pytest.mark.django_db


def monday(hour: int = 10) -> datetime:
    return datetime(2026, 10, 26, hour, 0, tzinfo=TASHKENT)


def test_a_doctor_has_slots_only_at_the_scheduled_clinic(clock: Any) -> None:
    link = ServiceFactory.create()
    doctor = link.professional
    second = LocationFactory.create()
    doctor.additional_locations.add(second)
    WorkingHoursFactory.create(professional=doctor, weekday=0, location=second)

    assert not selectors.free_slots(doctor, link, monday().date(), location_id=doctor.location_id).slots
    assert monday() in selectors.free_slots(doctor, link, monday().date(), location_id=second.pk).slots
    customer = CustomerFactory.create()
    with pytest.raises(ValidationError) as wrong_clinic:
        services.request_by_customer(
            customer.pk, doctor.pk, link.pk, monday(), location_id=doctor.location_id
        )
    assert wrong_clinic.value.get_codes() == {"location_id": ["invalid"]}

    appointment = services.request_by_customer(
        customer.pk, doctor.pk, link.pk, monday(), location_id=second.pk
    )
    actor = StaffUserFactory.create()
    professional_services.set_working_week(
        doctor.pk,
        [{"weekday": 0, "start": time(9), "end": time(18), "location_id": doctor.location_id}],
        actor=actor,
    )
    professional_services.update_specialist(doctor.pk, {"location_ids": [doctor.location_id]}, actor=actor)
    assert [item.pk for item in selectors.schedule_conflicts(doctor)] == [appointment.pk]
    appointment.refresh_from_db()
    assert appointment.location_id == second.pk
    with pytest.raises(Conflict):
        professional_services.update_location(second.pk, {"is_active": False}, actor=actor)


def test_an_active_assignment_prevents_archive_even_without_a_visit() -> None:
    link = ServiceFactory.create()
    doctor = link.professional
    second = LocationFactory.create()
    doctor.additional_locations.add(second)
    actor = StaffUserFactory.create()

    with pytest.raises(Conflict):
        professional_services.update_location(second.pk, {"is_active": False}, actor=actor)

    professional_services.update_specialist(doctor.pk, {"location_ids": [doctor.location_id]}, actor=actor)
    assert not professional_services.update_location(second.pk, {"is_active": False}, actor=actor).is_active
    with pytest.raises(ValidationError) as inactive:
        professional_services.update_specialist(
            doctor.pk, {"location_ids": [doctor.location_id, second.pk]}, actor=actor
        )
    assert inactive.value.get_codes() == {"location_ids": ["does_not_exist"]}


def test_admin_reschedule_keeps_the_booking_and_updates_its_selected_clinic(clock: Any) -> None:
    appointment = AppointmentFactory.create()
    doctor = appointment.professional
    target = LocationFactory.create()
    doctor.additional_locations.add(target)
    WorkingHoursFactory.create(professional=doctor, weekday=0, location=target)
    actor = StaffUserFactory.create()

    moved = services.reschedule(appointment.pk, start=monday(11), location_id=target.pk, actor=actor)

    assert moved.location_id == target.pk
    assert moved.status == appointment.status
    assert moved.price == appointment.price
    event = moved.events.get(type="rescheduled")
    assert event.data["from_location_id"] == appointment.location_id
    assert event.data["to_location_id"] == target.pk


@pytest.mark.django_db(transaction=True)
def test_backfill_preserves_existing_visit_and_schedule_clinic() -> None:
    executor = MigrationExecutor(connection)
    latest = executor.loader.graph.leaf_nodes()
    before = [
        ("appointments", "0005_uz_field_labels"),
        ("professionals", "0007_uz_field_labels"),
    ]
    executor.migrate(before)
    old = executor.loader.project_state(before).apps
    try:
        clinic = old.get_model("professionals", "Location").objects.create(name_uz="Original clinic")
        doctor = old.get_model("professionals", "Professional").objects.create(
            location_id=clinic.pk, name="Doctor A", role=enums.SpecialistRole.DERMATOLOGIST
        )
        old_service = old.get_model("professionals", "Service").objects.create(
            name_uz="Initial visit", short_name_uz="Visit", default_duration_min=30
        )
        customer = CustomerFactory.create()
        hours = old.get_model("professionals", "WorkingHours").objects.create(
            professional_id=doctor.pk, weekday=0, start=time(9), end=time(18)
        )
        overrides = old.get_model("professionals", "WorkingHoursOverride")
        working = overrides.objects.create(
            professional_id=doctor.pk, date=monday().date(), start=time(10), end=time(16)
        )
        day_off = overrides.objects.create(professional_id=doctor.pk, date=monday().replace(day=27).date())
        visit = old.get_model("appointments", "Appointment").objects.create(
            professional_id=doctor.pk,
            service_id=old_service.pk,
            customer_id=customer.pk,
            start=monday(),
            end=monday().replace(minute=30),
            duration_min=30,
            price=100_000,
            requested_via=enums.RequestedVia.APP,
        )
    finally:
        MigrationExecutor(connection).migrate(latest)

    assert Appointment.objects.get(pk=visit.pk).location_id == clinic.pk
    assert WorkingHours.objects.get(pk=hours.pk).location_id == clinic.pk
    assert WorkingHoursOverride.objects.get(pk=working.pk).location_id == clinic.pk
    assert WorkingHoursOverride.objects.get(pk=day_off.pk).location_id is None
