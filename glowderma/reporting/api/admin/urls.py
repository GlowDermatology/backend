"""Admin routes of ``reporting`` (mounted under ``api/v1/admin/`` by ``config.urls``).

Every route name is prefixed ``admin-`` and unique across modules; the auth matrix test walks them all.
"""

from django.urls import URLPattern, URLResolver, path

from .views import AuditLogViewSet, DashboardView, NavCountsView

urlpatterns: list[URLPattern | URLResolver] = [
    path("audit-log/", AuditLogViewSet.as_view({"get": "list"}), name="admin-audit-log"),
    path("nav-counts/", NavCountsView.as_view(), name="admin-nav-counts"),
    path("dashboard/", DashboardView.as_view(), name="admin-dashboard"),
]
