"""Admin routes of ``professionals`` (mounted under ``api/v1/admin/`` by ``config.urls``).

Every route name is prefixed ``admin-`` and unique across modules; the auth matrix test walks them all.
``specialists/{id}/conflicts/`` is declared by ``appointments`` (it renders appointments).
"""

from django.urls import URLPattern, URLResolver, path

from ..specialist import (
    AvailabilityView,
    BlocksView,
    OverridesView,
    OwnPhotoView,
    OwnProfileView,
    OwnServicesView,
    OwnServiceView,
    WeeklyHoursView,
)
from .views import (
    ClinicsView,
    ClinicView,
    SpecialistAccountRoleView,
    SpecialistBlockViewSet,
    SpecialistInviteView,
    SpecialistOverrideViewSet,
    SpecialistPhotoView,
    SpecialistServicesView,
    SpecialistServiceView,
    SpecialistViewSet,
    SpecialistWorkingHoursView,
)

specialists = SpecialistViewSet.as_view({"get": "list"})
specialist = SpecialistViewSet.as_view({"get": "retrieve", "patch": "partial_update"})
overrides = SpecialistOverrideViewSet.as_view({"get": "list", "post": "create"})
override = SpecialistOverrideViewSet.as_view({"delete": "destroy"})
blocks = SpecialistBlockViewSet.as_view({"get": "list", "post": "create"})
block = SpecialistBlockViewSet.as_view({"delete": "destroy"})

urlpatterns: list[URLPattern | URLResolver] = [
    path(
        "staff/<int:pk>/specialist-role/",
        SpecialistAccountRoleView.as_view(),
        name="admin-staff-specialist-role",
    ),
    path("specialist/profile/", OwnProfileView.as_view(), name="specialist-profile"),
    path("specialist/profile/photo/", OwnPhotoView.as_view(), name="specialist-profile-photo"),
    path("specialist/availability/", AvailabilityView.as_view(), name="specialist-availability"),
    path("specialist/weekly-hours/", WeeklyHoursView.as_view(), name="specialist-weekly-hours"),
    path("specialist/services/", OwnServicesView.as_view(), name="specialist-services"),
    path("specialist/services/<int:pk>/", OwnServiceView.as_view(), name="specialist-service"),
    path(
        "specialist/overrides/",
        OverridesView.as_view(http_method_names=["post", "options"]),
        name="specialist-overrides",
    ),
    path(
        "specialist/overrides/<int:pk>/",
        OverridesView.as_view(http_method_names=["put", "delete", "options"]),
        name="specialist-override",
    ),
    path(
        "specialist/blocks/",
        BlocksView.as_view(http_method_names=["post", "options"]),
        name="specialist-blocks",
    ),
    path(
        "specialist/blocks/<int:pk>/",
        BlocksView.as_view(http_method_names=["put", "delete", "options"]),
        name="specialist-block",
    ),
    path("specialists/", specialists, name="admin-specialists-list"),
    path("specialists/invite/", SpecialistInviteView.as_view(), name="admin-specialist-invite"),
    path("specialists/<int:pk>/", specialist, name="admin-specialists-detail"),
    path("specialists/<int:pk>/photo/", SpecialistPhotoView.as_view(), name="admin-specialists-photo"),
    path(
        "specialists/<int:pk>/services/", SpecialistServicesView.as_view(), name="admin-specialists-services"
    ),
    path(
        "specialists/<int:pk>/services/<int:service_id>/",
        SpecialistServiceView.as_view(),
        name="admin-specialists-service-detail",
    ),
    path(
        "specialists/<int:pk>/working-hours/",
        SpecialistWorkingHoursView.as_view(),
        name="admin-specialists-working-hours",
    ),
    path("specialists/<int:pk>/overrides/", overrides, name="admin-specialists-overrides"),
    path(
        "specialists/<int:pk>/overrides/<int:override_id>/",
        override,
        name="admin-specialists-override-detail",
    ),
    path("specialists/<int:pk>/blocks/", blocks, name="admin-specialists-blocks"),
    path("specialists/<int:pk>/blocks/<int:block_id>/", block, name="admin-specialists-block-detail"),
    path("clinics/", ClinicsView.as_view(), name="admin-clinics-list"),
    path("clinics/<int:pk>/", ClinicView.as_view(), name="admin-clinics-detail"),
]
