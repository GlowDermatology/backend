"""Admin views of `commerce`: orders (contract §2.2) and the refund operations of the payments screen
(contract §7 item 18: ``payments/{id}/refunds/{refund_id}/retry|resolve/``, declared here because they write
order events).

One subject per module: ``orders`` (list, detail, status counts), ``order_actions`` (cancel, transition,
retry the shipment, confirm a return), ``courier`` (dispatch), ``refunds``, ``delivery_issues`` and
``suspension``; ``common`` holds the lookups and responses they share. Their OpenAPI declarations live in
``glowderma.commerce.api.admin.schemas``.
"""

from glowderma.commerce.api.admin.views.courier import (
    OrderDispatchView,
)
from glowderma.commerce.api.admin.views.delivery_issues import (
    DeliveryIssueResolveView,
    DeliveryIssueViewSet,
    OrderIssueCreateView,
)
from glowderma.commerce.api.admin.views.order_actions import (
    OrderCancelView,
    OrderConfirmReturnView,
    OrderRetryShipmentView,
    OrderTransitionView,
)
from glowderma.commerce.api.admin.views.orders import (
    OrderStatusCountsView,
    OrderViewSet,
)
from glowderma.commerce.api.admin.views.refunds import (
    RefundResolveView,
    RefundRetryView,
)
from glowderma.commerce.api.admin.views.suspension import (
    DeliverySuspensionView,
)

__all__ = [
    "DeliveryIssueResolveView",
    "DeliveryIssueViewSet",
    "DeliverySuspensionView",
    "OrderCancelView",
    "OrderConfirmReturnView",
    "OrderDispatchView",
    "OrderIssueCreateView",
    "OrderRetryShipmentView",
    "OrderStatusCountsView",
    "OrderTransitionView",
    "OrderViewSet",
    "RefundResolveView",
    "RefundRetryView",
]
