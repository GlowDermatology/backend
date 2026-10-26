"""Admin routes of ``professionals`` (mounted under ``api/v1/admin/`` by ``config.urls``).

Every route name is prefixed ``admin-`` and unique across modules; the auth matrix test walks them all.
``specialists/{id}/conflicts/`` is declared by ``appointments`` (it renders appointments).
"""

from django.urls import URLPattern, URLResolver, path

from .views import (
    LocationView,
    ServiceViewSet,
    SpecialistBlockViewSet,
    SpecialistOverrideViewSet,
    SpecialistPhotoView,
    SpecialistServicesView,
    SpecialistViewSet,
    SpecialistWorkingHoursView,
)

specialists = SpecialistViewSet.as_view({"get": "list", "post": "create"})
specialist = SpecialistViewSet.as_view({"get": "retrieve", "patch": "partial_update"})
overrides = SpecialistOverrideViewSet.as_view({"get": "list", "post": "create"})
override = SpecialistOverrideViewSet.as_view({"delete": "destroy"})
blocks = SpecialistBlockViewSet.as_view({"get": "list", "post": "create"})
block = SpecialistBlockViewSet.as_view({"delete": "destroy"})
services = ServiceViewSet.as_view({"get": "list", "post": "create"})
service = ServiceViewSet.as_view({"get": "retrieve", "patch": "partial_update"})

urlpatterns: list[URLPattern | URLResolver] = [
    path("specialists/", specialists, name="admin-specialists-list"),
    path("specialists/<int:pk>/", specialist, name="admin-specialists-detail"),
    path("specialists/<int:pk>/photo/", SpecialistPhotoView.as_view(), name="admin-specialists-photo"),
    path(
        "specialists/<int:pk>/services/", SpecialistServicesView.as_view(), name="admin-specialists-services"
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
    path("services/", services, name="admin-services-list"),
    path("services/<int:pk>/", service, name="admin-services-detail"),
    path("location/", LocationView.as_view(), name="admin-location"),
]
