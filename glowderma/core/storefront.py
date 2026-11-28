"""Storefront cache invalidation: a write the website shows marks the website's cached reads stale at once.

The website (Next.js) keeps each catalog read for minutes (its ``cacheLife``: products and categories 5
minutes, the landing 1 minute) under the tags of ``src/data/shop/tags.ts``. Without a signal from here, an
edit reached the website only when that lifetime ran out. Now every write the website shows calls
:func:`changed` with the tags of the reads it changes, and the website refetches them on the next visit.

How a change travels, and why it cannot be missed:

1. :func:`changed` runs inside the writer's transaction and registers the tags on its commit. A rolled-back
   write sends nothing.
2. After the commit the tags are enqueued as an outbox message (``storefront.revalidate``) keyed by the tag
   set and a :data:`WINDOW` of time, so a burst of writes (a Billz import, a reorder, a checkout touching many
   stocks) costs one call per tag set.
3. The message is due at the end of its window. A write that committed inside a window is therefore always
   covered by that window's call, which is sent after it. The outbox delivers at least once and retries a
   website that did not answer.
4. ``integrations.storefront`` makes the call (``POST /api/revalidate`` with the shared secret); the website
   expires the tags at once.

The cache lifetimes stay, as the safety net for a lost call (a crash between the commit and step 2) and for
what changes with time alone, with no write to signal it: a banner's start and end, a scheduled cashback
policy taking effect, a clinic opening or closing, the spotlight rotating with the hour.

Each app declares what it shows in its own ``storefront`` module: :func:`watch` for its models (a save or a
delete through the ORM, the Django admin and the ops console included), and :func:`changed` where it writes
with ``QuerySet.update()`` / ``bulk_*`` (no model signal) or outside the database (the bestsellers cache).
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Callable, Iterable
from datetime import timedelta
from functools import partial
from typing import Any

from django.db import models, transaction
from django.db.models.signals import m2m_changed, post_delete, post_save

from glowderma.core import outbox, timeutils

logger = logging.getLogger(__name__)

# The website's cache tags (frontend ``shopTags``). A product read always carries PRODUCTS (lists, pages,
# the landing), so one tag covers every product change; per-product tags would refresh nothing more.
PRODUCTS = "shop:products"
CATEGORIES = "shop:categories"
BANNERS = "shop:banners"
SPECIALISTS = "shop:specialists"
SERVICES = "shop:services"
SETTINGS = "shop:settings"
HOME = "shop:home"
AVAILABILITY = "shop:availability"
CLINICS = "shop:clinics"
TAGS = frozenset(
    {PRODUCTS, CATEGORIES, BANNERS, SPECIALISTS, SERVICES, SETTINGS, HOME, AVAILABILITY, CLINICS}
)

TOPIC = "storefront.revalidate"
# Writes committed within one window share one call per tag set; the call waits for the window's end.
WINDOW = timedelta(seconds=2)


def changed(*tags: str) -> None:
    """Marks the website's reads under ``tags`` stale once the current transaction commits (at once outside
    one). Unknown tags are a programming error."""
    unknown = set(tags) - TAGS
    if unknown:
        raise ValueError(f"Unknown storefront tags: {sorted(unknown)}")
    if tags:
        transaction.on_commit(partial(_enqueue, frozenset(tags)), robust=True)


def _enqueue(tags: frozenset[str]) -> None:
    """One outbox message per tag set and window, due at the window's end (after every write in it)."""
    now = timeutils.now()
    window = WINDOW.total_seconds()
    start = int(now.timestamp() // window * window)
    due_in = timedelta(seconds=start + window - now.timestamp())
    ordered = sorted(tags)
    digest = hashlib.sha256(",".join(ordered).encode()).hexdigest()[:16]
    outbox.enqueue(
        TOPIC,
        {"tags": ordered},
        dedupe_key=f"{TOPIC}:{digest}:{start}",
        delay=due_in,
    )
    _schedule_dispatch(start, due_in)


# Windows this process already asked the dispatcher to run after (an optimisation only: the outbox row is
# what guarantees delivery, the beat delivers it anyway).
_scheduled: dict[int, None] = {}
_SCHEDULED_MAX = 64


def _schedule_dispatch(window_start: int, due_in: timedelta) -> None:
    """Runs the outbox dispatcher right after the window instead of waiting for its 10-second beat."""
    from glowderma.core.tasks import dispatch_outbox

    if window_start in _scheduled:
        return
    _scheduled[window_start] = None
    while len(_scheduled) > _SCHEDULED_MAX:
        _scheduled.pop(next(iter(_scheduled)))
    try:
        dispatch_outbox.apply_async(countdown=due_in.total_seconds() + 0.2)
    except Exception:  # noqa: BLE001 - a broker outage: the beat delivers the message anyway
        logger.warning("storefront: could not schedule the outbox dispatcher; the beat will deliver")


def watch(model: type[models.Model], tags: Iterable[str] | Callable[[Any], Iterable[str]]) -> None:
    """Calls :func:`changed` with ``tags`` (or ``tags(instance)``) whenever a ``model`` row is saved or
    deleted through the ORM. Connected once per model (``dispatch_uid``), from the owning app's ``ready``."""
    fixed = () if callable(tags) else tuple(tags)

    def resolve(instance: Any) -> Iterable[str]:
        return tags(instance) if callable(tags) else fixed

    def handler(sender: type[models.Model], instance: Any, **kwargs: Any) -> None:
        if kwargs.get("raw"):  # loaddata: fixtures are not website edits
            return
        changed(*resolve(instance))

    uid = f"storefront:{model._meta.label}"
    post_save.connect(handler, sender=model, weak=False, dispatch_uid=f"{uid}:save")
    post_delete.connect(handler, sender=model, weak=False, dispatch_uid=f"{uid}:delete")


def watch_m2m(through: type[models.Model], tags: Iterable[str]) -> None:
    """Calls :func:`changed` with ``tags`` when a many-to-many relation (its ``through`` model) gains, loses
    or clears rows (``add`` / ``remove`` / ``set`` / ``clear`` send no ``post_save``)."""
    fixed = tuple(tags)

    def handler(sender: type[models.Model], action: str, **kwargs: Any) -> None:
        if action in {"post_add", "post_remove", "post_clear"}:
            changed(*fixed)

    m2m_changed.connect(
        handler, sender=through, weak=False, dispatch_uid=f"storefront:{through._meta.label}:m2m"
    )


__all__ = [
    "AVAILABILITY",
    "BANNERS",
    "CATEGORIES",
    "CLINICS",
    "HOME",
    "PRODUCTS",
    "SERVICES",
    "SETTINGS",
    "SPECIALISTS",
    "TAGS",
    "TOPIC",
    "WINDOW",
    "changed",
    "watch",
    "watch_m2m",
]
