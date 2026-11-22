"""Contract "Mutaxassislar va xizmatlar" — every service belongs to one specialist: staff
``POST specialists/{id}/services/``, ``PATCH`` / ``DELETE specialists/{id}/services/{service_id}/`` (the
``ClinicSpecialist`` body back, audited, idempotent); the global ``services/`` catalogue is gone."""

import uuid
from datetime import datetime, time, timedelta
from typing import Any

import pytest
from rest_framework.test import APIClient

from glowderma.accounts.models import StaffUser
from glowderma.appointments.models import Appointment
from glowderma.appointments.tests.factories import AppointmentFactory
from glowderma.core import timeutils
from glowderma.core.models import AuditEntry
from glowderma.core.timeutils import TASHKENT
from glowderma.professionals import services
from glowderma.professionals.models import Service
from glowderma.professionals.tests.conftest import ANCHOR, errors
from glowderma.professionals.tests.factories import ProfessionalFactory, ServiceFactory

pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures("clock")]

MONDAY = ANCHOR.date() + timedelta(days=2)
BODY: dict[str, Any] = {"name": {"uz": "Konsultatsiya"}, "price": 150_000, "duration_min": 30}


def base(professional_id: int) -> str:
    return f"/api/v1/admin/specialists/{professional_id}/services/"


def test_create_defaults_audit_and_the_specialist_body(staff_client: APIClient, staff: StaffUser) -> None:
    professional = ProfessionalFactory.create(role="cosmetologist")
    ServiceFactory.create(professional=professional, position=3)
    ServiceFactory.create(position=9)  # another specialist's positions do not count
    body: dict[str, Any] = {
        "name": {"uz": "Lazer epilyatsiya — to‘liq kurs va konsultatsiya bilan birga", "ru": "Лазер"},
        "price": 320_000,
        "duration_min": 45,
    }
    response = staff_client.post(base(professional.pk), body)
    assert response.status_code == 201, response.json()
    service = Service.objects.get(name_uz__startswith="Lazer")
    data = response.json()
    assert data["id"] == professional.pk and "publish_blockers" in data  # the ClinicSpecialist body
    row = next(item for item in data["services"] if item["id"] == service.pk)
    assert row == {
        "id": service.pk,
        "specialist_id": professional.pk,
        "name": {"uz": body["name"]["uz"], "ru": "Лазер", "en": ""},
        "short_name": {"uz": body["name"]["uz"][:40], "ru": "", "en": ""},  # name.uz cut to 40
        "description": {"uz": "", "ru": "", "en": ""},
        "category": "cosmetology",  # from the specialist's role
        "price": 320_000,
        "duration_min": 45,
        "position": 4,
        "is_active": True,
        "created_at": timeutils.isoformat(ANCHOR),
        "updated_at": timeutils.isoformat(ANCHOR),
    }
    [entry] = AuditEntry.objects.filter(action="service.created")
    assert (entry.entity_type, entry.entity_id, entry.actor_id) == ("service", str(service.pk), staff.pk)
    assert entry.after is not None
    assert (entry.after["professional_id"], entry.after["price"], entry.after["duration_min"]) == (
        professional.pk,
        320_000,
        45,
    )
    full = staff_client.post(
        base(professional.pk),
        {
            "name": {"uz": "Kimyoviy pilling"},
            "short_name": {"uz": "Pilling", "en": "Peel"},
            "description": {"ru": "Описание"},
            "category": "dermatology",
            "price": 0,
            "duration_min": 60,
            "position": 0,
            "is_active": False,
        },
    )
    assert full.status_code == 201, full.json()
    created = Service.objects.get(professional=professional, name_uz="Kimyoviy pilling")
    assert (created.short_name_en, created.description_ru, created.category) == (
        "Peel",
        "Описание",
        "dermatology",
    )
    assert (created.price, created.position, created.is_active) == (0, 0, False)


@pytest.mark.parametrize(
    ("change", "expected"),
    [
        ({"name": {"uz": "A"}}, ("name.uz", "min_length", "Kamida 2 ta belgi kiriting.")),
        ({"name": {"ru": "Без узбекского"}}, ("name.uz", "required", "Majburiy maydon.")),
        ({"name": {"uz": "x" * 121}}, ("name.uz", "max_length", "120 belgidan oshmasin.")),
        ({"short_name": {"uz": "x" * 41}}, ("short_name.uz", "max_length", "40 belgidan oshmasin.")),
        ({"category": "surgery"}, ("category", "invalid_choice", "Ruxsat etilmagan qiymat.")),
        ({"duration_min": 4}, ("duration_min", "min_value", "Qiymat 5 dan kam bo‘lmasin.")),
        ({"duration_min": 481}, ("duration_min", "max_value", "Qiymat 480 dan oshmasin.")),
        ({"duration_min": 32}, ("duration_min", "invalid", "Davomiylik 5 daqiqaga karrali bo‘lsin.")),
        ({"duration_min": None}, ("duration_min", "null", "Majburiy maydon.")),
        ({"price": -1}, ("price", "min_value", "Qiymat 0 dan kam bo‘lmasin.")),
        ({"price": 100_000_001}, ("price", "max_value", "Narx juda katta.")),
        ({"price": None}, ("price", "null", "Majburiy maydon.")),
        ({"description": {"uz": "x" * 2001}}, ("description.uz", "max_length", "2000 belgidan oshmasin.")),
    ],
)
def test_create_validation(
    staff_client: APIClient, change: dict[str, Any], expected: tuple[str, str, str]
) -> None:
    professional = ProfessionalFactory.create()
    assert errors(staff_client.post(base(professional.pk), {**BODY, **change})) == [expected]
    assert not Service.objects.exists() and not AuditEntry.objects.exists()


def test_price_and_duration_are_required_on_create(staff_client: APIClient) -> None:
    professional = ProfessionalFactory.create()
    found = errors(staff_client.post(base(professional.pk), {"name": {"uz": "Konsultatsiya"}}))
    assert sorted(str(field) for field, _, _ in found) == ["duration_min", "price"]


def test_a_repeated_name_of_one_specialist_is_unique_and_the_limit_is_50(staff_client: APIClient) -> None:
    professional = ProfessionalFactory.create()
    ServiceFactory.create(professional=professional, name_uz="Konsultatsiya")
    ServiceFactory.create(name_uz="Konsultatsiya")  # another specialist's name is free
    assert errors(staff_client.post(base(professional.pk), BODY)) == [
        ("name", "unique", "Bu mutaxassisda shu nomdagi xizmat allaqachon bor.")
    ]
    for index in range(services.MAX_SERVICES_PER_SPECIALIST - 1):
        ServiceFactory.create(professional=professional, name_uz=f"Xizmat {index}")
    response = staff_client.post(base(professional.pk), {**BODY, "name": {"uz": "Ellik birinchi"}})
    assert response.status_code == 409 and response.json()["code"] == "conflict", response.json()
    assert Service.objects.filter(professional=professional).count() == services.MAX_SERVICES_PER_SPECIALIST


def test_create_is_idempotent_and_404_for_an_unknown_specialist(staff_client: APIClient) -> None:
    professional = ProfessionalFactory.create()
    key = str(uuid.uuid4())
    first = staff_client.post(base(professional.pk), BODY, HTTP_IDEMPOTENCY_KEY=key)
    replay = staff_client.post(base(professional.pk), BODY, HTTP_IDEMPOTENCY_KEY=key)
    assert first.status_code == replay.status_code == 201 and replay["Idempotent-Replayed"] == "true"
    assert Service.objects.count() == 1
    assert staff_client.post(base(999_999), BODY).status_code == 404


def test_patch_merges_texts_audits_once_and_404s_across_specialists(
    staff_client: APIClient, staff: StaffUser
) -> None:
    service = ServiceFactory.create(name_uz="Konsultatsiya", name_ru="Консультация", price=100_000)
    detail = f"{base(service.professional_id)}{service.pk}/"
    response = staff_client.patch(detail, {"name": {"en": "Consultation"}, "price": 120_000})
    assert response.status_code == 200, response.json()
    [row] = response.json()["services"]
    assert row["name"] == {"uz": "Konsultatsiya", "ru": "Консультация", "en": "Consultation"}
    assert row["price"] == 120_000
    [entry] = AuditEntry.objects.filter(action="service.updated")
    assert entry.before == {"professional_id": service.professional_id, "name.en": "", "price": 100_000}
    assert entry.after == {
        "professional_id": service.professional_id,
        "name.en": "Consultation",
        "price": 120_000,
    }
    assert entry.actor_id == staff.pk
    assert staff_client.patch(detail, {"price": 120_000}).status_code == 200  # no change, no audit
    assert AuditEntry.objects.filter(action="service.updated").count() == 1
    assert errors(staff_client.patch(detail, {"name": {"uz": ""}}))[0][:2] == ("name.uz", "blank")
    other = ProfessionalFactory.create()
    assert staff_client.patch(f"{base(other.pk)}{service.pk}/", {"price": 1}).status_code == 404
    assert staff_client.delete(f"{base(other.pk)}{service.pk}/").status_code == 404
    assert staff_client.patch(f"{base(service.professional_id)}999999/", {"price": 1}).status_code == 404
    renamed = ServiceFactory.create(professional=service.professional, name_uz="Qayta ko‘rik")
    clash = staff_client.patch(
        f"{base(service.professional_id)}{renamed.pk}/", {"name": {"uz": "Konsultatsiya"}}
    )
    assert errors(clash)[0][:2] == ("name", "unique")


def test_deactivation_hides_the_service_and_keeps_its_appointments(
    staff_client: APIClient, public_client: APIClient
) -> None:
    service = ServiceFactory.create()
    appointment = AppointmentFactory.create(
        professional=service.professional,
        service=service,
        start=datetime.combine(MONDAY, time(10), tzinfo=TASHKENT),
    )
    assert [s["id"] for s in public_client.get("/api/v1/services/").json()["results"]] == [service.pk]
    detail = f"{base(service.professional_id)}{service.pk}/"
    response = staff_client.patch(detail, {"is_active": False})
    assert response.status_code == 200 and response.json()["services"][0]["is_active"] is False
    assert "service" in response.json()["publish_blockers"]  # no active service left
    [entry] = AuditEntry.objects.filter(action="service.deactivated")
    assert entry.before == {"professional_id": service.professional_id, "is_active": True}
    assert entry.after == {"professional_id": service.professional_id, "is_active": False}
    # gone from the customer catalogue at once (no cache), the booked appointment untouched
    assert public_client.get("/api/v1/services/").json()["results"] == []
    specialist = public_client.get(f"/api/v1/specialists/{service.professional_id}/").json()
    assert specialist["services"] == []
    stored = Appointment.objects.get(pk=appointment.pk)
    assert (stored.status, stored.service_id, stored.price) == ("requested", service.pk, 150_000)
    assert staff_client.patch(detail, {"is_active": True}).status_code == 200
    assert AuditEntry.objects.filter(action="service.activated").count() == 1


def test_delete_only_a_service_that_was_never_booked(staff_client: APIClient, staff: StaffUser) -> None:
    booked = ServiceFactory.create()
    AppointmentFactory.create(
        professional=booked.professional,
        service=booked,
        status="cancelled",
        cancelled_at=ANCHOR - timedelta(days=31),
        cancelled_by="customer",
        start=ANCHOR - timedelta(days=30),
    )
    free = ServiceFactory.create(professional=booked.professional)
    refused = staff_client.delete(f"{base(booked.professional_id)}{booked.pk}/")
    assert refused.status_code == 409 and refused.json()["code"] == "conflict", refused.json()
    assert Service.objects.filter(pk=booked.pk).exists()
    assert not AuditEntry.objects.filter(action="service.deleted").exists()
    response = staff_client.delete(
        f"{base(free.professional_id)}{free.pk}/", HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4())
    )
    assert response.status_code == 204
    assert not Service.objects.filter(pk=free.pk).exists()
    [entry] = AuditEntry.objects.filter(action="service.deleted")
    assert (entry.entity_id, entry.actor_id) == (str(free.pk), staff.pk)
    assert entry.before is not None and entry.before["professional_id"] == free.professional_id


def test_the_global_catalogue_is_gone(staff_client: APIClient) -> None:
    service = ServiceFactory.create()
    assert staff_client.get("/api/v1/admin/services/").status_code == 404
    assert staff_client.get(f"/api/v1/admin/services/{service.pk}/").status_code == 404
    assert staff_client.put(base(service.professional_id), {"services": []}).status_code == 405
