"""Read-side helpers of `professionals`: the day-schedule derivation of design 01 §5.8 (contract §2.9).

Four queries for a day: active professionals, overrides for the date, weekly hours of the weekday and the
blocks intersecting the day. The appointments of the day belong to the `appointments` module (B6).
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, time

from django.db.models import QuerySet

from glowderma.core import enums, timeutils
from glowderma.professionals.models import BlockedPeriod, Professional, WorkingHours, WorkingHoursOverride

# A block that runs to (or past) midnight is clipped to the last minute of the day: the FE parser accepts
# ``HH:MM`` ≤ 23:59 only (design 01 §5.7, open point 11).
DAY_END_LABEL = "23:59"


def format_clock(value: time) -> str:
    return value.strftime("%H:%M")


@dataclass(frozen=True, slots=True)
class DayBlock:
    start: str  # "HH:MM" Tashkent, clipped to the day
    end: str
    kind: str  # enums.BlockKind
    label: str | None  # None ↔ stored ""; the FE maps a null label by kind (contract §5.6)


@dataclass(frozen=True, slots=True)
class DaySchedule:
    professional: Professional
    working_hours: tuple[str, str] | None  # ("09:00", "18:00") | None = not working
    blocks: list[DayBlock] = field(default_factory=list)


def _clip(block: BlockedPeriod, day_start: datetime, day_end: datetime) -> DayBlock:
    start = max(block.start, day_start).astimezone(timeutils.TASHKENT)
    end = min(block.end, day_end).astimezone(timeutils.TASHKENT)
    end_label = DAY_END_LABEL if end >= day_end else format_clock(end.time())
    return DayBlock(
        start=format_clock(start.time()), end=end_label, kind=block.kind, label=block.label or None
    )


def day_schedules(day: date, professionals: QuerySet[Professional] | None = None) -> list[DaySchedule]:
    """Working hours and clipped blocks of every (active, by default) professional on ``day``.

    ``WorkingHoursOverride`` wins over ``WorkingHours``; a recurring break is rendered as an ``off`` block
    with a null label only when the hours came from ``WorkingHours``.
    """
    if professionals is None:
        professionals = Professional.objects.filter(is_active=True)
    rows = list(professionals.order_by("position", "name", "id"))
    ids = [row.pk for row in rows]
    overrides = {
        o.professional_id: o for o in WorkingHoursOverride.objects.filter(professional_id__in=ids, date=day)
    }
    weekly = {
        w.professional_id: w
        for w in WorkingHours.objects.filter(professional_id__in=ids, weekday=day.weekday())
    }
    day_start, day_end = timeutils.day_bounds(day)
    blocks: dict[int, list[DayBlock]] = defaultdict(list)
    for block in BlockedPeriod.objects.filter(
        professional_id__in=ids, start__lt=day_end, end__gt=day_start
    ).order_by("start", "id"):
        blocks[block.professional_id].append(_clip(block, day_start, day_end))

    schedules: list[DaySchedule] = []
    for professional in rows:
        hours: tuple[str, str] | None
        day_blocks = list(blocks.get(professional.pk, ()))
        override = overrides.get(professional.pk)
        if override is not None:
            hours = (
                None
                if override.start is None or override.end is None
                else (format_clock(override.start), format_clock(override.end))
            )
        else:
            week = weekly.get(professional.pk)
            if week is None:
                hours = None
            else:
                hours = (format_clock(week.start), format_clock(week.end))
                if week.break_start is not None and week.break_end is not None:
                    day_blocks.append(
                        DayBlock(
                            start=format_clock(week.break_start),
                            end=format_clock(week.break_end),
                            kind=enums.BlockKind.OFF,
                            label=None,
                        )
                    )
        day_blocks.sort(key=lambda b: (b.start, b.end))
        schedules.append(DaySchedule(professional=professional, working_hours=hours, blocks=day_blocks))
    return schedules
