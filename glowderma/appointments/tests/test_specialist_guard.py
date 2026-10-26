"""The appointments side of the specialists admin API (FND-1): ``specialists/{id}/conflicts/`` and the
upcoming-appointments counter registered for ``professionals.bookings``."""

from datetime import date, datetime, time, timedelta
from typing import Any

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from rest_framework.test import APIClient

from glowderma.appointments import selectors
from glowderma.appointments.models import Appointment
from glowderma.appointments.tests.factories import AppointmentFactory
from glowderma.core.timeutils import TASHKENT
from glowderma.professionals import bookings
from glowderma.professionals.models import Professional
from glowderma.professionals.tests.factories import (
    BlockedPeriodFactory,
    ProfessionalFactory,
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
