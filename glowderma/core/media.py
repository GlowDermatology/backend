"""Public media naming and URLs.

Contract 1.4: media URLs are "Absolute, built from `PUBLIC_MEDIA_BASE_URL`, never from the request Host".
"""

from hashlib import sha256
from typing import Final, Literal

from django.conf import settings

type Variant = Literal["thumbnail", "medium"]

# Longest side in pixels of each smaller copy written next to every stored image (``core.variants``).
VARIANT_SIDES: Final[dict[Variant, int]] = {"thumbnail": 400, "medium": 1000}


def absolute_media_url(name: str) -> str:
    base = str(settings.PUBLIC_MEDIA_BASE_URL)
    return base.rstrip("/") + "/" + name.lstrip("/")


def media_name(kind: str, owner_id: int | str, content: bytes, extension: str) -> str:
    """Storage name ``<kind>/<owner_id>/<sha256 prefix>.<extension>``: content-addressed, cache-safe."""
    return f"{kind}/{owner_id}/{sha256(content).hexdigest()[:16]}.{extension}"


def variant_name(name: str, variant: Variant) -> str:
    """``products/1/ab.jpg`` → ``products/1/ab_400.webp``: the variant of a stored name (or of its URL).

    Derived from the name the field holds (which may carry a storage collision suffix), never from the bytes;
    only the last path segment changes.
    """
    head, slash, segment = name.rpartition("/")
    stem = segment.rpartition(".")[0] or segment
    return f"{head}{slash}{stem}_{VARIANT_SIDES[variant]}.webp"


def variant_url(name: str, variant: Variant) -> str:
    return absolute_media_url(variant_name(name, variant))
