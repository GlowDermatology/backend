"""Customer routes of `site_config` (customer contract §2.4); mounted by ``config.urls`` at ``api/v1/``."""

from django.urls import URLPattern, URLResolver, path

from glowderma.site_config.api.customer.views import PublicSettingsView

urlpatterns: list[URLPattern | URLResolver] = [path("settings/public/", PublicSettingsView.as_view())]
