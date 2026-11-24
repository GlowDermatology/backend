"""The ``specialist/`` routes of ``professionals`` (mounted by ``professionals/api/admin/urls.py``): the
signed-in specialist's own profile, photo, services and availability (weekly hours, date overrides, blocked
periods). Every route resolves the profile from the signed-in account only (``base``); ``serializers`` holds
the own profile and availability shapes, ``views`` one module per subject and ``schemas`` their OpenAPI
declarations.
"""

from glowderma.professionals.api.specialist.base import (
    SpecialistProfileAPIView,
    require_active_profile,
)
from glowderma.professionals.api.specialist.views.availability import (
    AvailabilityView,
    BlocksView,
    OverridesView,
    WeeklyHoursView,
)
from glowderma.professionals.api.specialist.views.own_services import (
    OwnServicesView,
    OwnServiceView,
)
from glowderma.professionals.api.specialist.views.profile import (
    OwnPhotoView,
    OwnProfileView,
)

__all__ = [
    "AvailabilityView",
    "BlocksView",
    "OverridesView",
    "OwnPhotoView",
    "OwnProfileView",
    "OwnServiceView",
    "OwnServicesView",
    "SpecialistProfileAPIView",
    "WeeklyHoursView",
    "require_active_profile",
]
