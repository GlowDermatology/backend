"""Admin serializers of `commerce` (contract §2.2 orders, §4 ``OrderEvent``; §7 item 18 refund operations).

One subject per module: ``shipment`` (the shipment union), ``delivery`` (the delivery object), ``orders``
(the summary and the detail), ``queries``, ``order_actions``, ``refunds`` and ``delivery_issues``;
``messages`` holds the shared reason rule.
"""

from glowderma.commerce.api.admin.serializers.delivery import (
    DeliveryIssueRefSerializer,
    OrderDeliverySerializer,
    render_delivery,
)
from glowderma.commerce.api.admin.serializers.delivery_issues import (
    ISSUE_NOTE_MAX_LENGTH,
    ISSUE_NOTE_MIN_LENGTH,
    MESSAGE_NOTE,
    MESSAGE_NOTE_MAX,
    MESSAGE_SUSPENSION_NOTE_MAX,
    MESSAGE_UNTIL_PAST,
    SUSPENSION_NOTE_MAX_LENGTH,
    DeliveryIssueQuerySerializer,
    DeliveryIssueSerializer,
    OpenIssueSerializer,
    ResolveIssueSerializer,
    SuspensionBodySerializer,
    SuspensionSerializer,
    clean_note,
)
from glowderma.commerce.api.admin.serializers.messages import (
    MESSAGE_REASON,
    MESSAGE_REASON_MAX,
    MESSAGE_REASON_MIN,
    REASON_MAX_LENGTH,
    REASON_MIN_LENGTH,
)
from glowderma.commerce.api.admin.serializers.order_actions import (
    DISPATCH_PRICE_MAX,
    DISPATCH_WEIGHT_MAX_G,
    MESSAGE_CONTENTS_UNVERIFIED,
    CancelOrderSerializer,
    ConfirmReturnSerializer,
    DispatchSerializer,
    TransitionSerializer,
)
from glowderma.commerce.api.admin.serializers.orders import (
    AvailableActionsSerializer,
    CancellationSerializer,
    CashbackHoldSerializer,
    OrderCashbackSerializer,
    OrderDetailSerializer,
    OrderEventSerializer,
    OrderItemSerializer,
    OrderPaymentSerializer,
    OrderSummarySerializer,
    PaymentSummarySerializer,
)
from glowderma.commerce.api.admin.serializers.queries import (
    OrderCountsQuerySerializer,
    OrderListQuerySerializer,
    StatusCountsSerializer,
)
from glowderma.commerce.api.admin.serializers.refunds import (
    RESOLVE_STATES,
    ResolveRefundSerializer,
)
from glowderma.commerce.api.admin.serializers.shipment import (
    SHIPMENT_SCHEMA,
    SHIPMENT_SERIALIZERS,
    ShipmentCreatedSerializer,
    ShipmentFailedSerializer,
    ShipmentNotCreatedSerializer,
    ShipmentNotRequiredSerializer,
    render_shipment,
)

__all__ = [
    "DISPATCH_PRICE_MAX",
    "DISPATCH_WEIGHT_MAX_G",
    "ISSUE_NOTE_MAX_LENGTH",
    "ISSUE_NOTE_MIN_LENGTH",
    "MESSAGE_CONTENTS_UNVERIFIED",
    "MESSAGE_NOTE",
    "MESSAGE_NOTE_MAX",
    "MESSAGE_REASON",
    "MESSAGE_REASON_MAX",
    "MESSAGE_REASON_MIN",
    "MESSAGE_SUSPENSION_NOTE_MAX",
    "MESSAGE_UNTIL_PAST",
    "REASON_MAX_LENGTH",
    "REASON_MIN_LENGTH",
    "RESOLVE_STATES",
    "SHIPMENT_SCHEMA",
    "SHIPMENT_SERIALIZERS",
    "SUSPENSION_NOTE_MAX_LENGTH",
    "AvailableActionsSerializer",
    "CancelOrderSerializer",
    "CancellationSerializer",
    "CashbackHoldSerializer",
    "ConfirmReturnSerializer",
    "DeliveryIssueQuerySerializer",
    "DeliveryIssueRefSerializer",
    "DeliveryIssueSerializer",
    "DispatchSerializer",
    "OpenIssueSerializer",
    "OrderCashbackSerializer",
    "OrderCountsQuerySerializer",
    "OrderDeliverySerializer",
    "OrderDetailSerializer",
    "OrderEventSerializer",
    "OrderItemSerializer",
    "OrderListQuerySerializer",
    "OrderPaymentSerializer",
    "OrderSummarySerializer",
    "PaymentSummarySerializer",
    "ResolveIssueSerializer",
    "ResolveRefundSerializer",
    "ShipmentCreatedSerializer",
    "ShipmentFailedSerializer",
    "ShipmentNotCreatedSerializer",
    "ShipmentNotRequiredSerializer",
    "StatusCountsSerializer",
    "SuspensionBodySerializer",
    "SuspensionSerializer",
    "TransitionSerializer",
    "clean_note",
    "render_delivery",
    "render_shipment",
]
