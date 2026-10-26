"""Provider callbacks (customer contract §2.13), mounted at ``api/v1/webhooks/``."""

from django.urls import URLPattern, URLResolver

urlpatterns: list[URLPattern | URLResolver] = []
