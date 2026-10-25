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
