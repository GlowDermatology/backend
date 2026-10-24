from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from django.utils import timezone

TASHKENT = ZoneInfo("Asia/Tashkent")


def now() -> datetime:
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
