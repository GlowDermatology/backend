"""Admin routes of ``commerce`` (mounted under ``api/v1/admin/`` by ``config.urls``).

Every route name is prefixed ``admin-`` and unique across modules; the auth matrix test walks them all. The
refund operations live under ``payments/`` (contract §7 item 18) but write order events, so they are
declared here (T16 pattern).
"""

from django.urls import URLPattern, URLResolver, path

from .views import (
    OrderCancelView,
    OrderRetryShipmentView,
    OrderStatusCountsView,
    OrderTransitionView,
    OrderViewSet,
    RefundResolveView,
    RefundRetryView,
)

urlpatterns: list[URLPattern | URLResolver] = [
    path("orders/", OrderViewSet.as_view({"get": "list"}), name="admin-orders"),
    path("orders/status-counts/", OrderStatusCountsView.as_view(), name="admin-orders-status-counts"),
    path("orders/<int:pk>/", OrderViewSet.as_view({"get": "retrieve"}), name="admin-order-detail"),
    path("orders/<int:pk>/cancel/", OrderCancelView.as_view(), name="admin-order-cancel"),
    path("orders/<int:pk>/transition/", OrderTransitionView.as_view(), name="admin-order-transition"),
    path(
        "orders/<int:pk>/retry-shipment/", OrderRetryShipmentView.as_view(), name="admin-order-retry-shipment"
    ),
    path(
        "payments/<int:pk>/refunds/<int:refund_id>/retry/",
        RefundRetryView.as_view(),
        name="admin-payment-refund-retry",
    ),
    path(
        "payments/<int:pk>/refunds/<int:refund_id>/resolve/",
        RefundResolveView.as_view(),
        name="admin-payment-refund-resolve",
    ),
]
