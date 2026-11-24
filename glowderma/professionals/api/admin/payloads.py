"""The response bodies the admin and specialist ``professionals`` views share: a specialist's fresh read with
its upcoming appointment count."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from rest_framework.exceptions import NotFound

from glowderma.core import timeutils
from glowderma.professionals import bookings, selectors
from glowderma.professionals.api.admin.serializers import (
    ClinicSpecialistSerializer,
)
from glowderma.professionals.models import (
    Professional,
)


def upcoming_counts(professionals: Iterable[Professional]) -> dict[int, int]:
    return bookings.upcoming_counts([professional.pk for professional in professionals], timeutils.now())


def specialist_body(
    professional_id: int, *, serializer: type[ClinicSpecialistSerializer] = ClinicSpecialistSerializer
) -> dict[str, Any]:
    """The ``ClinicSpecialist`` (or a subclass such as ``SpecialistOwnProfile``) of a fresh read (writes
    return it)."""
    professional = selectors.get_specialist(professional_id)
    if professional is None:  # pragma: no cover - the write just locked the row
        raise NotFound
    context = {"upcoming": upcoming_counts([professional])}
    return dict(serializer(professional, context=context).data)


__all__ = [
    "specialist_body",
    "upcoming_counts",
]
