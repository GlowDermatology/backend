"""OpenAPI declarations shared by the admin ``notifications`` views: the multipart form components and the
error map of the workflow actions."""

from __future__ import annotations

from drf_spectacular.utils import inline_serializer
from rest_framework import serializers

from glowderma.core import enums
from glowderma.core.api.schema import (
    ADMIN_ERRORS,
    ErrorEnvelopeSerializer,
)

NotificationFormSerializer = inline_serializer(
    name="NotificationForm",
    fields={
        "kind": serializers.ChoiceField(choices=enums.NotificationKind.values),
        "title": serializers.CharField(min_length=3, max_length=60),
        "body": serializers.CharField(min_length=5, max_length=180),
        "image": serializers.FileField(required=False),
        "link.kind": serializers.ChoiceField(choices=enums.LinkKind.values, required=False),
        "link.product_id": serializers.IntegerField(required=False),
        "link.url": serializers.CharField(required=False, max_length=500),
        "audience.type": serializers.ChoiceField(choices=enums.AudienceType.values),
        "audience.segment_id": serializers.IntegerField(required=False),
        "audience.phone": serializers.CharField(required=False),
        "scheduled_at": serializers.DateTimeField(required=False),
    },
)


# ``PATCH notifications/{id}/``: every content key optional (a missing key keeps the stored value), plus the
# revision the editor saw and the image removal flag.
NotificationPatchFormSerializer = inline_serializer(
    name="NotificationPatchForm",
    fields={
        "expected_revision": serializers.IntegerField(min_value=1),
        "kind": serializers.ChoiceField(choices=enums.NotificationKind.values, required=False),
        "title": serializers.CharField(min_length=3, max_length=60, required=False),
        "body": serializers.CharField(min_length=5, max_length=180, required=False),
        "image": serializers.FileField(required=False),
        "remove_image": serializers.BooleanField(required=False),
        "link.kind": serializers.ChoiceField(choices=enums.LinkKind.values, required=False),
        "link.product_id": serializers.IntegerField(required=False),
        "link.url": serializers.CharField(required=False, max_length=500),
        "audience.type": serializers.ChoiceField(choices=enums.AudienceType.values, required=False),
        "audience.segment_id": serializers.IntegerField(required=False),
        "audience.phone": serializers.CharField(required=False),
        "scheduled_at": serializers.DateTimeField(required=False, allow_null=True),
    },
)


# A workflow action may hit a provider (test pushes) or refuse a state (409); the envelope is the same.
ACTION_ERRORS = {**ADMIN_ERRORS, 503: ErrorEnvelopeSerializer}


__all__ = [
    "ACTION_ERRORS",
    "NotificationFormSerializer",
    "NotificationPatchFormSerializer",
]
