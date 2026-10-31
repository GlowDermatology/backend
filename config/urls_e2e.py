"""The URLconf of the browser e2e server (config.settings.e2e): the project's routes plus ``/media/`` from
MEDIA_ROOT, standing in for the edge, which serves the console's image previews from the media volume
(deploy/Caddyfile, OPS_DOMAIN). ``DEBUG`` is off there, so ``config.urls`` adds no media route itself."""

from django.conf import settings
from django.urls import URLPattern, URLResolver, re_path
from django.views.static import serve

from config.urls import handler404, handler500
from config.urls import urlpatterns as project_urlpatterns

__all__ = ["handler404", "handler500", "urlpatterns"]

urlpatterns: list[URLPattern | URLResolver] = [
    *project_urlpatterns,
    re_path(r"^media/(?P<path>.*)$", serve, {"document_root": settings.MEDIA_ROOT}),
]
