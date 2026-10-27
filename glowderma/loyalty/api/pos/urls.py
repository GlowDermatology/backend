"""Cashier terminal routes (customer contract §2.10), mounted at ``api/v1/pos/``."""

from django.urls import URLPattern, URLResolver, path

from .views import ResolveQrView

urlpatterns: list[URLPattern | URLResolver] = [
    path("cashback/resolve-qr/", ResolveQrView.as_view(), name="pos-cashback-resolve-qr"),
]
