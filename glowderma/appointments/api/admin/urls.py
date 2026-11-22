"""Admin routes of ``appointments`` (mounted under ``api/v1/admin/`` by ``config.urls``).

Every route name is prefixed ``admin-`` and unique across modules; the auth matrix test walks them all.
``reschedule/`` and ``cancel/`` are declared before the generic ``<str:action>/`` route (confirm | reject |
attend, contract §2.9) so they are never read as an action.
"""

from django.urls import URLPattern, URLResolver, path

from ..specialist import (
    ConflictsView,
    SpecialistAppointmentActionView,
    SpecialistAppointmentCancelView,
    SpecialistAppointmentRangeView,
    SpecialistAppointmentRescheduleView,
    SpecialistAppointmentsMetaView,
    SpecialistAppointmentsView,
    SpecialistPendingSummaryView,
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
    # The specialist's own board: the static routes come before ``<int:pk>``.
    path("specialist/appointments/", SpecialistAppointmentsView.as_view(), name="specialist-appointments"),
    path(
        "specialist/appointments/meta/",
        SpecialistAppointmentsMetaView.as_view(),
        name="specialist-appointments-meta",
    ),
    path(
        "specialist/appointments/range/",
        SpecialistAppointmentRangeView.as_view(),
        name="specialist-appointments-range",
    ),
    path(
        "specialist/appointments/pending-summary/",
        SpecialistPendingSummaryView.as_view(),
        name="specialist-appointments-pending-summary",
    ),
    path(
        "specialist/appointments/<int:pk>/reschedule/",
        SpecialistAppointmentRescheduleView.as_view(),
        name="specialist-appointments-reschedule",
    ),
    path(
        "specialist/appointments/<int:pk>/cancel/",
        SpecialistAppointmentCancelView.as_view(),
        name="specialist-appointments-cancel",
    ),
    path(
        "specialist/appointments/<int:pk>/<str:action>/",
        SpecialistAppointmentActionView.as_view(),
        name="specialist-appointments-action",
    ),
    path("specialist/conflicts/", ConflictsView.as_view(), name="specialist-conflicts"),
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
