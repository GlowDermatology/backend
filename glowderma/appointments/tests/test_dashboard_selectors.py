"""Ops console reads of ``appointments``: conflicts of every specialist at once, today's status counts."""

from datetime import datetime, time, timedelta
from typing import Any

import pytest

from glowderma.appointments import selectors
from glowderma.appointments.tests.factories import AppointmentFactory
from glowderma.core import enums
from glowderma.core.timeutils import TASHKENT
from glowderma.professionals.tests.factories import (
    BlockedPeriodFactory,
    ProfessionalFactory,
    WorkingHoursFactory,
)

pytestmark = pytest.mark.django_db
NOW = datetime(2026, 11, 2, 8, 0, tzinfo=TASHKENT)  # a Monday


def _weekdays(professional: Any) -> None:
    for weekday in range(7):
        WorkingHoursFactory.create(professional=professional, weekday=weekday, start=time(9), end=time(18))


def test_upcoming_conflicts_across_specialists_in_four_queries(django_assert_num_queries: Any) -> None:
    covered, blocked, off = (
        ProfessionalFactory.create(),
        ProfessionalFactory.create(),
        ProfessionalFactory.create(),
    )
    _weekdays(covered)
    _weekdays(blocked)  # "off" has no weekly hours at all
    fine = AppointmentFactory.create(professional=covered, start=NOW.replace(hour=10))
    late = AppointmentFactory.create(
        professional=covered, start=NOW.replace(hour=17, minute=45)
    )  # past 18:00
    clash = AppointmentFactory.create(
        professional=blocked,
        start=NOW.replace(hour=12) + timedelta(days=1),
        status=enums.AppointmentStatus.CONFIRMED,
        confirmed_at=NOW,
    )
    BlockedPeriodFactory.create(
        professional=blocked, start=clash.start - timedelta(minutes=10), end=clash.start + timedelta(hours=1)
    )
    no_hours = AppointmentFactory.create(professional=off, start=NOW.replace(hour=11) + timedelta(days=2))
    AppointmentFactory.create(professional=off, start=NOW.replace(hour=11) + timedelta(days=20))  # beyond
    AppointmentFactory.create(professional=off, start=NOW - timedelta(hours=1))  # already started
    with django_assert_num_queries(4):
        conflicts = selectors.upcoming_conflicts(NOW, days=14)
    assert [row.pk for row in conflicts] == [late.pk, clash.pk, no_hours.pk]
    assert fine.pk not in {row.pk for row in conflicts}
    assert selectors.schedule_conflicts(covered, NOW) == [late]  # the per-specialist rule agrees


def test_upcoming_conflicts_without_appointments_is_one_query(django_assert_num_queries: Any) -> None:
    with django_assert_num_queries(1):
        assert selectors.upcoming_conflicts(NOW, days=14) == []


def test_day_status_counts(django_assert_num_queries: Any) -> None:
    AppointmentFactory.create(start=NOW.replace(hour=10))
    AppointmentFactory.create(
        start=NOW.replace(hour=11), status=enums.AppointmentStatus.CONFIRMED, confirmed_at=NOW
    )
    AppointmentFactory.create(start=NOW + timedelta(days=1))
    with django_assert_num_queries(1):
        counts = selectors.day_status_counts(NOW.date())
    assert counts["requested"] == 1 and counts["confirmed"] == 1 and sum(counts.values()) == 2
