"""Admin serializers of `professionals` (contract "Mutaxassislar va xizmatlar", FND-1 / FND-7), one subject
per module: ``specialists``, ``clinic_services``, ``weekly_hours``, ``overrides``, ``blocks``,
``schedule_query``, ``locations`` and ``workforce`` (staff accounts of specialists); ``texts`` holds the
translated write shapes, ``fields`` the field factories and ``messages`` the refusal texts.

Write serializers validate shapes and the value rules of the contract tables and resolve references
(``location_id``, ``staff_user_id``); the services own the state rules (uniqueness against stored rows,
overlaps, the deactivation guard). Read serializers render instances of the selectors' querysets
(``specialist_queryset`` prefetches ``service_rows`` and ``week_rows``).

OpenAPI components are prefixed ``Clinic`` (``ClinicSpecialist``, ``ClinicService``, ``ClinicLocation``)
because the customer API already owns ``Specialist`` and ``Location``.
"""

from glowderma.professionals.api.admin.serializers.blocks import (
    BlockedPeriodCreateSerializer,
    BlockedPeriodSerializer,
)
from glowderma.professionals.api.admin.serializers.clinic_services import (
    ClinicServiceSerializer,
    ClinicServiceWriteSerializer,
)
from glowderma.professionals.api.admin.serializers.fields import (
    CLOCK,
    DB_INT_MAX,
    MAX_POSITION,
    SLOT_STEPS,
    ClockField,
    field_refusal,
    id_field,
    price_field,
    validate_duration,
)
from glowderma.professionals.api.admin.serializers.locations import (
    ClinicLocationCreateSerializer,
    ClinicLocationPatchSerializer,
    ClinicLocationSerializer,
    ClinicLocationsSerializer,
)
from glowderma.professionals.api.admin.serializers.messages import (
    MESSAGES,
)
from glowderma.professionals.api.admin.serializers.overrides import (
    HoursOverrideCreateSerializer,
    HoursOverrideSerializer,
)
from glowderma.professionals.api.admin.serializers.schedule_query import (
    RANGE_LIMIT_DAYS,
    ScheduleRangeQuerySerializer,
)
from glowderma.professionals.api.admin.serializers.specialists import (
    PUBLISH_BLOCKERS,
    ClinicSpecialistPatchSerializer,
    ClinicSpecialistSerializer,
    ClinicSpecialistWriteSerializer,
    SpecialistAccountSerializer,
    SpecialistListQuerySerializer,
)
from glowderma.professionals.api.admin.serializers.texts import (
    LocationAddressSerializer,
    LocationCreateAddressSerializer,
    LocationCreateNameSerializer,
    LocationDirectionsSerializer,
    LocationNameSerializer,
    ServiceDescriptionSerializer,
    ServiceNameSerializer,
    ServiceShortNameSerializer,
    SpecialistTextSerializer,
)
from glowderma.professionals.api.admin.serializers.weekly_hours import (
    WorkingDayInputSerializer,
    WorkingDaySerializer,
    WorkingWeekSerializer,
)
from glowderma.professionals.api.admin.serializers.workforce import (
    SpecialistInviteCredentialsSerializer,
    SpecialistRoleChangeResultSerializer,
    SpecialistRoleChangeSerializer,
)

__all__ = [
    "SpecialistInviteCredentialsSerializer",
    "SpecialistRoleChangeResultSerializer",
    "SpecialistRoleChangeSerializer",
    "CLOCK",
    "DB_INT_MAX",
    "MAX_POSITION",
    "MESSAGES",
    "PUBLISH_BLOCKERS",
    "RANGE_LIMIT_DAYS",
    "SLOT_STEPS",
    "BlockedPeriodCreateSerializer",
    "BlockedPeriodSerializer",
    "ClinicLocationCreateSerializer",
    "ClinicLocationPatchSerializer",
    "ClinicLocationSerializer",
    "ClinicLocationsSerializer",
    "ClinicServiceSerializer",
    "ClinicServiceWriteSerializer",
    "ClinicSpecialistPatchSerializer",
    "ClinicSpecialistSerializer",
    "ClinicSpecialistWriteSerializer",
    "ClockField",
    "HoursOverrideCreateSerializer",
    "HoursOverrideSerializer",
    "LocationAddressSerializer",
    "LocationCreateAddressSerializer",
    "LocationCreateNameSerializer",
    "LocationDirectionsSerializer",
    "LocationNameSerializer",
    "ScheduleRangeQuerySerializer",
    "ServiceDescriptionSerializer",
    "ServiceNameSerializer",
    "ServiceShortNameSerializer",
    "SpecialistAccountSerializer",
    "SpecialistListQuerySerializer",
    "SpecialistTextSerializer",
    "WorkingDayInputSerializer",
    "WorkingDaySerializer",
    "WorkingWeekSerializer",
    "field_refusal",
    "id_field",
    "price_field",
    "validate_duration",
]
