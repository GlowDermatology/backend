"""OpenAPI declarations for the specialist's own profile and photo."""

from __future__ import annotations

from drf_spectacular.utils import extend_schema, inline_serializer
from rest_framework import serializers

from glowderma.core.api.schema import ADMIN_ERRORS, IDEMPOTENCY_KEY_HEADER
from glowderma.professionals.api.specialist.serializers import (
    OwnProfileSerializer,
    SpecialistOwnProfileSerializer,
)

OWN_PROFILE_GET = extend_schema(
    operation_id="specialist_profile", responses={200: SpecialistOwnProfileSerializer, **ADMIN_ERRORS}
)

OWN_PROFILE_PATCH = extend_schema(
    operation_id="specialist_profile_update",
    description=(
        "Every field optional; texts merge leaf by leaf; `location_id`, `location_ids` and `position` "
        "are ignored. `is_active: true` with `publish_blockers` left answers 409 `conflict` naming them; "
        "`is_active: false` with requested/confirmed appointments ahead answers 409 unless `force: true` "
        "(the appointments are kept either way)."
    ),
    request=OwnProfileSerializer,
    parameters=[IDEMPOTENCY_KEY_HEADER],
    responses={200: SpecialistOwnProfileSerializer, **ADMIN_ERRORS},
)

OWN_PHOTO_PUT = extend_schema(
    operation_id="specialist_profile_photo_update",
    description="One multipart part `file` (JPEG, PNG or WebP, ≤ 5 MiB); replaces the current photo.",
    request={
        "multipart/form-data": inline_serializer(
            name="OwnPhotoUpload", fields={"file": serializers.FileField()}
        )
    },
    responses={200: SpecialistOwnProfileSerializer, **ADMIN_ERRORS},
)

OWN_PHOTO_DELETE = extend_schema(
    operation_id="specialist_profile_photo_destroy", responses={204: None, **ADMIN_ERRORS}
)


__all__ = [
    "OWN_PHOTO_DELETE",
    "OWN_PHOTO_PUT",
    "OWN_PROFILE_GET",
    "OWN_PROFILE_PATCH",
]
