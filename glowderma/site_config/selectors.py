"""Read side of `site_config` for the customer API (customer contract §2.4, §2.1.3, §2.2.4).

:func:`public_settings` serves ``GET settings/public/``: the database part (the singleton's texts and
phones) is cached for ``PUBLIC_SETTINGS_TTL`` seconds under one key, tagged with the cache generation it was
built under; ``services.update_settings`` starts a new generation when it commits, so a snapshot a request
read before that commit and writes back after it is never served (it carries the old generation). The
provider-derived parts (``payment_providers``, ``fulfilment_methods``, ``online_sales_enabled``) and the app
versions come from settings on every call. Reads never create the singleton row.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from typing import Any, cast

from django.conf import settings
from django.core.cache import cache

from glowderma.core import enums, integration_modes, timeutils
from glowderma.site_config.models import SiteSettings

logger = logging.getLogger(__name__)

PUBLIC_SETTINGS_CACHE_KEY = "site_config:public_settings:v2"  # {"generation", "snapshot"}
PUBLIC_SETTINGS_GENERATION_KEY = "site_config:public_settings:generation"
PUBLIC_SETTINGS_TTL = 60
LANGUAGES = tuple(enums.ContentLanguage.values)


@dataclass(frozen=True, slots=True)
class Terms:
    """The current terms version (``""`` = none) and whether the terms count as published (C§2.1.3)."""

    version: str
    published: bool
    updated_at: Any


def _row() -> SiteSettings:
    return SiteSettings.objects.get(pk=1)


def current_terms() -> Terms:
    """Published = ``terms_version`` set **and** the uz texts of the terms and the privacy policy filled
    (``terms_version`` versions both documents)."""
    row = _row()
    published = bool(row.terms_version and row.terms_uz.strip() and row.privacy_policy_uz.strip())
    return Terms(row.terms_version, published, row.terms_updated_at)


def outdated_consent(customer_terms_version: str) -> str | None:
    """The published terms version when the customer's consent is older (C§2.2.4), else ``None``: the one rule
    checkout and booking refuse with 409 ``terms_update_required`` (and ``me/`` reports as
    ``update_required``). Unpublished terms (§2.1.3) never block."""
    terms = current_terms()
    if terms.published and customer_terms_version != terms.version:
        return terms.version
    return None


def _texts(row: SiteSettings, field: str) -> dict[str, str]:
    return {lang: str(getattr(row, f"{field}_{lang}")) for lang in LANGUAGES}


def _snapshot() -> dict[str, Any]:
    row = _row()
    return {
        "support_phone": row.support_phone or None,
        "store_phone": row.store_phone or None,
        "cosmetologist_phone": row.cosmetologist_phone or None,
        "telegram_url": row.telegram_url or None,
        "instagram_url": row.instagram_url or None,
        "website_url": row.website_url or None,
        "pickup_address": _texts(row, "pickup_address"),
        "privacy_policy": _texts(row, "privacy_policy"),
        "terms": _texts(row, "terms"),
        "booking_policy": _texts(row, "booking_policy"),
        "terms_version": row.terms_version or None,
        "terms_updated_at": timeutils.isoformat(row.terms_updated_at) if row.terms_updated_at else None,
        "reservation_minutes": row.reservation_minutes,
    }


def invalidate_public_settings() -> None:
    """Start a new cache generation (on commit of a settings change): every snapshot built under an older
    one is a miss from now on, including one a request that read the row before the change writes back
    after this call."""
    cache.set(PUBLIC_SETTINGS_GENERATION_KEY, uuid.uuid4().hex, timeout=None)


def localized(texts: dict[str, str], lang: str) -> str:
    """Requested language → uz → ``""`` (customer contract §1.4)."""
    return texts.get(lang) or texts.get(enums.ContentLanguage.UZ) or ""


def _generation(found: dict[str, Any]) -> str:
    """The current cache generation (read with the snapshot), created when missing (first use, eviction).
    It is taken before the database read, so a change committing after it is a newer generation."""
    generation = found.get(PUBLIC_SETTINGS_GENERATION_KEY)
    if isinstance(generation, str):
        return generation
    fresh = uuid.uuid4().hex
    if cache.add(PUBLIC_SETTINGS_GENERATION_KEY, fresh, timeout=None):
        return fresh
    current = cache.get(PUBLIC_SETTINGS_GENERATION_KEY)
    return current if isinstance(current, str) else fresh


def _cached_snapshot() -> dict[str, Any]:
    """The 60 s cached snapshot of the current generation (one round trip on a hit); a Redis error falls back
    to the database (settings/public/ is read at every app start and must not answer 500 while the cache is
    down)."""
    try:
        found = cache.get_many([PUBLIC_SETTINGS_GENERATION_KEY, PUBLIC_SETTINGS_CACHE_KEY])
        entry = found.get(PUBLIC_SETTINGS_CACHE_KEY)
        generation = _generation(found)
    except Exception:
        logger.warning("public_settings cache read failed", exc_info=True)
        return _snapshot()
    if isinstance(entry, dict) and entry.get("generation") == generation:
        return cast(dict[str, Any], entry["snapshot"])
    snapshot = _snapshot()
    try:
        cache.set(
            PUBLIC_SETTINGS_CACHE_KEY, {"generation": generation, "snapshot": snapshot}, PUBLIC_SETTINGS_TTL
        )
    except Exception:
        logger.warning("public_settings cache write failed", exc_info=True)
    return snapshot


def public_settings(lang: str) -> dict[str, Any]:
    """The ``GET settings/public/`` body in ``lang`` (customer contract §2.4)."""
    snapshot = _cached_snapshot()
    return {
        "support_phone": snapshot["support_phone"],
        "store_phone": snapshot["store_phone"],
        "cosmetologist_phone": snapshot["cosmetologist_phone"],
        "telegram_url": snapshot["telegram_url"],
        "instagram_url": snapshot["instagram_url"],
        "website_url": snapshot["website_url"],
        "pickup_address": localized(snapshot["pickup_address"], lang),
        "privacy_policy": localized(snapshot["privacy_policy"], lang),
        "terms": localized(snapshot["terms"], lang),
        "booking_policy": localized(snapshot["booking_policy"], lang),
        "terms_version": snapshot["terms_version"] if _published(snapshot) else None,
        "terms_updated_at": snapshot["terms_updated_at"],
        "reservation_minutes": snapshot["reservation_minutes"],
        "payment_providers": integration_modes.payable_providers(),
        "online_sales_enabled": integration_modes.online_sales_enabled(),
        "fulfilment_methods": integration_modes.fulfilment_methods(),
        "min_app_version": {
            enums.OrderChannel.IOS.value: str(settings.MIN_APP_VERSION_IOS),
            enums.OrderChannel.ANDROID.value: str(settings.MIN_APP_VERSION_ANDROID),
        },
        "store_urls": {
            enums.OrderChannel.IOS.value: str(settings.APP_STORE_URL),
            enums.OrderChannel.ANDROID.value: str(settings.PLAY_STORE_URL),
        },
    }


def _published(snapshot: dict[str, Any]) -> bool:
    return bool(
        snapshot["terms_version"]
        and snapshot["terms"].get(enums.ContentLanguage.UZ, "").strip()
        and snapshot["privacy_policy"].get(enums.ContentLanguage.UZ, "").strip()
    )
