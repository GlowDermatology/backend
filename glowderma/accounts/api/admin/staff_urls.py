"""Admin routes of ``accounts`` outside ``auth/`` (mounted under ``api/v1/admin/`` by ``config.urls``).

Every route name is prefixed ``admin-`` and unique across modules; the auth matrix test walks them all.
"""

from django.urls import URLPattern, URLResolver, path

from .views import StaffViewSet

urlpatterns: list[URLPattern | URLResolver] = [
    path("staff/", StaffViewSet.as_view({"get": "list"}), name="admin-staff"),
]
