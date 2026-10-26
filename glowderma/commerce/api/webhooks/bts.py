"""``POST webhooks/bts/`` (customer contract §2.13.3): BTS status notifications.

Authenticated by the shared secret ``BTS_WEBHOOK_SECRET`` (header ``X-GD-Webhook-Token``, or ``?token=`` when
BTS cannot set headers — never stored: ``WebhookInbox.path`` has no query string); a mismatch answers 401 with
a bounded ``rejected:`` row. The payload's status is **never trusted**: our ``Shipment`` is found by
``clientId`` (our order id) or by the tracking number stored on it, and the view re-tracks with **our** stored
tracking number; the authoritative status goes through ``commerce.services.apply_bts_status(via="webhook")``.
The webhook never writes ``IntegrationState(bts)`` (polling alone reports the link's health).
"""

from __future__ import annotations

import hmac
import json
import logging
import time
import uuid
from typing import Any

from django.conf import settings
from django.core.exceptions import SuspiciousOperation
from django.db import transaction
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema
from rest_framework.request import Request
from rest_framework.response import Response

from glowderma.accounts.api.customer_base import WebhookAPIView
from glowderma.commerce import services, tasks
from glowderma.commerce.api.webhooks import inbox
from glowderma.commerce.models import Order
from glowderma.core import enums, integration_modes
from glowderma.delivery import selectors as delivery_selectors
from glowderma.delivery.models import Shipment
from glowderma.integrations.bts.ports import BtsRejected, BtsUnavailable
from glowderma.integrations.bts.registry import get_bts

logger = logging.getLogger(__name__)
PROVIDER = "bts"
TOKEN_HEADER = "X-GD-Webhook-Token"  # noqa: S105 - a header name
OK: dict[str, Any] = {"ok": True}
IGNORED: dict[str, Any] = {"ok": True, "ignored": True}
REFUSED: dict[str, Any] = {"ok": False}


def _first(payload: dict[str, Any], *names: str) -> str:
    for name in names:
        value = payload.get(name)
        if value not in (None, ""):
            return str(value)
    return ""


class BtsWebhookView(WebhookAPIView):
    @extend_schema(
        operation_id="webhook_bts_status",
        description="BTS status notification; the status is re-tracked with our tracking number (customer "
        "contract §2.13.3).",
        request={"application/json": OpenApiTypes.OBJECT},
        responses={200: OpenApiTypes.OBJECT, 401: OpenApiTypes.OBJECT, 503: OpenApiTypes.OBJECT},
        auth=[],
    )
    def post(self, request: Request) -> Response:
        started = time.monotonic()
        try:
            return self._handle(request, started)
        except SuspiciousOperation:
            raise  # an oversized body or too many fields: core's handler answers 400/413; nothing written
        except Exception:
            logger.exception("bts.webhook failed; everything rolled back")
            return Response(REFUSED, status=500)

    def _handle(self, request: Request, started: float) -> Response:
        if integration_modes.callbacks_refused(PROVIDER):
            inbox.store_rejected(
                PROVIDER, request, None, status=401, body=REFUSED, error_code="fake_mode", started=started
            )
            return Response(REFUSED, status=401)
        raw = request.body.decode("utf-8", errors="replace")
        try:
            payload: Any = json.loads(raw) if raw else {}
        except ValueError, RecursionError:  # RecursionError: a body nested deeper than the decoder allows
            payload = None
        token = str(request.headers.get(TOKEN_HEADER) or request.query_params.get("token") or "")
        secret = str(settings.BTS_WEBHOOK_SECRET)
        if not secret or not hmac.compare_digest(token.encode(), secret.encode()):
            inbox.store_rejected(
                PROVIDER,
                request,
                payload if payload is not None else raw,
                status=401,
                body=REFUSED,
                error_code="bad_token",
                started=started,
            )
            return Response(REFUSED, status=401)
        body = payload if isinstance(payload, dict) else {}
        client_id = _first(body, "clientId", "client_id")
        their_number = _first(body, "trackingNumber", "tracking_number", "barcode")
        shipment = None
        if client_id.isdigit():
            shipment = delivery_selectors.shipment_info(int(client_id))
        if shipment is None and their_number:
            shipment = delivery_selectors.shipment_by_tracking(their_number)
        if (
            shipment is None
            or not shipment.tracking_number
            or shipment.state
            not in (
                Shipment.State.CREATED,
                Shipment.State.CANCELLED,
            )
        ):
            logger.warning("bts.webhook_unknown_parcel %s", {"client_id": client_id or None})
            return self._stored(request, body, raw, f"{uuid.uuid4().hex}", IGNORED, started, "")
        order_id = shipment.order_id
        if their_number and their_number != shipment.tracking_number:
            logger.warning("bts.webhook_tracking_mismatch %s", {"order_id": order_id})
        status = _first(body, "status", "statusCode", "status_code")
        event_time = _first(body, "eventTime", "event_time", "time", "date")
        event_key = (
            f"{shipment.tracking_number}:{status}:{event_time}" if status and event_time else uuid.uuid4().hex
        )
        entity = f"order:{order_id}"
        with transaction.atomic():
            tasks.lock_parcel(order_id)  # the key polling takes too: re-tracks never apply out of order
            if inbox.exists(PROVIDER, event_key) is not None:
                inbox.store_delivery(
                    PROVIDER,
                    event_key,
                    request,
                    body,
                    status=200,
                    body=OK,
                    outcome=inbox.OUTCOME_DUPLICATE,
                    entity_ref=entity,
                    started=started,
                )
                return Response(OK)
            order = Order.objects.get(pk=order_id)
            try:
                parcel = get_bts(services.fake_scenario(order, "bts")).track(shipment.tracking_number)
            except BtsUnavailable:
                transaction.set_rollback(True)
                return Response(REFUSED, status=503)  # nothing stored: BTS retries, polling catches up
            except BtsRejected:
                logger.warning("bts.track_rejected %s", {"order_id": order_id})
                return self._stored(request, body, raw, event_key, IGNORED, started, entity)
            services.apply_bts_status(
                order_id, parcel.status, enums.ShipmentVia.WEBHOOK, payload=tasks.stable_payload(parcel)
            )
            return self._stored(request, body, raw, event_key, OK, started, entity)

    @staticmethod
    def _stored(
        request: Request,
        body: Any,
        raw: str,
        event_key: str,
        answer: dict[str, Any],
        started: float,
        entity: str,
    ) -> Response:
        inbox.store_delivery(
            PROVIDER,
            event_key,
            request,
            body,
            status=200,
            body=answer,
            outcome=inbox.OUTCOME_ACCEPTED,
            entity_ref=entity,
            raw_body=raw,
            started=started,
        )
        return Response(answer)
