from datetime import datetime, time, timedelta
from typing import Any
from uuid import uuid4

import pytest
from django.conf import settings
from rest_framework.test import APIClient

from glowderma.accounts import services as account_services
from glowderma.accounts import tokens
from glowderma.accounts.tests.factories import CustomerFactory, StaffUserFactory
from glowderma.appointments import selectors
from glowderma.appointments.tests.factories import AppointmentFactory
from glowderma.core import enums, timeutils
from glowderma.core.models import OutboxMessage
from glowderma.notifications import services as notifications
from glowderma.professionals.models import BlockedPeriod, WorkingHoursOverride
from glowderma.professionals.tests.factories import (
    LocationFactory,
    ProfessionalFactory,
    ServiceFactory,
    WorkingHoursFactory,
)

pytestmark = pytest.mark.django_db
ADMIN = "/api/v1/admin/"
ROOT = ADMIN + "specialist/"


def write_headers() -> dict[str, Any]:
    return {"format": "json", "HTTP_IDEMPOTENCY_KEY": str(uuid4())}


def panel_client(user: Any) -> APIClient:
    session, _ = account_services.start_staff_session(user, device_label="test")
    bearer, _ = tokens.issue_access(session)
    client = APIClient()
    client.credentials(
        HTTP_AUTHORIZATION=f"Bearer {bearer}", HTTP_X_GD_INTERNAL_SECRET=settings.INTERNAL_API_SECRET
    )
    return client


def specialist() -> tuple[Any, APIClient]:
    user = StaffUserFactory.create(role=enums.StaffRole.SPECIALIST)
    return ProfessionalFactory.create(staff_user=user, is_active=True), panel_client(user)


def visit(hour: int) -> datetime:
    today = timeutils.local_date(timeutils.now())
    monday = today + timedelta(days=7 - today.weekday())
    return datetime.combine(monday, time(hour), tzinfo=timeutils.TASHKENT)


def test_isolation_after_transfer_and_deactivation(staff_client: APIClient) -> None:
    doctor, client = specialist()
    other, _ = specialist()
    customer = CustomerFactory.create(email="private@clinic.example", email_verified_at=timeutils.now())
    own = AppointmentFactory.create(
        professional=doctor, customer=customer, start=visit(10), customer_note="Call on arrival"
    )
    foreign = AppointmentFactory.create(professional=other, start=visit(11))
    day = visit(10).date().isoformat()
    calendar = ROOT + f"calendar/?from={day}&to={day}"
    assert staff_client.get(calendar).status_code == 403
    result = client.get(calendar)
    assert result.status_code == 200, result.data
    [booking] = result.data["appointments"]
    assert booking["id"] == own.pk and booking["customer_note"] == "Call on arrival"
    assert booking["customer_phone"] == customer.phone
    assert "email" not in str(booking) and "staff_note" not in booking
    for path in ("staff/", "customers/", "orders/", "appointments/", "specialists/"):
        assert client.get(ADMIN + path).status_code == 403
    assert client.get(ADMIN + "auth/me/").status_code == 200
    assert client.get("/django-admin/").status_code in {302, 404}
    assert client.post(f"{ROOT}bookings/{foreign.pk}/confirm/", {}, **write_headers()).status_code == 404

    foreign_override = WorkingHoursOverride.objects.create(professional=other, date=visit(10).date())
    assert client.delete(f"{ROOT}overrides/{foreign_override.pk}/", **write_headers()).status_code == 404
    staff_client.patch(f"{ADMIN}specialists/{doctor.pk}/", {"staff_user_id": None}, format="json")
    doctor.refresh_from_db()
    assert doctor.staff_user_id is not None  # the owner account is not part of the profile body
    headers = write_headers()
    action_url = f"{ROOT}bookings/{own.pk}/confirm/"
    assert client.post(action_url, {}, **headers).status_code == 204

    type(own).objects.filter(pk=own.pk).update(professional=other)
    assert client.get(calendar).data["appointments"] == []
    assert client.post(action_url, {}, **headers).status_code == 404
    type(doctor).objects.filter(pk=doctor.pk).update(is_active=False)
    assert client.get(ROOT + "availability/").status_code == 200  # an unpublished profile stays editable
    type(doctor.staff_user).objects.filter(pk=doctor.staff_user_id).update(role=enums.StaffRole.ADMIN)
    assert client.get(ROOT + "availability/").status_code == 403


def test_availability_keeps_bookings_and_changes_free_slots(staff_client: APIClient) -> None:
    doctor, client = specialist()
    offer = ServiceFactory.create(professional=doctor)
    day = visit(10).date()
    WorkingHoursFactory.create(professional=doctor, weekday=day.weekday(), start=time(9), end=time(18))
    own = AppointmentFactory.create(professional=doctor, start=visit(10))
    assert visit(11) in selectors.free_slots(doctor, offer, day, location_id=doctor.location_id).slots
    response = client.post(ROOT + "overrides/", {"date": day.isoformat()}, **write_headers())
    assert response.status_code == 201, response.data
    assert response.data["date"] == day.isoformat()
    own.refresh_from_db()
    assert own.start == visit(10)
    assert [a["id"] for a in client.get(ROOT + "conflicts/").data["results"]] == [own.pk]
    assert [
        a["id"] for a in staff_client.get(f"{ADMIN}specialists/{doctor.pk}/conflicts/").data["results"]
    ] == [own.pk]
    assert not selectors.free_slots(doctor, offer, day, location_id=doctor.location_id).slots
    row = WorkingHoursOverride.objects.get(professional=doctor)
    response = client.put(
        f"{ROOT}overrides/{row.pk}/",
        {
            "date": day.isoformat(),
            "start": "09:00",
            "end": "18:00",
            "location_id": doctor.location_id,
        },
        **write_headers(),
    )
    assert response.status_code == 200, response.data
    assert client.get(ROOT + "conflicts/").data["results"] == []
    body = {"start": visit(11).isoformat(), "end": visit(12).isoformat(), "kind": "busy"}
    assert client.post(ROOT + "blocks/", body, **write_headers()).status_code == 201
    block = BlockedPeriod.objects.get(professional=doctor)
    assert (
        client.put(
            f"{ROOT}blocks/{block.pk}/", {**body, "end": visit(13).isoformat()}, **write_headers()
        ).status_code
        == 200
    )
    assert client.delete(f"{ROOT}blocks/{block.pk}/", **write_headers()).status_code == 204


def test_booking_actions_at_start_and_cross_clinic_notice() -> None:
    doctor, client = specialist()
    destination = LocationFactory.create(name_uz="Second clinic", address_en="1 Clinic Road")
    doctor.additional_locations.add(destination)
    WorkingHoursFactory.create(
        professional=doctor, weekday=0, location=destination, start=time(9), end=time(18)
    )
    own = AppointmentFactory.create(
        professional=doctor, start=visit(10), customer=CustomerFactory.create(language="en")
    )
    endpoint = f"{ROOT}bookings/{own.pk}/"
    assert client.post(endpoint + "confirm/", {}, **write_headers()).status_code == 204
    assert client.post(endpoint + "attend/", {}, **write_headers()).status_code == 409
    body = {"start": visit(11).isoformat(), "location_id": destination.pk}
    response = client.post(endpoint + "reschedule/", body, **write_headers())
    assert response.status_code == 400, response.data
    headers = write_headers()
    response = client.post(endpoint + "reschedule/", {**body, "customer_agreed": True}, **headers)
    assert response.status_code == 204, response.data
    assert (
        client.post(endpoint + "reschedule/", {**body, "customer_agreed": True}, **headers).status_code == 204
    )
    own.refresh_from_db()
    assert own.location_id == destination.pk and own.reschedule_count == 1
    assert own.events.get(type="rescheduled").data["customer_agreed"] is True
    outbox = OutboxMessage.objects.get(dedupe_key=f"push.appointment:{own.pk}:rescheduled:1")
    inbox = notifications.deliver_appointment(outbox.payload)
    assert inbox is not None and "11:00" in inbox.body and "1 Clinic Road" in inbox.body
    started = timeutils.now() - timedelta(minutes=1)
    past = AppointmentFactory.create(
        professional=doctor, start=started, status="confirmed", confirmed_at=started - timedelta(hours=1)
    )
    past_endpoint = f"{ROOT}bookings/{past.pk}/"
    assert client.post(past_endpoint + "cancel/", {}, **write_headers()).status_code == 409
    assert client.post(past_endpoint + "attend/", {}, **write_headers()).status_code == 204
