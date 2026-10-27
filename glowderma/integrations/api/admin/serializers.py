"""Admin serializers of `integrations` (contract §2.3 Billz import, §2.14 Job, shared ``Actor``)."""

from typing import Any

from rest_framework import serializers

from glowderma.core import enums
from glowderma.core.api.serializers import ActorSerializer


class JobSerializer(serializers.Serializer[Any]):
    """Contract §2.14 ``Job``."""

    id = serializers.IntegerField()
    kind = serializers.ChoiceField(choices=enums.JobKind.values)
    state = serializers.ChoiceField(choices=enums.JobState.values)
    params = serializers.JSONField()
    created_at = serializers.DateTimeField()
    started_at = serializers.DateTimeField(allow_null=True)
    finished_at = serializers.DateTimeField(allow_null=True)
    progress = serializers.JSONField(allow_null=True)
    result = serializers.JSONField(allow_null=True)
    error = serializers.JSONField(allow_null=True)
    created_by = ActorSerializer(allow_null=True)


class BillzImportSerializer(serializers.Serializer[dict[str, Any]]):
    """Body of ``POST integrations/billz/import/``: ``{}`` or ``{"mode": "delta"|"full"}``."""

    mode = serializers.ChoiceField(choices=enums.BillzImportMode.values, default=enums.BillzImportMode.DELTA)
