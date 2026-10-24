from datetime import datetime

from django.utils import timezone


def now() -> datetime:
    return timezone.now()


def isoformat(value: datetime) -> str:
    return timezone.localtime(value).isoformat(timespec="seconds")
