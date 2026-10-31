"""``professionals.selectors.schedules_for_many``: the day-schedule rules for many specialists × days."""

from datetime import date, datetime, time
from typing import Any

import pytest

from glowderma.core.timeutils import TASHKENT
from glowderma.professionals import selectors
from glowderma.professionals.tests.factories import (
    BlockedPeriodFactory,
    ProfessionalFactory,
    WorkingHoursFactory,
    WorkingHoursOverrideFactory,
)

pytestmark = pytest.mark.django_db
MONDAY, TUESDAY = date(2026, 11, 2), date(2026, 11, 3)


def test_matches_schedules_for_days_in_three_queries(django_assert_num_queries: Any) -> None:
    first, second = ProfessionalFactory.create(), ProfessionalFactory.create()
    WorkingHoursFactory.create(
        professional=first, weekday=0, start=time(9), end=time(18), break_start=time(13), break_end=time(14)
    )
    WorkingHoursFactory.create(professional=second, weekday=1, start=time(10), end=time(16))
    WorkingHoursOverrideFactory.create(professional=first, date=TUESDAY, start=time(12), end=time(15))
    BlockedPeriodFactory.create(
        professional=second,
        start=datetime(2026, 11, 3, 11, 0, tzinfo=TASHKENT),
        end=datetime(2026, 11, 3, 12, 0, tzinfo=TASHKENT),
    )
    with django_assert_num_queries(3):
        many = selectors.schedules_for_many([first, second], [MONDAY, TUESDAY])
    for professional in (first, second):
        single = selectors.schedules_for_days(professional, [MONDAY, TUESDAY])
        for day in (MONDAY, TUESDAY):
            assert many[(professional.pk, day)] == single[day]
    assert many[(second.pk, MONDAY)].working_hours is None
    assert many[(first.pk, TUESDAY)].working_hours == ("12:00", "15:00")


def test_no_professionals_or_days_reads_nothing(django_assert_num_queries: Any) -> None:
    with django_assert_num_queries(0):
        assert selectors.schedules_for_many([], [MONDAY]) == {}
        assert selectors.schedules_for_many([ProfessionalFactory.build()], []) == {}
