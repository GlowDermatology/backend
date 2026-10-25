"""Admin routes of ``site_config`` (mounted under ``api/v1/admin/`` by ``config.urls``).

Every route name is prefixed ``admin-`` and unique across modules; the auth matrix test walks them all.
"""

from django.urls import URLPattern, URLResolver, path

from .views import SettingsView

urlpatterns: list[URLPattern | URLResolver] = [
    path("settings/", SettingsView.as_view(), name="admin-settings"),
]
