"""Customer routes of `appointments` (customer contract §2.11); mounted by ``config.urls`` at ``api/v1/``."""

from django.urls import URLPattern, URLResolver, path

from .views import (
    AppointmentCancelView,
    AppointmentCreateView,
    ClinicListView,
    MyAppointmentsViewSet,
    ServiceListView,
    SpecialistAvailabilityView,
    SpecialistBySlugView,
    SpecialistDetailView,
    SpecialistListView,
    SpecialistSlotsView,
)

urlpatterns: list[URLPattern | URLResolver] = [
    path("specialists/", SpecialistListView.as_view(), name="customer-specialists"),
    # declared before the slug route: ``availability`` is a reserved specialist slug
    path(
        "specialists/availability/",
        SpecialistAvailabilityView.as_view(),
        name="customer-specialists-availability",
    ),
    path("specialists/<int:pk>/", SpecialistDetailView.as_view(), name="customer-specialist"),
    path("specialists/<int:pk>/slots/", SpecialistSlotsView.as_view(), name="customer-specialist-slots"),
    path("specialists/<slug:slug>/", SpecialistBySlugView.as_view(), name="customer-specialist-by-slug"),
    path("services/", ServiceListView.as_view(), name="customer-services"),
    path("clinics/", ClinicListView.as_view(), name="customer-clinics"),
    path("appointments/", AppointmentCreateView.as_view(), name="customer-appointments"),
    path(
        "appointments/<int:pk>/cancel/", AppointmentCancelView.as_view(), name="customer-appointment-cancel"
    ),
    path("me/appointments/", MyAppointmentsViewSet.as_view({"get": "list"}), name="customer-my-appointments"),
    path(
        "me/appointments/<int:pk>/",
        MyAppointmentsViewSet.as_view({"get": "retrieve"}),
        name="customer-my-appointment",
    ),
]
