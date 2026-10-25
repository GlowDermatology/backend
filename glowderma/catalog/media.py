"""``upload_to`` callables of the catalog images.

Every stored name is ``<kind>/<owner id>/<sha256[:16]>.<ext>`` (``core.media.media_name``): the service
computes the content-addressed basename from the re-encoded bytes and passes it as the file name, so these
callables only prefix the owner directory. ``Banner``/``Category`` files are saved in a second step, after the
row has an id.
"""

from pathlib import PurePosixPath
from typing import Any


def _basename(filename: str) -> str:
    return PurePosixPath(filename).name


def category_image_path(instance: Any, filename: str) -> str:
    return f"categories/{instance.pk}/{_basename(filename)}"


def product_image_path(instance: Any, filename: str) -> str:
    return f"products/{instance.product_id}/{_basename(filename)}"


def banner_image_path(instance: Any, filename: str) -> str:
    return f"banners/{instance.pk}/{_basename(filename)}"
