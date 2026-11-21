"""Admin routes of ``appointments`` (mounted under ``api/v1/admin/`` by ``config.urls``).

Every route name is prefixed ``admin-`` and unique across modules; the auth matrix test walks them all.
``reschedule/`` and ``cancel/`` are declared before the generic ``<str:action>/`` route (confirm | reject |
attend, contract §2.9) so they are never read as an action.
"""

from django.urls import URLPattern, URLResolver, path

from ..specialist import (
    BookingActionView,
    BookingCancelView,
    BookingRescheduleView,
    CalendarView,
    ConflictsView,
)
from .views import (
    AppointmentActionView,
    AppointmentCancelView,
    AppointmentRangeView,
    AppointmentRescheduleView,
    AppointmentsMetaView,
    AppointmentsView,
    PendingSummaryView,
    SpecialistConflictsView,
)

urlpatterns: list[URLPattern | URLResolver] = [
    path("specialist/calendar/", CalendarView.as_view(), name="specialist-calendar"),
    path("specialist/conflicts/", ConflictsView.as_view(), name="specialist-conflicts"),
    path(
        "specialist/bookings/<int:pk>/reschedule/",
        BookingRescheduleView.as_view(),
        name="specialist-booking-reschedule",
    ),
    path(
        "specialist/bookings/<int:pk>/cancel/", BookingCancelView.as_view(), name="specialist-booking-cancel"
    ),
    path(
        "specialist/bookings/<int:pk>/<str:action>/",
        BookingActionView.as_view(),
        name="specialist-booking-action",
    ),
    path("appointments/", AppointmentsView.as_view(), name="admin-appointments"),
    path("appointments/meta/", AppointmentsMetaView.as_view(), name="admin-appointments-meta"),
    path("appointments/range/", AppointmentRangeView.as_view(), name="admin-appointments-range"),
    path(
        "appointments/pending-summary/",
        PendingSummaryView.as_view(),
        name="admin-appointments-pending-summary",
    ),
    path(
        "appointments/<int:pk>/reschedule/",
        AppointmentRescheduleView.as_view(),
        name="admin-appointments-reschedule",
    ),
    path("appointments/<int:pk>/cancel/", AppointmentCancelView.as_view(), name="admin-appointments-cancel"),
    path(
        "appointments/<int:pk>/<str:action>/",
        AppointmentActionView.as_view(),
        name="admin-appointments-action",
    ),
    # The specialists API lives in ``professionals``; its conflicts route renders appointments, so it is
    # declared here (contract "Mutaxassislar va xizmatlar").
    path(
        "specialists/<int:pk>/conflicts/",
        SpecialistConflictsView.as_view(),
        name="admin-specialists-conflicts",
    ),
]
