"""The four dashboards of the ops console (``/django-admin/``; plan §5): data builders.

Each builder (:mod:`.ops`, :mod:`.money`, :mod:`.sales`, :mod:`.customers`) reads the owning modules'
selectors — a handful of aggregate statements, never a per-row query — and returns a plain, picklable
``dict`` of preformatted view data: numbers as text (thousands groups, so‘m), traffic-light states with
their Uzbek label, and for every number the admin changelist (with real query parameters) that lists the
rows behind it. :mod:`glowderma.reporting.admin_views` caches each dict for :data:`CACHE_SECONDS` and renders
it with Unfold's layout.

This module holds what the builders share: the states and their thresholds' vocabulary, link and number
formatting, and the cache.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Mapping
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import urlencode

from django.core.cache import cache
from django.urls import reverse

from glowderma.core.i18n import format_number
from glowderma.core.timeutils import TASHKENT

logger = logging.getLogger(__name__)

CACHE_SECONDS = 60
CACHE_PREFIX = "ops:dash:"
# The most ids a "show these rows" link carries (``?id__in=…``); a longer list links its first ids.
LINK_IDS = 100

# Traffic-light states of a tile or a row, worst last.
NEUTRAL, OK, WARN, CRITICAL = "neutral", "ok", "warn", "critical"
STATE_RANK = {NEUTRAL: 0, OK: 1, WARN: 2, CRITICAL: 3}
STATE_LABELS = {NEUTRAL: "Ma’lumot", OK: "Yaxshi", WARN: "Diqqat", CRITICAL: "Jiddiy"}
STATE_ICONS = {NEUTRAL: "info", OK: "check_circle", WARN: "warning", CRITICAL: "error"}


def worst(states: Iterable[str]) -> str:
    """The worst of ``states`` (``neutral`` for none)."""
    return max(states, key=lambda state: STATE_RANK[state], default=NEUTRAL)


def by_count(count: int, *, warn: int = 1, critical: int | None = None) -> str:
    """``ok`` below ``warn``, ``warn`` from it, ``critical`` from ``critical`` (when given)."""
    if critical is not None and count >= critical:
        return CRITICAL
    return WARN if count >= warn else OK


def by_age(age: timedelta | None, *, warn: timedelta, critical: timedelta) -> str:
    """``critical`` without a trace (``None``) or past ``critical``, ``warn`` past ``warn``, else ``ok``."""
    if age is None or age > critical:
        return CRITICAL
    return WARN if age > warn else OK


def state(value: str, text: str | None = None) -> dict[str, str]:
    """A state as the templates render it: key, Uzbek label (or ``text``), Material icon."""
    return {"key": value, "label": text or STATE_LABELS[value], "icon": STATE_ICONS[value]}


# --- links --------------------------------------------------------------------------------------------------


def changelist(model: str, **params: Any) -> str:
    """The admin changelist of ``"app_label_modelname"`` filtered by ``params`` (field lookups the admin
    accepts: local fields and the list filters' parameters)."""
    url = reverse(f"admin:{model}_changelist")
    clean = {key: value for key, value in params.items() if value is not None}
    return f"{url}?{urlencode(clean)}" if clean else url


def change(model: str, pk: Any) -> str:
    return reverse(f"admin:{model}_change", args=[pk])


def ids_link(model: str, ids: Iterable[int]) -> str | None:
    """The changelist of exactly these rows (``?id__in=``, at most :data:`LINK_IDS`); ``None`` for none."""
    chosen = [str(int(pk)) for pk in list(ids)[:LINK_IDS]]
    return changelist(model, id__in=",".join(chosen)) if chosen else None


def iso(instant: datetime) -> str:
    """A changelist datetime parameter: ISO 8601 with the Tashkent offset (never a naive value)."""
    return instant.astimezone(TASHKENT).isoformat(timespec="seconds")


# --- numbers and times --------------------------------------------------------------------------------------


def num(value: int | None) -> str:
    """``12 480`` (NBSP groups, the admin's number rule); ``—`` for ``None``."""
    return "—" if value is None else format_number(int(value), "uz")


def som(value: int | None) -> str:
    """``1 250 000 so‘m``; ``—`` for ``None``."""
    return "—" if value is None else f"{format_number(int(value), 'uz')} so‘m"


def som_short(value: int) -> str:
    """Compact so‘m for tiles: ``12,4 mln so‘m``, ``1,2 mlrd so‘m``, ``850 ming so‘m``."""
    amount = int(value)
    for size, unit in ((1_000_000_000, "mlrd"), (1_000_000, "mln"), (1_000, "ming")):
        if abs(amount) >= size:
            scaled = f"{amount / size:.1f}".replace(".", ",").removesuffix(",0")
            return f"{scaled} {unit} so‘m"
    return som(amount)


def percent(part: int, whole: int) -> int:
    """``part`` of ``whole`` as a whole percent (0 when ``whole`` is 0)."""
    return round(100 * part / whole) if whole else 0


def clock(instant: datetime | None) -> str:
    """``14:05`` (Tashkent)."""
    return "—" if instant is None else instant.astimezone(TASHKENT).strftime("%H:%M")


def stamp(instant: datetime | None) -> str:
    """``31.10 14:05`` (Tashkent)."""
    return "—" if instant is None else instant.astimezone(TASHKENT).strftime("%d.%m %H:%M")


def ago(instant: datetime | None, now: datetime) -> str:
    """``hozirgina`` · ``12 daqiqa oldin`` · ``3 soat oldin`` · ``2 kun oldin``; ``hech qachon`` for
    ``None``."""
    if instant is None:
        return "hech qachon"
    seconds = int((now - instant).total_seconds())
    if seconds < 60:
        return "hozirgina"
    if seconds < 3600:
        return f"{seconds // 60} daqiqa oldin"
    if seconds < 86400:
        return f"{seconds // 3600} soat oldin"
    return f"{seconds // 86400} kun oldin"


def duration(seconds: float | None) -> str:
    """``42 s`` · ``3 daq 05 s`` · ``1 soat 12 daq``; ``—`` for ``None``."""
    if seconds is None:
        return "—"
    total = int(seconds)
    if total < 60:
        return f"{total} s"
    if total < 3600:
        return f"{total // 60} daq {total % 60:02d} s"
    return f"{total // 3600} soat {total % 3600 // 60:02d} daq"


# --- cache --------------------------------------------------------------------------------------------------


def cached(
    name: str, build: Callable[[], dict[str, Any]], *, refresh: bool = False, seconds: int = CACHE_SECONDS
) -> dict[str, Any]:
    """``build()`` cached under ``name`` for ``seconds``; ``refresh`` rebuilds and stores. A cache outage
    never takes a dashboard down: it is built directly (logged)."""
    key = f"{CACHE_PREFIX}{name}"
    if not refresh:
        try:
            value = cache.get(key)
        except Exception:
            logger.warning("ops dashboard cache unavailable", exc_info=True)
            return build()
        if isinstance(value, Mapping):
            return dict(value)
    value = build()
    try:
        cache.set(key, value, timeout=seconds)
    except Exception:
        logger.warning("ops dashboard cache unavailable", exc_info=True)
    return value


def generated(now: datetime) -> dict[str, str]:
    """The «Yangilangan» stamp of a dashboard built at ``now``."""
    return {"clock": clock(now), "iso": iso(now)}


__all__ = [
    "CACHE_SECONDS",
    "CRITICAL",
    "LINK_IDS",
    "NEUTRAL",
    "OK",
    "WARN",
    "ago",
    "by_age",
    "by_count",
    "cached",
    "change",
    "changelist",
    "clock",
    "duration",
    "generated",
    "ids_link",
    "iso",
    "num",
    "percent",
    "som",
    "som_short",
    "stamp",
    "state",
    "worst",
]
