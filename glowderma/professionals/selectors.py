"""Read-side helpers of `professionals`: the day-schedule derivation of design 01 §5.8 (contract §2.9).

Four queries for a day: active professionals, overrides for the date, weekly hours of the weekday and the
blocks intersecting the day. The appointments of the day belong to the `appointments` module (B6).
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Collection
from dataclasses import dataclass, field
from datetime import date, datetime, time
from typing import Any

from django.db.models import Count, F, Model, Prefetch, Q, QuerySet, Value
from django.db.models.functions import Collate
from django.db.models.lookups import Contains

from glowderma.core import enums, timeutils
from glowderma.core.search import SearchFold
from glowderma.professionals.models import (
    BlockedPeriod,
    Location,
    Professional,
    ProfessionalService,
    Service,
    WorkingHours,
    WorkingHoursOverride,
)

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


def _schedule(
    professional: Professional,
    override: WorkingHoursOverride | None,
    week: WorkingHours | None,
    blocks: list[DayBlock],
) -> DaySchedule:
    """One professional's day from its override (wins), else its weekly row, plus the clipped blocks.

    A recurring break is rendered as an ``off`` block with a null label only when the hours came from
    ``WorkingHours``.
    """
    hours: tuple[str, str] | None
    day_blocks = list(blocks)
    if override is not None:
        hours = (
            None
            if override.start is None or override.end is None
            else (format_clock(override.start), format_clock(override.end))
        )
    elif week is None:
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
    return DaySchedule(professional=professional, working_hours=hours, blocks=day_blocks)


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
    return [
        _schedule(
            professional,
            overrides.get(professional.pk),
            weekly.get(professional.pk),
            blocks.get(professional.pk, []),
        )
        for professional in rows
    ]


def schedules_for_days(professional: Professional, days: Collection[date]) -> dict[date, DaySchedule]:
    """The schedule of one professional on each of ``days``, by the rules of :func:`day_schedules`, in three
    queries whatever the number of days (overrides of those dates, the weekly rows, the blocks touching the
    span)."""
    wanted = sorted(set(days))
    if not wanted:
        return {}
    overrides = {
        o.date: o for o in WorkingHoursOverride.objects.filter(professional=professional, date__in=wanted)
    }
    weekly = {w.weekday: w for w in WorkingHours.objects.filter(professional=professional)}
    span_start, _ = timeutils.day_bounds(wanted[0])
    _, span_end = timeutils.day_bounds(wanted[-1])
    raw_blocks = list(
        BlockedPeriod.objects.filter(
            professional=professional, start__lt=span_end, end__gt=span_start
        ).order_by("start", "id")
    )
    result: dict[date, DaySchedule] = {}
    for day in wanted:
        day_start, day_end = timeutils.day_bounds(day)
        clipped = [
            _clip(block, day_start, day_end)
            for block in raw_blocks
            if block.start < day_end and block.end > day_start
        ]
        result[day] = _schedule(professional, overrides.get(day), weekly.get(day.weekday()), clipped)
    return result


def schedules_for_many(
    professionals: Collection[Professional], days: Collection[date]
) -> dict[tuple[int, date], DaySchedule]:
    """``{(professional id, day): DaySchedule}`` for every pair of ``professionals`` × ``days``, by the rules
    of :func:`day_schedules`, in three queries whatever the number of professionals and days."""
    wanted = sorted(set(days))
    rows = list(professionals)
    if not wanted or not rows:
        return {}
    ids = [row.pk for row in rows]
    overrides = {
        (o.professional_id, o.date): o
        for o in WorkingHoursOverride.objects.filter(professional_id__in=ids, date__in=wanted)
    }
    weekly = {(w.professional_id, w.weekday): w for w in WorkingHours.objects.filter(professional_id__in=ids)}
    span_start, _ = timeutils.day_bounds(wanted[0])
    _, span_end = timeutils.day_bounds(wanted[-1])
    raw_blocks: dict[int, list[BlockedPeriod]] = defaultdict(list)
    for block in BlockedPeriod.objects.filter(
        professional_id__in=ids, start__lt=span_end, end__gt=span_start
    ).order_by("start", "id"):
        raw_blocks[block.professional_id].append(block)
    result: dict[tuple[int, date], DaySchedule] = {}
    for professional in rows:
        for day in wanted:
            day_start, day_end = timeutils.day_bounds(day)
            clipped = [
                _clip(block, day_start, day_end)
                for block in raw_blocks.get(professional.pk, [])
                if block.start < day_end and block.end > day_start
            ]
            result[(professional.pk, day)] = _schedule(
                professional,
                overrides.get((professional.pk, day)),
                weekly.get((professional.pk, day.weekday())),
                clipped,
            )
    return result


def booking_catalogue() -> QuerySet[Professional]:
    """Active professionals (by position, then name) with their active, bookable services prefetched.

    ``bookable_links`` holds the professional's active ``ProfessionalService`` rows whose service is active,
    ordered by the service position then name — what the admin booking dialog offers (contract §2.9 meta).
    """
    links = (
        ProfessionalService.objects.filter(is_active=True, service__is_active=True)
        .select_related("service")
        .order_by("service__position", "service__name_uz", "service_id")
    )
    return (
        Professional.objects.filter(is_active=True)
        .order_by("position", "name", "id")
        .prefetch_related(Prefetch("services", queryset=links, to_attr="bookable_links"))
    )


# --- admin API (FND-1): specialists, services, schedules and the location ----------------------------------

SEARCH_MAX_LENGTH = 64
CI_COLLATION = "glowderma_ci"  # the ICU case-insensitive collation of core.0007 (decision T12)


def the_location() -> Location | None:
    """The clinic (decision P4: one location): the first row by position, then id; ``None`` before staff
    entered it."""
    return Location.objects.order_by("position", "id").first()


def specialist_queryset() -> QuerySet[Professional]:
    """Professionals with everything the admin ``Specialist`` shape needs, in three queries for any page:
    the rows with the linked staff account, the offered services (``offered_links``, by service position
    then name) and the weekly hours (``week_rows``, by weekday)."""
    links = ProfessionalService.objects.select_related("service").order_by(
        "service__position", "service__name_uz", "service_id"
    )
    return Professional.objects.select_related("staff_user").prefetch_related(
        Prefetch("services", queryset=links, to_attr="offered_links"),
        Prefetch("working_hours", queryset=WorkingHours.objects.order_by("weekday"), to_attr="week_rows"),
    )


def get_specialist(pk: int) -> Professional | None:
    return specialist_queryset().filter(pk=pk).first()


def _search_term(raw: str) -> str:
    return raw.strip()[:SEARCH_MAX_LENGTH]


def _folded_contains(field_name: str, term: str) -> Q:
    """``field_name`` contains ``term``, both folded like every name search (decision GC-1: case, the six
    apostrophe encodings of o‘/g‘, ``ё`` = ``е``). The tables are small, so no index is needed."""
    return Q(Contains(SearchFold(F(field_name)), SearchFold(Value(term))))


def _order[M: Model](
    qs: QuerySet[M], ordering: str, name_field: str, position: tuple[str, ...]
) -> QuerySet[M]:
    if ordering == enums.DirectoryOrdering.NAME:
        return qs.order_by(Collate(F(name_field), CI_COLLATION).asc(), "id")
    if ordering == enums.DirectoryOrdering.NAME_DESC:
        return qs.order_by(Collate(F(name_field), CI_COLLATION).desc(), "-id")
    if ordering == enums.DirectoryOrdering.CREATED_AT:
        return qs.order_by("created_at", "id")
    if ordering == enums.DirectoryOrdering.CREATED_AT_DESC:
        return qs.order_by("-created_at", "-id")
    return qs.order_by(*position)


def filter_specialists(qs: QuerySet[Professional], filters: dict[str, Any]) -> QuerySet[Professional]:
    """``GET specialists/``: ``search`` (name contains, folded), ``is_active``, ``role`` and
    ``ordering`` (``position`` = position, name, id)."""
    term = _search_term(filters.get("search", ""))
    if term:
        qs = qs.filter(_folded_contains("name", term))
    if "is_active" in filters:
        qs = qs.filter(is_active=filters["is_active"])
    if "role" in filters:
        qs = qs.filter(role=filters["role"])
    return _order(qs, filters.get("ordering", ""), "name", ("position", "name", "id"))


def service_queryset() -> QuerySet[Service]:
    """Services with ``specialist_count``: the active specialists offering them through an active link (what
    the booking catalogue offers)."""
    offered = Q(professional_links__is_active=True, professional_links__professional__is_active=True)
    return Service.objects.annotate(
        specialist_count=Count("professional_links", filter=offered, distinct=True)
    )


def get_service(pk: int) -> Service | None:
    return service_queryset().filter(pk=pk).first()


def filter_services(qs: QuerySet[Service], filters: dict[str, Any]) -> QuerySet[Service]:
    """``GET services/``: ``search`` (name or short name in any language contains, folded), ``is_active``,
    ``category`` and ``ordering`` (``position`` = position, Uzbek name, id)."""
    term = _search_term(filters.get("search", ""))
    if term:
        condition = Q()
        for language in ("uz", "ru", "en"):
            condition |= _folded_contains(f"name_{language}", term)
            condition |= _folded_contains(f"short_name_{language}", term)
        qs = qs.filter(condition)
    if "is_active" in filters:
        qs = qs.filter(is_active=filters["is_active"])
    if "category" in filters:
        qs = qs.filter(category=filters["category"])
    return _order(qs, filters.get("ordering", ""), "name_uz", ("position", "name_uz", "id"))


def overrides_of(
    professional: Professional, date_from: date, date_to: date | None
) -> QuerySet[WorkingHoursOverride]:
    """The date overrides of ``professional`` in ``[date_from, date_to]`` (open-ended without ``date_to``),
    by date."""
    qs = WorkingHoursOverride.objects.select_related("created_by").filter(
        professional=professional, date__gte=date_from
    )
    if date_to is not None:
        qs = qs.filter(date__lte=date_to)
    return qs.order_by("date", "id")


def blocks_of(professional: Professional, date_from: date, date_to: date | None) -> QuerySet[BlockedPeriod]:
    """The blocked periods of ``professional`` touching the Tashkent days ``[date_from, date_to]``
    (open-ended without ``date_to``), by start."""
    span_start, _ = timeutils.day_bounds(date_from)
    qs = BlockedPeriod.objects.select_related("created_by").filter(
        professional=professional, end__gt=span_start
    )
    if date_to is not None:
        _, span_end = timeutils.day_bounds(date_to)
        qs = qs.filter(start__lt=span_end)
    return qs.order_by("start", "id")
