"""The specialist's appointments board (routes in ``appointments/api/admin/urls.py``).

The admin day, range, meta and pending-summary reads and the admin actions, narrowed to the signed-in
specialist's own profile. The item is the admin ``AppointmentItem`` without the customer's e-mail, plus the
customer's note and the actions the visit allows right now; the staff note is never part of it.

The package: ``serializers`` (the specialist's board shapes), ``base`` (the owned-appointment view and the
rendered item), ``views`` (``board``, ``bookings``, ``conflicts``) and ``schemas``.
"""

from glowderma.appointments.api.specialist.views.board import (
    SpecialistAppointmentRangeView,
    SpecialistAppointmentsMetaView,
    SpecialistAppointmentsView,
    SpecialistPendingSummaryView,
)
from glowderma.appointments.api.specialist.views.bookings import (
    SpecialistAppointmentActionView,
    SpecialistAppointmentCancelView,
    SpecialistAppointmentRescheduleView,
)
from glowderma.appointments.api.specialist.views.conflicts import (
    ConflictsView,
)

__all__ = [
    "ConflictsView",
    "SpecialistAppointmentActionView",
    "SpecialistAppointmentCancelView",
    "SpecialistAppointmentRangeView",
    "SpecialistAppointmentRescheduleView",
    "SpecialistAppointmentsMetaView",
    "SpecialistAppointmentsView",
    "SpecialistPendingSummaryView",
]
