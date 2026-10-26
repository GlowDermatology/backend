"""``OrderItem.image_url`` holds the media storage path, not an absolute URL.

Snapshots taken before held ``absolute_media_url(name)`` (e.g.
``http://127.0.0.1:8000/media/products/1/a.webp``), which broke whenever ``PUBLIC_MEDIA_BASE_URL`` changed.
The URL is now rendered at serialization time; this data step strips the scheme, the host and the
``MEDIA_URL`` prefix from existing rows. Reversing keeps the path (the absolute form is derived, never stored
again).
"""

from typing import Any
from urllib.parse import urlsplit

from django.conf import settings
from django.db import migrations


def media_path(value: str) -> str:
    """The storage path of a stored image reference (idempotent for a value that is already a path)."""
    if not value:
        return value
    parts = urlsplit(value)
    path = parts.path if (parts.scheme or parts.netloc) else value
    candidates = {
        urlsplit(str(settings.MEDIA_URL)).path,
        urlsplit(str(settings.PUBLIC_MEDIA_BASE_URL)).path,
        "/media/",
    }
    # each prefix ends with "/" so it matches only on a path-segment boundary; the longest wins
    prefixes = {"/" + p.strip("/") + "/" for p in candidates if p.strip("/")}
    for prefix in sorted(prefixes, key=len, reverse=True):
        if path.startswith(prefix):
            path = path[len(prefix) :]
            break
    return path.lstrip("/")


def forwards(apps: Any, schema_editor: Any) -> None:
    OrderItem = apps.get_model("commerce", "OrderItem")
    for item in OrderItem.objects.exclude(image_url="").only("id", "image_url").iterator():
        path = media_path(item.image_url)
        if path != item.image_url:
            OrderItem.objects.filter(pk=item.pk).update(image_url=path)


class Migration(migrations.Migration):
    dependencies = [("commerce", "0004_cartitem_price_at_add")]

    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]
