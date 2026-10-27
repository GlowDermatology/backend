"""Development routes (customer contract §2.15), mounted at ``api/v1/dev/`` only under the local/test settings
with fake payments (``config.urls.dev_routes_enabled``)."""

from django.urls import URLPattern, URLResolver, path

from glowderma.commerce.api.dev import views

urlpatterns: list[URLPattern | URLResolver] = [
    path("payments/<int:order_id>/simulate/", views.SimulatePaymentView.as_view()),
]
