"""Admin views of `appointments` (contract §2.9; T10 admin create / reschedule / cancel).

One subject per module: ``board`` (day and range boards, pending summary, meta), ``bookings`` (create,
act, reschedule, cancel) and ``conflicts``; ``common`` holds the lookups they share. Their OpenAPI
declarations live in ``glowderma.appointments.api.admin.schemas``.
"""

from glowderma.appointments.api.admin.views.board import (
    AppointmentRangeView,
    AppointmentsMetaView,
    AppointmentsView,
    PendingSummaryView,
)
from glowderma.appointments.api.admin.views.bookings import (
    AppointmentActionView,
    AppointmentCancelView,
    AppointmentRescheduleView,
)
from glowderma.appointments.api.admin.views.conflicts import (
    SpecialistConflictsView,
)

__all__ = [
    "AppointmentActionView",
    "AppointmentCancelView",
    "AppointmentRangeView",
    "AppointmentRescheduleView",
    "AppointmentsMetaView",
    "AppointmentsView",
    "PendingSummaryView",
    "SpecialistConflictsView",
]
