"""Helpers the customer reads share: media URLs, the translated fields and the SEO block."""

from __future__ import annotations

from typing import Any

from glowderma.catalog.models import Product
from glowderma.core import enums
from glowderma.core.media import absolute_media_url, url_fields

NAME_COLLATION = "glowderma_ci"  # ICU case-insensitive collation (core.0007), used in ORDER BY only


def media_url(file: Any) -> str | None:
    """The absolute URL of a stored file (``PUBLIC_MEDIA_BASE_URL``), ``None`` when empty."""
    name = getattr(file, "name", None)
    return absolute_media_url(str(name)) if name else None


def media_urls(prefix: str, file: Any) -> dict[str, str | None]:
    """``media_url`` and its variant URLs under the API's names (``core.media.url_fields``)."""
    name = getattr(file, "name", None)
    return url_fields(prefix, str(name) if name else None)


def localized_field(obj: object, field: str, lang: str) -> str:
    """``<field>_<lang>`` → ``<field>_uz`` → ``""`` (customer contract §1.4)."""
    return str(getattr(obj, f"{field}_{lang}", "") or getattr(obj, f"{field}_uz", "") or "")


def product_name(product: Product, lang: str) -> str:
    """The resolved-language name, then uz, then ``billz_name``."""
    return localized_field(product, "name", lang) or product.billz_name


def seo_payload(obj: Any, lang: str) -> dict[str, str]:
    """``seo_title`` / ``seo_description`` (B-W1) in the resolved language only — never another language's
    text in a page's meta tags; ``""`` = the website builds them (name, volume, category, price)."""
    language = lang if lang in enums.ContentLanguage.values else enums.ContentLanguage.UZ
    return {
        "seo_title": str(getattr(obj, f"seo_title_{language}", "") or ""),
        "seo_description": str(getattr(obj, f"seo_description_{language}", "") or ""),
    }


__all__ = [
    "NAME_COLLATION",
    "localized_field",
    "media_url",
    "media_urls",
    "product_name",
    "seo_payload",
]
