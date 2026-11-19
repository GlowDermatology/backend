"""Usage of the AI assistant for the console: today's and this month's totals (Tashkent days) by model and by
staff member, and the budgets they count against. Aggregates over ``GenerationJob`` (test jobs included)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from django.db.models import Count, Q, QuerySet, Sum
from django.db.models.functions import Coalesce

from glowderma.assistant.models import SETTINGS_PK, AssistantSettings, GenerationJob
from glowderma.core import enums, timeutils

ZERO = Decimal("0")


@dataclass(frozen=True, slots=True)
class Period:
    key: str
    start: datetime
    end: datetime


def periods(now: datetime | None = None) -> list[Period]:
    today = timeutils.local_date(now or timeutils.now())
    day_start, day_end = timeutils.day_bounds(today)
    month_start, _ = timeutils.day_bounds(timeutils.month_start(today))
    next_month = (timeutils.month_start(today) + timedelta(days=32)).replace(day=1)
    month_end, _ = timeutils.day_bounds(next_month)
    return [Period("day", day_start, day_end), Period("month", month_start, month_end)]


_TOTALS = {
    "jobs": Count("id"),
    "failed": Count("id", filter=Q(state=enums.JobState.FAILED)),
    "input_tokens": Coalesce(Sum("input_tokens"), 0),
    "output_tokens": Coalesce(Sum("output_tokens"), 0),
    "cost": Coalesce(Sum("cost_usd"), ZERO),
}


def _within(period: Period) -> QuerySet[GenerationJob]:
    return GenerationJob.objects.filter(created_at__gte=period.start, created_at__lt=period.end)


def totals(period: Period) -> dict[str, Any]:
    jobs = _within(period)
    by_model: list[dict[str, Any]] = [dict(row) for row in jobs.values("model_id").annotate(**_TOTALS)]
    by_user: list[dict[str, Any]] = []
    for row in jobs.values("created_by_id", "created_by__email", "created_by__full_name").annotate(**_TOTALS):
        item: dict[str, Any] = dict(row)
        item["user"] = item.pop("created_by__full_name") or item.pop("created_by__email")
        item.pop("created_by__email", None)
        by_user.append(item)
    by_model.sort(key=lambda item: (-item["cost"], str(item["model_id"])))
    by_user.sort(key=lambda item: (-item["cost"], item["created_by_id"]))
    overall = jobs.aggregate(**_TOTALS)
    return {"key": period.key, "overall": overall, "by_model": by_model, "by_user": by_user}


def usage(now: datetime | None = None) -> dict[str, Any]:
    """``{"day": totals, "month": totals, "budgets": {...}}``."""
    settings_row = AssistantSettings.objects.get(pk=SETTINGS_PK)
    day, month = (totals(period) for period in periods(now))
    return {
        "day": day,
        "month": month,
        "budgets": {
            "day": _budget(day["overall"]["cost"], settings_row.daily_budget_usd),
            "month": _budget(month["overall"]["cost"], settings_row.monthly_budget_usd),
            "per_user_daily_jobs": settings_row.per_user_daily_jobs,
        },
    }


def _budget(spent: Decimal, limit: Decimal) -> dict[str, Any]:
    percent = int(min(spent / limit * 100, Decimal(999))) if limit > 0 else (100 if spent > 0 else 0)
    return {"spent": spent, "limit": limit, "percent": percent, "exceeded": limit <= spent}
