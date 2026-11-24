"""The shapes of the ``specialist/`` routes: the own profile (a ``ClinicSpecialist`` plus the assigned
clinics), its write and the availability."""

from __future__ import annotations

from typing import Any

from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from glowderma.professionals import selectors
from glowderma.professionals.api.admin.serializers import (
    BlockedPeriodSerializer,
    ClinicLocationSerializer,
    ClinicSpecialistPatchSerializer,
    ClinicSpecialistSerializer,
    HoursOverrideSerializer,
    WorkingDaySerializer,
)
from glowderma.professionals.models import Professional


class SpecialistOwnProfileSerializer(ClinicSpecialistSerializer):
    """``SpecialistOwnProfile``: the ``ClinicSpecialist`` of the signed-in specialist plus the clinics staff
    assigned (read-only for the specialist)."""

    clinics = serializers.SerializerMethodField(
        help_text="Assigned clinics, primary first, inactive ones included (`is_active`)."
    )

    @extend_schema_field(ClinicLocationSerializer(many=True))
    def get_clinics(self, obj: Professional) -> list[dict[str, Any]]:
        return [ClinicLocationSerializer(location).data for location in selectors.assigned_locations(obj)]


class SpecialistAvailabilitySerializer(serializers.Serializer[dict[str, Any]]):
    clinics = ClinicLocationSerializer(
        many=True, help_text="Assigned clinics, primary first, inactive ones included (`is_active`)."
    )
    working_hours = WorkingDaySerializer(many=True)
    overrides = HoursOverrideSerializer(many=True)
    blocks = BlockedPeriodSerializer(many=True)


OWN_PROFILE_IGNORED = ("location_id", "location_ids", "position")


class OwnProfileSerializer(ClinicSpecialistPatchSerializer):
    """``PATCH specialist/profile/`` (validated ``partial``): the staff profile fields without the clinics
    and the site order, which stay with staff (sent anyway, they are ignored). Texts merge leaf by leaf;
    ``is_active`` publishes or unpublishes under the staff rules (``force`` confirms an unpublish despite
    upcoming visits)."""

    def get_fields(self) -> dict[str, Any]:
        fields = super().get_fields()
        for name in OWN_PROFILE_IGNORED:
            del fields[name]
        return fields


__all__ = [
    "OWN_PROFILE_IGNORED",
    "OwnProfileSerializer",
    "SpecialistAvailabilitySerializer",
    "SpecialistOwnProfileSerializer",
]
