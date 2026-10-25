"""Admin routes of ``customers`` (mounted under ``api/v1/admin/`` by ``config.urls``).

Every route name is prefixed ``admin-`` and unique across modules; the auth matrix test walks them all.
"""

from django.urls import URLPattern, URLResolver, path

from .views import CashbackAdjustmentView, CustomerViewSet

customers = CustomerViewSet.as_view({"get": "list"})
customer = CustomerViewSet.as_view({"get": "retrieve"})

urlpatterns: list[URLPattern | URLResolver] = [
    path("customers/", customers, name="admin-customers-list"),
    path("customers/<int:pk>/", customer, name="admin-customers-detail"),
    path(
        "customers/<int:pk>/cashback-adjustments/",
        CashbackAdjustmentView.as_view(),
        name="admin-customers-cashback-adjustments",
    ),
]
