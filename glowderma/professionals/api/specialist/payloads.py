"""The response body of the ``specialist/`` profile writes."""

from __future__ import annotations

from typing import Any

from glowderma.professionals.api.admin.payloads import specialist_body
from glowderma.professionals.api.specialist.serializers import SpecialistOwnProfileSerializer


def own_profile_body(professional_id: int) -> dict[str, Any]:
    """The ``SpecialistOwnProfile`` of a fresh read (the profile, service and weekly-hours writes return
    it)."""
    return specialist_body(professional_id, serializer=SpecialistOwnProfileSerializer)


__all__ = [
    "own_profile_body",
]
