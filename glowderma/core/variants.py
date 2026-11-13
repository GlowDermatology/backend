"""The smaller copies stored next to every uploaded image (``core.media.VARIANT_SIDES``).

Every access goes through the field's own storage object, so local disk and S3 behave the same. Variant names
are derived from the stored name of the original (``core.media.variant_name``); nothing about them is kept in
the database.
"""

import logging

from django.core.files.base import ContentFile
from django.core.files.storage import Storage

from glowderma.core.images import make_variants
from glowderma.core.media import VARIANT_SIDES, variant_name

logger = logging.getLogger(__name__)


def store_variants(storage: Storage, name: str, content: bytes) -> list[str]:
    """Write the variants of the original stored as ``name`` (``content`` = its bytes); return the names written.

    A variant that already exists is kept (names are content-addressed). Storages rename on a name collision,
    so a file that still landed under another name (a concurrent writer) is removed again. If a write fails,
    the variants this call wrote are removed and the error is re-raised: the caller's upload fails whole.
    """
    written: list[str] = []
    try:
        for variant, data in make_variants(content).items():
            target = variant_name(name, variant)
            if storage.exists(target):
                continue
            saved = storage.save(target, ContentFile(data))
            if saved != target:
                storage.delete(saved)
                continue
            written.append(saved)
    except BaseException:
        for path in written:
            try:
                storage.delete(path)
            except Exception:  # best effort: the original error matters more
                logger.warning("image variant cleanup failed for %s", path, exc_info=True)
        raise
    return written


def delete_with_variants(storage: Storage, name: str) -> None:
    """Delete the original stored as ``name`` and its variants; a missing file is not an error."""
    if not name:
        return
    for path in (name, *(variant_name(name, variant) for variant in VARIANT_SIDES)):
        storage.delete(path)
