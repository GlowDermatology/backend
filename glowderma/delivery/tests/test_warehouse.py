"""The warehouse calendar (plan §3.3): opening intervals, preparation start, working minutes, ETA windows and
the dispatch refusals. Pure: every calendar is built here, nothing touches the database."""

from dataclasses import replace
from datetime import date, datetime, time, timedelta

import pytest

from glowderma.core.timeutils import TASHKENT
from glowderma.delivery.warehouse import HORIZON_DAYS, EtaWindow, WarehouseCalendar

MONDAY = date(2026, 11, 16)
SATURDAY = date(2026, 11, 21)
SUNDAY = date(2026, 11, 22)

WEEK = WarehouseCalendar(
    hours={
        0: (time(9), time(20)),
        1: (time(9), time(20)),
        2: (time(9), time(20)),
        3: (time(9), time(20)),
        4: (time(9), time(20)),
        5: (time(10), time(16)),
    },
    prep_minutes=30,
    dispatch_cutoff_minutes=90,
)


def at(day: date, hour: int, minute: int = 0) -> datetime:
    return datetime.combine(day, time(hour, minute), tzinfo=TASHKENT)


def test_open_interval_follows_the_weekday_and_a_missing_row_closes_the_day() -> None:
    assert WEEK.open_interval(MONDAY) == (at(MONDAY, 9), at(MONDAY, 20))
    assert WEEK.open_interval(SATURDAY) == (at(SATURDAY, 10), at(SATURDAY, 16))
    assert WEEK.open_interval(SUNDAY) is None
    assert at(MONDAY, 9).utcoffset() == timedelta(hours=5)  # Asia/Tashkent, no DST


def test_a_closure_closes_the_day_and_special_hours_replace_the_weekday() -> None:
    calendar = replace(
        WEEK,
        closures={
            MONDAY: None,
            SATURDAY + timedelta(days=-1): (time(12), time(14)),
            SUNDAY: (time(11), time(13)),
        },
    )
    assert calendar.open_interval(MONDAY) is None
    assert calendar.open_interval(date(2026, 11, 20)) == (
        at(date(2026, 11, 20), 12),
        at(date(2026, 11, 20), 14),
    )
    assert calendar.open_interval(SUNDAY) == (
        at(SUNDAY, 11),
        at(SUNDAY, 13),
    )  # a holiday shift on a closed day
    assert calendar.open_interval(MONDAY + timedelta(days=1)) == WEEK.open_interval(
        MONDAY + timedelta(days=1)
    )


def test_is_open_is_half_open_and_accepts_other_time_zones() -> None:
    assert not WEEK.is_open(at(MONDAY, 8, 59))
    assert WEEK.is_open(at(MONDAY, 9))
    assert WEEK.is_open(at(MONDAY, 19, 59))
    assert not WEEK.is_open(at(MONDAY, 20))
    assert WEEK.is_open(at(MONDAY, 12).astimezone(datetime.now().astimezone().tzinfo))
    with pytest.raises(ValueError, match="aware"):
        WEEK.is_open(datetime(2026, 11, 16, 12))


def test_fulfilment_start_is_now_while_the_parcel_can_still_leave_today() -> None:
    noon = at(MONDAY, 12)
    assert WEEK.fulfilment_start(noon) == noon
    # 20:00 − 90 min cutoff = 18:30 is the last courier call; 30 min preparation must fit before it
    assert WEEK.fulfilment_start(at(MONDAY, 18)) == at(MONDAY, 18)
    assert WEEK.fulfilment_start(at(MONDAY, 18, 1)) == at(MONDAY + timedelta(days=1), 9)  # near closing
    assert WEEK.fulfilment_start(at(MONDAY, 7)) == at(MONDAY, 9)  # before opening: today's opening
    assert WEEK.fulfilment_start(at(MONDAY, 21)) == at(MONDAY + timedelta(days=1), 9)


def test_fulfilment_start_skips_closed_days_and_too_short_special_hours() -> None:
    assert WEEK.fulfilment_start(at(SATURDAY, 17)) == at(MONDAY + timedelta(days=7), 9)  # Sunday closed
    short = replace(WEEK, closures={MONDAY: (time(9), time(10))})  # 60 min: no room for 30 + 90
    assert short.fulfilment_start(at(MONDAY, 8)) == at(MONDAY + timedelta(days=1), 9)


def test_no_open_day_within_the_horizon_gives_no_window() -> None:
    closed = WarehouseCalendar(hours={})
    assert closed.fulfilment_start(at(MONDAY, 12)) is None
    assert closed.eta_window(60, 180, at(MONDAY, 12)) is None
    assert closed.add_working_minutes(at(MONDAY, 12), 10) is None
    holiday = replace(WEEK, closures={MONDAY + timedelta(days=n): None for n in range(HORIZON_DAYS + 1)})
    assert holiday.fulfilment_start(at(MONDAY, 1)) is None
    almost = replace(WEEK, closures={MONDAY + timedelta(days=n): None for n in range(HORIZON_DAYS)})
    assert almost.fulfilment_start(at(MONDAY, 1)) == at(MONDAY + timedelta(days=HORIZON_DAYS), 9)


def test_add_working_minutes_counts_only_open_time_across_days() -> None:
    assert WEEK.add_working_minutes(at(MONDAY, 12), 120) == at(MONDAY, 14)
    assert WEEK.add_working_minutes(at(MONDAY, 19), 120) == at(MONDAY + timedelta(days=1), 10)
    assert WEEK.add_working_minutes(at(MONDAY, 22), 30) == at(MONDAY + timedelta(days=1), 9, 30)
    assert WEEK.add_working_minutes(at(MONDAY, 19), 60) == at(MONDAY, 20)  # exactly at closing
    # Saturday 15:00 + 120: one hour on Saturday, Sunday closed, one hour on Monday
    assert WEEK.add_working_minutes(at(SATURDAY, 15), 120) == at(SUNDAY + timedelta(days=1), 10)
    assert WEEK.add_working_minutes(at(SUNDAY, 12), 0) == at(SUNDAY + timedelta(days=1), 9)
    with pytest.raises(ValueError):
        WEEK.add_working_minutes(at(MONDAY, 12), -1)


def test_eta_window_starts_after_preparation_and_flags_a_later_start() -> None:
    noon = at(MONDAY, 12)
    assert WEEK.eta_window(60, 180, noon) == EtaWindow(at(MONDAY, 13, 30), at(MONDAY, 15, 30), False)
    evening = WEEK.eta_window(30, 90, at(MONDAY, 19))
    assert evening == EtaWindow(at(MONDAY + timedelta(days=1), 10), at(MONDAY + timedelta(days=1), 11), True)
    eta_from, eta_to, scheduled = WEEK.eta_window(30, 90, at(SUNDAY, 10))  # type: ignore[misc]
    assert (eta_from, eta_to, scheduled) == (
        at(SUNDAY + timedelta(days=1), 10),
        at(SUNDAY + timedelta(days=1), 11),
        True,
    )
    with pytest.raises(ValueError):
        WEEK.eta_window(90, 30, noon)


@pytest.mark.parametrize(
    ("calendar", "instant", "refusal"),
    [
        (WEEK, at(MONDAY, 12), None),
        (replace(WEEK, configured=False), at(MONDAY, 12), "not_configured"),
        (replace(WEEK, dispatch_suspended=True), at(MONDAY, 12), "suspended"),
        (replace(WEEK, dispatch_suspended=True, suspended_until=at(MONDAY, 13)), at(MONDAY, 12), "suspended"),
        (replace(WEEK, dispatch_suspended=True, suspended_until=at(MONDAY, 12)), at(MONDAY, 12), None),
        (replace(WEEK, dispatch_suspended=True, suspended_until=at(MONDAY, 11)), at(MONDAY, 12), None),
        (WEEK, at(MONDAY, 8), "closed"),
        (WEEK, at(SUNDAY, 12), "closed"),
        (replace(WEEK, closures={MONDAY: None}), at(MONDAY, 12), "closed"),
        (WEEK, at(MONDAY, 18, 30), None),  # the dispatch deadline itself is still allowed
        (WEEK, at(MONDAY, 18, 31), "after_cutoff"),
        (replace(WEEK, returns_accepted=False), at(MONDAY, 12), "returns_unavailable"),
        # the checks run in a fixed order: configuration, suspension, hours, cutoff, returns
        (replace(WEEK, configured=False, dispatch_suspended=True), at(SUNDAY, 12), "not_configured"),
        (replace(WEEK, dispatch_suspended=True, returns_accepted=False), at(SUNDAY, 12), "suspended"),
        (replace(WEEK, returns_accepted=False), at(MONDAY, 19), "after_cutoff"),
    ],
)
def test_dispatch_refusal(calendar: WarehouseCalendar, instant: datetime, refusal: str | None) -> None:
    assert calendar.dispatch_refusal(instant) == refusal


def test_suspended_at_honours_the_end_of_the_suspension() -> None:
    until = at(MONDAY, 13)
    calendar = replace(WEEK, dispatch_suspended=True, suspended_until=until)
    assert calendar.suspended_at(until - timedelta(seconds=1))
    assert not calendar.suspended_at(until)
    assert not WEEK.suspended_at(at(MONDAY, 12))
    assert WEEK.dispatch_deadline(MONDAY) == at(MONDAY, 18, 30)
    assert WEEK.dispatch_deadline(SUNDAY) is None
