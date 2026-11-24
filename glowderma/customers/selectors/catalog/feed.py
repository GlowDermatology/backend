"""The storefront's endless product feed (``GET catalog/feed/``): the visible products newest first, paged by
an opaque keyset cursor over ``(created_at desc, id desc)`` (index ``catalog_product_created_order``)."""

from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass
from datetime import datetime

from django.db.models import Q

from glowderma.catalog.models import Category, Product
from glowderma.customers.selectors.catalog import products

FEED_PAGE_SIZE = 24
FEED_CURSOR_MAX_LENGTH = 128
_ID_MAX = 2**63 - 1


@dataclass(frozen=True, slots=True)
class FeedCursor:
    """The last row of the previous page: the next page starts strictly after it."""

    created_at: datetime
    id: int


def encode_cursor(cursor: FeedCursor) -> str:
    """URL-safe base64 (no padding) of ``"<ISO created_at>|<id>"``."""
    raw = f"{cursor.created_at.isoformat()}|{cursor.id}".encode()
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_cursor(value: str) -> FeedCursor | None:
    """The cursor ``value`` encodes, or ``None`` when it is not one of ours (malformed, naive, bad id)."""
    if not value or len(value) > FEED_CURSOR_MAX_LENGTH:
        return None
    try:
        raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)).decode("ascii")
        instant, _, pk = raw.partition("|")
        created_at = datetime.fromisoformat(instant)
        number = int(pk) if pk.isascii() and pk.isdigit() else 0
    except binascii.Error, UnicodeDecodeError, ValueError:
        return None
    if created_at.tzinfo is None or created_at.utcoffset() is None or not 0 < number <= _ID_MAX:
        return None
    return FeedCursor(created_at=created_at, id=number)


@dataclass(frozen=True, slots=True)
class FeedPage:
    products: list[Product]
    next_cursor: str | None


def product_feed(
    *, category: Category | None = None, after: FeedCursor | None = None, size: int = FEED_PAGE_SIZE
) -> FeedPage:
    """One page of ``size`` visible products (of ``category``, if given) after ``after``; ``next_cursor`` is
    ``None`` on the last page. One query (``size + 1`` rows tell whether another page follows)."""
    qs = products.customer_products()
    if category is not None:
        qs = qs.filter(category=category)
    if after is not None:
        qs = qs.filter(Q(created_at__lt=after.created_at) | Q(created_at=after.created_at, pk__lt=after.id))
    rows = list(qs.order_by("-created_at", "-id")[: size + 1])
    page = rows[:size]
    if len(rows) <= size or not page:
        return FeedPage(products=page, next_cursor=None)
    last = page[-1]
    cursor = FeedCursor(created_at=last.created_at, id=last.pk)
    return FeedPage(products=page, next_cursor=encode_cursor(cursor))


__all__ = [
    "FEED_CURSOR_MAX_LENGTH",
    "FEED_PAGE_SIZE",
    "FeedCursor",
    "FeedPage",
    "decode_cursor",
    "encode_cursor",
    "product_feed",
]
