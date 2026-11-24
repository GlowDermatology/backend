"""A specialist's photo (FND-1): upload and removal, with the file deleted once the transaction commits."""

from __future__ import annotations

from hashlib import sha256
from typing import Any

from django.core.files.base import ContentFile
from django.db import transaction
from rest_framework.exceptions import ValidationError as ApiValidationError

from glowderma.core import audit, timeutils
from glowderma.core.api.exceptions import field_error
from glowderma.core.images import ImageError, process_image
from glowderma.core.media import media_name
from glowderma.core.variants import delete_with_variants, store_variants
from glowderma.professionals.models import (
    Professional,
)
from glowderma.professionals.services import common


def set_specialist_photo(
    professional_id: int, upload: Any, *, actor: Any, request: Any = None
) -> Professional:
    """``PUT specialists/{id}/photo/``: re-encoded by ``core.images`` (the product-image limits), stored under
    a content-addressed name; the previous file is removed after commit. Audit ``professional.photo_updated``
    (the same bytes again change nothing).

    The new file is written under the row lock (local storage today). If this block fails after the write
    (the audit insert, say), the new file is removed again so a rollback leaves no orphan; a rollback of an
    enclosing transaction (the Idempotency-Key wrapper) after this block returned cannot be seen here. A
    remote storage backend should move the upload before the lock (catalog banners share this shape)."""
    try:
        processed = process_image(upload)
    except ImageError as exc:
        raise ApiValidationError({"file": [field_error(exc.code)]}) from exc
    written, storage = "", None
    try:
        with transaction.atomic():
            professional = common.locked_professional(professional_id)
            previous = professional.photo.name or ""
            name = media_name(common.PHOTO_KIND, professional.pk, processed.content, processed.extension)
            if previous == name:
                # nothing replaced; a variant that went missing (or predates the variants) is written again
                store_variants(professional.photo.storage, previous, processed.content)
                return professional
            professional.photo.save(name.rsplit("/", 1)[-1], ContentFile(processed.content), save=False)
            written, storage = professional.photo.name or "", professional.photo.storage
            store_variants(storage, written, processed.content)
            professional.save(update_fields=["photo"])
            if previous:
                remove_file_on_commit(professional.photo.storage, previous)
            audit.record(
                action="professional.photo_updated",
                entity_type="professional",
                entity_id=str(professional.pk),
                actor=actor,
                request=request,
                before={"photo": previous or None},
                after={"photo": professional.photo.name, "sha256": sha256(processed.content).hexdigest()},
            )
    except BaseException:
        if written and storage is not None:
            delete_with_variants(storage, written)
        raise
    return professional


def delete_specialist_photo(professional_id: int, *, actor: Any, request: Any = None) -> None:
    """``DELETE specialists/{id}/photo/`` (idempotent): audit ``professional.photo_deleted`` when there was
    one; the file is removed after commit."""
    with transaction.atomic():
        professional = common.locked_professional(professional_id)
        previous = professional.photo.name or ""
        if not previous:
            return
        Professional.objects.filter(pk=professional.pk).update(photo="", updated_at=timeutils.now())
        remove_file_on_commit(professional.photo.storage, previous)
        audit.record(
            action="professional.photo_deleted",
            entity_type="professional",
            entity_id=str(professional.pk),
            actor=actor,
            request=request,
            before={"photo": previous},
            after={"photo": None},
        )


def remove_file_on_commit(storage: Any, name: str) -> None:
    transaction.on_commit(lambda: delete_with_variants(storage, name))


__all__ = [
    "delete_specialist_photo",
    "remove_file_on_commit",
    "set_specialist_photo",
]
