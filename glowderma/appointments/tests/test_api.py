"""Contract §2.9 endpoints and the T10 admin create / reschedule / cancel over the seeded design data."""

from datetime import date, datetime, time
from typing import Any

import pytest
from rest_framework.test import APIClient

from glowderma.accounts import seeds as accounts_seeds
from glowderma.accounts.models import Customer, Device, StaffUser
from glowderma.accounts.tests.factories import CustomerFactory
from glowderma.appointments import seeds, selectors
from glowderma.appointments.models import Appointment
from glowderma.appointments.tests.conftest import ANCHOR
from glowderma.appointments.tests.factories import AppointmentFactory
from glowderma.core.models import AuditEntry, IdempotencyRecord, OutboxMessage
from glowderma.core.seeding import SeedContext, reset_sequences
from glowderma.core.timeutils import TASHKENT
from glowderma.professionals import seeds as professionals_seeds
from glowderma.professionals.models import Professional
from glowderma.professionals.tests.factories import ProfessionalServiceFactory, WorkingHoursFactory

pytestmark = pytest.mark.django_db

BASE = "/api/v1/admin/appointments/"
KEY = "6f1c2a0e-9b8d-4c37-8a1e-0123456789ab"
CONTEXT = SeedContext(anchor=ANCHOR, keep_open=True)


def at(day: str, clock: str) -> str:
    return f"{day}T{clock}:00+05:00"


@pytest.fixture
def seeded(monkeypatch: pytest.MonkeyPatch, clock: Any) -> None:
    monkeypatch.delenv("SEED_ADMIN_PASSWORD", raising=False)
    accounts_seeds.seed_accounts(CONTEXT)
    professionals_seeds.seed_professionals(CONTEXT)
    seeds.seed_appointments(CONTEXT)
    AuditEntry.objects.all()  # the replayed confirmations audited; the tests below count their own entries
    OutboxMessage.objects.all().delete()


def errors(response: Any) -> list[tuple[str | None, str, str]]:
    return [(e["field"], e["code"], e["message"]) for e in response.data["errors"]]


def test_day_schedule_equals_the_design(staff_client: APIClient, seeded: None) -> None:
    response = staff_client.get(BASE, {"date": "2026-10-26"})
    assert response.status_code == 200
    body = response.data
    assert body["date"] == "2026-10-26"
    assert [s["name"] for s in body["specialists"]] == ["Nodira Rahimova", "Jasur Aliyev", "Dilnoza Yusupova"]
    nodira, jasur, dilnoza = body["specialists"]
    assert nodira == {
        "id": 1,
        "name": "Nodira Rahimova",
        "role": "dermatologist",
        "working_hours": {"start": "09:00", "end": "18:00"},
        "blocks": [{"start": "16:00", "end": "16:30", "kind": "busy", "label": "Band qilingan"}],
    }
    assert jasur["blocks"] == [{"start": "13:00", "end": "14:00", "kind": "off", "label": None}]
    assert dilnoza["role"] == "cosmetologist" and dilnoza["blocks"] == []
    assert len(body["appointments"]) == 8
    assert [a["id"] for a in body["appointments"]] == [1, 4, 6, 2, 7, 3, 5, 8]  # by start
    assert body["appointments"][0] == {
        "id": 1,
        "specialist_id": 1,
        "customer": {"id": 8, "name": "Kamola Nazarova", "phone": "+998909741823"},
        "service_id": 1,
        "service_name": "Dermatolog konsultatsiyasi",
        "service_short_name": "Konsultatsiya",
        "duration_min": 30,
        "start": "2026-10-26T09:00:00+05:00",
        "end": "2026-10-26T09:30:00+05:00",
        "price": 150000,
        "status": "requested",
        "requested_at": "2026-10-24T08:41:00+05:00",
        "requested_via": "app",
    }
    sardor = next(a for a in body["appointments"] if a["id"] == 3)
    assert sardor["requested_via"] == "admin" and sardor["status"] == "confirmed"
    # a Sunday: nobody works, no appointments
    sunday = staff_client.get(BASE, {"date": "2026-10-25"}).data
    assert all(s["working_hours"] is None for s in sunday["specialists"]) and sunday["appointments"] == []


def test_day_defaults_to_today_in_tashkent_and_rejects_bad_dates(staff_client: APIClient, clock: Any) -> None:
    response = staff_client.get(BASE)
    assert response.status_code == 200 and response.data["date"] == "2026-10-24"
    bad = staff_client.get(BASE, {"date": "26.10.2026"})
    assert bad.status_code == 400 and errors(bad) == [("date", "invalid", "Noto‘g‘ri qiymat.")]


def test_pending_summary_at_the_anchor(staff_client: APIClient, seeded: None, clock: Any) -> None:
    response = staff_client.get(BASE + "pending-summary/")
    assert response.status_code == 200
    assert response.data == {"count": 3, "next_at": "2026-10-26T09:00:00+05:00"}
    # past requests no longer count (the selector takes the instant; the session would outlive no 12 h jump)
    assert selectors.pending_summary(datetime(2026, 10, 27, tzinfo=TASHKENT)) == {"count": 0, "next_at": None}
    assert selectors.upcoming_count(5) == 1  # Malika's card (design 03 §7.11)


def test_actions_require_an_idempotency_key(staff_client: APIClient, clock: Any) -> None:
    appointment = AppointmentFactory.create()
    response = staff_client.post(f"{BASE}{appointment.pk}/confirm/", {}, format="json")
    assert response.status_code == 400 and response.data["code"] == "idempotency_key_required"
    assert response.data["message"] == "Idempotency-Key sarlavhasi kerak."


def test_confirm_reject_attend_and_409(
    staff_client: APIClient, seeded: None, clock: Any, staff: StaffUser
) -> None:
    confirm = staff_client.post(f"{BASE}1/confirm/", {}, format="json", HTTP_IDEMPOTENCY_KEY=KEY)
    assert confirm.status_code == 200, confirm.data
    assert confirm.data["status"] == "confirmed" and confirm.data["id"] == 1
    assert set(confirm.data) == {
        "id", "specialist_id", "customer", "service_id", "service_name", "service_short_name", "duration_min",
        "start", "end", "price", "status", "requested_at", "requested_via",
    }  # fmt: skip
    message = OutboxMessage.objects.get(dedupe_key="push.appointment:1:confirmed")
    assert message.payload["customer_id"] == 8 and message.payload["action"] == "confirmed"
    assert AuditEntry.objects.get(action="appointment.confirmed", entity_id="1").actor_id == staff.pk

    replay = staff_client.post(f"{BASE}1/confirm/", {}, format="json", HTTP_IDEMPOTENCY_KEY=KEY)
    assert (
        replay.status_code == 200 and replay["Idempotent-Replayed"] == "true" and replay.data == confirm.data
    )
    assert IdempotencyRecord.objects.count() == 1

    again = staff_client.post(
        f"{BASE}1/confirm/", {}, format="json", HTTP_IDEMPOTENCY_KEY=KEY.replace("a", "b")
    )
    assert again.status_code == 409
    assert again.data == {
        "code": "invalid_transition",
        "message": "Bu amal ushbu holatda mumkin emas.",
        "errors": [],
        "request_id": again.data["request_id"],
    }
    assert IdempotencyRecord.objects.count() == 1  # errors are never stored

    attend = staff_client.post(
        f"{BASE}1/attend/", {}, format="json", HTTP_IDEMPOTENCY_KEY=KEY.replace("a", "c")
    )
    assert attend.status_code == 200 and attend.data["status"] == "attended"
    reject = staff_client.post(
        f"{BASE}4/reject/", {}, format="json", HTTP_IDEMPOTENCY_KEY=KEY.replace("a", "d")
    )
    assert reject.status_code == 200 and reject.data["status"] == "rejected"
    assert OutboxMessage.objects.filter(dedupe_key="push.appointment:4:rejected").exists()
    assert staff_client.get(BASE + "pending-summary/").data["count"] == 1


def test_unknown_action_and_unknown_id_are_404(staff_client: APIClient, clock: Any) -> None:
    appointment = AppointmentFactory.create()
    for path in (f"{BASE}{appointment.pk}/approve/", f"{BASE}999999/confirm/"):
        response = staff_client.post(path, {}, format="json", HTTP_IDEMPOTENCY_KEY=KEY)
        assert response.status_code == 404 and response.data["code"] == "not_found"


def test_admin_create_reschedule_cancel_flow(staff_client: APIClient, seeded: None, staff: StaffUser) -> None:
    body = {
        "specialist_id": 1,
        "service_id": 2,
        "phone": "+998901234567",  # Malika
        "start": at("2026-10-26", "10:00"),
        "note": "Telefon orqali",
    }
    created = staff_client.post(BASE, body, format="json", HTTP_IDEMPOTENCY_KEY=KEY)
    assert created.status_code == 201, created.data
    item = created.data
    assert item["status"] == "confirmed" and item["requested_via"] == "admin"
    assert item["customer"] == {"id": 5, "name": "Malika Karimova", "phone": "+998901234567"}
    assert (item["service_short_name"], item["duration_min"], item["price"]) == ("Qayta ko‘rik", 20, 100000)
    assert item["start"] == at("2026-10-26", "10:00") and item["end"] == at("2026-10-26", "10:20")
    assert item["requested_at"] == "2026-10-24T12:05:00+05:00"
    row = Appointment.objects.get(pk=item["id"])
    assert row.created_by == staff and row.staff_note == "Telefon orqali"
    assert [e.type for e in row.events.order_by("id")] == ["requested", "confirmed"]
    replay = staff_client.post(BASE, body, format="json", HTTP_IDEMPOTENCY_KEY=KEY)
    assert replay.status_code == 201 and replay["Idempotent-Replayed"] == "true"
    assert Appointment.objects.filter(professional_id=1, start=row.start).count() == 1

    by_id = staff_client.post(
        BASE,
        {"specialist_id": 2, "service_id": 1, "customer_id": 5, "start": at("2026-10-27", "11:00")},
        format="json",
    )
    assert by_id.status_code == 201 and by_id.data["price"] == 120000

    moved = staff_client.post(
        f"{BASE}{row.pk}/reschedule/", {"start": at("2026-10-26", "12:00")}, format="json"
    )
    assert moved.status_code == 200 and moved.data["start"] == at("2026-10-26", "12:00")
    assert moved.data["status"] == "confirmed" and moved.data["end"] == at("2026-10-26", "12:20")
    assert OutboxMessage.objects.filter(dedupe_key=f"push.appointment:{row.pk}:rescheduled:1").exists()
    to_jasur = staff_client.post(
        f"{BASE}{row.pk}/reschedule/", {"start": at("2026-10-26", "12:00"), "specialist_id": 2}, format="json"
    )
    assert (
        to_jasur.status_code == 200
        and to_jasur.data["specialist_id"] == 2
        and to_jasur.data["price"] == 90000
    )

    cancelled = staff_client.post(f"{BASE}{row.pk}/cancel/", {"reason": "Mijoz so‘radi"}, format="json")
    assert cancelled.status_code == 200 and cancelled.data["status"] == "cancelled"
    assert OutboxMessage.objects.filter(dedupe_key=f"push.appointment:{row.pk}:cancelled").exists()
    again = staff_client.post(f"{BASE}{row.pk}/cancel/", {}, format="json")
    assert again.status_code == 409 and again.data["code"] == "invalid_transition"
    assert set(AuditEntry.objects.filter(entity_id=str(row.pk)).values_list("action", flat=True)) == {
        "appointment.created",
        "appointment.rescheduled",
        "appointment.cancelled",
    }
    # the cancelled appointment is listed with its status; the slot is free again
    day = staff_client.get(BASE, {"date": "2026-10-26"}).data
    assert next(a for a in day["appointments"] if a["id"] == row.pk)["status"] == "cancelled"
    assert staff_client.post(BASE, body, format="json").status_code == 201


@pytest.mark.parametrize(
    "body,expected",
    [
        ({}, [
            ("specialist_id", "required", "Majburiy maydon."),
            ("service_id", "required", "Majburiy maydon."),
            ("start", "required", "Majburiy maydon."),
        ]),
        ({"specialist_id": 99, "service_id": 1, "customer_id": 5, "start": at("2026-10-26", "10:00")},
         [("specialist_id", "does_not_exist", "Mutaxassis topilmadi.")]),
        ({"specialist_id": 1, "service_id": 99, "customer_id": 5, "start": at("2026-10-26", "10:00")},
         [("service_id", "does_not_exist", "Xizmat topilmadi.")]),
        ({"specialist_id": 1, "service_id": 1, "start": at("2026-10-26", "10:00")},
         [("customer_id", "required", "Majburiy maydon.")]),
        ({"specialist_id": 1, "service_id": 1, "customer_id": 5, "phone": "+998901234567",
          "start": at("2026-10-26", "10:00")},
         [("customer_id", "invalid", "customer_id yoki phone dan faqat bittasini yuboring.")]),
        ({"specialist_id": 1, "service_id": 1, "customer_id": 999999, "start": at("2026-10-26", "10:00")},
         [("customer_id", "does_not_exist", "Mijoz topilmadi.")]),
        ({"specialist_id": 1, "service_id": 1, "phone": "998901234567", "start": at("2026-10-26", "10:00")},
         [("phone", "invalid", "Telefon raqamini to‘g‘ri kiriting.")]),
        ({"specialist_id": 1, "service_id": 1, "phone": "+998900000001", "start": at("2026-10-26", "10:00")},
         [("phone", "customer_not_found", "Bu raqam bilan mijoz topilmadi.")]),
        ({"specialist_id": 1, "service_id": 1, "customer_id": 5, "start": "2026-10-26T10:00:00"},
         [("start", "invalid", "Noto‘g‘ri qiymat.")]),
        ({"specialist_id": 1, "service_id": 3, "customer_id": 5, "start": at("2026-10-26", "10:00")},
         [("service_id", "does_not_exist", "Mutaxassis bu xizmatni ko‘rsatmaydi.")]),
        ({"specialist_id": 1, "service_id": 1, "customer_id": 5, "start": at("2026-10-26", "09:10")},
         [("start", "slot_unavailable", "Bu vaqt band.")]),
        ({"specialist_id": 1, "service_id": 1, "customer_id": 5, "start": at("2026-10-26", "16:00")},
         [("start", "slot_unavailable", "Bu vaqt band.")]),
        ({"specialist_id": 2, "service_id": 1, "customer_id": 5, "start": at("2026-10-26", "13:30")},
         [("start", "slot_unavailable", "Bu vaqt band.")]),
        ({"specialist_id": 1, "service_id": 1, "customer_id": 5, "start": at("2026-10-26", "18:00")},
         [("start", "outside_working_hours", "Mutaxassis bu vaqtda ishlamaydi.")]),
        ({"specialist_id": 1, "service_id": 1, "customer_id": 5, "start": at("2026-10-24", "09:00")},
         [("start", "in_past", "Kelajakdagi vaqtni tanlang.")]),
        ({"specialist_id": 1, "service_id": 1, "customer_id": 5, "start": at("2026-10-26", "10:02")},
         [("start", "invalid", "Vaqt 5 daqiqalik qadamda bo‘lsin.")]),
        ({"specialist_id": 1, "service_id": 1, "customer_id": 5, "start": at("2026-10-26", "10:00"),
          "note": "x" * 301},
         [("note", "max_length", "300 belgidan oshmasin.")]),
    ],
)  # fmt: skip
def test_create_validation_table(
    staff_client: APIClient, seeded: None, body: dict[str, Any], expected: list[Any]
) -> None:
    response = staff_client.post(BASE, body, format="json")
    assert response.status_code == 400, response.data
    assert response.data["code"] == "validation_error" and errors(response) == expected
    assert Appointment.objects.count() == 45


def test_create_refuses_deleted_customers_and_inactive_specialists(
    staff_client: APIClient, seeded: None
) -> None:
    deleted = Customer.objects.get(pk=427)
    response = staff_client.post(
        BASE,
        {"specialist_id": 1, "service_id": 1, "customer_id": deleted.pk, "start": at("2026-10-26", "10:00")},
        format="json",
    )
    assert errors(response) == [("customer_id", "does_not_exist", "Mijoz topilmadi.")]
    Professional.objects.filter(pk=1).update(is_active=False)
    response = staff_client.post(
        BASE,
        {"specialist_id": 1, "service_id": 1, "customer_id": 5, "start": at("2026-10-26", "10:00")},
        format="json",
    )
    assert errors(response) == [("specialist_id", "does_not_exist", "Mutaxassis topilmadi.")]
    assert [s["id"] for s in staff_client.get(BASE, {"date": "2026-10-26"}).data["specialists"]] == [2, 3]


def test_reschedule_validation_and_404(staff_client: APIClient, clock: Any) -> None:
    link = ProfessionalServiceFactory.create()
    WorkingHoursFactory.create(professional=link.professional, weekday=0)
    appointment = AppointmentFactory.create(
        professional=link.professional, service=link.service, start=datetime(2026, 10, 26, 9, tzinfo=TASHKENT)
    )
    missing = staff_client.post(f"{BASE}{appointment.pk}/reschedule/", {}, format="json")
    assert errors(missing) == [("start", "required", "Majburiy maydon.")]
    bad = staff_client.post(
        f"{BASE}{appointment.pk}/reschedule/",
        {"start": at("2026-10-26", "10:00"), "specialist_id": 999},
        format="json",
    )
    assert errors(bad) == [("specialist_id", "does_not_exist", "Mutaxassis topilmadi.")]
    assert (
        staff_client.post(
            f"{BASE}999999/reschedule/", {"start": at("2026-10-26", "10:00")}, format="json"
        ).status_code
        == 404
    )
    assert staff_client.post(f"{BASE}999999/cancel/", {}, format="json").status_code == 404
    long_reason = staff_client.post(f"{BASE}{appointment.pk}/cancel/", {"reason": "x" * 301}, format="json")
    assert errors(long_reason) == [("reason", "max_length", "300 belgidan oshmasin.")]


def test_day_item_renders_a_scrubbed_customer_name(staff_client: APIClient, clock: Any) -> None:
    customer = CustomerFactory.create(name="")
    AppointmentFactory.create(customer=customer, start=datetime(2026, 10, 26, 9, tzinfo=TASHKENT))
    item = staff_client.get(BASE, {"date": "2026-10-26"}).data["appointments"][0]
    assert item["customer"]["name"] == "" and item["customer"]["id"] == customer.pk


def test_seeded_confirmations_went_through_the_service(seeded: None) -> None:
    """Design 03 §7.11: the replayed ``confirm`` leaves events, audits and outbox pushes behind."""
    sardor = Appointment.objects.get(pk=3)
    confirmed = sardor.events.get(type="confirmed")
    pushable = Device.objects.filter(customer_id=11, is_active=True, push_enabled=True).exists()
    assert confirmed.data == {"push_sent": pushable} and confirmed.at == sardor.confirmed_at
    assert AuditEntry.objects.filter(action="appointment.confirmed").count() == 6 + 36
    assert AuditEntry.objects.filter(action="appointment.attended").count() == 11
    entry = AuditEntry.objects.get(action="appointment.confirmed", entity_id="3")
    assert entry.created_at == sardor.confirmed_at and entry.actor_id == 1
    assert not Appointment.objects.filter(start__date=date(2026, 10, 25)).exists()
    assert time(9, 0) <= Appointment.objects.get(pk=6).start.astimezone(TASHKENT).time()


def test_meta_lists_active_specialists_with_their_bookable_services(
    staff_client: APIClient, seeded: None
) -> None:
    response = staff_client.get(f"{BASE}meta/")
    assert response.status_code == 200, response.content
    body = response.json()
    assert body["slot_step_min"] == 5
    names = [specialist["name"] for specialist in body["specialists"]]
    assert names == ["Nodira Rahimova", "Jasur Aliyev", "Dilnoza Yusupova"]
    jasur = body["specialists"][1]
    assert jasur["role"] == "dermatologist"
    consultation = next(s for s in jasur["services"] if s["short_name"] == "Konsultatsiya")
    assert consultation == {
        "id": consultation["id"],
        "name": "Dermatolog konsultatsiyasi",
        "short_name": "Konsultatsiya",
        "duration_min": 30,
        "price": 120_000,
    }
    # Inactive links, inactive services and inactive specialists are not offered (the seeders insert explicit
    # ids, so re-sync the sequences before the factories add rows).
    reset_sequences()
    jasur_row = Professional.objects.get(name="Jasur Aliyev")
    link = ProfessionalServiceFactory.create(professional=jasur_row)
    link.service.is_active = False
    link.service.save()
    inactive = ProfessionalServiceFactory.create(professional=jasur_row, is_active=False)
    Professional.objects.filter(name="Dilnoza Yusupova").update(is_active=False)
    body = staff_client.get(f"{BASE}meta/").json()
    assert [s["name"] for s in body["specialists"]] == ["Nodira Rahimova", "Jasur Aliyev"]
    offered = {s["id"] for specialist in body["specialists"] for s in specialist["services"]}
    assert link.service_id not in offered and inactive.service_id not in offered


def test_meta_runs_in_a_bounded_number_of_queries(
    staff_client: APIClient, seeded: None, django_assert_max_num_queries: Any
) -> None:
    with django_assert_max_num_queries(8):
        assert staff_client.get(f"{BASE}meta/").status_code == 200
