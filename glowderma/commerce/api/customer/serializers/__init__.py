"""Serializers of the commerce customer routes (customer contract §2.5–§2.8): request bodies with the
contract's field codes, and the response shapes (``Cart``, ``DeliveryQuote``, ``PayLink``, ``OrderSummary``,
``OrderDetail``) rendered from the dictionaries ``commerce.selectors`` builds.

One subject per module: ``cart``, ``delivery_quotes``, ``checkout`` (checkout, pay and reorder) and
``orders`` (the summary and the detail).
"""

from glowderma.commerce.api.customer.serializers.cart import (
    QUANTITY_MAX,
    CustomerCartAddSerializer,
    CustomerCartCashbackSerializer,
    CustomerCartChangeSerializer,
    CustomerCartLineSerializer,
    CustomerCartQuantitySerializer,
    CustomerCartSerializer,
)
from glowderma.commerce.api.customer.serializers.checkout import (
    NOTE_MAX,
    CustomerCheckoutSerializer,
    CustomerPayLinkSerializer,
    CustomerPaySerializer,
    CustomerReorderSerializer,
)
from glowderma.commerce.api.customer.serializers.delivery_quotes import (
    CustomerDeliveryCalculateSerializer,
    CustomerDeliveryOptionSerializer,
    CustomerDeliveryQuoteBaseSerializer,
    CustomerDeliveryQuoteSerializer,
)
from glowderma.commerce.api.customer.serializers.orders import (
    CustomerCancellationSerializer,
    CustomerDetailItemSerializer,
    CustomerDetailPaymentSerializer,
    CustomerOrderActionsSerializer,
    CustomerOrderCashbackSerializer,
    CustomerOrderDeliverySerializer,
    CustomerOrderDetailSerializer,
    CustomerOrderScopeQuerySerializer,
    CustomerOrderSummarySerializer,
    CustomerProgressStepSerializer,
    CustomerRefundSerializer,
    CustomerShipmentSerializer,
    CustomerSummaryPaymentSerializer,
    CustomerTimelineEntrySerializer,
)

__all__ = [
    "NOTE_MAX",
    "QUANTITY_MAX",
    "CustomerCancellationSerializer",
    "CustomerCartAddSerializer",
    "CustomerCartCashbackSerializer",
    "CustomerCartChangeSerializer",
    "CustomerCartLineSerializer",
    "CustomerCartQuantitySerializer",
    "CustomerCartSerializer",
    "CustomerCheckoutSerializer",
    "CustomerDeliveryCalculateSerializer",
    "CustomerDeliveryOptionSerializer",
    "CustomerDeliveryQuoteBaseSerializer",
    "CustomerDeliveryQuoteSerializer",
    "CustomerDetailItemSerializer",
    "CustomerDetailPaymentSerializer",
    "CustomerOrderActionsSerializer",
    "CustomerOrderCashbackSerializer",
    "CustomerOrderDeliverySerializer",
    "CustomerOrderDetailSerializer",
    "CustomerOrderScopeQuerySerializer",
    "CustomerOrderSummarySerializer",
    "CustomerPayLinkSerializer",
    "CustomerPaySerializer",
    "CustomerProgressStepSerializer",
    "CustomerRefundSerializer",
    "CustomerReorderSerializer",
    "CustomerShipmentSerializer",
    "CustomerSummaryPaymentSerializer",
    "CustomerTimelineEntrySerializer",
]
