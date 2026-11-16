"""Admin routes of ``commerce`` (mounted under ``api/v1/admin/`` by ``config.urls``).

Every route name is prefixed ``admin-`` and unique across modules; the auth matrix test walks them all. The
refund operations live under ``payments/`` (contract §7 item 18) but write order events, so they are
declared here (T16 pattern).
"""

from django.urls import URLPattern, URLResolver, path

from glowderma.commerce.api.dev import yandex as dev_yandex

from .views import (
    DeliveryIssueResolveView,
    DeliveryIssueViewSet,
    DeliverySuspensionView,
    OrderCancelView,
    OrderConfirmReturnView,
    OrderDispatchView,
    OrderIssueCreateView,
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
    # Yandex Delivery: call a courier (plan §3.6)
    path("orders/<int:pk>/dispatch/", OrderDispatchView.as_view(), name="admin-order-dispatch"),
    # Yandex Delivery: the returned parcel is back at the warehouse (plan §3.8)
    path(
        "orders/<int:pk>/confirm-return/", OrderConfirmReturnView.as_view(), name="admin-order-confirm-return"
    ),
    # Yandex Delivery exception queue and dispatch suspension (plan §3.9)
    path("orders/<int:pk>/issues/", OrderIssueCreateView.as_view(), name="admin-order-issues"),
    path("delivery/issues/", DeliveryIssueViewSet.as_view({"get": "list"}), name="admin-delivery-issues"),
    path(
        "delivery/issues/<int:pk>/resolve/",
        DeliveryIssueResolveView.as_view(),
        name="admin-delivery-issue-resolve",
    ),
    path("delivery/suspension/", DeliverySuspensionView.as_view(), name="admin-delivery-suspension"),
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
# Local/test only (DEV_FEATURES_ALLOWED and the fake Yandex adapter): drive a fake courier (plan §3.11).
if dev_yandex.enabled():
    urlpatterns.append(
        path(
            "dev/yandex/<int:pk>/advance/",
            dev_yandex.YandexAdvanceView.as_view(),
            name="admin-dev-yandex-advance",
        )
    )
