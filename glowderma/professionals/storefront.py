"""What the website shows from ``professionals`` (``core.storefront``): the specialists (their pages, the
landing's section, the booking pages), their services, the clinics with their hours, the next free slots and
the landing's hand-picked specialists. The website's slot and clinic reads carry the specialists tag too.

``connect`` watches the models' saves and deletes and the clinics a specialist works at (a many-to-many);
the ``QuerySet.update()`` / ``bulk_*`` writes in ``professionals.services`` call ``storefront.changed``."""

from __future__ import annotations

from glowderma.core import storefront
from glowderma.professionals.models import (
    BlockedPeriod,
    HomeSpecialist,
    Location,
    Professional,
    Service,
    WorkingHours,
    WorkingHoursOverride,
)


def connect() -> None:
    storefront.watch(Professional, [storefront.SPECIALISTS, storefront.SERVICES])
    storefront.watch_m2m(
        Professional.additional_locations.through, [storefront.SPECIALISTS, storefront.CLINICS]
    )
    storefront.watch(Service, [storefront.SERVICES, storefront.SPECIALISTS])
    storefront.watch(Location, [storefront.CLINICS, storefront.SPECIALISTS])
    for schedule in (WorkingHours, WorkingHoursOverride, BlockedPeriod):
        storefront.watch(schedule, [storefront.AVAILABILITY, storefront.CLINICS])
    storefront.watch(HomeSpecialist, [storefront.HOME])


__all__ = [
    "connect",
]
