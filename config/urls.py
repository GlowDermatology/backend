from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import URLPattern, URLResolver, path

from glowderma.core.api.health import HealthView

urlpatterns: list[URLPattern | URLResolver] = [path("api/v1/health/", HealthView.as_view())]
if settings.DJANGO_ADMIN_ENABLED:
    urlpatterns.append(path("django-admin/", admin.site.urls))
if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
