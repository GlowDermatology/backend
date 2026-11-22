"""Specialist self-service: ``specialist/profile/``, ``availability/``, the schedule writes and
``specialist/services/``. The specialist owns the whole profile (publication included, under the staff rules)
and their services; clinics and the site order stay with staff. Every route resolves the profile from the
signed-in account, never from an id."""

import uuid
from datetime import timedelta
from io import BytesIO
from typing import Any

import pytest
from django.conf import settings
from django.core.files.uploadedfile import SimpleUploadedFile
from PIL import Image
from rest_framework.test import APIClient

from glowderma.accounts import services as account_services
from glowderma.accounts import tokens
from glowderma.accounts.tests.factories import StaffUserFactory
from glowderma.appointments.tests.factories import AppointmentFactory
from glowderma.core import enums, timeutils
from glowderma.core.models import AuditEntry
from glowderma.professionals.models import BlockedPeriod, Professional, Service, WorkingHoursOverride
from glowderma.professionals.tests.factories import (
    LocationFactory,
    ProfessionalFactory,
    ServiceFactory,
    WorkingHoursFactory,
)

pytestmark = pytest.mark.django_db

ADMIN = "/api/v1/admin/"
OWN = ADMIN + "specialist/"
SERVICE: dict[str, Any] = {"name": {"uz": "Konsultatsiya"}, "price": 150_000, "duration_min": 30}


def keyed() -> dict[str, Any]:
    return {"format": "json", "HTTP_IDEMPOTENCY_KEY": str(uuid.uuid4())}


def panel(user: Any) -> APIClient:
    session, _ = account_services.start_staff_session(user, device_label="test")
    bearer, _ = tokens.issue_access(session)
    client = APIClient()
    client.credentials(
        HTTP_AUTHORIZATION=f"Bearer {bearer}", HTTP_X_GD_INTERNAL_SECRET=settings.INTERNAL_API_SECRET
    )
    return client


def specialist(**fields: Any) -> tuple[Professional, APIClient]:
    user = StaffUserFactory.create(role=enums.StaffRole.SPECIALIST)
    return ProfessionalFactory.create(staff_user=user, **fields), panel(user)


def ok(response: Any, status: int = 200) -> Any:
    assert response.status_code == status, getattr(response, "data", response.content)
    return response.data


def field_errors(response: Any) -> list[tuple[str, str]]:
    assert response.status_code == 400, response.data
    return [(error["field"], error["code"]) for error in response.data["errors"]]


def photo() -> SimpleUploadedFile:
    buffer = BytesIO()
    Image.new("RGB", (40, 40), (200, 120, 90)).save(buffer, format="PNG")
    return SimpleUploadedFile("me.png", buffer.getvalue(), content_type="image/png")


# --- profile ------------------------------------------------------------------------------------------------


def test_own_profile_lists_the_assigned_clinics_primary_first() -> None:
    primary = LocationFactory.create()
    closed = LocationFactory.create(is_active=False)
    doctor, client = specialist(location=primary)
    doctor.additional_locations.set([closed])
    LocationFactory.create()  # not assigned

    body = ok(client.get(OWN + "profile/"))
    assert body["id"] == doctor.pk and "publish_blockers" in body and "services" in body
    clinics = [(row["id"], row["is_active"]) for row in body["clinics"]]
    assert clinics == [(primary.pk, True), (closed.pk, False)]
    assert body["clinics"][0]["name"]["uz"] == primary.name_uz

    availability = ok(client.get(OWN + "availability/"))
    assert [(row["id"], row["is_active"]) for row in availability["clinics"]] == [
        (primary.pk, True),
        (closed.pk, False),
    ]
    assert set(availability["clinics"][0]) >= {"id", "name", "address", "is_active", "phone"}


def test_specialist_edits_every_profile_field_but_not_clinics_or_order() -> None:
    doctor, client = specialist(
        role=enums.SpecialistRole.DERMATOLOGIST, qualifications_uz="Oliy toifa", position=7
    )
    other_clinic = LocationFactory.create()
    body = {
        "name": "Dr. Amina",
        "role": "cosmetologist",
        "qualifications": {"ru": "Высшая категория"},
        "bio": {"uz": "Teri shifokori", "en": "Dermatologist"},
        "experience_years": 12,
        "slot_step_min": 15,
        "slug": "dr-amina",
        "location_id": other_clinic.pk,
        "location_ids": [other_clinic.pk],
        "position": 0,
    }
    edited = ok(client.patch(OWN + "profile/", body, format="json"))  # the key is optional
    assert (edited["name"], edited["role"], edited["experience_years"], edited["slot_step_min"]) == (
        "Dr. Amina",
        "cosmetologist",
        12,
        15,
    )
    assert edited["slug"] == "dr-amina"
    assert edited["qualifications"] == {"uz": "Oliy toifa", "ru": "Высшая категория", "en": ""}  # leaf merge
    assert edited["bio"]["uz"] == "Teri shifokori" and edited["bio"]["en"] == "Dermatologist"
    doctor.refresh_from_db()
    assert doctor.location_id != other_clinic.pk and doctor.position == 7  # ignored
    assert [row["id"] for row in edited["clinics"]] == [doctor.location_id]
    [entry] = AuditEntry.objects.filter(action="professional.updated", entity_id=str(doctor.pk))
    assert entry.actor_id == doctor.staff_user_id


def test_own_patch_validates_like_the_staff_patch() -> None:
    doctor, client = specialist(slug="me")
    ProfessionalFactory.create(slug="taken")
    assert ("slug", "unique") in field_errors(client.patch(OWN + "profile/", {"slug": "taken"}, **keyed()))
    invalid = client.patch(OWN + "profile/", {"slug": "Bad Slug"}, **keyed())
    assert ("slug", "invalid") in field_errors(invalid)
    assert ok(client.patch(OWN + "profile/", {"slug": "me"}, **keyed()))["slug"] == "me"  # own slug kept
    assert ("slot_step_min", "invalid_choice") in field_errors(
        client.patch(OWN + "profile/", {"slot_step_min": 7}, **keyed())
    )
    assert ("role", "invalid_choice") in field_errors(
        client.patch(OWN + "profile/", {"role": "surgeon"}, **keyed())
    )
    doctor.refresh_from_db()
    assert (doctor.slot_step_min, doctor.role) != (7, "surgeon")


def test_specialist_publishes_and_unpublishes_under_the_staff_rules() -> None:
    doctor, client = specialist(is_active=False, role="", bio_uz="")
    refused = client.patch(OWN + "profile/", {"is_active": True}, **keyed())
    assert refused.status_code == 409 and refused.data["code"] == "conflict"
    assert Professional.objects.get(pk=doctor.pk).is_active is False

    ok(client.patch(OWN + "profile/", {"role": "dermatologist", "bio": {"uz": "Teri shifokori"}}, **keyed()))
    ok(client.put(OWN + "profile/photo/", {"file": photo()}, format="multipart"))
    ok(client.post(OWN + "services/", SERVICE, **keyed()), 201)
    week = {"days": [{"weekday": 0, "start": "09:00", "end": "13:00", "location_id": doctor.location_id}]}
    assert ok(client.put(OWN + "weekly-hours/", week, **keyed()))["publish_blockers"] == []
    published = ok(client.patch(OWN + "profile/", {"is_active": True}, **keyed()))
    assert published["is_active"] is True
    assert AuditEntry.objects.filter(action="professional.activated", actor_id=doctor.staff_user_id).exists()

    AppointmentFactory.create(professional=doctor, start=timeutils.now() + timedelta(days=2))
    held = client.patch(OWN + "profile/", {"is_active": False}, **keyed())
    assert held.status_code == 409 and Professional.objects.get(pk=doctor.pk).is_active is True
    forced = ok(client.patch(OWN + "profile/", {"is_active": False, "force": True}, **keyed()))
    assert forced["is_active"] is False
    [entry] = AuditEntry.objects.filter(action="professional.deactivated", entity_id=str(doctor.pk))
    assert entry.after is not None and entry.after["forced"] is True


# --- schedule -----------------------------------------------------------------------------------------------


def test_schedule_writes_return_bodies_and_take_an_optional_key() -> None:
    doctor, client = specialist()
    week = {"days": [{"weekday": 1, "start": "10:00", "end": "14:00", "location_id": doctor.location_id}]}
    profile = ok(client.put(OWN + "weekly-hours/", week, format="json"))
    assert profile["id"] == doctor.pk and "clinics" in profile
    assert [(row["weekday"], row["start"]) for row in profile["working_hours"]] == [(1, "10:00")]

    day = (timeutils.local_date(timeutils.now()) + timedelta(days=10)).isoformat()
    override = ok(client.post(OWN + "overrides/", {"date": day}, format="json"), 201)
    row = WorkingHoursOverride.objects.get(professional=doctor)
    assert (override["id"], override["date"], override["start"]) == (row.pk, day, None)

    start = timeutils.now().replace(minute=0, second=0, microsecond=0) + timedelta(days=3)
    block_body = {"start": start.isoformat(), "end": (start + timedelta(hours=1)).isoformat(), "kind": "busy"}
    block = ok(client.post(OWN + "blocks/", block_body, **keyed()), 201)
    assert block["id"] == BlockedPeriod.objects.get(professional=doctor).pk and block["kind"] == "busy"

    assert client.delete(f"{OWN}overrides/{override['id']}/").status_code == 204
    assert client.delete(f"{OWN}blocks/{block['id']}/", **keyed()).status_code == 204
    assert not WorkingHoursOverride.objects.filter(professional=doctor).exists()
    assert not BlockedPeriod.objects.filter(professional=doctor).exists()


def test_a_keyed_schedule_write_replays() -> None:
    doctor, client = specialist()
    day = (timeutils.local_date(timeutils.now()) + timedelta(days=10)).isoformat()
    headers = keyed()
    first = ok(client.post(OWN + "overrides/", {"date": day}, **headers), 201)
    again = client.post(OWN + "overrides/", {"date": day}, **headers)
    assert again.status_code == 201 and again.data == first
    assert WorkingHoursOverride.objects.filter(professional=doctor).count() == 1


# --- services -----------------------------------------------------------------------------------------------


def test_specialist_manages_their_own_services() -> None:
    doctor, client = specialist()
    created = ok(client.post(OWN + "services/", SERVICE, **keyed()), 201)
    service = Service.objects.get(professional=doctor)
    assert created["id"] == doctor.pk and "clinics" in created
    [row] = created["services"]
    assert (row["id"], row["specialist_id"], row["price"], row["duration_min"]) == (
        service.pk,
        doctor.pk,
        150_000,
        30,
    )
    assert AuditEntry.objects.filter(action="service.created", actor_id=doctor.staff_user_id).exists()
    assert ("name", "unique") in field_errors(client.post(OWN + "services/", SERVICE, **keyed()))

    change = {"price": 180_000, "is_active": False}
    edited = ok(client.patch(f"{OWN}services/{service.pk}/", change, format="json"))
    assert (edited["services"][0]["price"], edited["services"][0]["is_active"]) == (180_000, False)

    assert client.delete(f"{OWN}services/{service.pk}/").status_code == 204
    assert not Service.objects.filter(pk=service.pk).exists()


def test_a_booked_service_can_only_be_deactivated() -> None:
    doctor, client = specialist()
    service = ServiceFactory.create(professional=doctor)
    WorkingHoursFactory.create(professional=doctor, weekday=0)
    AppointmentFactory.create(professional=doctor, service=service, start=timeutils.now() + timedelta(days=2))
    refused = client.delete(f"{OWN}services/{service.pk}/", **keyed())
    assert refused.status_code == 409 and refused.data["code"] == "conflict"
    assert Service.objects.filter(pk=service.pk).exists()
    ok(client.patch(f"{OWN}services/{service.pk}/", {"is_active": False}, **keyed()))
    service.refresh_from_db()
    assert service.is_active is False


def test_another_specialists_service_is_not_found() -> None:
    _, client = specialist()
    foreign = ServiceFactory.create(price=99_000)
    assert client.patch(f"{OWN}services/{foreign.pk}/", {"price": 1}, **keyed()).status_code == 404
    assert client.delete(f"{OWN}services/{foreign.pk}/", **keyed()).status_code == 404
    foreign.refresh_from_db()
    assert foreign.price == 99_000


def test_staff_cannot_use_the_specialist_routes(staff_client: APIClient) -> None:
    assert staff_client.get(OWN + "profile/").status_code == 403
    assert staff_client.post(OWN + "services/", SERVICE, format="json").status_code == 403
