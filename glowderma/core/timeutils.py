from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from django.conf import settings
from django.utils import timezone

TASHKENT = ZoneInfo("Asia/Tashkent")


def now() -> datetime:
    """The one clock of the codebase.

    ``settings.FAKE_NOW`` (local/test settings only; production refuses it) pins the instant so a seeded
    demo dataset and the browser end-to-end checks see the same "today". Tests monkeypatch this function.
    """
    fixed: datetime | None = getattr(settings, "FAKE_NOW", None)
    if fixed is not None:
        return fixed
    return timezone.now()


def isoformat(value: datetime) -> str:
    return value.astimezone(TASHKENT).replace(microsecond=0).isoformat()


def local_date(value: datetime) -> date:
    return value.astimezone(TASHKENT).date()


def day_bounds(value: date) -> tuple[datetime, datetime]:
    start = datetime.combine(value, time.min, tzinfo=TASHKENT)
    return start, start + timedelta(days=1)


def month_start(value: date) -> date:
    return value.replace(day=1)


PERIODS = ("today", "week", "month", "all")


def period_bounds(period: str, now: datetime) -> dict[str, datetime]:
    """The ``OrderPeriod`` window of contract §2.2 as ORM lookups (``{"gte": …, "lt": …}``), Tashkent time.

    ``today`` = the current Tashkent day ``[d0, d1)``; ``week`` = rolling ``(now − 7×24 h, now]``; ``month`` =
    the Tashkent calendar month to date ``[first day 00:00, now]``; ``all`` = unbounded (``{}``). Callers
    prefix the keys with the column (``{f"created_at__{k}": v}``) so ``orders/`` and ``payments/`` share the
    semantics.
    """
    if period == "all":
        return {}
    if period == "today":
        start, end = day_bounds(local_date(now))
        return {"gte": start, "lt": end}
    if period == "week":
        return {"gt": now - timedelta(days=7), "lte": now}
    if period == "month":
        start, _ = day_bounds(month_start(local_date(now)))
        return {"gte": start, "lte": now}
    raise ValueError(f"Unknown period {period!r}")


def date_range_bounds(date_from: date | None, date_to: date | None) -> dict[str, datetime]:
    """Inclusive Tashkent days ``[date_from 00:00, date_to + 1 day 00:00)`` as ORM lookups."""
    bounds: dict[str, datetime] = {}
    if date_from is not None:
        bounds["gte"] = day_bounds(date_from)[0]
    if date_to is not None:
        bounds["lt"] = day_bounds(date_to)[1]
    return bounds
