"""Admin serializers of `appointments` (contract §2.9 and the T10 admin create / reschedule / cancel).

One subject per module: ``board`` (the day board), ``range`` (the week and month boards), ``bookings``
(create, reschedule, cancel) and ``meta``; ``messages`` holds the refusal texts.
"""

from glowderma.appointments.api.admin.serializers.board import (
    AppointmentClinicSerializer,
    AppointmentItemSerializer,
    DayBlockSerializer,
    DayQuerySerializer,
    DayScheduleSerializer,
    PendingSummarySerializer,
    ScheduleConflictsSerializer,
    SpecialistDaySerializer,
    WorkingHoursSerializer,
)
from glowderma.appointments.api.admin.serializers.bookings import (
    AppointmentCancelSerializer,
    AppointmentCreateSerializer,
    AppointmentRescheduleSerializer,
    active_service,
    customer_by_phone,
)
from glowderma.appointments.api.admin.serializers.messages import (
    MESSAGES,
)
from glowderma.appointments.api.admin.serializers.meta import (
    AppointmentsMetaSerializer,
    BookingServiceSerializer,
    BookingSpecialistSerializer,
)
from glowderma.appointments.api.admin.serializers.range import (
    RANGE_MESSAGES,
    AppointmentRangeSerializer,
    RangeDaySerializer,
    RangeQuerySerializer,
    RangeScheduleSerializer,
    RangeSpecialistSerializer,
    RangeStatusCountsSerializer,
)

__all__ = [
    "MESSAGES",
    "RANGE_MESSAGES",
    "AppointmentCancelSerializer",
    "AppointmentClinicSerializer",
    "AppointmentCreateSerializer",
    "AppointmentItemSerializer",
    "AppointmentRangeSerializer",
    "AppointmentRescheduleSerializer",
    "AppointmentsMetaSerializer",
    "BookingServiceSerializer",
    "BookingSpecialistSerializer",
    "DayBlockSerializer",
    "DayQuerySerializer",
    "DayScheduleSerializer",
    "PendingSummarySerializer",
    "RangeDaySerializer",
    "RangeQuerySerializer",
    "RangeScheduleSerializer",
    "RangeSpecialistSerializer",
    "RangeStatusCountsSerializer",
    "ScheduleConflictsSerializer",
    "SpecialistDaySerializer",
    "WorkingHoursSerializer",
    "active_service",
    "customer_by_phone",
]
