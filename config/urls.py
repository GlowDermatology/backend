from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import URLPattern, URLResolver, include, path

from glowderma.accounts.api.guard import guarded_admin_login
from glowderma.core.api.health import HealthView

handler404 = "glowderma.core.api.exceptions.not_found_view"
handler500 = "glowderma.core.api.exceptions.server_error_view"

urlpatterns: list[URLPattern | URLResolver] = [
    path("api/v1/health/", HealthView.as_view()),
    path("api/v1/admin/auth/", include("glowderma.accounts.api.admin.urls")),
]
if settings.DJANGO_ADMIN_ENABLED:
    urlpatterns.append(path("django-admin/login/", guarded_admin_login))
    urlpatterns.append(path("django-admin/", admin.site.urls))
if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
