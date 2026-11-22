"""``POST specialist/appointments/``: a specialist books a phone or walk-in visit in their own calendar."""

from datetime import time
from typing import Any

import pytest
from rest_framework.test import APIClient

from glowderma.accounts.models import Customer
from glowderma.accounts.tests.factories import CustomerFactory
from glowderma.appointments.models import Appointment, AppointmentEvent
from glowderma.appointments.tests.test_specialist_workspace import (
    BOARD,
    specialist,
    visit,
    write_headers,
)
from glowderma.core import enums, timeutils
from glowderma.core.models import AuditEntry
from glowderma.professionals.tests.factories import LocationFactory, ServiceFactory, WorkingHoursFactory

pytestmark = pytest.mark.django_db


def errors(response: Any) -> list[tuple[str | None, str]]:
    return [(error["field"], error["code"]) for error in response.data["errors"]]


def bookable() -> tuple[Any, Any, APIClient]:
    doctor, client = specialist()
    offer = ServiceFactory.create(professional=doctor)
    WorkingHoursFactory.create(professional=doctor, weekday=visit(10).weekday(), start=time(9), end=time(18))
    return doctor, offer, client


def test_specialist_books_a_walk_in_and_an_existing_customer() -> None:
    doctor, offer, client = bookable()
    body = {
        "service_id": offer.pk,
        "location_id": doctor.location_id,
        "phone": "+998 97 123 45 67",
        "start": visit(10).isoformat(),
    }
    assert client.post(BOARD, body, format="json").status_code == 400  # the Idempotency-Key is required
    assert errors(client.post(BOARD, body, **write_headers())) == [("name", "required")]
    assert not Customer.objects.filter(phone="+998971234567").exists()

    headers = write_headers()
    created = client.post(BOARD, {**body, "name": "Yangi Mijoz"}, **headers)
    assert created.status_code == 201, created.data
    customer = Customer.objects.get(phone="+998971234567")
    assert (customer.name, customer.language, customer.password) == ("Yangi Mijoz", "uz", "")
    assert created.data["customer"] == {"id": customer.pk, "name": "Yangi Mijoz", "phone": "+998971234567"}
    assert created.data["status"] == "confirmed" and created.data["specialist_id"] == doctor.pk
    assert created.data["allowed_actions"] == ["reschedule", "cancel"]
    assert "staff_note" not in created.data
    appointment = Appointment.objects.get(pk=created.data["id"])
    assert (appointment.requested_via, appointment.created_by_id, appointment.staff_note) == (
        enums.RequestedVia.ADMIN,
        doctor.staff_user_id,
        "",
    )
    assert (appointment.price, appointment.duration_min) == (offer.price, offer.duration_min)
    events = AppointmentEvent.objects.filter(appointment=appointment).order_by("id")
    assert [(e.type, e.source, e.actor_id) for e in events] == [
        ("requested", "admin", doctor.staff_user_id),
        ("confirmed", "admin", doctor.staff_user_id),
    ]
    [walk_in] = AuditEntry.objects.filter(action="customer.created_by_staff")
    assert (walk_in.entity_id, walk_in.actor_id) == (str(customer.pk), doctor.staff_user_id)
    assert AuditEntry.objects.filter(
        action="appointment.created", entity_id=str(appointment.pk), actor_id=doctor.staff_user_id
    ).exists()

    replay = client.post(BOARD, {**body, "name": "Yangi Mijoz"}, **headers)
    assert replay.status_code == 201 and replay.data["id"] == appointment.pk
    assert Appointment.objects.filter(professional=doctor).count() == 1

    regular = CustomerFactory.create()
    again = client.post(
        BOARD, {**body, "phone": regular.phone, "start": visit(12).isoformat()}, **write_headers()
    )
    assert again.status_code == 201, again.data
    assert again.data["customer"]["id"] == regular.pk
    assert Customer.objects.filter(phone=regular.phone).count() == 1
    deleted = CustomerFactory.create(status=enums.CustomerStatus.DELETED, deleted_at=timeutils.now())
    refused = client.post(
        BOARD,
        {**body, "phone": deleted.phone, "name": "Boshqa", "start": visit(14).isoformat()},
        **write_headers(),
    )
    assert errors(refused) == [("phone", "customer_not_found")]


def test_specialist_booking_is_limited_to_own_services_clinics_hours_and_a_published_profile() -> None:
    doctor, offer, client = bookable()
    customer = CustomerFactory.create()
    body = {
        "service_id": offer.pk,
        "location_id": doctor.location_id,
        "phone": customer.phone,
        "start": visit(10).isoformat(),
    }
    foreign = ServiceFactory.create()
    retired = ServiceFactory.create(professional=doctor, is_active=False)
    for service in (foreign, retired):
        refused = client.post(BOARD, {**body, "service_id": service.pk}, **write_headers())
        assert errors(refused) == [("service_id", "does_not_exist")]
    elsewhere = LocationFactory.create()
    assert errors(client.post(BOARD, {**body, "location_id": elsewhere.pk}, **write_headers())) == [
        ("location_id", "does_not_exist")
    ]
    closed = LocationFactory.create(is_active=False)
    doctor.additional_locations.add(closed)
    assert errors(client.post(BOARD, {**body, "location_id": closed.pk}, **write_headers())) == [
        ("location_id", "does_not_exist")
    ]
    late = client.post(BOARD, {**body, "start": visit(19).isoformat()}, **write_headers())
    assert late.status_code == 400 and errors(late)[0][0] == "start"
    assert not Appointment.objects.exists()

    type(doctor).objects.filter(pk=doctor.pk).update(is_active=False)
    draft = client.post(BOARD, body, **write_headers())
    assert draft.status_code == 409 and draft.data["code"] == "conflict", draft.data
    assert not Appointment.objects.exists()
