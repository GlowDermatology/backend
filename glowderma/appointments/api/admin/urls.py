"""Admin routes of ``appointments`` (mounted under ``api/v1/admin/`` by ``config.urls``).

Every route name is prefixed ``admin-`` and unique across modules; the auth matrix test walks them all.
``reschedule/`` and ``cancel/`` are declared before the generic ``<str:action>/`` route (confirm | reject |
attend, contract §2.9) so they are never read as an action.
"""

from django.urls import URLPattern, URLResolver, path

from .views import (
    AppointmentActionView,
    AppointmentCancelView,
    AppointmentRescheduleView,
    AppointmentsMetaView,
    AppointmentsView,
    PendingSummaryView,
)

urlpatterns: list[URLPattern | URLResolver] = [
    path("appointments/", AppointmentsView.as_view(), name="admin-appointments"),
    path("appointments/meta/", AppointmentsMetaView.as_view(), name="admin-appointments-meta"),
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
]
