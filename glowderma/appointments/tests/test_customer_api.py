"""Customer appointment routes (customer contract §2.11; plan §4 P3): specialists, services, free slots,
booking (validation rows in order, races), own appointments and the customer cancel."""

import threading
import uuid
from datetime import date, datetime, time, timedelta
from typing import Any

import psycopg
import pytest
from django.db import IntegrityError, OperationalError, connection, transaction
from django.test import override_settings
from rest_framework.test import APIClient

from glowderma.accounts.models import Customer
from glowderma.accounts.tests.factories import CustomerFactory
from glowderma.appointments import selectors, services
from glowderma.appointments.models import Appointment, AppointmentEvent
from glowderma.appointments.tests.factories import AppointmentFactory
from glowderma.core.api.exceptions import AuthFailed, BookingLimit, CustomerError, SlotTaken
from glowderma.core.models import AuditEntry
from glowderma.core.timeutils import TASHKENT
from glowderma.professionals.models import ProfessionalService
from glowderma.professionals.tests.factories import (
    BlockedPeriodFactory,
    LocationFactory,
    ProfessionalFactory,
    ProfessionalServiceFactory,
    ServiceFactory,
    WorkingHoursFactory,
    WorkingHoursOverrideFactory,
)
from glowderma.site_config.models import SiteSettings

pytestmark = pytest.mark.django_db

MONDAY = date(2026, 11, 2)
TUESDAY = date(2026, 11, 3)
ANCHOR = datetime(2026, 11, 2, 8, 0, tzinfo=TASHKENT)  # Monday 08:00 Tashkent


def at(day: date, hour: int, minute: int = 0) -> datetime:
    return datetime.combine(day, time(hour, minute), tzinfo=TASHKENT)


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest) -> None:
    """Pin the clock — after the customer's access token was issued at the real time (PyJWT checks ``iat``
    and ``exp`` against the real clock)."""
    if "customer_client" in request.fixturenames:
        request.getfixturevalue("customer_client")
    monkeypatch.setattr("glowderma.core.timeutils.now", lambda: ANCHOR)


def setup_link(*, step: int = 30, weekday: int = 0, start: time = time(9, 0)) -> ProfessionalService:
    location = LocationFactory.create(
        name_uz="Glow Derma Yunusobod",
        name_ru="Глоу Дерма Юнусабад",
        latitude="41.360000",
        longitude="69.280000",
    )
    professional = ProfessionalFactory.create(
        location=location,
        name="Nodira Rahimova",
        slot_step_min=step,
        bio_uz="Tajribali dermatolog",
        bio_ru="Опытный дерматолог",
        experience_years=8,
    )
    service = ServiceFactory.create(
        name_uz="Dermatolog konsultatsiyasi",
        name_ru="Консультация дерматолога",
        short_name_uz="Konsultatsiya",
        default_duration_min=30,
    )
    WorkingHoursFactory.create(
        professional=professional,
        weekday=weekday,
        start=start,
        end=time(18, 0),
        break_start=time(13, 0),
        break_end=time(14, 0),
    )
    return ProfessionalServiceFactory.create(
        professional=professional, service=service, price=150_000, duration_min=30
    )


def book(client: APIClient, body: dict[str, Any], key: str | None = None) -> Any:
    return client.post(
        "/api/v1/appointments/", body, format="json", HTTP_IDEMPOTENCY_KEY=key or str(uuid.uuid4())
    )


def body_for(link: ProfessionalService, start: datetime | str, **extra: Any) -> dict[str, Any]:
    value = start if isinstance(start, str) else start.isoformat()
    return {"specialist_id": link.professional_id, "service_id": link.service_id, "start": value, **extra}


def field_errors(response: Any) -> list[tuple[str, str, str]]:
    assert response.status_code == 400, response.content
    return [(e["field"], e["code"], e["message"]) for e in response.json()["errors"]]


# --- catalogue --------------------------------------------------------------------------------------------


def test_specialists_list_and_detail(public_client: APIClient) -> None:
    link = setup_link()
    inactive = ProfessionalFactory.create(is_active=False)
    ProfessionalServiceFactory.create(professional=link.professional, is_active=False)
    body = public_client.get("/api/v1/specialists/").json()
    assert [row["id"] for row in body["results"]] == [link.professional_id]
    specialist = body["results"][0]
    assert specialist == {
        "id": link.professional_id,
        "name": "Nodira Rahimova",
        "role": "dermatologist",
        "role_label": "Dermatolog",
        "photo_url": None,
        "bio": "Tajribali dermatolog",
        "experience_years": 8,
        "location": {
            "id": link.professional.location_id,
            "name": "Glow Derma Yunusobod",
            "address": "Toshkent, Chilonzor tumani",
            "phone": "+998712000000",
            "latitude": 41.36,
            "longitude": 69.28,
        },
        "services": [
            {
                "id": link.service_id,
                "name": "Dermatolog konsultatsiyasi",
                "short_name": "Konsultatsiya",
                "duration_min": 30,
                "price": 150_000,
            }
        ],
        "slot_step_min": 30,
    }
    ru = public_client.get(f"/api/v1/specialists/{link.professional_id}/", HTTP_ACCEPT_LANGUAGE="ru").json()
    assert (ru["bio"], ru["role_label"], ru["services"][0]["name"]) == (
        "Опытный дерматолог",
        "Дерматолог",
        "Консультация дерматолога",
    )
    assert ru["services"][0]["short_name"] == "Konsultatsiya"  # no ru text → uz
    assert public_client.get(f"/api/v1/specialists/{inactive.pk}/").status_code == 404
    assert public_client.get("/api/v1/specialists/999999/").json()["code"] == "not_found"


def test_services_list_with_price_range(public_client: APIClient) -> None:
    link = setup_link()
    other = ProfessionalFactory.create()
    ProfessionalServiceFactory.create(professional=other, service=link.service, price=200_000)
    lonely = ServiceFactory.create()  # offered by nobody
    ProfessionalServiceFactory.create(
        professional=ProfessionalFactory.create(is_active=False), service=lonely
    )
    body = public_client.get("/api/v1/services/").json()
    assert body == {
        "results": [
            {
                "id": link.service_id,
                "name": "Dermatolog konsultatsiyasi",
                "short_name": "Konsultatsiya",
                "description": "",
                "duration_min": 30,
                "price_from": 150_000,
                "price_to": 200_000,
                "specialist_ids": [link.professional_id, other.pk],
            }
        ]
    }


# --- free slots -------------------------------------------------------------------------------------------


def slots(client: APIClient, link: ProfessionalService, day: date, **params: Any) -> Any:
    query = {"date": day.isoformat(), "service_id": link.service_id, **params}
    return client.get(f"/api/v1/specialists/{link.professional_id}/slots/", query)


def starts(response: Any) -> list[str]:
    assert response.status_code == 200, response.content
    return [slot["start"] for slot in response.json()["slots"]]


def test_slots_weekly_hours_with_break_and_lead_time(clock: None, public_client: APIClient) -> None:
    link = setup_link()
    response = slots(public_client, link, MONDAY)
    body = response.json()
    assert {k: v for k, v in body.items() if k != "slots"} == {
        "date": "2026-11-02",
        "specialist_id": link.professional_id,
        "service_id": link.service_id,
        "duration_min": 30,
        "price": 150_000,
        "slot_step_min": 30,
        "working_hours": {"start": "09:00", "end": "18:00"},
    }
    times = starts(response)
    # now 08:00 + 60 min lead → from 09:00; the 13:00–14:00 break removed; the last slot ends at 18:00
    assert times[0] == "09:00" and times[-1] == "17:30"
    assert "12:30" in times and "13:00" not in times and "13:30" not in times and "14:00" in times
    assert body["slots"][0] == {"start": "09:00", "end": "09:30", "start_at": "2026-11-02T09:00:00+05:00"}


def test_slots_lead_time_drops_the_near_ones(
    monkeypatch: pytest.MonkeyPatch, public_client: APIClient
) -> None:
    link = setup_link()
    monkeypatch.setattr("glowderma.core.timeutils.now", lambda: at(MONDAY, 9, 10))
    assert starts(slots(public_client, link, MONDAY))[0] == "10:30"


def test_slots_override_day_off_and_no_hours(clock: None, public_client: APIClient) -> None:
    link = setup_link()
    WorkingHoursOverrideFactory.create(
        professional=link.professional, date=MONDAY, start=time(10, 0), end=time(12, 0)
    )
    assert starts(slots(public_client, link, MONDAY)) == ["10:00", "10:30", "11:00", "11:30"]  # no break
    WorkingHoursOverrideFactory.create(
        professional=link.professional, date=TUESDAY + timedelta(days=6), start=None, end=None
    )
    off = slots(public_client, link, TUESDAY + timedelta(days=6)).json()
    assert off["working_hours"] is None and off["slots"] == []
    # Tuesday has no weekly hours
    assert slots(public_client, link, TUESDAY).json()["working_hours"] is None


def test_slots_grid_is_relative_to_the_start_of_the_hours(clock: None, public_client: APIClient) -> None:
    link = setup_link(start=time(9, 15), step=60)
    times = starts(slots(public_client, link, MONDAY))
    assert times[:4] == ["09:15", "10:15", "11:15", "12:15"] and "13:15" not in times


def test_slots_blocked_period_across_midnight(clock: None, public_client: APIClient) -> None:
    link = setup_link()
    BlockedPeriodFactory.create(
        professional=link.professional, start=at(MONDAY - timedelta(days=1), 22), end=at(MONDAY, 10, 0)
    )
    assert starts(slots(public_client, link, MONDAY))[0] == "10:00"
    # and the same block seen from its first day ends that day
    BlockedPeriodFactory.create(
        professional=link.professional, start=at(MONDAY, 17, 0), end=at(TUESDAY, 9, 0)
    )
    assert starts(slots(public_client, link, MONDAY))[-1] == "16:30"


def test_slots_open_appointments_hold_and_rejected_free(clock: None, public_client: APIClient) -> None:
    link = setup_link()
    AppointmentFactory.create(professional=link.professional, service=link.service, start=at(MONDAY, 10))
    AppointmentFactory.create(
        professional=link.professional,
        service=link.service,
        start=at(MONDAY, 11),
        status="confirmed",
        confirmed_at=ANCHOR,
    )
    AppointmentFactory.create(
        professional=link.professional,
        service=link.service,
        start=at(MONDAY, 12),
        status="rejected",
        rejected_at=ANCHOR,
    )
    times = starts(slots(public_client, link, MONDAY))
    assert "10:00" not in times and "11:00" not in times and "12:00" in times


def test_slots_validation(clock: None, public_client: APIClient) -> None:
    link = setup_link()
    url = f"/api/v1/specialists/{link.professional_id}/slots/"
    assert {(f, c) for f, c, _m in field_errors(public_client.get(url))} == {
        ("date", "required"),
        ("service_id", "required"),
    }
    bad_date = public_client.get(url, {"date": "02.11.2026", "service_id": str(link.service_id)})
    assert field_errors(bad_date)[0][:2] == ("date", "invalid")
    for day in (MONDAY - timedelta(days=1), MONDAY + timedelta(days=31)):
        errors = field_errors(slots(public_client, link, day))
        assert errors == [("date", "out_of_range", "Faqat 30 kun ichidagi sanani tanlash mumkin.")]
    assert starts(slots(public_client, link, MONDAY + timedelta(days=28))) != []  # day 30 is still in range
    other = ServiceFactory.create()
    errors = field_errors(slots(public_client, link, MONDAY, service_id=other.pk))
    assert errors == [("service_id", "does_not_exist", "Mutaxassis bu xizmatni ko‘rsatmaydi.")]
    en = public_client.get(
        url, {"date": MONDAY.isoformat(), "service_id": str(other.pk)}, HTTP_ACCEPT_LANGUAGE="en"
    )
    assert en.json()["errors"][0]["message"] == "The specialist does not offer this service."
    inactive = ProfessionalFactory.create(is_active=False)
    assert public_client.get(f"/api/v1/specialists/{inactive.pk}/slots/").status_code == 404


# --- booking ----------------------------------------------------------------------------------------------


def test_booking_happy_path(clock: None, customer: Customer, customer_client: APIClient) -> None:
    link = setup_link()
    response = book(customer_client, body_for(link, at(MONDAY, 10), note="Birinchi tashrif"))
    assert response.status_code == 201, response.content
    body = response.json()
    appointment = Appointment.objects.get()
    assert body == {
        "id": appointment.pk,
        "status": "requested",
        "status_label": "Tasdiqlanishi kutilmoqda",
        "specialist": {
            "id": link.professional_id,
            "name": "Nodira Rahimova",
            "role": "dermatologist",
            "role_label": "Dermatolog",
            "photo_url": None,
        },
        "service": {
            "id": link.service_id,
            "name": "Dermatolog konsultatsiyasi",
            "short_name": "Konsultatsiya",
        },
        "location": {
            "name": "Glow Derma Yunusobod",
            "address": "Toshkent, Chilonzor tumani",
            "phone": "+998712000000",
        },
        "start": "2026-11-02T10:00:00+05:00",
        "end": "2026-11-02T10:30:00+05:00",
        "duration_min": 30,
        "price": 150_000,
        "note": "Birinchi tashrif",
        "requested_at": "2026-11-02T08:00:00+05:00",
        "confirmed_at": None,
        "cancelled_at": None,
        "cancelled_by": None,
        "can_cancel": True,
    }
    assert (appointment.requested_via, appointment.created_by_id, appointment.customer_id) == (
        "app",
        None,
        customer.pk,
    )
    event = AppointmentEvent.objects.get(appointment=appointment)
    assert (event.type, event.source, event.actor_id) == ("requested", "customer", None)
    assert event.data == {
        "via": "app",
        "start": "2026-11-02T10:00:00+05:00",
        "end": "2026-11-02T10:30:00+05:00",
        "service_id": link.service_id,
        "professional_id": link.professional_id,
        "price": 150_000,
        "duration_min": 30,
    }
    entry = AuditEntry.objects.get(action="appointment.requested")
    assert (entry.actor_type, entry.actor_id) == ("customer", customer.pk)
    from glowderma.core.models import OutboxMessage

    assert not OutboxMessage.objects.filter(topic="push.appointment").exists()  # no push


def test_booking_requires_an_idempotency_key_and_replays(clock: None, customer_client: APIClient) -> None:
    link = setup_link()
    response = customer_client.post("/api/v1/appointments/", body_for(link, at(MONDAY, 10)), format="json")
    assert response.status_code == 400 and response.json()["code"] == "idempotency_key_required"
    key = str(uuid.uuid4())
    first = book(customer_client, body_for(link, at(MONDAY, 10)), key)
    again = book(customer_client, body_for(link, at(MONDAY, 10)), key)
    assert first.status_code == again.status_code == 201
    assert again["Idempotent-Replayed"] == "true" and Appointment.objects.count() == 1


@pytest.mark.parametrize(
    ("change", "expected"),
    [
        ({"specialist_id": 999_999}, ("specialist_id", "does_not_exist", "Mutaxassis topilmadi.")),
        ({"service_id": 999_999}, ("service_id", "does_not_exist", "Mutaxassis bu xizmatni ko‘rsatmaydi.")),
        ({"start": "2026-11-02T10:00:00"}, ("start", "invalid", "Noto‘g‘ri qiymat.")),
        ({"start": "2026-11-02T08:30:00+05:00"}, ("start", "in_past", "Kamida 60 daqiqa oldin yoziling.")),
        (
            {"start": "2026-12-04T10:00:00+05:00"},
            ("start", "out_of_range", "Faqat 30 kun ichidagi sanani tanlash mumkin."),
        ),
        ({"start": "2026-11-02T10:10:00+05:00"}, ("start", "invalid", "Vaqt 30 daqiqalik qadamda bo‘lsin.")),
        ({"start": "2026-11-02T10:00:30+05:00"}, ("start", "invalid", "Vaqt 30 daqiqalik qadamda bo‘lsin.")),
        (
            {"start": "2026-11-02T17:45:00+05:00"},
            ("start", "invalid", "Vaqt 30 daqiqalik qadamda bo‘lsin."),
        ),
        (
            {"start": "2026-11-02T18:00:00+05:00"},
            ("start", "outside_working_hours", "Mutaxassis bu vaqtda ishlamaydi."),
        ),
        (
            {"start": "2026-11-03T10:00:00+05:00"},
            ("start", "outside_working_hours", "Mutaxassis bu vaqtda ishlamaydi."),
        ),
        ({"start": "2026-11-02T13:00:00+05:00"}, ("start", "slot_unavailable", "Bu vaqt band.")),
        ({"note": "x" * 301}, ("note", "max_length", "300 belgidan oshmasin.")),
    ],
)
def test_booking_validation_rows(
    clock: None, customer_client: APIClient, change: dict[str, Any], expected: tuple[str, str, str]
) -> None:
    link = setup_link()
    response = book(customer_client, {**body_for(link, at(MONDAY, 10)), **change})
    assert field_errors(response) == [expected]
    assert not Appointment.objects.exists()


def test_booking_validation_messages_in_ru(clock: None, customer_client: APIClient) -> None:
    link = setup_link()
    response = customer_client.post(
        "/api/v1/appointments/",
        body_for(link, "2026-11-02T10:10:00+05:00"),
        format="json",
        HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()),
        HTTP_ACCEPT_LANGUAGE="ru",
    )
    assert response.json()["errors"][0]["message"] == "Время должно быть кратно 30 минутам."


def test_booking_busy_slot_and_own_overlap(
    clock: None, customer: Customer, customer_client: APIClient
) -> None:
    link = setup_link()
    AppointmentFactory.create(professional=link.professional, service=link.service, start=at(MONDAY, 10))
    assert field_errors(book(customer_client, body_for(link, at(MONDAY, 10)))) == [
        ("start", "slot_unavailable", "Bu vaqt band.")
    ]
    BlockedPeriodFactory.create(professional=link.professional, start=at(MONDAY, 15), end=at(MONDAY, 15, 45))
    assert field_errors(book(customer_client, body_for(link, at(MONDAY, 15, 30))))[0][1] == "slot_unavailable"
    AppointmentFactory.create(customer=customer, start=at(MONDAY, 11))  # another specialist, same customer
    assert field_errors(book(customer_client, body_for(link, at(MONDAY, 11)))) == [
        ("start", "slot_unavailable", "Sizda bu vaqtda boshqa qabul bor.")
    ]


def test_booking_consent_and_limit_come_before_the_start_checks(
    clock: None, customer: Customer, customer_client: APIClient
) -> None:
    link = setup_link()
    SiteSettings.objects.update_or_create(
        pk=1,
        defaults={"terms_version": "2026-10-01", "terms_uz": "Shartlar", "privacy_policy_uz": "Maxfiylik"},
    )
    Customer.objects.filter(pk=customer.pk).update(terms_version="2026-01-01", consent_at=ANCHOR)
    response = book(customer_client, body_for(link, "2026-11-02T10:10:00"))  # naive and off-grid
    assert response.status_code == 409
    assert response.json() == {
        "code": "terms_update_required",
        "message": "Foydalanish shartlarining yangi tahririni qabul qiling.",
        "errors": [],
        "details": {"current_version": "2026-10-01"},
        "request_id": response.json()["request_id"],
    }
    Customer.objects.filter(pk=customer.pk).update(terms_version="2026-10-01")
    for hour in (10, 11, 12):
        AppointmentFactory.create(customer=customer, start=at(TUESDAY, hour))
    # a past open appointment and a cancelled one do not count
    AppointmentFactory.create(customer=customer, start=at(MONDAY, 7))
    response = book(customer_client, body_for(link, "2026-11-02T10:10:00"))
    assert response.status_code == 409
    assert response.json()["code"] == "booking_limit"
    assert response.json()["details"] == {"limit": 3}
    assert (
        response.json()["message"]
        == "Sizda 3 ta faol qabul bor. Yangisini olishdan oldin birini bekor qiling."
    )


def test_unpublished_terms_do_not_block_booking(clock: None, customer_client: APIClient) -> None:
    link = setup_link()
    SiteSettings.objects.update_or_create(pk=1, defaults={"terms_version": "2026-10-01", "terms_uz": ""})
    assert book(customer_client, body_for(link, at(MONDAY, 10))).status_code == 201


@pytest.mark.django_db(transaction=True)
def test_two_customers_book_the_same_slot(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("glowderma.core.timeutils.now", lambda: ANCHOR)
    link = setup_link()
    customers = [CustomerFactory.create(), CustomerFactory.create()]
    barrier = threading.Barrier(2, timeout=30)
    monkeypatch.setattr(services, "_before_write", lambda: barrier.wait())
    outcomes: dict[int, Any] = {}

    def run(index: int) -> None:
        try:
            outcomes[index] = services.request_by_customer(
                customers[index].pk, link.professional_id, link.service_id, at(MONDAY, 10)
            )
        except Exception as exc:  # noqa: BLE001 - the outcome is the assertion
            outcomes[index] = exc
        finally:
            connection.close()

    threads = [threading.Thread(target=run, args=(index,)) for index in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=40)
    results = sorted(outcomes.values(), key=lambda value: isinstance(value, Exception))
    assert isinstance(results[0], Appointment) and isinstance(results[1], SlotTaken), outcomes
    assert results[1].get_codes() == "slot_taken"
    assert Appointment.objects.count() == 1 and AppointmentEvent.objects.count() == 1
    assert AuditEntry.objects.filter(action="appointment.requested").count() == 1


def test_a_deadlock_is_the_same_lost_race(
    clock: None, customer: Customer, monkeypatch: pytest.MonkeyPatch
) -> None:
    link = setup_link()
    deadlock = OperationalError("deadlock detected")
    deadlock.__cause__ = psycopg.errors.DeadlockDetected("deadlock detected")

    def raise_deadlock(*args: Any, **kwargs: Any) -> Any:
        raise deadlock

    monkeypatch.setattr(Appointment.objects, "create", raise_deadlock)
    with pytest.raises(SlotTaken):
        services.request_by_customer(customer.pk, link.professional_id, link.service_id, at(MONDAY, 10))
    other = OperationalError("server closed the connection")
    other.__cause__ = psycopg.OperationalError("closed")

    def raise_other(*args: Any, **kwargs: Any) -> Any:
        raise other

    monkeypatch.setattr(Appointment.objects, "create", raise_other)
    with pytest.raises(OperationalError):
        services.request_by_customer(customer.pk, link.professional_id, link.service_id, at(MONDAY, 10))


def test_slot_taken_through_the_api(
    clock: None, customer_client: APIClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    link = setup_link()

    def someone_else_wins() -> None:
        AppointmentFactory.create(professional=link.professional, service=link.service, start=at(MONDAY, 10))

    monkeypatch.setattr(services, "_before_write", someone_else_wins)
    response = book(customer_client, body_for(link, at(MONDAY, 10)))
    assert response.status_code == 409
    assert response.json()["code"] == "slot_taken"
    assert response.json()["message"] == "Bu vaqt band bo‘lib qoldi. Boshqa vaqtni tanlang."
    # nothing of the request was written (the winner's row was rolled back with the request too)
    assert not AuditEntry.objects.filter(action="appointment.requested").exists()


@pytest.mark.django_db(transaction=True)
def test_one_customer_books_two_slots_concurrently_at_the_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("glowderma.core.timeutils.now", lambda: ANCHOR)
    link = setup_link()
    customer = CustomerFactory.create()
    for hour in (10, 11):
        AppointmentFactory.create(customer=customer, start=at(TUESDAY, hour))
    first_in = threading.Event()
    release = threading.Event()

    def hold_the_first() -> None:
        if not first_in.is_set():
            first_in.set()
            release.wait(10)

    monkeypatch.setattr(services, "_before_write", hold_the_first)
    outcomes: dict[int, Any] = {}

    def run(index: int, hour: int) -> None:
        try:
            outcomes[index] = services.request_by_customer(
                customer.pk, link.professional_id, link.service_id, at(MONDAY, hour)
            )
        except Exception as exc:  # noqa: BLE001 - the outcome is the assertion
            outcomes[index] = exc
        finally:
            connection.close()

    first = threading.Thread(target=run, args=(0, 10))
    first.start()
    assert first_in.wait(10)
    second = threading.Thread(target=run, args=(1, 15))
    second.start()
    second.join(timeout=1)
    assert second.is_alive()  # blocked on the customer lock
    release.set()
    first.join(timeout=20)
    second.join(timeout=20)
    assert isinstance(outcomes[0], Appointment)
    assert isinstance(outcomes[1], BookingLimit) and outcomes[1].details == {"limit": 3}
    assert Appointment.objects.filter(customer=customer).count() == 3


# --- own appointments and cancel --------------------------------------------------------------------------


def test_my_appointments_scopes_horizon_and_ownership(
    clock: None, customer: Customer, customer_client: APIClient
) -> None:
    upcoming_late = AppointmentFactory.create(customer=customer, start=at(TUESDAY, 12))
    upcoming_soon = AppointmentFactory.create(
        customer=customer, start=at(TUESDAY, 10), status="confirmed", confirmed_at=ANCHOR
    )
    past = AppointmentFactory.create(
        customer=customer, start=at(MONDAY, 7), status="attended", confirmed_at=ANCHOR, attended_at=ANCHOR
    )
    rejected_future = AppointmentFactory.create(
        customer=customer, start=at(TUESDAY, 15), status="rejected", rejected_at=ANCHOR
    )
    foreign = AppointmentFactory.create(start=at(TUESDAY, 9))
    body = customer_client.get("/api/v1/me/appointments/").json()
    assert [row["id"] for row in body["results"]] == [upcoming_soon.pk, upcoming_late.pk]
    assert body["page_size"] == 20 and body["count"] == 2
    past_body = customer_client.get("/api/v1/me/appointments/", {"scope": "past"}).json()
    assert [row["id"] for row in past_body["results"]] == [rejected_future.pk, past.pk]
    assert past_body["results"][0]["status_label"] == "Rad etildi"
    assert past_body["results"][0]["can_cancel"] is False
    bad = customer_client.get("/api/v1/me/appointments/", {"scope": "all"})
    assert field_errors(bad)[0][:2] == ("scope", "invalid_choice")
    assert customer_client.get(f"/api/v1/me/appointments/{upcoming_late.pk}/").status_code == 200
    assert customer_client.get(f"/api/v1/me/appointments/{foreign.pk}/").status_code == 404
    # history horizon: rows created before it belong to a previous holder of the number
    Appointment.objects.filter(pk=upcoming_late.pk).update(created_at=at(MONDAY - timedelta(days=30), 9))
    Customer.objects.filter(pk=customer.pk).update(history_from=at(MONDAY - timedelta(days=1), 0))
    body = customer_client.get("/api/v1/me/appointments/").json()
    assert [row["id"] for row in body["results"]] == [upcoming_soon.pk]
    assert customer_client.get(f"/api/v1/me/appointments/{upcoming_late.pk}/").status_code == 404


def cancel(client: APIClient, pk: int, reason: str | None = None) -> Any:
    return client.post(
        f"/api/v1/appointments/{pk}/cancel/",
        {"reason": reason},
        format="json",
        HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()),
    )


def test_customer_cancel_frees_the_slot(
    clock: None, customer: Customer, customer_client: APIClient, public_client: APIClient
) -> None:
    link = setup_link()
    booked = book(customer_client, body_for(link, at(MONDAY, 10))).json()
    assert "10:00" not in starts(slots(public_client, link, MONDAY))
    response = cancel(customer_client, booked["id"], "Rejalar o‘zgardi")
    assert response.status_code == 200, response.content
    body = response.json()
    assert (body["status"], body["status_label"], body["cancelled_by"], body["can_cancel"]) == (
        "cancelled",
        "Bekor qilindi",
        "customer",
        False,
    )
    assert body["cancelled_at"] == "2026-11-02T08:00:00+05:00"
    assert "10:00" in starts(slots(public_client, link, MONDAY))
    event = AppointmentEvent.objects.get(appointment_id=booked["id"], type="cancelled")
    assert (event.source, event.actor_id) == ("customer", None)
    assert event.data == {"by": "customer", "reason": "Rejalar o‘zgardi", "push_sent": False}
    entry = AuditEntry.objects.get(action="appointment.cancelled")
    assert entry.actor_type == "customer" and (entry.after or {})["by"] == "customer"
    from glowderma.core.models import OutboxMessage

    assert not OutboxMessage.objects.filter(topic="push.appointment").exists()


def test_cancel_refusals_and_ownership(clock: None, customer: Customer, customer_client: APIClient) -> None:
    rejected = AppointmentFactory.create(
        customer=customer, start=at(TUESDAY, 10), status="rejected", rejected_at=ANCHOR
    )
    response = cancel(customer_client, rejected.pk)
    assert response.status_code == 409
    assert (response.json()["code"], response.json()["message"]) == (
        "not_cancellable",
        "Bu qabulni bekor qilib bo‘lmaydi.",
    )
    started = AppointmentFactory.create(customer=customer, start=at(MONDAY, 7, 30))  # already began
    assert cancel(customer_client, started.pk).status_code == 409
    foreign = AppointmentFactory.create(start=at(TUESDAY, 11))
    assert cancel(customer_client, foreign.pk).status_code == 404
    assert cancel(customer_client, 999_999).status_code == 404
    soon = AppointmentFactory.create(customer=customer, start=at(MONDAY, 9))
    with override_settings(BOOKING_CANCEL_MIN_LEAD_MINUTES=120):
        assert cancel(customer_client, soon.pk).status_code == 409
    assert cancel(customer_client, soon.pk).status_code == 200
    assert Appointment.objects.get(pk=foreign.pk).status == "requested"


def test_account_deletion_skips_only_the_lead_rule(clock: None, customer: Customer) -> None:
    soon = AppointmentFactory.create(customer=customer, start=at(MONDAY, 9))
    with override_settings(BOOKING_CANCEL_MIN_LEAD_MINUTES=120):
        done = services.cancel_by_customer(soon.pk, customer.pk, reason=services.ACCOUNT_DELETED_REASON)
    assert done.status == "cancelled"
    assert done.events.get(type="cancelled").data["reason"] == "account_deleted"
    # C§2.2.3 step 3 cancels open *future* appointments: one that already began is not cancellable
    started = AppointmentFactory.create(customer=customer, start=at(MONDAY, 7, 30))
    with pytest.raises(CustomerError) as refused:
        services.cancel_by_customer(started.pk, customer.pk, reason=services.ACCOUNT_DELETED_REASON)
    assert refused.value.get_codes() == "not_cancellable"
    assert Appointment.objects.get(pk=started.pk).status == "requested"


def test_the_account_deleted_flag_is_server_side(clock: None, customer: Customer) -> None:
    soon = AppointmentFactory.create(customer=customer, start=at(MONDAY, 9))
    with override_settings(BOOKING_CANCEL_MIN_LEAD_MINUTES=120), pytest.raises(CustomerError) as refused:
        services.cancel_by_customer(
            soon.pk, customer.pk, reason=services.ACCOUNT_DELETED_REASON, account_deleted=False
        )
    assert refused.value.get_codes() == "not_cancellable"
    with override_settings(BOOKING_CANCEL_MIN_LEAD_MINUTES=120):
        done = services.cancel_by_customer(soon.pk, customer.pk, account_deleted=True)
    assert done.status == "cancelled"


@pytest.mark.parametrize("reason", ["account_deleted", " account_deleted "])
def test_a_customer_cannot_claim_the_account_deleted_reason(
    clock: None, customer: Customer, customer_client: APIClient, reason: str
) -> None:
    started = AppointmentFactory.create(customer=customer, start=at(MONDAY, 7, 30))  # began at 07:30
    soon = AppointmentFactory.create(customer=customer, start=at(MONDAY, 9))
    assert cancel(customer_client, started.pk).status_code == 409
    response = cancel(customer_client, started.pk, reason)
    assert field_errors(response) == [("reason", "invalid", "Noto‘g‘ri qiymat.")]
    with override_settings(BOOKING_CANCEL_MIN_LEAD_MINUTES=120):
        assert field_errors(cancel(customer_client, soon.pk, reason))[0][:2] == ("reason", "invalid")
        assert cancel(customer_client, soon.pk, "account deleted soon").status_code == 409
    assert set(Appointment.objects.values_list("status", flat=True)) == {"requested"}
    assert not AppointmentEvent.objects.filter(type="cancelled").exists()
    assert not AuditEntry.objects.filter(action="appointment.cancelled").exists()
    # the view always enforces the lead rule, whatever the reason
    calls: list[dict[str, Any]] = []
    real = services.cancel_by_customer

    def spy(*args: Any, **kwargs: Any) -> Any:
        calls.append(kwargs)
        return real(*args, **kwargs)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(services, "cancel_by_customer", spy)
        assert cancel(customer_client, soon.pk, "Rejalar o‘zgardi").status_code == 200
    assert calls[0]["account_deleted"] is False


# --- booking edge cases (fix pass 1) ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("start", "code"),
    [
        ("9999-12-31T23:59:00+05:00", "out_of_range"),  # start + duration overflows
        ("9999-12-31T23:59:00-10:00", "out_of_range"),  # the conversion to Tashkent time overflows
        ("0001-01-01T00:00:00+14:00", "in_past"),
    ],
)
def test_booking_at_the_edge_of_the_datetime_range(
    clock: None, customer_client: APIClient, start: str, code: str
) -> None:
    link = setup_link()
    response = book(customer_client, body_for(link, start))
    assert field_errors(response)[0][:2] == ("start", code)
    assert not Appointment.objects.exists()


def test_booking_after_the_customer_was_deleted_answers_session_expired(
    clock: None, customer: Customer, customer_client: APIClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    link = setup_link()
    real = selectors.offered_link

    def deleted_meanwhile(*args: Any, **kwargs: Any) -> Any:
        # DELETE me/ commits between the authentication and the customer lock
        Customer.objects.filter(pk=customer.pk).update(status="deleted", deleted_at=ANCHOR)
        return real(*args, **kwargs)

    monkeypatch.setattr(selectors, "offered_link", deleted_meanwhile)
    response = book(customer_client, body_for(link, at(MONDAY, 10)))
    assert response.status_code == 401, response.content
    assert response.json()["code"] == "SESSION_EXPIRED"
    assert not Appointment.objects.exists()


@pytest.mark.django_db(transaction=True)
def test_booking_races_with_the_account_deletion(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("glowderma.core.timeutils.now", lambda: ANCHOR)
    link = setup_link()
    customer = CustomerFactory.create()
    locked = threading.Event()
    release = threading.Event()
    outcomes: dict[str, Any] = {}

    def delete_account() -> None:
        try:
            with transaction.atomic():
                row = Customer.objects.select_for_update(no_key=True).get(pk=customer.pk)
                row.status = "deleted"
                row.deleted_at = ANCHOR
                row.save(update_fields=["status", "deleted_at"])
                locked.set()
                release.wait(10)
        finally:
            connection.close()

    def book_slot() -> None:
        try:
            outcomes["booking"] = services.request_by_customer(
                customer.pk, link.professional_id, link.service_id, at(MONDAY, 10)
            )
        except Exception as exc:  # noqa: BLE001 - the outcome is the assertion
            outcomes["booking"] = exc
        finally:
            connection.close()

    deleter = threading.Thread(target=delete_account)
    deleter.start()
    assert locked.wait(10)
    booker = threading.Thread(target=book_slot)
    booker.start()
    booker.join(timeout=1)
    assert booker.is_alive()  # waits on the customer lock
    release.set()
    deleter.join(timeout=10)
    booker.join(timeout=10)
    result = outcomes["booking"]
    assert isinstance(result, AuthFailed), outcomes
    assert result.get_codes() == "SESSION_EXPIRED" and result.status_code == 401
    assert not Appointment.objects.exists()


def test_only_the_no_overlap_exclusion_is_a_lost_slot(
    clock: None, customer: Customer, monkeypatch: pytest.MonkeyPatch
) -> None:
    link = setup_link()
    check = IntegrityError("new row violates check constraint")
    check.__cause__ = psycopg.errors.CheckViolation("check")

    def raise_check(*args: Any, **kwargs: Any) -> Any:
        raise check

    monkeypatch.setattr(Appointment.objects, "create", raise_check)
    with pytest.raises(IntegrityError):
        services.request_by_customer(customer.pk, link.professional_id, link.service_id, at(MONDAY, 10))
    assert services._lost_the_slot(check) is False
    plain = IntegrityError("no cause")
    assert services._lost_the_slot(plain) is False
