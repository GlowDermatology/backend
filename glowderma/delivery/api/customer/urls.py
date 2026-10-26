"""Customer routes of `delivery` (customer contract §2.6): ``delivery/regions/`` only —
``delivery/calculate/``
reads the cart and loyalty, so ``commerce`` serves it."""

from django.urls import URLPattern, URLResolver, path

from glowderma.delivery.api.customer import views

urlpatterns: list[URLPattern | URLResolver] = [path("delivery/regions/", views.DeliveryRegionsView.as_view())]
