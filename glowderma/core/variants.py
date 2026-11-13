"""The smaller copies stored next to every uploaded image (``core.media.VARIANT_SIDES``).

Every access goes through the field's own storage object, so local disk and S3 behave the same. Variant names
are derived from the stored name of the original (``core.media.variant_name``); nothing about them is kept in
the database.
"""

import logging
from dataclasses import dataclass, field

from django.apps import apps
from django.core.files.base import ContentFile
from django.core.files.storage import Storage
from django.db import models

from glowderma.core.images import make_variants
from glowderma.core.media import VARIANT_SIDES, variant_name

logger = logging.getLogger(__name__)


def store_variants(storage: Storage, name: str, content: bytes) -> list[str]:
    """Write the variants of the original stored as ``name`` (``content``: its bytes); return the new names.

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


@dataclass
class BackfillReport:
    checked: int = 0  # stored images looked at
    created: int = 0  # variant files written (or that a dry run would write)
    missing_originals: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)  # originals that could not be decoded or written


def backfill(*, dry_run: bool = False) -> BackfillReport:
    """Write the missing variants of every image stored in an ``ImageField`` of any installed model.

    Re-runnable: an existing variant is kept. An original missing from storage, or one whose variants cannot
    be made, is reported and skipped.
    """
    report = BackfillReport()
    for model in apps.get_models():
        for image_field in model._meta.get_fields():
            if not isinstance(image_field, models.ImageField):
                continue
            rows = model._default_manager.exclude(**{image_field.name: ""}).exclude(
                **{f"{image_field.name}__isnull": True}
            )
            for name in rows.values_list(image_field.name, flat=True).iterator():
                _backfill_one(image_field.storage, str(name), report, dry_run=dry_run)
    return report


def _backfill_one(storage: Storage, name: str, report: BackfillReport, *, dry_run: bool) -> None:
    report.checked += 1
    missing = [variant for variant in VARIANT_SIDES if not storage.exists(variant_name(name, variant))]
    if not missing:
        return
    if not storage.exists(name):
        report.missing_originals.append(name)
        return
    if dry_run:
        report.created += len(missing)
        return
    try:
        with storage.open(name, "rb") as original:
            content = original.read()
        report.created += len(store_variants(storage, name, content))
    except Exception:
        logger.warning("image variants failed for %s", name, exc_info=True)
        report.failed.append(name)
