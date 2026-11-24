"""``POST webhooks/uzum/`` (docs/integrations/uzum.md): Uzum Checkout's payment callback.

**What Uzum sends** (``acquiring_merchant_callback``, configured on the terminal by Uzum): a JSON
``{orderId, orderNumber, operationType (AUTHORIZE|COMPLETE|REFUND|REVERSE|…), operationState (SUCCESS|FAIL),
merchantOperationId?, rrn?, cardType?}`` — no amount, **no signature**, no documented source IPs; Uzum retries
until it gets HTTP 200, at most five times.

So the callback is only a hint:

1. refused while the fake runs under the production settings (``callbacks_refused("uzum")``, 403);
2. ``?token=`` (or the header ``X-GD-Webhook-Token``) must equal ``UZUM_CALLBACK_TOKEN`` (constant time; else
   401 with a bounded ``rejected:`` row), and the optional ``UZUM_ALLOWED_IPS`` must contain the caller;
3. ``orderNumber`` must be one of **this environment's** numbers and ``orderId`` the Uzum order of that
   payment, else the call is acknowledged (200) and ignored;
4. the ``WebhookInbox`` key ``"{orderId}:{operationType}:{operationState}:{merchantOperationId}"`` drops
   repeats, then ``uzum.sync_order`` is enqueued: ``getOrderStatus`` — never the body — decides the payment
   (``commerce.uzum_flow.apply_status``). No Uzum call runs in the request.

The token never reaches a log or a stored row (gunicorn and the edge log no query string,
``WebhookInbox.path`` has none); the stored payload keeps the allow-listed fields only (no ``rrn``, no card
data).
"""

from __future__ import annotations

import hmac
import json
import logging
import time
from typing import Any

from django.conf import settings
from django.db import transaction
from rest_framework.request import Request
from rest_framework.response import Response

from glowderma.accounts.api.customer_base import WebhookAPIView
from glowderma.commerce import uzum_flow
from glowderma.commerce.api.webhooks import inbox
from glowderma.commerce.api.webhooks.schemas.uzum import UZUM_WEBHOOK_POST
from glowderma.core import enums, integration_modes
from glowderma.integrations.uzum import protocol
from glowderma.payments import selectors as payment_selectors

logger = logging.getLogger(__name__)
PROVIDER = "uzum"
TOKEN_HEADER = "X-GD-Webhook-Token"  # noqa: S105 - a header name
OK: dict[str, Any] = {"ok": True}
IGNORED: dict[str, Any] = {"ok": True, "ignored": True}
REFUSED: dict[str, Any] = {"ok": False}
MAX_BODY_BYTES = 16 * 1024


def _body(raw: bytes) -> dict[str, Any] | None:
    try:
        value = json.loads(raw.decode("utf-8", errors="replace"))
    except ValueError, RecursionError:
        return None
    return value if isinstance(value, dict) else None


class UzumWebhookView(WebhookAPIView):
    @UZUM_WEBHOOK_POST
    def post(self, request: Request) -> Response:
        started = time.monotonic()
        try:
            return self._handle(request, started)
        except Exception:
            logger.exception("uzum.webhook failed; everything rolled back")
            return Response(REFUSED, status=500)

    def _handle(self, request: Request, started: float) -> Response:
        if integration_modes.callbacks_refused(PROVIDER):
            inbox.store_rejected(
                PROVIDER, request, None, status=403, body=REFUSED, error_code="fake_mode", started=started
            )
            return Response(REFUSED, status=403)
        length = str(request.META.get("CONTENT_LENGTH") or "0")
        if (length.isdigit() and int(length) > MAX_BODY_BYTES) or len(request.body) > MAX_BODY_BYTES:
            # the header is the caller's: only a digit string reaches the log line
            logger.warning("uzum.webhook_too_large %s", {"bytes": length if length.isdigit() else "invalid"})
            return Response(REFUSED, status=413)
        payload = protocol.callback_fields(_body(request.body))
        token = str(request.headers.get(TOKEN_HEADER) or request.query_params.get("token") or "")
        secret = str(getattr(settings, "UZUM_CALLBACK_TOKEN", "") or "")
        if not secret or not hmac.compare_digest(token.encode(), secret.encode()):
            inbox.store_rejected(
                PROVIDER, request, payload, status=401, body=REFUSED, error_code="bad_token", started=started
            )
            return Response(REFUSED, status=401)
        if not inbox.ip_allowed(request, getattr(settings, "UZUM_ALLOWED_IPS", [])):
            inbox.store_rejected(
                PROVIDER, request, payload, status=403, body=REFUSED, error_code="ip_refused", started=started
            )
            return Response(REFUSED, status=403)
        order_id = payload.get("orderId", "").lower()
        payment_id = protocol.payment_id_of(payload.get("orderNumber"), str(settings.UZUM_ORDER_PREFIX))
        payment = payment_selectors.get_payment(payment_id) if payment_id is not None else None
        if (
            payment is None
            or payment.provider != enums.PaymentProvider.UZUM
            or not protocol.is_uuid(order_id)
            or (payment.transaction_id or "").lower() != order_id
        ):
            logger.warning("uzum.webhook_foreign_order %s", {"payment_id": payment_id})
            inbox.store_rejected(
                PROVIDER,
                request,
                payload,
                status=200,
                body=IGNORED,
                error_code="foreign_order",
                started=started,
            )
            return Response(IGNORED)
        event_key = ":".join(
            (
                order_id,
                payload.get("operationType", ""),
                payload.get("operationState", ""),
                payload.get("merchantOperationId", ""),
            )
        )
        entity = f"order:{payment.order_id}"
        with transaction.atomic():
            duplicate = inbox.exists(PROVIDER, event_key) is not None
            if not duplicate:
                uzum_flow.enqueue_sync(payment.id, event_key)
            inbox.store_delivery(
                PROVIDER,
                event_key,
                request,
                payload,
                status=200,
                body=OK,
                outcome=inbox.OUTCOME_DUPLICATE if duplicate else inbox.OUTCOME_ACCEPTED,
                entity_ref=entity,
                started=started,
            )
        return Response(OK)
