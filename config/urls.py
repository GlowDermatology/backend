from typing import cast

from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import URLPattern, URLResolver, include, path
from django.utils.module_loading import import_string
from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerView

from glowderma.accounts.api.guard import guarded_admin_login
from glowderma.core.api.health import HealthView

handler404 = "glowderma.core.api.exceptions.not_found_view"
handler500 = "glowderma.core.api.exceptions.server_error_view"

schema_access = {
    "authentication_classes": [
        import_string(value)
        for value in cast(list[str], settings.SPECTACULAR_SETTINGS["SERVE_AUTHENTICATION"])
    ],
    "permission_classes": [
        import_string(value) for value in cast(list[str], settings.SPECTACULAR_SETTINGS["SERVE_PERMISSIONS"])
    ],
}

# Layer order (lowest first); a module's admin routes live in glowderma/<module>/api/admin/urls.py.
ADMIN_API_MODULES = (
    "integrations",
    "site_config",
    "catalog",
    "loyalty",
    "payments",
    "commerce",
    "appointments",
    "notifications",
    "customers",
    "reporting",
)

# Customer API (M2): one include per module, all mounted at api/v1/ (each urls.py carries its own prefixes:
# auth/, me/, catalog/, cart/, orders/, …), in the plan's order (§1 P0; the prefixes never overlap, so the
# order does not change which view a path resolves to); customer contract §1.1.
CUSTOMER_API_MODULES = (
    "customers",
    "site_config",
    "delivery",
    "commerce",
    "loyalty",
    "appointments",
    "notifications",
)


def dev_routes_enabled() -> bool:
    """``api/v1/dev/`` exists only under the local/test settings with fake payments (contract §2.15)."""
    return bool(
        settings.DEV_FEATURES_ALLOWED
        and settings.FAKE_PAYMENTS_ENABLED
        and settings.CLICK_MODE == "fake"
        and settings.PAYME_MODE == "fake"
    )


urlpatterns: list[URLPattern | URLResolver] = [
    path("api/v1/health/", HealthView.as_view()),
    path("api/v1/admin/auth/", include("glowderma.accounts.api.admin.urls")),
    path("api/v1/admin/", include("glowderma.accounts.api.admin.staff_urls")),
    # One include per module, all mounted at the admin root: each module's urls.py carries its own prefixes
    # (products/, categories/, orders/, …) so slices never edit this file or each other's route tables.
    *(
        path(
            "api/v1/admin/",
            include(f"glowderma.{module}.api.admin.urls"),
        )
        for module in ADMIN_API_MODULES
    ),
    # Provider callbacks are always mounted: a fake provider under the production settings is refused in the
    # view (customer contract §2.13.0), not by a missing route.
    path("api/v1/webhooks/", include("glowderma.commerce.api.webhooks.urls")),
    path("api/v1/pos/", include("glowderma.loyalty.api.pos.urls")),
    *(path("api/v1/", include(f"glowderma.{module}.api.customer.urls")) for module in CUSTOMER_API_MODULES),
    path("api/v1/schema/", SpectacularAPIView.as_view(**schema_access), name="schema"),
    path("api/v1/docs/", SpectacularSwaggerView.as_view(url_name="schema", **schema_access), name="docs"),
]
if dev_routes_enabled():
    urlpatterns.append(path("api/v1/dev/", include("glowderma.commerce.api.dev.urls")))
if settings.DJANGO_ADMIN_ENABLED:
    urlpatterns.append(path("django-admin/login/", guarded_admin_login))
    urlpatterns.append(path("django-admin/", admin.site.urls))
if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
