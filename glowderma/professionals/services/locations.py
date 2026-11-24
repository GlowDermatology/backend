"""The clinic locations (FND-1): create and update, under the location lock."""

from __future__ import annotations

from typing import Any

from django.db import models, transaction
from rest_framework.exceptions import NotFound
from rest_framework.exceptions import ValidationError as ApiValidationError

from glowderma.core import audit, timeutils
from glowderma.core.api.exceptions import Conflict, field_error
from glowderma.professionals import bookings
from glowderma.professionals.models import (
    Location,
    Professional,
)
from glowderma.professionals.services import common


def _location_state(location: Location) -> dict[str, Any]:
    return {
        **common.i18n_state(location, "name"),
        **common.i18n_state(location, "address"),
        **common.i18n_state(location, "directions"),
        "is_active": location.is_active,
        "phone": location.phone or None,
        "latitude": None if location.latitude is None else str(location.latitude),
        "longitude": None if location.longitude is None else str(location.longitude),
    }


def clinic_in_use(location_id: int) -> bool:
    return Professional.objects.filter(is_active=True).filter(
        models.Q(location_id=location_id) | models.Q(additional_locations=location_id)
    ).exists() or bookings.location_has_upcoming(location_id, timeutils.now())


@transaction.atomic
def create_location(data: dict[str, Any], *, actor: Any, request: Any = None) -> Location:
    common.advisory_lock(common.LOCATION_LOCK)
    return _write_location(
        Location(position=common.next_position(Location)), data, actor=actor, request=request
    )


@transaction.atomic
def update_location(location_id: int, data: dict[str, Any], *, actor: Any, request: Any = None) -> Location:
    location = Location.objects.select_for_update().filter(pk=location_id).first()
    if location is None:
        raise NotFound
    return _write_location(location, data, actor=actor, request=request)


def _write_location(location: Location, data: dict[str, Any], *, actor: Any, request: Any) -> Location:
    before = _location_state(location) if location.pk is not None else None
    for field in ("name", "address", "directions"):
        common.set_i18n(location, field, data.get(field))
    if "phone" in data:
        location.phone = data["phone"] or ""
    for key in ("latitude", "longitude", "is_active"):
        if key in data:
            setattr(location, key, data[key])
    if (location.latitude is None) != (location.longitude is None):
        missing = "latitude" if location.latitude is None else "longitude"
        raise ApiValidationError({missing: [field_error("latlng_pair", common.API_MESSAGES["latlng_pair"])]})
    after = _location_state(location)
    if after == before:
        return location
    if before and before["is_active"] and not location.is_active and clinic_in_use(location.pk):
        raise Conflict(common.API_MESSAGES["clinic_in_use"])
    location.save()
    diff_before, diff_after = (None, after) if before is None else common.diff(before, after)
    audit.record(
        action="location.created" if before is None else "location.updated",
        entity_type="location",
        entity_id=str(location.pk),
        actor=actor,
        request=request,
        before=diff_before,
        after=diff_after,
    )
    return location


__all__ = [
    "create_location",
    "update_location",
]
