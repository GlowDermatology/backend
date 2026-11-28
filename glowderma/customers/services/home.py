"""The admin «Bosh sahifa» write (admin contract §2.21): a landing section's hand-picked items, saved by the
module that owns them (``catalog`` for products and categories, ``professionals`` for specialists), each of
which checks them and audits ``home_section.saved``."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from glowderma.catalog import services as catalog_services
from glowderma.core import enums
from glowderma.professionals import services as professional_services


def save_home_section(key: str, ids: Sequence[str], *, actor: Any, request: Any = None) -> None:
    """Replaces section ``key``'s picks with ``ids`` (product or specialist ids, category slugs) in order; an
    empty list makes the section automatic again."""
    if key == enums.HomeSectionKey.SPECIALISTS:
        professional_services.set_home_specialists(ids, actor=actor, request=request)
    else:
        catalog_services.set_home_picks(key, ids, actor=actor, request=request)


__all__ = [
    "save_home_section",
]
