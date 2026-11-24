"""The customer catalog's banners (customer contract §2.3)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from django.db.models import Q

from glowderma.catalog.models import Banner
from glowderma.core import enums
from glowderma.customers.selectors import common


def customer_banners(now: datetime) -> list[Banner]:
    """``is_active`` ∧ started ∧ not ended, by position and id, at most 20."""
    return list(
        Banner.objects.select_related("link_product", "link_category")
        .filter(is_active=True)
        .filter(Q(starts_at__isnull=True) | Q(starts_at__lte=now))
        .filter(Q(ends_at__isnull=True) | Q(ends_at__gt=now))
        .order_by("position", "id")[:BANNER_LIMIT]
    )


BANNER_LIMIT = 20


def banner_payload(banner: Banner, lang: str, visible_ids: set[int]) -> dict[str, Any]:
    """A link to an invisible product or an inactive category renders ``kind: "none"``; a URL link
    (``kind: "url"``, gap X-BANNER-URL) carries the https ``url`` the app opens."""
    link: dict[str, Any] = {
        "kind": enums.BannerLinkKind.NONE.value,
        "product_id": None,
        "category": None,
        "url": None,
    }
    if banner.link_kind == enums.BannerLinkKind.PRODUCT and banner.link_product_id in visible_ids:
        link = {"kind": banner.link_kind, "product_id": banner.link_product_id, "category": None, "url": None}
    elif (
        banner.link_kind == enums.BannerLinkKind.CATEGORY
        and banner.link_category is not None
        and banner.link_category.is_active
    ):
        link = {
            "kind": banner.link_kind,
            "product_id": None,
            "category": banner.link_category.slug,
            "url": None,
        }
    elif banner.link_kind == enums.BannerLinkKind.URL and banner.link_url:
        link = {"kind": banner.link_kind, "product_id": None, "category": None, "url": banner.link_url}
    return {
        "id": banner.pk,
        "title": common.localized_field(banner, "title", lang),
        **common.media_urls("image", banner.image),
        "image_width": banner.image_width,
        "image_height": banner.image_height,
        "link": link,
    }


__all__ = [
    "BANNER_LIMIT",
    "banner_payload",
    "customer_banners",
]
