"""Public media naming and URLs.

Contract 1.4: media URLs are "Absolute, built from `PUBLIC_MEDIA_BASE_URL`, never from the request Host".
"""

from hashlib import sha256

from django.conf import settings


def absolute_media_url(name: str) -> str:
    base = str(settings.PUBLIC_MEDIA_BASE_URL)
    return base.rstrip("/") + "/" + name.lstrip("/")


def media_name(kind: str, owner_id: int | str, content: bytes, extension: str) -> str:
    """Storage name ``<kind>/<owner_id>/<sha256 prefix>.<extension>``: content-addressed, cache-safe."""
    return f"{kind}/{owner_id}/{sha256(content).hexdigest()[:16]}.{extension}"
