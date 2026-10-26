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
    path("api/v1/schema/", SpectacularAPIView.as_view(**schema_access), name="schema"),
    path("api/v1/docs/", SpectacularSwaggerView.as_view(url_name="schema", **schema_access), name="docs"),
]
if settings.DJANGO_ADMIN_ENABLED:
    urlpatterns.append(path("django-admin/login/", guarded_admin_login))
    urlpatterns.append(path("django-admin/", admin.site.urls))
if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
