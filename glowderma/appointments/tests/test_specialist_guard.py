"""The appointments side of the specialists admin API (FND-1): ``specialists/{id}/conflicts/``, the
upcoming-appointments counter registered for ``professionals.bookings``, and the booking guard against a
concurrent deactivation (``FOR KEY SHARE`` on the specialist vs the deactivation's ``FOR UPDATE``)."""

import threading
from datetime import date, datetime, time, timedelta
from typing import Any

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from rest_framework.exceptions import ValidationError
from rest_framework.test import APIClient

from glowderma.accounts.tests.factories import CustomerFactory, StaffUserFactory
from glowderma.appointments import selectors, services
from glowderma.appointments.models import Appointment
from glowderma.appointments.tests.factories import AppointmentFactory
from glowderma.core.api.exceptions import Conflict
from glowderma.core.timeutils import TASHKENT
from glowderma.professionals import bookings
from glowderma.professionals import services as professionals_services
from glowderma.professionals.models import Professional, ProfessionalService
from glowderma.professionals.tests.factories import (
    BlockedPeriodFactory,
    ProfessionalFactory,
    ProfessionalServiceFactory,
    WorkingHoursFactory,
    WorkingHoursOverrideFactory,
)

pytestmark = pytest.mark.django_db

ANCHOR = datetime(2026, 10, 24, 12, 5, tzinfo=TASHKENT)  # Saturday
SUNDAY, MONDAY, TUESDAY = date(2026, 10, 25), date(2026, 10, 26), date(2026, 10, 27)


def at(day: date, hour: int, minute: int = 0) -> datetime:
    return datetime.combine(day, time(hour, minute), tzinfo=TASHKENT)


def week(professional: Professional) -> None:
    WorkingHoursFactory.create(
        professional=professional, weekday=0, break_start=time(13, 0), break_end=time(14, 0)
    )
    WorkingHoursFactory.create(professional=professional, weekday=1)


# --- conflicts ----------------------------------------------------------------------------------------------


def test_conflicts_list_the_open_future_appointments_the_schedule_no_longer_covers(
    staff_client: APIClient, clock: Any
) -> None:
    professional = ProfessionalFactory.create()
    week(professional)
    BlockedPeriodFactory.create(professional=professional, start=at(MONDAY, 15), end=at(MONDAY, 16))
    WorkingHoursOverrideFactory.create(professional=professional, date=TUESDAY, start=None, end=None)

    def book(day: date, hour: int, minute: int = 0, **kwargs: Any) -> Appointment:
        return AppointmentFactory.create(professional=professional, start=at(day, hour, minute), **kwargs)

    book(MONDAY, 10)  # inside the hours: fine
    in_break = book(MONDAY, 13, 30, status="confirmed", confirmed_at=ANCHOR)
    past_close = book(MONDAY, 17, 45)
    day_off = book(TUESDAY, 10)
    no_hours = book(SUNDAY, 10)
    in_block = book(MONDAY, 15)
    book(MONDAY, 11, status="cancelled", cancelled_at=ANCHOR, cancelled_by="admin")  # not open
    book(ANCHOR.date() - timedelta(days=5), 10)  # past
    response = staff_client.get(f"/api/v1/admin/specialists/{professional.pk}/conflicts/")
    assert response.status_code == 200
    results = response.json()["results"]
    assert [row["id"] for row in results] == [
        no_hours.pk,
        in_break.pk,
        in_block.pk,
        past_close.pk,
        day_off.pk,
    ]
    assert set(results[0]) == {
        "id",
        "specialist_id",
        "customer",
        "service_id",
        "service_name",
        "service_short_name",
        "duration_min",
        "start",
        "end",
        "price",
        "status",
        "requested_at",
        "requested_via",
    }
    missing = staff_client.get("/api/v1/admin/specialists/999999/conflicts/")
    assert missing.status_code == 404


def test_conflicts_run_in_four_queries_whatever_the_number_of_days(clock: Any) -> None:
    professional = ProfessionalFactory.create()
    week(professional)
    for offset in range(10):
        AppointmentFactory.create(professional=professional, start=at(MONDAY + timedelta(days=offset), 10))
    with CaptureQueriesContext(connection) as queries:
        conflicts = selectors.schedule_conflicts(professional)
    assert len(queries.captured_queries) == 4
    assert len(conflicts) == 6  # 26 Oct – 4 Nov: only the two Mondays and two Tuesdays have hours


def test_the_counter_is_registered_and_counts_open_future_appointments(clock: Any) -> None:
    busy, idle = ProfessionalFactory.create(), ProfessionalFactory.create()
    AppointmentFactory.create(professional=busy, start=at(MONDAY, 10))
    AppointmentFactory.create(
        professional=busy, start=at(MONDAY, 11), status="confirmed", confirmed_at=ANCHOR
    )
    AppointmentFactory.create(professional=busy, start=at(MONDAY, 12), status="rejected", rejected_at=ANCHOR)
    AppointmentFactory.create(professional=busy, start=ANCHOR - timedelta(minutes=1))
    assert bookings.upcoming_counts([busy.pk, idle.pk], ANCHOR) == {busy.pk: 2, idle.pk: 0}
    assert bookings.upcoming_counts([], ANCHOR) == {}


# --- the booking guard --------------------------------------------------------------------------------------


def bookable() -> ProfessionalService:
    link = ProfessionalServiceFactory.create(price=150_000, duration_min=30)
    WorkingHoursFactory.create(professional=link.professional, weekday=0)
    return link


def test_an_admin_booking_of_a_specialist_deactivated_meanwhile_is_refused(
    staff_client: APIClient, clock: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    link = bookable()
    customer = CustomerFactory.create()

    def deactivate() -> None:
        Professional.objects.filter(pk=link.professional_id).update(is_active=False)

    monkeypatch.setattr(services, "_before_write", deactivate)
    response = staff_client.post(
        "/api/v1/admin/appointments/",
        {
            "specialist_id": link.professional_id,
            "service_id": link.service_id,
            "customer_id": customer.pk,
            "start": at(MONDAY, 10).isoformat(),
        },
        format="json",
    )
    assert response.status_code == 400
    assert response.json()["errors"] == [
        {"field": "specialist_id", "code": "does_not_exist", "message": "Mutaxassis topilmadi."}
    ]
    assert not Appointment.objects.exists()


def test_a_reschedule_to_a_deactivated_specialist_is_refused(clock: Any) -> None:
    link = bookable()
    appointment = AppointmentFactory.create(
        professional=link.professional, service=link.service, start=at(MONDAY, 10)
    )
    target = ProfessionalServiceFactory.create(
        service=link.service, professional__is_active=False
    ).professional
    with pytest.raises(ValidationError) as caught:
        services.reschedule(
            appointment.pk, start=at(MONDAY, 11), professional=target, actor=StaffUserFactory.create()
        )
    assert caught.value.get_codes() == {"specialist_id": ["does_not_exist"]}
    assert Appointment.objects.get(pk=appointment.pk).professional_id == link.professional_id


def _run(outcomes: dict[str, Any], key: str, call: Any) -> threading.Thread:
    def target() -> None:
        try:
            outcomes[key] = call()
        except Exception as exc:  # noqa: BLE001 - the outcome is the assertion
            outcomes[key] = exc
        finally:
            connection.close()

    thread = threading.Thread(target=target)
    thread.start()
    return thread


@pytest.mark.django_db(transaction=True)
def test_a_booking_waits_for_a_deactivation_and_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("glowderma.core.timeutils.now", lambda: ANCHOR)
    link = bookable()
    customer, staff = CustomerFactory.create(), StaffUserFactory.create()
    counted, release = threading.Event(), threading.Event()
    count = bookings.upcoming_counts

    def count_then_hold(ids: Any, instant: datetime) -> dict[int, int]:
        result = count(ids, instant)
        counted.set()
        release.wait(10)
        return result

    monkeypatch.setattr(bookings, "upcoming_counts", count_then_hold)
    outcomes: dict[str, Any] = {}
    deactivation = _run(
        outcomes,
        "deactivation",
        lambda: professionals_services.update_specialist(
            link.professional_id, {"is_active": False}, actor=staff
        ),
    )
    assert counted.wait(10)  # counted 0 and holds the specialist's row
    booking = _run(
        outcomes,
        "booking",
        lambda: services.request_by_customer(
            customer.pk, link.professional_id, link.service_id, at(MONDAY, 10)
        ),
    )
    booking.join(timeout=1)
    assert booking.is_alive()  # waits on the specialist's row lock
    release.set()
    deactivation.join(timeout=10)
    booking.join(timeout=10)
    assert isinstance(outcomes["deactivation"], Professional), outcomes
    refused = outcomes["booking"]
    assert isinstance(refused, ValidationError), outcomes
    assert refused.get_codes() == {"specialist_id": ["does_not_exist"]}
    assert not Appointment.objects.exists()
    assert Professional.objects.get().is_active is False


@pytest.mark.django_db(transaction=True)
def test_a_deactivation_waits_for_a_booking_and_counts_it(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("glowderma.core.timeutils.now", lambda: ANCHOR)
    link = bookable()
    customer, staff = CustomerFactory.create(), StaffUserFactory.create()
    inside, release = threading.Event(), threading.Event()

    def hold() -> None:
        inside.set()
        release.wait(10)

    monkeypatch.setattr(services, "_before_write", hold)
    outcomes: dict[str, Any] = {}
    booking = _run(
        outcomes,
        "booking",
        lambda: services.request_by_customer(
            customer.pk, link.professional_id, link.service_id, at(MONDAY, 10)
        ),
    )
    assert inside.wait(10)  # checked, holds the specialist FOR KEY SHARE, not yet written
    deactivation = _run(
        outcomes,
        "deactivation",
        lambda: professionals_services.update_specialist(
            link.professional_id, {"is_active": False}, actor=staff
        ),
    )
    deactivation.join(timeout=1)
    assert deactivation.is_alive()  # FOR UPDATE waits for the booking
    release.set()
    booking.join(timeout=10)
    deactivation.join(timeout=10)
    assert isinstance(outcomes["booking"], Appointment), outcomes
    refused = outcomes["deactivation"]
    assert isinstance(refused, Conflict) and "1 ta kelgusi qabul" in str(refused.detail), outcomes
    assert Professional.objects.get().is_active is True
