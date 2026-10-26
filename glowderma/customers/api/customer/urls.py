"""Customer routes of `customers` (customer contract §2.0); mounted by ``config.urls`` at ``api/v1/``."""

from django.urls import URLPattern, URLResolver

urlpatterns: list[URLPattern | URLResolver] = []
