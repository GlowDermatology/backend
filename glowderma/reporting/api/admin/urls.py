"""Admin routes of ``reporting`` (mounted under ``api/v1/admin/`` by ``config.urls``).

Every route name is prefixed ``admin-`` and unique across modules; the auth matrix test walks them all.
``dashboard/`` belongs to another slice and is declared there.
"""

from django.urls import URLPattern, URLResolver, path

from .views import AuditLogViewSet, NavCountsView

urlpatterns: list[URLPattern | URLResolver] = [
    path("audit-log/", AuditLogViewSet.as_view({"get": "list"}), name="admin-audit-log"),
    path("nav-counts/", NavCountsView.as_view(), name="admin-nav-counts"),
]
