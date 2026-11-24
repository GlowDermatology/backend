"""Customer appointment routes (customer contract §2.11).

**O**: ``specialists/``, ``specialists/{id}/``, ``services/``, ``specialists/{id}/slots/``.
**C**: ``POST appointments/`` (**I**), ``GET me/appointments/`` (**P**), ``GET me/appointments/{id}/``,
``POST appointments/{id}/cancel/`` (**I**). The rules live in ``appointments.services`` / ``selectors``;
appointments of another customer or before the history horizon answer 404.

One subject per module: ``catalogue``, ``slots`` and ``bookings``; ``common`` holds the signed-in
customer. Their OpenAPI declarations live in ``glowderma.appointments.api.customer.schemas``.
"""

from glowderma.appointments.api.customer.views.bookings import (
    AppointmentCancelView,
    AppointmentCreateView,
    MyAppointmentsViewSet,
)
from glowderma.appointments.api.customer.views.catalogue import (
    ServiceListView,
    SpecialistBySlugView,
    SpecialistDetailView,
    SpecialistListView,
)
from glowderma.appointments.api.customer.views.slots import (
    SpecialistSlotsView,
)

__all__ = [
    "AppointmentCancelView",
    "AppointmentCreateView",
    "MyAppointmentsViewSet",
    "ServiceListView",
    "SpecialistBySlugView",
    "SpecialistDetailView",
    "SpecialistListView",
    "SpecialistSlotsView",
]
