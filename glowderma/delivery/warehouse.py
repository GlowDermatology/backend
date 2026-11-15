"""The warehouse calendar (plan §3.3): when an order can be prepared, when a courier may be called, and the
delivery window a customer is promised. Pure: no database, no clock.

Everything is Asia/Tashkent local time (Uzbekistan has no daylight saving time, so local wall-clock times map
one-to-one onto instants). A day is open when its :class:`WarehouseClosure` row gives special hours, or —
without such a row — when :class:`WarehouseHours` has its weekday; a closure row without times closes the
day. Searches look at most :data:`HORIZON_DAYS` ahead: a warehouse with no open day in that range yields
``None`` (no window is ever invented).

:func:`glowderma.delivery.selectors.warehouse_calendar` builds the :class:`WarehouseCalendar` from the
database; tests build it directly.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Literal, NamedTuple

from glowderma.core.timeutils import TASHKENT

HORIZON_DAYS = 14

DispatchRefusal = Literal["not_configured", "suspended", "closed", "after_cutoff", "returns_unavailable"]
REFUSAL_NOT_CONFIGURED: DispatchRefusal = "not_configured"
REFUSAL_SUSPENDED: DispatchRefusal = "suspended"
REFUSAL_CLOSED: DispatchRefusal = "closed"
REFUSAL_AFTER_CUTOFF: DispatchRefusal = "after_cutoff"
REFUSAL_RETURNS_UNAVAILABLE: DispatchRefusal = "returns_unavailable"


class EtaWindow(NamedTuple):
    """The promised delivery window; ``scheduled`` = it does not start from "now" (closed or too late)."""

    eta_from: datetime
    eta_to: datetime
    scheduled: bool


def _aware(value: datetime, name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")


@dataclass(frozen=True, slots=True)
class WarehouseCalendar:
    """The warehouse's hours, closures and dispatch rules as plain values.

    ``hours`` maps a weekday (0 = Monday) to its ``(opens_at, closes_at)``; ``closures`` maps a date to its
    special ``(opens_at, closes_at)`` or ``None`` (closed). ``configured`` = the warehouse has what a Yandex
    claim needs (pin, address, contact).
    """

    hours: Mapping[int, tuple[time, time]] = field(default_factory=dict)
    closures: Mapping[date, tuple[time, time] | None] = field(default_factory=dict)
    prep_minutes: int = 30
    dispatch_cutoff_minutes: int = 90
    returns_accepted: bool = True
    dispatch_suspended: bool = False
    suspended_until: datetime | None = None
    configured: bool = True

    # --- opening hours

    def open_interval(self, day: date) -> tuple[datetime, datetime] | None:
        """``[opens, closes)`` of ``day`` as aware instants, or ``None`` when the warehouse is closed."""
        if day in self.closures:
            times = self.closures[day]
        else:
            times = self.hours.get(day.weekday())
        if times is None:
            return None
        opens, closes = times
        if opens >= closes:  # the models forbid it; a hand-built calendar must not loop
            return None
        return datetime.combine(day, opens, tzinfo=TASHKENT), datetime.combine(day, closes, tzinfo=TASHKENT)

    def _intervals_from(self, at: datetime) -> Iterator[tuple[datetime, datetime]]:
        """Open intervals ending after ``at``, in order, within the horizon (the first may contain ``at``)."""
        first = at.astimezone(TASHKENT).date()
        for offset in range(HORIZON_DAYS + 1):
            interval = self.open_interval(first + timedelta(days=offset))
            if interval is not None and interval[1] > at:
                yield interval

    def is_open(self, at: datetime) -> bool:
        _aware(at, "at")
        interval = self.open_interval(at.astimezone(TASHKENT).date())
        return interval is not None and interval[0] <= at < interval[1]

    def dispatch_deadline(self, day: date) -> datetime | None:
        """The last instant of ``day`` a courier may be called (closing − ``dispatch_cutoff_minutes``)."""
        interval = self.open_interval(day)
        if interval is None:
            return None
        return interval[1] - timedelta(minutes=self.dispatch_cutoff_minutes)

    # --- preparation and promises

    def fulfilment_start(self, now: datetime) -> datetime | None:
        """When preparing an order paid at ``now`` starts: ``now`` while open and the parcel can still be
        prepared and handed to a courier today (``now + prep_minutes`` ≤ the dispatch deadline), else the
        next opening that leaves that much time; ``None`` when no such day lies within the horizon."""
        _aware(now, "now")
        prep = timedelta(minutes=self.prep_minutes)
        cutoff = timedelta(minutes=self.dispatch_cutoff_minutes)
        for opens, closes in self._intervals_from(now):
            start = max(opens, now)
            if start + prep <= closes - cutoff:
                return start
        return None

    def add_working_minutes(self, start: datetime, minutes: int) -> datetime | None:
        """``start`` plus ``minutes`` counted only while the warehouse is open (a start outside the hours
        counts from the next opening); ``None`` beyond the horizon. Zero minutes return the first open
        instant at or after ``start``."""
        _aware(start, "start")
        if minutes < 0:
            raise ValueError("minutes must be ≥ 0")
        left = timedelta(minutes=minutes)
        for opens, closes in self._intervals_from(start):
            begin = max(opens, start)
            if begin + left <= closes:
                return begin + left
            left -= closes - begin
        return None

    def eta_window(self, eta_min_minutes: int, eta_max_minutes: int, now: datetime) -> EtaWindow | None:
        """The window promised for a method whose courier needs ``eta_min..eta_max`` minutes once called: from
        :meth:`fulfilment_start` + ``prep_minutes``; ``scheduled`` when that start is not ``now``. ``None``
        when no open day lies within the horizon."""
        if not 0 <= eta_min_minutes <= eta_max_minutes:
            raise ValueError("expected 0 ≤ eta_min_minutes ≤ eta_max_minutes")
        start = self.fulfilment_start(now)
        if start is None:
            return None
        ready = start + timedelta(minutes=self.prep_minutes)
        return EtaWindow(
            eta_from=ready + timedelta(minutes=eta_min_minutes),
            eta_to=ready + timedelta(minutes=eta_max_minutes),
            scheduled=start != now,
        )

    # --- dispatch

    def suspended_at(self, now: datetime) -> bool:
        """Dispatch is suspended at ``now`` (no ``suspended_until`` = until staff lift it)."""
        _aware(now, "now")
        return self.dispatch_suspended and (self.suspended_until is None or now < self.suspended_until)

    def dispatch_refusal(self, now: datetime) -> DispatchRefusal | None:
        """Why a courier may not be called at ``now``, or ``None``: the warehouse is not configured,
        dispatch is suspended, the warehouse is closed, it is past the dispatch deadline (a failed hand-over
        could not come back before closing), or returned parcels are not accepted (in that order)."""
        _aware(now, "now")
        if not self.configured:
            return REFUSAL_NOT_CONFIGURED
        if self.suspended_at(now):
            return REFUSAL_SUSPENDED
        if not self.is_open(now):
            return REFUSAL_CLOSED
        deadline = self.dispatch_deadline(now.astimezone(TASHKENT).date())
        if deadline is not None and now > deadline:
            return REFUSAL_AFTER_CUTOFF
        if not self.returns_accepted:
            return REFUSAL_RETURNS_UNAVAILABLE
        return None


__all__ = [
    "HORIZON_DAYS",
    "REFUSAL_AFTER_CUTOFF",
    "REFUSAL_CLOSED",
    "REFUSAL_NOT_CONFIGURED",
    "REFUSAL_RETURNS_UNAVAILABLE",
    "REFUSAL_SUSPENDED",
    "DispatchRefusal",
    "EtaWindow",
    "WarehouseCalendar",
]
