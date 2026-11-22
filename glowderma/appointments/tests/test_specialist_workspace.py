from datetime import datetime, time, timedelta
from typing import Any
from uuid import uuid4

import pytest
from django.conf import settings
from rest_framework.test import APIClient

from glowderma.accounts import services as account_services
from glowderma.accounts import tokens
from glowderma.accounts.tests.factories import CustomerFactory, StaffUserFactory
from glowderma.appointments import selectors, services
from glowderma.appointments.models import Appointment
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
BOARD = ROOT + "appointments/"


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


def error_fields(response: Any) -> set[str]:
    return {error["field"] for error in response.data["errors"]}


def test_isolation_after_transfer_and_deactivation(staff_client: APIClient) -> None:
    doctor, client = specialist()
    other, _ = specialist()
    customer = CustomerFactory.create(email="private@clinic.example", email_verified_at=timeutils.now())
    own = AppointmentFactory.create(
        professional=doctor,
        customer=customer,
        start=visit(10),
        customer_note="Call on arrival",
        staff_note="Staff only",
    )
    foreign = AppointmentFactory.create(professional=other, start=visit(11))
    day_url = f"{BOARD}?date={visit(10).date().isoformat()}"
    assert staff_client.get(day_url).status_code == 403
    result = client.get(day_url)
    assert result.status_code == 200, result.data
    assert [row["id"] for row in result.data["specialists"]] == [doctor.pk]
    [booking] = result.data["appointments"]
    assert booking["id"] == own.pk and booking["customer_note"] == "Call on arrival"
    assert booking["customer"] == {"id": customer.pk, "name": customer.name, "phone": customer.phone}
    assert booking["allowed_actions"] == ["confirm", "reject", "reschedule", "cancel"]
    assert "private@clinic.example" not in str(result.data) and "Staff only" not in str(result.data)
    assert "staff_note" not in booking and "email" not in booking["customer"]
    for path in ("staff/", "customers/", "orders/", "appointments/", "specialists/"):
        assert client.get(ADMIN + path).status_code == 403
    assert client.get(ADMIN + "auth/me/").status_code == 200
    assert client.get("/django-admin/").status_code in {302, 404}
    assert client.post(f"{BOARD}{foreign.pk}/confirm/", {}, **write_headers()).status_code == 404
    assert client.post(f"{BOARD}{foreign.pk}/cancel/", {}, **write_headers()).status_code == 404
    assert client.post(f"{BOARD}999999/confirm/", {}, **write_headers()).status_code == 404

    foreign_override = WorkingHoursOverride.objects.create(professional=other, date=visit(10).date())
    assert client.delete(f"{ROOT}overrides/{foreign_override.pk}/", **write_headers()).status_code == 404
    staff_client.patch(f"{ADMIN}specialists/{doctor.pk}/", {"staff_user_id": None}, format="json")
    doctor.refresh_from_db()
    assert doctor.staff_user_id is not None  # the owner account is not part of the profile body
    headers = write_headers()
    action_url = f"{BOARD}{own.pk}/confirm/"
    confirmed = client.post(action_url, {}, **headers)
    assert confirmed.status_code == 200, confirmed.data
    assert confirmed.data["status"] == "confirmed"
    assert confirmed.data["allowed_actions"] == ["reschedule", "cancel"]
    assert "email" not in confirmed.data["customer"] and "staff_note" not in confirmed.data

    type(own).objects.filter(pk=own.pk).update(professional=other)
    assert client.get(day_url).data["appointments"] == []
    assert client.post(action_url, {}, **headers).status_code == 404
    type(doctor).objects.filter(pk=doctor.pk).update(is_active=False)
    assert client.get(ROOT + "availability/").status_code == 200  # an unpublished profile stays editable
    type(doctor.staff_user).objects.filter(pk=doctor.staff_user_id).update(role=enums.StaffRole.ADMIN)
    assert client.get(ROOT + "availability/").status_code == 403
    assert client.get(day_url).status_code == 403


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
    [conflict] = client.get(ROOT + "conflicts/").data["results"]
    assert conflict["id"] == own.pk and "email" not in conflict["customer"]
    assert conflict["allowed_actions"] == ["confirm", "reject", "reschedule", "cancel"]
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
    endpoint = f"{BOARD}{own.pk}/"
    assert client.post(endpoint + "confirm/", {}, **write_headers()).status_code == 200
    assert client.post(endpoint + "attend/", {}, **write_headers()).status_code == 409
    body = {"start": visit(11).isoformat(), "location_id": destination.pk}
    response = client.post(endpoint + "reschedule/", body, **write_headers())
    assert response.status_code == 400, response.data
    assert "customer_agreed" in error_fields(response)
    headers = write_headers()
    response = client.post(endpoint + "reschedule/", {**body, "customer_agreed": True}, **headers)
    assert response.status_code == 200, response.data
    assert response.data["location"]["id"] == destination.pk
    assert response.data["start"] == timeutils.isoformat(visit(11))
    replay = client.post(endpoint + "reschedule/", {**body, "customer_agreed": True}, **headers)
    assert replay.status_code == 200 and replay.data == response.data
    own.refresh_from_db()
    assert own.location_id == destination.pk and own.reschedule_count == 1
    assert own.events.get(type="rescheduled").data["customer_agreed"] is True
    outbox = OutboxMessage.objects.get(dedupe_key=f"push.appointment:{own.pk}:rescheduled:1")
    inbox = notifications.deliver_appointment(outbox.payload)
    assert inbox is not None and "11:00" in inbox.body and "1 Clinic Road" in inbox.body
    cancelled = client.post(endpoint + "cancel/", {"reason": "Ill"}, **write_headers())
    assert cancelled.status_code == 200, cancelled.data
    assert cancelled.data["status"] == "cancelled" and cancelled.data["allowed_actions"] == []
    assert "staff_note" not in cancelled.data and "Ill" not in str(cancelled.data)
    started = timeutils.now() - timedelta(minutes=1)
    past = AppointmentFactory.create(
        professional=doctor, start=started, status="confirmed", confirmed_at=started - timedelta(hours=1)
    )
    past_endpoint = f"{BOARD}{past.pk}/"
    assert client.post(past_endpoint + "cancel/", {}, **write_headers()).status_code == 409
    assert client.post(past_endpoint + "reschedule/", body, **write_headers()).status_code == 409
    attended = client.post(past_endpoint + "attend/", {}, **write_headers())
    assert attended.status_code == 200, attended.data
    assert attended.data["status"] == "attended" and attended.data["allowed_actions"] == []


def test_allowed_actions_follow_status_and_clock() -> None:
    now = timeutils.now()
    later, earlier = now + timedelta(hours=1), now - timedelta(minutes=1)
    cases = [
        ("requested", later, ["confirm", "reject", "reschedule", "cancel"]),
        ("confirmed", later, ["reschedule", "cancel"]),
        ("confirmed", earlier, ["attend"]),
        ("requested", earlier, []),
        ("attended", earlier, []),
        ("rejected", later, []),
        ("cancelled", later, []),
    ]
    for status, start, expected in cases:
        row = Appointment(status=status, start=start)
        assert services.specialist_allowed_actions(row, now) == expected, (status, start)


def test_range_lists_a_draft_and_counts_only_own_visits(staff_client: APIClient) -> None:
    doctor, client = specialist()
    type(doctor).objects.filter(pk=doctor.pk).update(is_active=False)
    other, _ = specialist()
    first = visit(10).date()
    month = {"from": first.isoformat(), "to": (first + timedelta(days=41)).isoformat()}
    assert staff_client.get(BOARD + "range/", month).status_code == 403
    empty = client.get(BOARD + "range/", month)
    assert empty.status_code == 200, empty.data
    assert [row["id"] for row in empty.data["specialists"]] == [doctor.pk]
    assert empty.data["specialists"][0]["is_active"] is False
    assert len(empty.data["days"]) == 42 and all(day["total"] == 0 for day in empty.data["days"])
    assert empty.data["schedules"] is None and empty.data["appointments"] is None

    WorkingHoursFactory.create(professional=doctor, weekday=0, start=time(9), end=time(18))
    own = AppointmentFactory.create(professional=doctor, start=visit(10))
    gone = AppointmentFactory.create(
        professional=doctor,
        start=visit(12),
        status="cancelled",
        cancelled_at=timeutils.now(),
        cancelled_by="customer",
    )
    AppointmentFactory.create(professional=other, start=visit(11))
    counts = client.get(BOARD + "range/", {**month, "specialist_id": other.pk}).data
    [monday] = [day for day in counts["days"] if day["date"] == first.isoformat()]
    assert monday["total"] == 2
    assert monday["counts"]["requested"] == 1 and monday["counts"]["cancelled"] == 1
    assert monday["working"] is True and monday["hours"] == {"start": "09:00", "end": "18:00"}
    assert [row["id"] for row in counts["specialists"]] == [doctor.pk]  # specialist_id is ignored

    week = {"from": first.isoformat(), "to": (first + timedelta(days=6)).isoformat(), "detail": "full"}
    full = client.get(BOARD + "range/", week)
    assert full.status_code == 200, full.data
    assert [row["id"] for row in full.data["appointments"]] == [own.pk, gone.pk]
    assert {row["specialist_id"] for row in full.data["schedules"]} == {doctor.pk}
    assert "email" not in full.data["appointments"][0]["customer"]
    assert full.data["appointments"][1]["allowed_actions"] == []

    too_long = {**month, "to": (first + timedelta(days=42)).isoformat()}
    assert client.get(BOARD + "range/", too_long).status_code == 400
    long_week = {**week, "to": (first + timedelta(days=7)).isoformat()}
    assert client.get(BOARD + "range/", long_week).status_code == 400


def test_meta_and_pending_summary_are_own_only() -> None:
    doctor, client = specialist()
    own_service = ServiceFactory.create(professional=doctor)
    ServiceFactory.create(professional=doctor, is_active=False)
    other, _ = specialist()
    ServiceFactory.create(professional=other)
    meta = client.get(BOARD + "meta/")
    assert meta.status_code == 200, meta.data
    [me] = meta.data["specialists"]
    assert me["id"] == doctor.pk and [row["id"] for row in me["services"]] == [own_service.pk]
    assert [row["id"] for row in me["locations"]] == [doctor.location_id]
    assert meta.data["slot_step_min"] == 5

    own = AppointmentFactory.create(professional=doctor, start=visit(11))
    AppointmentFactory.create(
        professional=doctor, start=visit(12), status="confirmed", confirmed_at=timeutils.now()
    )
    AppointmentFactory.create(professional=other, start=visit(9))
    summary = client.get(BOARD + "pending-summary/")
    assert summary.status_code == 200, summary.data
    assert summary.data == {"count": 1, "next_at": timeutils.isoformat(own.start)}
    day = client.get(BOARD).data  # no date: today
    assert day["date"] == timeutils.local_date(timeutils.now()).isoformat()
    assert [row["id"] for row in day["specialists"]] == [doctor.pk]

    type(doctor).objects.filter(pk=doctor.pk).update(is_active=False)
    assert client.get(BOARD + "meta/").data["specialists"] == []
    assert client.get(BOARD + "pending-summary/").data["count"] == 1
