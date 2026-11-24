"""Admin views of `professionals` (contract "Mutaxassislar va xizmatlar"; gaps FND-1 and FND-7), one subject
per module: ``specialists`` (list / retrieve / PATCH), ``specialist_photo``, ``clinic_services`` (their own
services), ``workforce`` (the staff account: invite and role), ``weekly_hours``, ``overrides``, ``blocks``
(blocked periods) and ``clinics`` (the clinic locations); ``common`` holds the lookups they share. Their
OpenAPI declarations live in ``glowderma.professionals.api.admin.schemas``.

Every service belongs to one specialist (there is no global catalogue); a service is deleted only if it was
never booked, else deactivated. Every write goes through :mod:`glowderma.professionals.services` (audited) and
honours an optional ``Idempotency-Key``. The customer catalogue and the free slots read the same rows on every
request (there is no slot cache), so a change is bookable — or no longer offered — at once.
``specialists/{id}/conflicts/`` is declared by ``appointments`` (it renders appointments).
"""

from glowderma.professionals.api.admin.views.blocks import (
    SpecialistBlockViewSet,
)
from glowderma.professionals.api.admin.views.clinic_services import (
    SpecialistServicesView,
    SpecialistServiceView,
)
from glowderma.professionals.api.admin.views.clinics import (
    ClinicsView,
    ClinicView,
)
from glowderma.professionals.api.admin.views.overrides import (
    SpecialistOverrideViewSet,
)
from glowderma.professionals.api.admin.views.specialist_photo import (
    SpecialistPhotoView,
)
from glowderma.professionals.api.admin.views.specialists import (
    SpecialistViewSet,
)
from glowderma.professionals.api.admin.views.weekly_hours import (
    SpecialistWorkingHoursView,
)
from glowderma.professionals.api.admin.views.workforce import (
    SpecialistAccountRoleView,
    SpecialistInviteView,
)

__all__ = [
    "ClinicView",
    "ClinicsView",
    "SpecialistAccountRoleView",
    "SpecialistBlockViewSet",
    "SpecialistInviteView",
    "SpecialistOverrideViewSet",
    "SpecialistPhotoView",
    "SpecialistServiceView",
    "SpecialistServicesView",
    "SpecialistViewSet",
    "SpecialistWorkingHoursView",
]
