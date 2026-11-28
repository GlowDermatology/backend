"""Test helper for ``core.storefront``: the website cache tags an action marks stale once it commits."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from glowderma.core import storefront
from glowderma.core.models import OutboxMessage


def revalidated(capture: Any, action: Callable[[], object]) -> set[str]:
    """Runs ``action`` and its commit callbacks (``capture`` is pytest-django's
    ``django_capture_on_commit_callbacks``); returns every tag the storefront messages it enqueued carry."""
    OutboxMessage.objects.filter(topic=storefront.TOPIC).delete()
    with capture(execute=True):
        action()
    rows = OutboxMessage.objects.filter(topic=storefront.TOPIC)
    return {tag for row in rows for tag in row.payload["tags"]}
