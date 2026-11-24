"""Customer routes of `commerce` (customer contract §2.5–§2.8): the server cart, ``delivery/calculate/``,
checkout, pay, customer cancel, reorder and the customer's orders. ``delivery/calculate/`` lives here (it
reads the cart, ``loyalty`` and ``commerce.selectors.cashback_max``, which ``delivery`` may not import);
``delivery`` serves ``delivery/regions/``.

One subject per module: ``cart``, ``delivery_quote``, ``checkout``, ``payment``, ``order_actions`` (cancel and
reorder) and ``my_orders``; ``common`` holds the request helpers and responses they share, ``destinations``
and ``quote_answers`` the delivery address and the quote options. Their OpenAPI declarations live in
``glowderma.commerce.api.customer.schemas``.
"""

from glowderma.commerce.api.customer.views.cart import (
    CartItemsView,
    CartItemView,
    CartView,
)
from glowderma.commerce.api.customer.views.checkout import (
    CheckoutView,
)
from glowderma.commerce.api.customer.views.common import (
    fake_scenario_header,
)
from glowderma.commerce.api.customer.views.delivery_quote import (
    DeliveryCalculateView,
)
from glowderma.commerce.api.customer.views.my_orders import (
    MyOrdersViewSet,
)
from glowderma.commerce.api.customer.views.order_actions import (
    CancelView,
    ReorderView,
)
from glowderma.commerce.api.customer.views.payment import (
    PayView,
)

__all__ = [
    "CancelView",
    "CartItemView",
    "CartItemsView",
    "CartView",
    "CheckoutView",
    "DeliveryCalculateView",
    "MyOrdersViewSet",
    "PayView",
    "ReorderView",
    "fake_scenario_header",
]
