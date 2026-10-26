"""Contract "Mutaxassislar va xizmatlar" — ``services/``: list (pagination, search, filters, ordering,
export, query count), create, retrieve and PATCH (deactivation instead of delete), audited and idempotent."""

import csv
import uuid
from datetime import datetime, time, timedelta
from io import StringIO
from typing import Any

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from rest_framework.test import APIClient

from glowderma.accounts.models import StaffUser
from glowderma.appointments.models import Appointment
from glowderma.appointments.tests.factories import AppointmentFactory
from glowderma.core import timeutils
from glowderma.core.models import AuditEntry
from glowderma.core.timeutils import TASHKENT
from glowderma.professionals.models import Service
from glowderma.professionals.tests.conftest import ANCHOR, errors
from glowderma.professionals.tests.factories import (
    ProfessionalFactory,
    ProfessionalServiceFactory,
    ServiceFactory,
)

pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures("clock")]

BASE = "/api/v1/admin/services/"
MONDAY = ANCHOR.date() + timedelta(days=2)


def names(client: APIClient, **params: Any) -> list[str]:
    response = client.get(BASE, params)
    assert response.status_code == 200, response.json()
    return [row["name"]["uz"] for row in response.json()["results"]]


def test_create_defaults_and_audit(staff_client: APIClient, staff: StaffUser) -> None:
    ServiceFactory.create(position=3)
    body: dict[str, Any] = {
        "name": {"uz": "Lazer epilyatsiya — to‘liq kurs va konsultatsiya bilan birga", "ru": "Лазер"},
        "category": "cosmetology",
        "default_duration_min": 45,
    }
    response = staff_client.post(BASE, body)
    assert response.status_code == 201, response.json()
    service = Service.objects.get(name_uz__startswith="Lazer")
    assert response.json() == {
        "id": service.pk,
        "name": {
            "uz": "Lazer epilyatsiya — to‘liq kurs va konsultatsiya bilan birga",
            "ru": "Лазер",
            "en": "",
        },
        "short_name": {"uz": body["name"]["uz"][:40], "ru": "", "en": ""},  # name.uz cut to 40
        "description": {"uz": "", "ru": "", "en": ""},
        "category": "cosmetology",
        "default_duration_min": 45,
        "default_price": None,
        "position": 4,
        "is_active": True,
        "specialist_count": 0,
        "created_at": timeutils.isoformat(ANCHOR),
        "updated_at": timeutils.isoformat(ANCHOR),
    }
    [entry] = AuditEntry.objects.filter(action="service.created")
    assert (entry.entity_type, entry.entity_id, entry.actor_id) == ("service", str(service.pk), staff.pk)
    assert entry.after is not None and entry.after["category"] == "cosmetology"
    full = staff_client.post(
        BASE,
        {
            "name": {"uz": "Kimyoviy pilling"},
            "short_name": {"uz": "Pilling", "en": "Peel"},
            "description": {"ru": "Описание"},
            "category": "cosmetology",
            "default_duration_min": 60,
            "default_price": 380_000,
            "position": 0,
            "is_active": False,
        },
    )
    assert full.status_code == 201, full.json()
    data = full.json()
    assert data["short_name"] == {"uz": "Pilling", "ru": "", "en": "Peel"}
    assert data["description"] == {"uz": "", "ru": "Описание", "en": ""}
    assert (data["default_price"], data["position"], data["is_active"]) == (380_000, 0, False)


@pytest.mark.parametrize(
    ("change", "expected"),
    [
        ({"name": {"uz": "A"}}, ("name.uz", "min_length", "Kamida 2 ta belgi kiriting.")),
        ({"name": {"ru": "Без узбекского"}}, ("name.uz", "required", "Majburiy maydon.")),
        ({"name": {"uz": "x" * 121}}, ("name.uz", "max_length", "120 belgidan oshmasin.")),
        ({"short_name": {"uz": "x" * 41}}, ("short_name.uz", "max_length", "40 belgidan oshmasin.")),
        ({"category": "surgery"}, ("category", "invalid_choice", "Ruxsat etilmagan qiymat.")),
        ({"category": None}, ("category", "null", "Majburiy maydon.")),
        ({"default_duration_min": 4}, ("default_duration_min", "min_value", "Qiymat 5 dan kam bo‘lmasin.")),
        ({"default_duration_min": 481}, ("default_duration_min", "max_value", "Qiymat 480 dan oshmasin.")),
        (
            {"default_duration_min": 32},
            ("default_duration_min", "invalid", "Davomiylik 5 daqiqaga karrali bo‘lsin."),
        ),
        ({"default_price": -1}, ("default_price", "min_value", "Qiymat 0 dan kam bo‘lmasin.")),
        ({"default_price": 100_000_001}, ("default_price", "max_value", "Narx juda katta.")),
        ({"description": {"uz": "x" * 2001}}, ("description.uz", "max_length", "2000 belgidan oshmasin.")),
    ],
)
def test_create_validation(
    staff_client: APIClient, change: dict[str, Any], expected: tuple[str, str, str]
) -> None:
    body = {"name": {"uz": "Konsultatsiya"}, "category": "dermatology", "default_duration_min": 30, **change}
    assert errors(staff_client.post(BASE, body)) == [expected]
    assert not Service.objects.exists() and not AuditEntry.objects.exists()


def test_create_is_idempotent(staff_client: APIClient) -> None:
    key = str(uuid.uuid4())
    body = {"name": {"uz": "Konsultatsiya"}, "category": "dermatology", "default_duration_min": 30}
    first = staff_client.post(BASE, body, HTTP_IDEMPOTENCY_KEY=key)
    replay = staff_client.post(BASE, body, HTTP_IDEMPOTENCY_KEY=key)
    assert first.status_code == replay.status_code == 201 and replay["Idempotent-Replayed"] == "true"
    assert Service.objects.count() == 1


def test_list_search_filters_ordering_and_specialist_count(staff_client: APIClient) -> None:
    consult = ServiceFactory.create(
        name_uz="Dermatolog konsultatsiyasi",
        name_ru="Консультация",
        short_name_uz="Konsultatsiya",
        position=1,
    )
    peel = ServiceFactory.create(
        name_uz="Kimyoviy pilling", short_name_en="Peel", category="cosmetology", position=0
    )
    retired = ServiceFactory.create(name_uz="Eski xizmat", is_active=False, position=2)
    ProfessionalServiceFactory.create(service=consult)
    ProfessionalServiceFactory.create(service=consult)
    ProfessionalServiceFactory.create(service=consult, is_active=False)  # a paused link
    ProfessionalServiceFactory.create(
        service=consult, professional=ProfessionalFactory.create(is_active=False)
    )
    body = staff_client.get(BASE).json()
    assert (body["count"], body["page_size"]) == (3, 20)
    assert [row["name"]["uz"] for row in body["results"]] == [peel.name_uz, consult.name_uz, retired.name_uz]
    assert [row["specialist_count"] for row in body["results"]] == [0, 2, 0]
    assert names(staff_client, search="консульт") == [consult.name_uz]
    assert names(staff_client, search=" peel ") == [peel.name_uz]
    assert names(staff_client, is_active="false") == [retired.name_uz]
    assert names(staff_client, category="cosmetology") == [peel.name_uz]
    assert names(staff_client, ordering="name") == [consult.name_uz, retired.name_uz, peel.name_uz]
    assert names(staff_client, ordering="-name") == [peel.name_uz, retired.name_uz, consult.name_uz]
    assert names(staff_client, ordering="-created_at")[0] == retired.name_uz
    assert errors(staff_client.get(BASE, {"category": "x"}))[0][:2] == ("category", "invalid_choice")
    assert errors(staff_client.get(BASE, {"is_active": "maybe"}))[0][:2] == ("is_active", "invalid_choice")


def test_list_runs_a_constant_number_of_queries(staff_client: APIClient) -> None:
    for _ in range(2):
        ProfessionalServiceFactory.create()
    assert staff_client.get(BASE).status_code == 200  # warm the session cache of the authentication
    with CaptureQueriesContext(connection) as small:
        assert staff_client.get(BASE).status_code == 200
    for _ in range(8):
        ProfessionalServiceFactory.create()
    with CaptureQueriesContext(connection) as large:
        response = staff_client.get(BASE)
    assert response.json()["count"] == 10
    assert len(large.captured_queries) == len(small.captured_queries) <= 4


def test_export(staff_client: APIClient) -> None:
    service = ServiceFactory.create(
        name_uz="+Konsultatsiya", short_name_uz="Konsultatsiya", default_price=None, default_duration_min=30
    )
    ProfessionalServiceFactory.create(service=service)
    response = staff_client.get(BASE, {"export": "csv", "search": "kons"})
    assert response.status_code == 200
    assert response["Content-Disposition"].startswith('attachment; filename="xizmatlar-')
    rows = list(csv.reader(StringIO(response.content.decode("utf-8-sig")), delimiter=";"))
    assert rows == [
        [
            "ID",
            "Nomi",
            "Qisqa nomi",
            "Kategoriya",
            "Davomiyligi (daq.)",
            "Narx (so‘m)",
            "Mutaxassislar",
            "Holat",
        ],
        [str(service.pk), "'+Konsultatsiya", "Konsultatsiya", "Dermatologiya", "30", "", "1", "Faol"],
    ]
    entry = AuditEntry.objects.get(action="export.downloaded")
    assert entry.after == {
        "resource": "services",
        "filters": {"search": "kons"},
        "row_count": 1,
        "format": "csv",
    }


def test_retrieve_patch_and_404(staff_client: APIClient, staff: StaffUser) -> None:
    service = ServiceFactory.create(name_uz="Konsultatsiya", name_ru="Консультация", default_price=100_000)
    detail = f"{BASE}{service.pk}/"
    assert staff_client.get(detail).json()["name"] == {"uz": "Konsultatsiya", "ru": "Консультация", "en": ""}
    response = staff_client.patch(detail, {"name": {"en": "Consultation"}, "default_price": None})
    assert response.status_code == 200, response.json()
    assert response.json()["name"] == {"uz": "Konsultatsiya", "ru": "Консультация", "en": "Consultation"}
    assert response.json()["default_price"] is None
    [entry] = AuditEntry.objects.filter(action="service.updated")
    assert entry.before == {"name.en": "", "default_price": 100_000}
    assert entry.after == {"name.en": "Consultation", "default_price": None}
    assert entry.actor_id == staff.pk
    assert staff_client.patch(detail, {"default_price": None}).status_code == 200  # no change, no audit
    assert AuditEntry.objects.filter(action="service.updated").count() == 1
    assert errors(staff_client.patch(detail, {"name": {"uz": ""}}))[0][:2] == ("name.uz", "blank")
    assert errors(staff_client.patch(detail, {"short_name": {"uz": ""}}))[0][:2] == ("short_name.uz", "blank")
    assert staff_client.get(f"{BASE}999999/").status_code == 404
    assert staff_client.patch(f"{BASE}999999/", {"is_active": False}).status_code == 404
    assert staff_client.delete(detail).status_code == 405  # never deleted: deactivated


def test_deactivation_hides_the_service_and_keeps_its_appointments(
    staff_client: APIClient, public_client: APIClient
) -> None:
    link = ProfessionalServiceFactory.create()
    appointment = AppointmentFactory.create(
        professional=link.professional,
        service=link.service,
        start=datetime.combine(MONDAY, time(10), tzinfo=TASHKENT),
    )
    assert [s["id"] for s in public_client.get("/api/v1/services/").json()["results"]] == [link.service_id]
    response = staff_client.patch(f"{BASE}{link.service_id}/", {"is_active": False})
    assert response.status_code == 200 and response.json()["is_active"] is False
    [entry] = AuditEntry.objects.filter(action="service.deactivated")
    assert (entry.before, entry.after) == ({"is_active": True}, {"is_active": False})
    # gone from the customer catalogue at once (no cache), the booked appointment untouched
    assert public_client.get("/api/v1/services/").json()["results"] == []
    specialist = public_client.get(f"/api/v1/specialists/{link.professional_id}/").json()
    assert specialist["services"] == []
    stored = Appointment.objects.get(pk=appointment.pk)
    assert (stored.status, stored.service_id, stored.price) == ("requested", link.service_id, 150_000)
    assert staff_client.patch(f"{BASE}{link.service_id}/", {"is_active": True}).status_code == 200
    assert AuditEntry.objects.filter(action="service.activated").count() == 1
