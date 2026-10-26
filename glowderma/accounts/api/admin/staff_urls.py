"""Admin routes of ``accounts`` outside ``auth/`` (mounted under ``api/v1/admin/`` by ``config.urls``).

Every route name is prefixed ``admin-`` and unique across modules; the auth matrix test walks them all.
"""

from django.urls import URLPattern, URLResolver, path

from .views import StaffViewSet

urlpatterns: list[URLPattern | URLResolver] = [
    path("staff/", StaffViewSet.as_view({"get": "list", "post": "create"}), name="admin-staff"),
    path(
        "staff/<int:pk>/",
        StaffViewSet.as_view({"get": "retrieve", "patch": "partial_update"}),
        name="admin-staff-detail",
    ),
    path(
        "staff/<int:pk>/deactivate/",
        StaffViewSet.as_view({"post": "deactivate"}),
        name="admin-staff-deactivate",
    ),
    path(
        "staff/<int:pk>/reactivate/",
        StaffViewSet.as_view({"post": "reactivate"}),
        name="admin-staff-reactivate",
    ),
    path(
        "staff/<int:pk>/reset-password/",
        StaffViewSet.as_view({"post": "reset_password"}),
        name="admin-staff-reset-password",
    ),
]
