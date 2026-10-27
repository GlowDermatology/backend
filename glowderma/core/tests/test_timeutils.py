from datetime import UTC, date, datetime

from glowderma.core import timeutils


def test_day_bounds_tashkent() -> None:
    start, end = timeutils.day_bounds(date(2026, 10, 24))
    assert start.astimezone(UTC) == datetime(2026, 10, 23, 19, tzinfo=UTC)
    assert end.astimezone(UTC) == datetime(2026, 10, 24, 19, tzinfo=UTC)


def test_isoformat_has_offset_and_seconds() -> None:
    assert (
        timeutils.isoformat(datetime(2026, 10, 24, 7, 0, 0, 123, tzinfo=UTC)) == "2026-10-24T12:00:00+05:00"
    )


def test_local_date_and_month_start() -> None:
    assert timeutils.local_date(datetime(2026, 10, 23, 20, tzinfo=UTC)) == date(2026, 10, 24)
    assert timeutils.month_start(date(2026, 10, 24)) == date(2026, 10, 1)
