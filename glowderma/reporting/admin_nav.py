"""Sidebar badges of the ops console (``UNFOLD["SIDEBAR"]``, config/settings/ops_admin.py).

Each callback takes the request and returns the count as text, or ``None`` for zero (Unfold hides the badge).
Counts come from the owning modules' selectors and are cached for a minute, so a page load never runs more
than three cheap ``COUNT(*)`` statements; a cache outage falls back to the direct count.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import timedelta

from django.core.cache import cache
from django.http import HttpRequest

from glowderma.core import consistency, timeutils
from glowderma.core.admin import NAV_CACHE_PREFIX, is_active_staff
from glowderma.integrations import selectors as integration_selectors
from glowderma.payments import selectors as payment_selectors

logger = logging.getLogger(__name__)

CACHE_SECONDS = 60
CACHE_PREFIX = NAV_CACHE_PREFIX  # a console action clears its badge (core.admin.forget_cached_counts)


def cached_count(name: str, compute: Callable[[], int]) -> int:
    key = f"{CACHE_PREFIX}{name}"
    try:
        value = cache.get(key)
    except Exception:  # a cache outage must not take the console down
        logger.warning("ops nav cache unavailable", exc_info=True)
        return compute()
    if value is None:
        value = compute()
        try:
            cache.set(key, value, timeout=CACHE_SECONDS)
        except Exception:
            logger.warning("ops nav cache unavailable", exc_info=True)
    return int(value)


def _badge(request: HttpRequest, name: str, compute: Callable[[], int]) -> str | None:
    if not is_active_staff(request):
        return None
    count = cached_count(name, compute)
    return str(count) if count else None


def dead_outbox(request: HttpRequest) -> str | None:
    """Outbox messages that went ``dead`` (any age): each needs a requeue or a manual resolution."""
    return _badge(request, "dead_outbox", consistency.dead_count)


def failed_webhooks(request: HttpRequest) -> str | None:
    """Provider callbacks rejected or failed in the last 24 hours."""
    return _badge(
        request,
        "failed_webhooks",
        lambda: integration_selectors.failed_webhooks_since(timeutils.now() - timedelta(hours=24)),
    )


def refunds_needing_action(request: HttpRequest) -> str | None:
    """Refunds ``failed`` / ``unknown`` / ``manual_required`` (the panel dashboard's attention count)."""
    return _badge(request, "refunds_needing_action", payment_selectors.refunds_needing_action)
