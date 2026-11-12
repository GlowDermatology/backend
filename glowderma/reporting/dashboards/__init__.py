"""The four dashboards of the ops console (``/django-admin/``; plan §5): data builders.

Each builder (:mod:`.ops`, :mod:`.money`, :mod:`.sales`, :mod:`.customers`) reads the owning modules'
selectors — a handful of aggregate statements, never a per-row query — and returns a plain, picklable
``dict`` of preformatted view data: numbers as text (thousands groups, so‘m), traffic-light states with
their label, and for every number the admin changelist (with real query parameters) that lists the
rows behind it. :mod:`glowderma.reporting.admin_views` caches each dict for :data:`CACHE_SECONDS` and renders
it with Unfold's layout.

The texts are written in Uzbek (the gettext source language) and translated when a dashboard is built, into
the language of the console request that builds it; :func:`cached` therefore keeps one copy per language.

This module holds what the builders share: the states and their thresholds' vocabulary, link and number
formatting, and the cache.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable, Iterable, Mapping
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any
from urllib.parse import urlencode

from django.core.cache import cache
from django.urls import reverse
from django.utils.translation import get_language, gettext, ngettext
from django.utils.translation import gettext_lazy as _

from glowderma.core.i18n import format_number
from glowderma.core.timeutils import TASHKENT

if TYPE_CHECKING:
    from django_stubs_ext import StrOrPromise  # a development dependency: never imported at runtime

logger = logging.getLogger(__name__)

CACHE_SECONDS = 60
CACHE_PREFIX = "ops:dash:"  # = core.admin.DASHBOARD_CACHE_PREFIX (a console action clears its dashboards)
# How long the epoch of a translated dashboard (:func:`cached`) is kept: it only has to outlive the copies.
EPOCH_SECONDS = 24 * 60 * 60
# The most ids a "show these rows" link carries (``?id__in=…``); a longer list links its first ids.
LINK_IDS = 100

# Traffic-light states of a tile or a row, worst last.
NEUTRAL, OK, WARN, CRITICAL = "neutral", "ok", "warn", "critical"
STATE_RANK = {NEUTRAL: 0, OK: 1, WARN: 2, CRITICAL: 3}
STATE_LABELS: dict[str, StrOrPromise] = {
    NEUTRAL: _("Ma’lumot"),
    OK: _("Yaxshi"),
    WARN: _("Diqqat"),
    CRITICAL: _("Jiddiy"),
}
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
    """A state as the templates render it: key, label in the active language (or ``text``), Material
    icon."""
    return {"key": value, "label": text or str(STATE_LABELS[value]), "icon": STATE_ICONS[value]}


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


def capped(count: int, ids: Iterable[int]) -> str | None:
    """``Ro‘yxatda 100 / 234`` when a ``?id__in=`` link (:func:`ids_link`) lists fewer rows than the number it
    stands for (``None`` when it lists them all): the number is never silently larger than the list."""
    shown = min(len(list(ids)), LINK_IDS)
    if not shown or count <= shown:
        return None
    return gettext("Ro‘yxatda %(shown)s / %(count)s") % {"shown": num(shown), "count": num(count)}


def iso(instant: datetime) -> str:
    """A changelist datetime parameter: ISO 8601 with the Tashkent offset (never a naive value)."""
    return instant.astimezone(TASHKENT).isoformat(timespec="seconds")


# --- numbers and times --------------------------------------------------------------------------------------


def num(value: int | None) -> str:
    """``12 480`` (NBSP groups, the admin's number rule); ``—`` for ``None``."""
    return "—" if value is None else format_number(int(value), "uz")


def som(value: int | None) -> str:
    """``1 250 000 so‘m``; ``—`` for ``None``."""
    return "—" if value is None else gettext("%(amount)s so‘m") % {"amount": format_number(int(value), "uz")}


SOM_SHORT_UNITS: tuple[tuple[int, StrOrPromise], ...] = (
    (1_000_000_000, _("%(amount)s mlrd so‘m")),
    (1_000_000, _("%(amount)s mln so‘m")),
    (1_000, _("%(amount)s ming so‘m")),
)


def som_short(value: int) -> str:
    """Compact so‘m for tiles: ``12,4 mln so‘m``, ``1,2 mlrd so‘m``, ``850 ming so‘m``."""
    amount = int(value)
    for size, text in SOM_SHORT_UNITS:
        if abs(amount) >= size:
            scaled = f"{amount / size:.1f}".replace(".", ",").removesuffix(",0")
            return str(text) % {"amount": scaled}
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
        return gettext("hech qachon")
    seconds = int((now - instant).total_seconds())
    if seconds < 60:
        return gettext("hozirgina")
    if seconds < 3600:
        minutes = seconds // 60
        return ngettext("%(count)s daqiqa oldin", "%(count)s daqiqa oldin", minutes) % {"count": minutes}
    if seconds < 86400:
        hours = seconds // 3600
        return ngettext("%(count)s soat oldin", "%(count)s soat oldin", hours) % {"count": hours}
    days = seconds // 86400
    return ngettext("%(count)s kun oldin", "%(count)s kun oldin", days) % {"count": days}


def duration(seconds: float | None) -> str:
    """``42 s`` · ``3 daq 05 s`` · ``1 soat 12 daq``; ``—`` for ``None``."""
    if seconds is None:
        return "—"
    total = int(seconds)
    if total < 60:
        return gettext("%(seconds)s s") % {"seconds": total}
    if total < 3600:
        return gettext("%(minutes)s daq %(seconds)s s") % {
            "minutes": total // 60,
            "seconds": f"{total % 60:02d}",
        }
    return gettext("%(hours)s soat %(minutes)s daq") % {
        "hours": total // 3600,
        "minutes": f"{total % 3600 // 60:02d}",
    }


# --- cache --------------------------------------------------------------------------------------------------


def cached(
    name: str,
    build: Callable[[], dict[str, Any]],
    *,
    refresh: bool = False,
    seconds: int = CACHE_SECONDS,
    translated: bool = True,
) -> dict[str, Any]:
    """``build()`` cached under ``name`` for ``seconds``; ``refresh`` rebuilds and stores. A cache outage
    never takes a dashboard down: it is built directly (logged).

    A dashboard holds text in the language of the request that built it, so a ``translated`` payload (the
    default) is kept once per active language under ``<key>:<language>`` and never shown to a viewer of
    another language. ``<key>`` itself — the key a console action deletes
    (``core.admin.forget_cached_counts``) — then holds the *epoch* of those copies: a copy counts only while
    the key still holds the epoch it was built in, so deleting the key drops the dashboard in every language
    and ``refresh`` (a new epoch) renews it for every language. ``translated=False`` is for plain data
    without text: one copy under ``<key>``."""
    key = f"{CACHE_PREFIX}{name}"
    if not translated:
        return _cached_plain(key, build, refresh=refresh, seconds=seconds)
    copy = f"{key}:{get_language() or ''}"
    epoch: Any = None
    if not refresh:
        try:
            found = cache.get_many([key, copy])
        except Exception:
            logger.warning("ops dashboard cache unavailable", exc_info=True)
            return build()
        epoch, entry = found.get(key), found.get(copy)
        if isinstance(epoch, str) and isinstance(entry, Mapping) and entry.get("epoch") == epoch:
            data = entry.get("data")
            if isinstance(data, Mapping):
                return dict(data)
    value = build()
    try:
        if not isinstance(epoch, str):
            epoch = uuid.uuid4().hex
            cache.set(key, epoch, timeout=max(EPOCH_SECONDS, seconds))
        cache.set(copy, {"epoch": epoch, "data": value}, timeout=seconds)
    except Exception:
        logger.warning("ops dashboard cache unavailable", exc_info=True)
    return value


def _cached_plain(
    key: str, build: Callable[[], dict[str, Any]], *, refresh: bool, seconds: int
) -> dict[str, Any]:
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
    "capped",
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
