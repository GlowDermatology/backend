"""OpenAPI declarations for ``POST webhooks/uzum/`` (docs/integrations/uzum.md): Uzum Checkout's
``acquiring_merchant_callback``.

Uzum posts ``{orderId, orderNumber, operationType, operationState, …}`` to the URL configured on the terminal
(``https://<api>/api/v1/webhooks/uzum/?token=<UZUM_CALLBACK_TOKEN>``) — no amount, **no signature**. The
token is checked in constant time; then the callback is only a hint: our payment is found by the order
number and Uzum's order id, ``uzum.sync_order`` is enqueued (``getOrderStatus`` decides) and 200 answers
(Uzum retries up to five times until it gets one)."""

from __future__ import annotations

from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema

UZUM_WEBHOOK_POST = extend_schema(
    operation_id="webhook_uzum_checkout",
    description="Uzum Checkout payment callback (an unsigned hint): `?token=` is our shared secret; answers "
    "200 for a verified token and re-reads the payment's status asynchronously.",
    parameters=[OpenApiParameter("token", OpenApiTypes.STR, required=True)],
    request=OpenApiTypes.OBJECT,
    responses={200: OpenApiTypes.OBJECT, 401: OpenApiTypes.OBJECT, 403: OpenApiTypes.OBJECT},
    auth=[],
)


__all__ = [
    "UZUM_WEBHOOK_POST",
]
