"""Day-schedule derivation (design 01 §5.8 / contract §2.9)."""

from datetime import date, datetime, time

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext

from glowderma.core import enums
from glowderma.core.timeutils import TASHKENT
from glowderma.professionals.models import Professional
from glowderma.professionals.selectors import DayBlock, day_schedules
from glowderma.professionals.tests.factories import (
    BlockedPeriodFactory,
    ProfessionalFactory,
    WorkingHoursFactory,
    WorkingHoursOverrideFactory,
)

pytestmark = pytest.mark.django_db

MONDAY = date(2026, 10, 26)


def at(day: date, hour: int, minute: int = 0) -> datetime:
    return datetime.combine(day, time(hour, minute), tzinfo=TASHKENT)


def test_weekly_hours_break_and_clipped_blocks() -> None:
    nodira = ProfessionalFactory.create(name="Nodira Rahimova", position=0)
    jasur = ProfessionalFactory.create(name="Jasur Aliyev", position=1)
    dilnoza = ProfessionalFactory.create(name="Dilnoza Yusupova", position=2)
    for professional in (nodira, jasur, dilnoza):
        WorkingHoursFactory.create(professional=professional, weekday=0)
    WorkingHoursFactory.create(professional=jasur, weekday=1, break_start=time(13, 0), break_end=time(14, 0))
    BlockedPeriodFactory.create(professional=nodira, start=at(MONDAY, 16), end=at(MONDAY, 16, 30))
    BlockedPeriodFactory.create(  # another day: ignored
        professional=nodira, start=at(date(2026, 10, 27), 9), end=at(date(2026, 10, 27), 10)
    )
    BlockedPeriodFactory.create(  # label "" renders as None
        professional=dilnoza, start=at(MONDAY, 9), end=at(MONDAY, 9, 30), kind=enums.BlockKind.OFF, label=""
    )

    with CaptureQueriesContext(connection) as queries:
        schedules = day_schedules(MONDAY)
    assert len(queries) == 4
    assert [s.professional for s in schedules] == [nodira, jasur, dilnoza]
    assert all(s.working_hours == ("09:00", "18:00") for s in schedules)
    assert schedules[0].blocks == [DayBlock("16:00", "16:30", enums.BlockKind.BUSY, "Band qilingan")]
    assert schedules[1].blocks == []  # the break is on Tuesday only
    assert schedules[2].blocks == [DayBlock("09:00", "09:30", enums.BlockKind.OFF, None)]

    tuesday = day_schedules(date(2026, 10, 27))
    assert tuesday[0].working_hours is None and tuesday[0].blocks[0].start == "09:00"  # no Tuesday hours
    assert tuesday[1].blocks == [DayBlock("13:00", "14:00", enums.BlockKind.OFF, None)]


def test_override_wins_and_a_day_off_override_yields_null_hours_without_the_break() -> None:
    jasur = ProfessionalFactory.create(name="Jasur Aliyev")
    WorkingHoursFactory.create(professional=jasur, weekday=0, break_start=time(13, 0), break_end=time(14, 0))
    WorkingHoursOverrideFactory.create(professional=jasur, date=MONDAY, start=time(10, 0), end=time(15, 0))
    [schedule] = day_schedules(MONDAY)
    assert schedule.working_hours == ("10:00", "15:00") and schedule.blocks == []  # override: no break block
    WorkingHoursOverrideFactory.create(professional=jasur, date=date(2026, 11, 2), start=None, end=None)
    [day_off] = day_schedules(date(2026, 11, 2))
    assert day_off.working_hours is None and day_off.blocks == []


def test_block_spanning_midnight_is_clipped_to_each_day() -> None:
    nodira = ProfessionalFactory.create()
    BlockedPeriodFactory.create(
        professional=nodira,
        start=at(MONDAY, 17),
        end=at(date(2026, 10, 27), 10),
        kind=enums.BlockKind.OFF,
        label="Seminar",
    )
    [monday] = day_schedules(MONDAY)
    assert monday.blocks == [DayBlock("17:00", "23:59", enums.BlockKind.OFF, "Seminar")]
    [tuesday] = day_schedules(date(2026, 10, 27))
    assert tuesday.blocks == [DayBlock("00:00", "10:00", enums.BlockKind.OFF, "Seminar")]
    [wednesday] = day_schedules(date(2026, 10, 28))
    assert wednesday.blocks == []


def test_inactive_professionals_are_excluded_unless_a_queryset_is_given() -> None:
    active = ProfessionalFactory.create(name="B", position=0)
    inactive = ProfessionalFactory.create(name="A", position=0, is_active=False)
    assert [s.professional for s in day_schedules(MONDAY)] == [active]
    everyone = day_schedules(MONDAY, Professional.objects.all())
    assert [s.professional for s in everyone] == [inactive, active]  # position, then name
