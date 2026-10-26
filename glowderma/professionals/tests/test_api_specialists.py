"""Contract "Mutaxassislar va xizmatlar" — ``specialists/``: list (pagination, search, filters, ordering,
export, query count), create, retrieve, PATCH with the deactivation guard, the photo, the offered services,
the weekly hours, the date overrides and the blocked periods. Every write is audited and idempotent."""

import csv
import uuid
from collections.abc import Callable
from datetime import datetime, time, timedelta
from io import StringIO
from typing import Any

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from rest_framework.exceptions import ValidationError
from rest_framework.test import APIClient

from glowderma.accounts.models import StaffUser
from glowderma.accounts.tests.factories import StaffUserFactory
from glowderma.appointments.models import Appointment
from glowderma.appointments.tests.factories import AppointmentFactory
from glowderma.catalog.tests.helpers import upload
from glowderma.core import enums, timeutils
from glowderma.core.images import MAX_BYTES
from glowderma.core.media import absolute_media_url
from glowderma.core.models import AuditEntry
from glowderma.core.timeutils import TASHKENT
from glowderma.professionals import services
from glowderma.professionals.models import (
    BlockedPeriod,
    Professional,
    ProfessionalService,
    WorkingHours,
    WorkingHoursOverride,
)
from glowderma.professionals.tests.conftest import ANCHOR, errors
from glowderma.professionals.tests.factories import (
    BlockedPeriodFactory,
    LocationFactory,
    ProfessionalFactory,
    ProfessionalServiceFactory,
    ServiceFactory,
    WorkingHoursFactory,
    WorkingHoursOverrideFactory,
)

pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures("clock")]

BASE = "/api/v1/admin/specialists/"
MONDAY = ANCHOR.date() + timedelta(days=2)  # 26 Oct 2026
EMPTY = {"uz": "", "ru": "", "en": ""}


def url(professional: Professional, suffix: str = "") -> str:
    return f"{BASE}{professional.pk}/{suffix}"


def at(day: Any, hour: int, minute: int = 0) -> datetime:
    return datetime.combine(day, time(hour, minute), tzinfo=TASHKENT)


def audits(action: str) -> list[AuditEntry]:
    return list(AuditEntry.objects.filter(action=action).order_by("id"))


# --- create -------------------------------------------------------------------------------------------------


def test_create_defaults_to_the_clinic_and_inactive(staff_client: APIClient, staff: StaffUser) -> None:
    location = LocationFactory.create()
    response = staff_client.post(BASE, {"name": "  Malika Usmonova ", "role": "cosmetologist"})
    assert response.status_code == 201, response.json()
    professional = Professional.objects.get()
    assert response.json() == {
        "id": professional.pk,
        "name": "Malika Usmonova",
        "role": "cosmetologist",
        "qualifications": EMPTY,
        "bio": EMPTY,
        "experience_years": None,
        "photo_url": None,
        "location_id": location.pk,
        "staff_user": None,
        "slot_step_min": 30,
        "position": 0,
        "is_active": False,
        "services": [],
        "working_hours": [],
        "upcoming_appointments": 0,
        "created_at": timeutils.isoformat(ANCHOR),
        "updated_at": timeutils.isoformat(ANCHOR),
    }
    [entry] = audits("professional.created")
    assert (entry.entity_type, entry.entity_id, entry.actor_id) == (
        "professional",
        str(professional.pk),
        staff.pk,
    )
    assert entry.after is not None and entry.after["name"] == "Malika Usmonova"
    assert entry.after["is_active"] is False and entry.after["location_id"] == location.pk


def test_create_with_every_field(staff_client: APIClient) -> None:
    LocationFactory.create()
    other = LocationFactory.create()
    doctor = StaffUserFactory.create(full_name="Nodira Rahimova")
    ProfessionalFactory.create(position=4)
    body = {
        "name": "Nodira Rahimova",
        "role": "dermatologist",
        "qualifications": {"uz": "Toshkent tibbiyot akademiyasi", "ru": "ТМА"},
        "bio": {"uz": "Tajribali dermatolog", "en": "Experienced"},
        "experience_years": 8,
        "slot_step_min": 15,
        "location_id": other.pk,
        "staff_user_id": doctor.pk,
        "is_active": True,
    }
    response = staff_client.post(BASE, body)
    assert response.status_code == 201, response.json()
    data = response.json()
    assert data["qualifications"] == {"uz": "Toshkent tibbiyot akademiyasi", "ru": "ТМА", "en": ""}
    assert data["bio"] == {"uz": "Tajribali dermatolog", "ru": "", "en": "Experienced"}
    assert (data["experience_years"], data["slot_step_min"], data["location_id"]) == (8, 15, other.pk)
    assert data["staff_user"] == {"id": doctor.pk, "display_name": "Nodira Rahimova"}
    assert data["is_active"] is True and data["position"] == 5  # appended last


def test_create_without_a_clinic_asks_for_the_location(staff_client: APIClient) -> None:
    response = staff_client.post(BASE, {"name": "Malika Usmonova", "role": "cosmetologist"})
    assert errors(response) == [("location_id", "required", "Avval klinika manzilini kiriting.")]
    assert not Professional.objects.exists() and not AuditEntry.objects.exists()


@pytest.mark.parametrize(
    ("change", "expected"),
    [
        ({"name": None}, ("name", "null", "Majburiy maydon.")),
        ({"name": " a "}, ("name", "min_length", "Kamida 2 ta belgi kiriting.")),
        ({"name": "a" * 151}, ("name", "max_length", "150 belgidan oshmasin.")),
        ({"role": "surgeon"}, ("role", "invalid_choice", "Ruxsat etilmagan qiymat.")),
        (
            {"slot_step_min": 25},
            ("slot_step_min", "invalid_choice", "Ruxsat etilgan qadamlar: 10, 15, 20, 30 yoki 60 daqiqa."),
        ),
        ({"experience_years": 81}, ("experience_years", "max_value", "Qiymat 80 dan oshmasin.")),
        ({"location_id": 999_999}, ("location_id", "does_not_exist", "Manzil topilmadi.")),
        ({"staff_user_id": 999_999}, ("staff_user_id", "does_not_exist", "Xodim topilmadi.")),
        (
            {"qualifications": {"uz": "x" * 2001}},
            ("qualifications.uz", "max_length", "2000 belgidan oshmasin."),
        ),
        ({"position": -1}, ("position", "min_value", "Qiymat 0 dan kam bo‘lmasin.")),
    ],
)
def test_create_validation(
    staff_client: APIClient, change: dict[str, Any], expected: tuple[str, str, str]
) -> None:
    LocationFactory.create()
    body = {"name": "Malika Usmonova", "role": "cosmetologist", **change}
    assert errors(staff_client.post(BASE, body)) == [expected]
    assert not Professional.objects.exists()


def test_staff_link_must_be_active_and_free(staff_client: APIClient) -> None:
    LocationFactory.create()
    inactive = StaffUserFactory.create(is_active=False)
    linked = StaffUserFactory.create()
    holder = ProfessionalFactory.create(staff_user=linked)
    body = {"name": "Malika Usmonova", "role": "cosmetologist"}
    response = staff_client.post(BASE, {**body, "staff_user_id": inactive.pk})
    assert errors(response) == [("staff_user_id", "does_not_exist", "Xodim topilmadi.")]
    response = staff_client.post(BASE, {**body, "staff_user_id": linked.pk})
    assert errors(response) == [("staff_user_id", "unique", "Bu xodim boshqa mutaxassisga bog‘langan.")]
    # the holder may keep (re-send) its own link, and unlink it
    assert staff_client.patch(url(holder), {"staff_user_id": linked.pk}).status_code == 200
    response = staff_client.patch(url(holder), {"staff_user_id": None})
    assert response.status_code == 200 and response.json()["staff_user"] is None


def test_create_is_idempotent(staff_client: APIClient) -> None:
    LocationFactory.create()
    key = str(uuid.uuid4())
    body = {"name": "Malika Usmonova", "role": "cosmetologist"}
    first = staff_client.post(BASE, body, HTTP_IDEMPOTENCY_KEY=key)
    replay = staff_client.post(BASE, body, HTTP_IDEMPOTENCY_KEY=key)
    assert first.status_code == replay.status_code == 201
    assert replay["Idempotent-Replayed"] == "true" and replay.json() == first.json()
    assert Professional.objects.count() == 1 and len(audits("professional.created")) == 1
    reused = staff_client.post(BASE, {**body, "name": "Boshqa"}, HTTP_IDEMPOTENCY_KEY=key)
    assert reused.status_code == 422 and reused.json()["code"] == "idempotency_key_reused"


def test_anonymous_and_customer_callers_are_refused(anon_client: APIClient) -> None:
    assert anon_client.get(BASE).status_code == 401
    assert anon_client.post(BASE, {"name": "X", "role": "dermatologist"}).status_code == 401


# --- list, retrieve, export ---------------------------------------------------------------------------------


def test_list_search_filters_ordering_and_upcoming_counts(staff_client: APIClient) -> None:
    nodira = ProfessionalFactory.create(name="Nodira Rahimova", position=1)
    jasur = ProfessionalFactory.create(name="jasur Aliyev", position=0)
    dilnoza = ProfessionalFactory.create(name="Dilnoza Yusupova", role="cosmetologist", position=2)
    dilnoza.is_active = False
    dilnoza.save()
    AppointmentFactory.create(professional=nodira, start=at(MONDAY, 10))
    AppointmentFactory.create(
        professional=nodira, start=at(MONDAY, 11), status="confirmed", confirmed_at=ANCHOR
    )
    AppointmentFactory.create(
        professional=nodira, start=at(MONDAY, 12), status="rejected", rejected_at=ANCHOR
    )
    AppointmentFactory.create(professional=nodira, start=ANCHOR - timedelta(days=1))  # already started

    def names(**params: Any) -> list[str]:
        response = staff_client.get(BASE, params)
        assert response.status_code == 200, response.json()
        return [row["name"] for row in response.json()["results"]]

    body = staff_client.get(BASE).json()
    assert (body["count"], body["page"], body["page_size"], body["page_count"]) == (3, 1, 20, 1)
    assert [row["name"] for row in body["results"]] == ["jasur Aliyev", "Nodira Rahimova", "Dilnoza Yusupova"]
    assert [row["upcoming_appointments"] for row in body["results"]] == [0, 2, 0]
    assert names(search="  rahim ") == ["Nodira Rahimova"]
    assert names(search="x" * 70) == []
    assert names(is_active="false") == ["Dilnoza Yusupova"]
    assert names(is_active="1", role="dermatologist") == ["jasur Aliyev", "Nodira Rahimova"]
    assert names(ordering="name") == ["Dilnoza Yusupova", "jasur Aliyev", "Nodira Rahimova"]
    assert names(ordering="-name") == ["Nodira Rahimova", "jasur Aliyev", "Dilnoza Yusupova"]
    assert names(ordering="created_at")[0] == "Nodira Rahimova"
    assert names(ordering="-created_at")[0] == "Dilnoza Yusupova"
    assert errors(staff_client.get(BASE, {"page_size": "8"}))[0][:2] == ("page_size", "invalid_choice")
    assert errors(staff_client.get(BASE, {"role": "x"}))[0][:2] == ("role", "invalid_choice")
    assert errors(staff_client.get(BASE, {"ordering": "position,name"}))[0][:2] == (
        "ordering",
        "invalid_choice",
    )
    assert jasur.pk != nodira.pk


def _populate(count: int) -> None:
    for _ in range(count):
        professional = ProfessionalFactory.create(staff_user=StaffUserFactory.create())
        for _service in range(2):
            ProfessionalServiceFactory.create(professional=professional)
        for weekday in range(3):
            WorkingHoursFactory.create(professional=professional, weekday=weekday)
        AppointmentFactory.create(professional=professional, start=at(MONDAY, 10))


def test_list_runs_a_constant_number_of_queries(staff_client: APIClient) -> None:
    _populate(2)
    assert staff_client.get(BASE).status_code == 200  # warm the session cache of the authentication
    with CaptureQueriesContext(connection) as small:
        assert staff_client.get(BASE).status_code == 200
    _populate(6)
    with CaptureQueriesContext(connection) as large:
        response = staff_client.get(BASE)
    assert response.status_code == 200 and response.json()["count"] == 8
    assert len(large.captured_queries) == len(small.captured_queries) <= 10, [
        query["sql"] for query in large.captured_queries
    ]


def test_retrieve_and_404(staff_client: APIClient) -> None:
    professional = ProfessionalFactory.create()
    link = ProfessionalServiceFactory.create(professional=professional, price=90_000)
    WorkingHoursFactory.create(
        professional=professional, weekday=2, break_start=time(13, 0), break_end=time(14, 0)
    )
    WorkingHoursFactory.create(professional=professional, weekday=0)
    body = staff_client.get(url(professional)).json()
    assert body["services"] == [
        {
            "service_id": link.service_id,
            "name": link.service.name_uz,
            "short_name": link.service.short_name_uz,
            "category": "dermatology",
            "price": 90_000,
            "duration_min": 30,
            "is_active": True,
            "service_is_active": True,
        }
    ]
    assert body["working_hours"] == [
        {"weekday": 0, "start": "09:00", "end": "18:00", "break_start": None, "break_end": None},
        {"weekday": 2, "start": "09:00", "end": "18:00", "break_start": "13:00", "break_end": "14:00"},
    ]
    missing = staff_client.get(f"{BASE}999999/")
    assert missing.status_code == 404 and missing.json()["code"] == "not_found"


def test_export_csv_and_xlsx(staff_client: APIClient) -> None:
    nodira = ProfessionalFactory.create(name="=Nodira", experience_years=8, position=0)
    consult = ServiceFactory.create(short_name_uz="Konsultatsiya", position=0)
    paused = ServiceFactory.create(short_name_uz="Pilling", position=1)
    ProfessionalServiceFactory.create(professional=nodira, service=consult)
    ProfessionalServiceFactory.create(professional=nodira, service=paused, is_active=False)
    for weekday in (0, 2, 5):
        WorkingHoursFactory.create(professional=nodira, weekday=weekday)
    AppointmentFactory.create(professional=nodira, start=at(MONDAY, 10))
    ProfessionalFactory.create(name="Dilnoza", role="cosmetologist", is_active=False, position=1)
    response = staff_client.get(BASE, {"export": "csv", "is_active": "true", "page": "3"})
    assert response.status_code == 200
    assert response["Content-Disposition"].startswith('attachment; filename="mutaxassislar-')
    rows = list(csv.reader(StringIO(response.content.decode("utf-8-sig")), delimiter=";"))
    assert rows == [
        [
            "ID",
            "Ism",
            "Mutaxassislik",
            "Tajriba (yil)",
            "Xizmatlar",
            "Ish kunlari",
            "Kelgusi qabullar",
            "Holat",
        ],
        [str(nodira.pk), "'=Nodira", "Dermatolog", "8", "Konsultatsiya", "Du, Ch, Sh", "1", "Faol"],
    ]
    entry = AuditEntry.objects.get(action="export.downloaded")
    assert entry.after == {
        "resource": "specialists",
        "filters": {"is_active": "true"},
        "row_count": 1,
        "format": "csv",
    }
    every = staff_client.get(BASE, {"export": "xlsx"})
    assert every.status_code == 200 and every["Content-Disposition"].endswith('.xlsx"')


# --- PATCH and the deactivation guard -----------------------------------------------------------------------


def test_patch_merges_texts_and_audits_only_the_changes(staff_client: APIClient, staff: StaffUser) -> None:
    professional = ProfessionalFactory.create(bio_uz="Eski", bio_ru="Старый", experience_years=3)
    response = staff_client.patch(
        url(professional), {"bio": {"ru": "Новый"}, "experience_years": 4, "role": "dermatologist"}
    )
    assert response.status_code == 200, response.json()
    assert response.json()["bio"] == {"uz": "Eski", "ru": "Новый", "en": ""}
    [entry] = audits("professional.updated")
    assert entry.before == {"bio.ru": "Старый", "experience_years": 3}
    assert entry.after == {"bio.ru": "Новый", "experience_years": 4}
    assert entry.actor_id == staff.pk and entry.entity_id == str(professional.pk)
    # nothing changed: no write, no audit row
    updated_at = Professional.objects.get().updated_at
    assert staff_client.patch(url(professional), {"experience_years": 4}).status_code == 200
    assert len(audits("professional.updated")) == 1 and Professional.objects.get().updated_at == updated_at
    assert errors(staff_client.patch(url(professional), {"name": ""}))[0][:2] == ("name", "blank")
    assert staff_client.patch(f"{BASE}999999/", {"name": "Xyz"}).status_code == 404


def test_deactivation_with_upcoming_appointments_needs_force(staff_client: APIClient) -> None:
    professional = ProfessionalFactory.create()
    kept = [
        AppointmentFactory.create(professional=professional, start=at(MONDAY, 10)),
        AppointmentFactory.create(
            professional=professional, start=at(MONDAY, 11), status="confirmed", confirmed_at=ANCHOR
        ),
    ]
    AppointmentFactory.create(professional=professional, start=ANCHOR - timedelta(hours=3))
    AppointmentFactory.create(
        professional=professional,
        start=at(MONDAY, 12),
        status="cancelled",
        cancelled_at=ANCHOR,
        cancelled_by="customer",
    )
    assert staff_client.get(url(professional)).json()["upcoming_appointments"] == 2
    refused = staff_client.patch(url(professional), {"is_active": False, "name": "Yangi ism"})
    assert refused.status_code == 409
    assert refused.json() == {
        "code": "conflict",
        "message": (
            "Mutaxassisda 2 ta kelgusi qabul bor (kutilayotgan yoki tasdiqlangan). Qabullar o‘zgarmaydi: "
            "avval ularni boshqa vaqtga yoki mutaxassisga ko‘chiring yoki bekor qiling, yoki baribir "
            "nofaol qiling."
        ),
        "errors": [],
        "request_id": refused.json()["request_id"],
    }
    assert Professional.objects.get().is_active and Professional.objects.get().name != "Yangi ism"
    assert not AuditEntry.objects.filter(entity_type="professional").exists()

    forced = staff_client.patch(url(professional), {"is_active": False, "force": True})
    assert forced.status_code == 200 and forced.json()["is_active"] is False
    [entry] = audits("professional.deactivated")
    assert entry.before == {"is_active": True}
    assert entry.after == {"is_active": False, "upcoming_appointments": 2, "forced": True}
    # the appointments are kept as they were
    assert [Appointment.objects.get(pk=a.pk).status for a in kept] == ["requested", "confirmed"]

    reactivated = staff_client.patch(url(professional), {"is_active": True})
    assert reactivated.status_code == 200 and len(audits("professional.activated")) == 1


def test_deactivation_without_upcoming_appointments_is_not_forced(staff_client: APIClient) -> None:
    professional = ProfessionalFactory.create()
    response = staff_client.patch(url(professional), {"is_active": False, "experience_years": 2})
    assert response.status_code == 200
    [entry] = audits("professional.deactivated")
    assert entry.after == {
        "is_active": False,
        "experience_years": 2,
        "upcoming_appointments": 0,
        "forced": False,
    }


# --- photo --------------------------------------------------------------------------------------------------


def test_photo_upload_replace_and_delete(
    staff_client: APIClient, django_capture_on_commit_callbacks: Callable[..., Any]
) -> None:
    professional = ProfessionalFactory.create()
    with django_capture_on_commit_callbacks(execute=True):
        response = staff_client.put(url(professional, "photo/"), {"file": upload("PNG")}, format="multipart")
    assert response.status_code == 200, response.json()
    photo = Professional.objects.get().photo
    first, storage = photo.name or "", photo.storage
    assert first.startswith(f"professionals/{professional.pk}/") and first.endswith(".png")
    assert response.json()["photo_url"] == absolute_media_url(first)
    assert storage.exists(first)
    with django_capture_on_commit_callbacks(execute=True):
        replaced = staff_client.put(
            url(professional, "photo/"), {"file": upload("JPEG", size=(30, 20))}, format="multipart"
        )
    second = Professional.objects.get().photo.name or ""
    assert replaced.status_code == 200 and second.endswith(".jpg") and second != first
    assert not storage.exists(first) and storage.exists(second)
    # the same bytes again change nothing
    staff_client.put(url(professional, "photo/"), {"file": upload("JPEG", size=(30, 20))}, format="multipart")
    assert [e.action for e in AuditEntry.objects.order_by("id")] == ["professional.photo_updated"] * 2
    with django_capture_on_commit_callbacks(execute=True):
        assert staff_client.delete(url(professional, "photo/")).status_code == 204
    assert Professional.objects.get().photo.name == "" and not storage.exists(second)
    assert staff_client.get(url(professional)).json()["photo_url"] is None
    assert staff_client.delete(url(professional, "photo/")).status_code == 204  # idempotent
    assert [e.action for e in AuditEntry.objects.order_by("id")][-1] == "professional.photo_deleted"
    assert len(audits("professional.photo_deleted")) == 1


def test_photo_validation(staff_client: APIClient) -> None:
    professional = ProfessionalFactory.create()
    target = url(professional, "photo/")
    big = upload(content=b"\x89PNG" + b"\0" * (MAX_BYTES + 1 - 4), name="big.png")
    assert errors(staff_client.put(target, {"file": big}, format="multipart")) == [
        ("file", "file_too_large", "Rasm 5 MB dan oshmasin.")
    ]
    text = upload(content=b"not an image", name="a.png")
    assert errors(staff_client.put(target, {"file": text}, format="multipart")) == [
        ("file", "unsupported_image", "Faqat JPEG, PNG yoki WebP rasm.")
    ]
    assert errors(staff_client.put(target, {"other": "x"}, format="multipart")) == [
        ("file", "required", "Majburiy maydon.")
    ]
    assert staff_client.put(target, {"file": "x"}, format="json").status_code == 415
    assert staff_client.put(f"{BASE}999999/photo/", {"file": upload()}, format="multipart").status_code == 404
    assert not AuditEntry.objects.exists()


# --- offered services ---------------------------------------------------------------------------------------


def test_offered_services_put_the_exact_set(staff_client: APIClient, staff: StaffUser) -> None:
    professional = ProfessionalFactory.create()
    consult = ServiceFactory.create(default_duration_min=30, position=1)
    peel = ServiceFactory.create(default_duration_min=60, position=0)
    old = ServiceFactory.create(position=2)
    ProfessionalServiceFactory.create(professional=professional, service=old, price=50_000)
    body = {
        "services": [
            {"service_id": consult.pk, "price": 150_000},
            {"service_id": peel.pk, "price": 0, "duration_min": 45, "is_active": False},
        ]
    }
    response = staff_client.put(url(professional, "services/"), body)
    assert response.status_code == 200, response.json()
    offered = [
        (s["service_id"], s["price"], s["duration_min"], s["is_active"]) for s in response.json()["services"]
    ]
    assert offered == [(peel.pk, 0, 45, False), (consult.pk, 150_000, 30, True)]  # by service position
    assert not ProfessionalService.objects.filter(service=old).exists()
    [entry] = audits("professional.services_updated")
    assert entry.before == {
        "services": [{"service_id": old.pk, "price": 50_000, "duration_min": 30, "is_active": True}]
    }
    assert entry.after is not None and len(entry.after["services"]) == 2 and entry.actor_id == staff.pk
    # the same set again: no audit row
    assert staff_client.put(url(professional, "services/"), body).status_code == 200
    assert len(audits("professional.services_updated")) == 1
    response = staff_client.put(url(professional, "services/"), {"services": []})
    assert response.status_code == 200 and response.json()["services"] == []


def test_a_price_change_keeps_the_booked_snapshot(staff_client: APIClient) -> None:
    link = ProfessionalServiceFactory.create(price=150_000)
    appointment = AppointmentFactory.create(
        professional=link.professional, service=link.service, start=at(MONDAY, 10), price=150_000
    )
    body = {"services": [{"service_id": link.service_id, "price": 200_000, "duration_min": 60}]}
    assert staff_client.put(url(link.professional, "services/"), body).status_code == 200
    stored = Appointment.objects.get(pk=appointment.pk)
    assert (stored.price, stored.duration_min, stored.end) == (150_000, 30, at(MONDAY, 10, 30))


def test_offered_services_validation(staff_client: APIClient) -> None:
    professional = ProfessionalFactory.create()
    service = ServiceFactory.create()
    target = url(professional, "services/")

    def put(*items: dict[str, Any]) -> list[tuple[str | None, str, str]]:
        return errors(staff_client.put(target, {"services": list(items)}))

    item = {"service_id": service.pk, "price": 1000}
    assert put(item, item) == [
        ("services.1.service_id", "unique", "Bu xizmat ro‘yxatda ikki marta berilgan.")
    ]
    assert put({**item, "service_id": 999_999}) == [
        ("services.0.service_id", "does_not_exist", "Xizmat topilmadi.")
    ]
    assert put({**item, "price": -1}) == [("services.0.price", "min_value", "Qiymat 0 dan kam bo‘lmasin.")]
    assert put({**item, "price": 100_000_001}) == [("services.0.price", "max_value", "Narx juda katta.")]
    assert put({**item, "duration_min": 7}) == [
        ("services.0.duration_min", "invalid", "Davomiylik 5 daqiqaga karrali bo‘lsin.")
    ]
    assert put({**item, "duration_min": 485}) == [
        ("services.0.duration_min", "max_value", "Qiymat 480 dan oshmasin.")
    ]
    assert put({"price": 1000}) == [("services.0.service_id", "required", "Majburiy maydon.")]
    too_many = errors(staff_client.put(target, {"services": [item] * 51}))
    assert too_many == [("services", "max_items", "Ko‘pi bilan 50 ta.")]
    assert errors(staff_client.put(target, {}))[0][:2] == ("services", "required")
    assert not ProfessionalService.objects.exists() and not AuditEntry.objects.exists()


# --- weekly hours -------------------------------------------------------------------------------------------


def test_working_week_put(staff_client: APIClient) -> None:
    professional = ProfessionalFactory.create()
    WorkingHoursFactory.create(professional=professional, weekday=6)  # Sunday, dropped by the PUT
    days = [
        {"weekday": 0, "start": "09:00", "end": "18:00", "break_start": "13:00", "break_end": "14:00"},
        {"weekday": 2, "start": "10:30", "end": "16:00"},
    ]
    response = staff_client.put(url(professional, "working-hours/"), {"days": days})
    assert response.status_code == 200, response.json()
    assert response.json()["working_hours"] == [
        {"weekday": 0, "start": "09:00", "end": "18:00", "break_start": "13:00", "break_end": "14:00"},
        {"weekday": 2, "start": "10:30", "end": "16:00", "break_start": None, "break_end": None},
    ]
    assert list(WorkingHours.objects.values_list("weekday", flat=True).order_by("weekday")) == [0, 2]
    [entry] = audits("schedule.hours_updated")
    assert entry.entity_type == "professional" and entry.entity_id == str(professional.pk)
    assert entry.before == {
        "days": [{"weekday": 6, "start": "09:00", "end": "18:00", "break_start": None, "break_end": None}]
    }
    assert entry.after is not None and [d["weekday"] for d in entry.after["days"]] == [0, 2]
    assert staff_client.put(url(professional, "working-hours/"), {"days": days}).status_code == 200
    assert len(audits("schedule.hours_updated")) == 1  # unchanged week: no audit
    assert staff_client.put(url(professional, "working-hours/"), {"days": []}).status_code == 200
    assert not WorkingHours.objects.exists()


@pytest.mark.parametrize(
    ("day", "expected"),
    [
        ({"end": "09:00"}, ("days.0.end", "invalid", "Tugash vaqti boshlanishidan keyin bo‘lsin.")),
        ({"start": "09:03"}, ("days.0.start", "invalid", "Vaqt 5 daqiqalik qadamda bo‘lsin.")),
        ({"start": "9:00"}, ("days.0.start", "invalid", "Vaqtni SS:DD ko‘rinishida kiriting.")),
        ({"end": "24:00"}, ("days.0.end", "invalid", "Vaqtni SS:DD ko‘rinishida kiriting.")),
        ({"start": 900}, ("days.0.start", "invalid", "Vaqtni SS:DD ko‘rinishida kiriting.")),
        ({"weekday": 7}, ("days.0.weekday", "max_value", "Qiymat 6 dan oshmasin.")),
        (
            {"break_start": "13:00"},
            ("days.0.break_end", "required", "Tanaffusning boshlanishi va tugashini birga kiriting."),
        ),
        (
            {"break_start": "14:00", "break_end": "13:00"},
            ("days.0.break_end", "invalid", "Tugash vaqti boshlanishidan keyin bo‘lsin."),
        ),
        (
            {"break_start": "08:00", "break_end": "09:30"},
            ("days.0.break_start", "invalid", "Tanaffus ish vaqti ichida bo‘lsin."),
        ),
    ],
)
def test_working_day_validation(
    staff_client: APIClient, day: dict[str, Any], expected: tuple[str, str, str]
) -> None:
    professional = ProfessionalFactory.create()
    body = {"days": [{"weekday": 0, "start": "09:00", "end": "18:00", **day}]}
    assert errors(staff_client.put(url(professional, "working-hours/"), body)) == [expected]
    assert not WorkingHours.objects.exists()


def test_working_week_refuses_a_weekday_twice_and_eight_days(staff_client: APIClient) -> None:
    professional = ProfessionalFactory.create()
    day = {"weekday": 1, "start": "09:00", "end": "18:00"}
    assert errors(staff_client.put(url(professional, "working-hours/"), {"days": [day, day]})) == [
        ("days.1.weekday", "unique", "Bu hafta kuni ikki marta berilgan.")
    ]
    eight = [{**day, "weekday": n % 7} for n in range(8)]
    assert errors(staff_client.put(url(professional, "working-hours/"), {"days": eight})) == [
        ("days", "max_items", "Ko‘pi bilan 7 ta.")
    ]


# --- date overrides -----------------------------------------------------------------------------------------


def test_overrides_list_create_delete(staff_client: APIClient, staff: StaffUser) -> None:
    professional = ProfessionalFactory.create()
    WorkingHoursOverrideFactory.create(professional=professional, date=ANCHOR.date() - timedelta(days=1))
    target = url(professional, "overrides/")
    day_off = staff_client.post(target, {"date": "2026-10-27", "note": "Ta’til"})
    assert day_off.status_code == 201, day_off.json()
    row = WorkingHoursOverride.objects.get(date="2026-10-27")
    assert day_off.json() == {
        "id": row.pk,
        "date": "2026-10-27",
        "start": None,
        "end": None,
        "day_off": True,
        "note": "Ta’til",
        "created_by": {"id": staff.pk, "display_name": staff.full_name or staff.email},
        "created_at": timeutils.isoformat(ANCHOR),
    }
    short = staff_client.post(target, {"date": "2026-10-24", "start": "10:00", "end": "14:00"})
    assert short.status_code == 201 and short.json()["day_off"] is False
    listed = staff_client.get(target).json()
    assert [r["date"] for r in listed["results"]] == ["2026-10-24", "2026-10-27"]  # from today by default
    assert listed["count"] == 2 and listed["page_size"] == 20
    ranged = staff_client.get(target, {"date_from": "2026-10-01", "date_to": "2026-10-24"}).json()
    assert [r["date"] for r in ranged["results"]] == ["2026-10-23", "2026-10-24"]
    assert errors(staff_client.get(target, {"date_from": "2026-10-24", "date_to": "2026-10-23"})) == [
        ("date_to", "invalid", "Oxirgi sana boshlang‘ich sanadan oldin bo‘lmasin.")
    ]
    assert staff_client.delete(f"{target}{row.pk}/").status_code == 204
    assert not WorkingHoursOverride.objects.filter(pk=row.pk).exists()
    other = WorkingHoursOverrideFactory.create()
    assert staff_client.delete(f"{target}{other.pk}/").status_code == 404
    assert [e.action for e in AuditEntry.objects.order_by("id")] == [
        "schedule.override_created",
        "schedule.override_created",
        "schedule.override_deleted",
    ]
    deleted = audits("schedule.override_deleted")[0]
    assert deleted.entity_id == str(professional.pk)
    assert deleted.before == {
        "override_id": row.pk,
        "date": "2026-10-27",
        "start": None,
        "end": None,
        "note": "Ta’til",
    }
    assert staff_client.get(f"{BASE}999999/overrides/").status_code == 404


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ({"date": "2026-10-23"}, ("date", "in_past", "O‘tgan sanani tanlab bo‘lmaydi.")),
        ({"date": "2027-10-25"}, ("date", "out_of_range", "Ko‘pi bilan bir yil oldinga kiritish mumkin.")),
        (
            {"date": "2026-10-27", "start": "10:00"},
            (
                "end",
                "required",
                "Boshlanish va tugash vaqtini birga kiriting (ikkalasi bo‘sh bo‘lsa, dam olish kuni).",
            ),
        ),
        (
            {"date": "2026-10-27", "start": "14:00", "end": "10:00"},
            ("end", "invalid", "Tugash vaqti boshlanishidan keyin bo‘lsin."),
        ),
        ({"date": "2026-10-27", "note": "x" * 121}, ("note", "max_length", "120 belgidan oshmasin.")),
        ({"date": "27.10.2026"}, ("date", "invalid", "Noto‘g‘ri qiymat.")),
    ],
)
def test_override_validation(
    staff_client: APIClient, body: dict[str, Any], expected: tuple[str, str, str]
) -> None:
    professional = ProfessionalFactory.create()
    assert errors(staff_client.post(url(professional, "overrides/"), body)) == [expected]


def test_one_override_per_date(staff_client: APIClient) -> None:
    professional = ProfessionalFactory.create()
    assert staff_client.post(url(professional, "overrides/"), {"date": "2026-10-27"}).status_code == 201
    duplicate = staff_client.post(
        url(professional, "overrides/"), {"date": "2026-10-27", "start": "09:00", "end": "10:00"}
    )
    assert errors(duplicate) == [("date", "unique", "Bu sana uchun alohida jadval allaqachon bor.")]


# --- blocked periods ----------------------------------------------------------------------------------------


def test_blocks_list_create_delete(staff_client: APIClient, staff: StaffUser) -> None:
    professional = ProfessionalFactory.create()
    BlockedPeriodFactory.create(
        professional=professional, start=at(ANCHOR.date(), 8), end=at(ANCHOR.date(), 9)
    )  # ended this morning
    target = url(professional, "blocks/")
    response = staff_client.post(
        target,
        {
            "start": "2026-10-26T16:00:00+05:00",
            "end": "2026-10-26T16:30:00+05:00",
            "kind": "busy",
            "label": " ",
        },
    )
    assert response.status_code == 201, response.json()
    block = BlockedPeriod.objects.get(start=at(MONDAY, 16))
    assert response.json() == {
        "id": block.pk,
        "start": "2026-10-26T16:00:00+05:00",
        "end": "2026-10-26T16:30:00+05:00",
        "kind": "busy",
        "label": None,
        "created_by": {"id": staff.pk, "display_name": staff.full_name or staff.email},
        "created_at": timeutils.isoformat(ANCHOR),
    }
    leave = staff_client.post(
        target,
        {
            "start": "2026-10-27T00:00:00+05:00",
            "end": "2026-10-29T00:00:00+05:00",
            "kind": "off",
            "label": "Ta’til",
        },
    )
    assert leave.status_code == 201 and leave.json()["label"] == "Ta’til"
    listed = staff_client.get(target).json()["results"]
    assert [b["start"] for b in listed] == [
        "2026-10-24T08:00:00+05:00",
        "2026-10-26T16:00:00+05:00",
        "2026-10-27T00:00:00+05:00",
    ]
    ranged = staff_client.get(target, {"date_from": "2026-10-28", "date_to": "2026-10-28"}).json()["results"]
    assert [b["kind"] for b in ranged] == ["off"]  # the leave runs through the 28th
    assert staff_client.delete(f"{target}{block.pk}/").status_code == 204
    assert staff_client.delete(f"{target}{BlockedPeriodFactory.create().pk}/").status_code == 404
    created, deleted = audits("schedule.block_created"), audits("schedule.block_deleted")
    assert len(created) == 2 and len(deleted) == 1
    assert deleted[0].before == {
        "block_id": block.pk,
        "start": "2026-10-26T16:00:00+05:00",
        "end": "2026-10-26T16:30:00+05:00",
        "kind": "busy",
        "label": "",
    }


@pytest.mark.parametrize(
    ("change", "expected"),
    [
        (
            {"end": "2026-10-26T15:00:00+05:00"},
            ("end", "invalid", "Tugash vaqti boshlanishidan keyin bo‘lsin."),
        ),
        (
            {"start": "2026-10-24T10:00:00+05:00", "end": "2026-10-24T12:00:00+05:00"},
            ("end", "in_past", "Tugagan vaqtni band qilib bo‘lmaydi."),
        ),
        ({"end": "2027-10-28T16:00:00+05:00"}, ("end", "out_of_range", "Ko‘pi bilan 366 kunlik davr.")),
        ({"start": "2026-10-26T16:00:00"}, ("start", "invalid", "Noto‘g‘ri qiymat.")),
        ({"kind": "lunch"}, ("kind", "invalid_choice", "Ruxsat etilmagan qiymat.")),
        ({"label": "x" * 81}, ("label", "max_length", "80 belgidan oshmasin.")),
        (
            {"start": "2026-10-26T16:15:00+05:00", "end": "2026-10-26T17:00:00+05:00"},
            ("start", "overlap", "Bu vaqt boshqa band vaqt bilan kesishadi."),
        ),
    ],
)
def test_block_validation(
    staff_client: APIClient, change: dict[str, Any], expected: tuple[str, str, str]
) -> None:
    professional = ProfessionalFactory.create()
    BlockedPeriodFactory.create(professional=professional, start=at(MONDAY, 16), end=at(MONDAY, 16, 30))
    body = {
        "start": "2026-10-26T15:00:00+05:00",
        "end": "2026-10-26T15:30:00+05:00",
        "kind": "busy",
        **change,
    }
    assert errors(staff_client.post(url(professional, "blocks/"), body)) == [expected]
    assert BlockedPeriod.objects.count() == 1


def test_adjacent_blocks_do_not_overlap(staff_client: APIClient) -> None:
    professional = ProfessionalFactory.create()
    BlockedPeriodFactory.create(professional=professional, start=at(MONDAY, 16), end=at(MONDAY, 16, 30))
    body = {
        "start": "2026-10-26T16:30:00+05:00",
        "end": "2026-10-26T17:00:00+05:00",
        "kind": enums.BlockKind.OFF,
    }
    assert staff_client.post(url(professional, "blocks/"), body).status_code == 201


def test_a_lost_race_for_a_staff_account_is_a_unique_error(staff: StaffUser) -> None:
    """The serializer pre-checks the link; two requests that both pass it meet the one-to-one constraint, and
    the loser answers 400 ``staff_user_id`` ``unique`` (not a 500)."""
    location = LocationFactory.create()
    holder = ProfessionalFactory.create(staff_user=StaffUserFactory.create())
    data = {"name": "Malika", "role": "cosmetologist", "staff_user": holder.staff_user, "location": location}
    with pytest.raises(ValidationError) as caught:
        services.create_specialist(data, actor=staff)
    assert caught.value.get_codes() == {"staff_user_id": ["unique"]}
    assert Professional.objects.count() == 1
