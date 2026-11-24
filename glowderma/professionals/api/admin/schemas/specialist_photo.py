"""OpenAPI declarations for A specialist's photo: upload and removal."""

from __future__ import annotations

from drf_spectacular.utils import extend_schema, inline_serializer
from rest_framework import serializers

from glowderma.core.api.schema import ADMIN_ERRORS
from glowderma.professionals.api.admin.serializers import (
    ClinicSpecialistSerializer,
)

SPECIALIST_PHOTO_PUT = extend_schema(
    operation_id="admin_specialists_photo_update",
    description="One multipart part `file` (JPEG, PNG or WebP, ≤ 5 MiB); replaces the current photo.",
    request={
        "multipart/form-data": inline_serializer(
            name="SpecialistPhotoUpload", fields={"file": serializers.FileField()}
        )
    },
    responses={200: ClinicSpecialistSerializer, **ADMIN_ERRORS},
)

SPECIALIST_PHOTO_DELETE = extend_schema(
    operation_id="admin_specialists_photo_destroy", responses={204: None, **ADMIN_ERRORS}
)


__all__ = [
    "SPECIALIST_PHOTO_DELETE",
    "SPECIALIST_PHOTO_PUT",
]
