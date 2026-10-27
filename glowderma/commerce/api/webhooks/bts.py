"""``POST webhooks/bts/`` (customer contract §2.13.3): BTS status notifications.

**What BTS sends** (docs.bts.uz ``/page/webhook``, ``/page/test-update-status``): a POST whose document is
``{"clientId", "orderId", "barcode", "status": {"id", "code"?, "name"}, "date"}``, as form-data in the
documented example. Every shape seen or plausible is read (:func:`notification`): a JSON body (also a JSON
string holding the document); flat form fields (multipart or urlencoded), PHP bracket keys
``status[id]`` / ``status[code]`` / ``status[name]`` included, and ``status`` as a JSON string; a form whose
single field's value — or name, when the field has no name — is the JSON document. A body larger than
:data:`MAX_BODY_BYTES` is refused (413), nothing stored.

Authenticated by the shared secret ``BTS_WEBHOOK_SECRET``: ``?token=`` in the URL BTS is configured with
(``webhook_url = https://<api>/api/v1/webhooks/bts/?token=<secret>``; BTS cannot set our header) or the header
``X-GD-Webhook-Token``. Never stored: ``WebhookInbox.path`` has no query string. A mismatch answers 401 with a
bounded ``rejected:`` row.

The payload's status is **never trusted**: our ``Shipment`` is found by ``clientId`` (``BTS_CLIENT_ID_PREFIX``
+ our order id; ``null`` / ``""`` / another environment's prefix are skipped) or else by ``orderId`` == the
tracking number stored on it (BTS's ``orderId`` is our tracking number; the ``barcode`` is never used), and
the view re-tracks with **our** stored tracking number; the authoritative status goes through
``commerce.services.apply_bts_status(via="webhook")``. The inbox event key is ``"{tracking}:{status.code or
status.id}:{date}"``. The webhook never writes ``IntegrationState(bts)`` (polling alone reports the link's
health).
"""

from __future__ import annotations

import hmac
import io
import json
import logging
import time
import uuid
from typing import Any

from django.conf import settings
from django.core.exceptions import SuspiciousOperation
from django.db import transaction
from django.http import QueryDict
from django.http.multipartparser import MultiPartParser, MultiPartParserError
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework.request import Request
from rest_framework.response import Response

from glowderma.accounts.api.customer_base import WebhookAPIView
from glowderma.commerce import services, tasks
from glowderma.commerce.api.webhooks import inbox
from glowderma.commerce.models import Order
from glowderma.core import enums, integration_modes
from glowderma.delivery import selectors as delivery_selectors
from glowderma.delivery.models import Shipment
from glowderma.integrations.bts import protocol as bts_protocol
from glowderma.integrations.bts.ports import BtsRejected, BtsUnavailable
from glowderma.integrations.bts.registry import get_bts

logger = logging.getLogger(__name__)
PROVIDER = "bts"
TOKEN_HEADER = "X-GD-Webhook-Token"  # noqa: S105 - a header name
OK: dict[str, Any] = {"ok": True}
IGNORED: dict[str, Any] = {"ok": True, "ignored": True}
REFUSED: dict[str, Any] = {"ok": False}
MAX_BODY_BYTES = 16 * 1024  # a status notification is a few hundred bytes
STATUS_KEYS = ("id", "code", "name")


def _json_object(text: str) -> dict[str, Any] | None:
    """``text`` as a JSON object; a JSON string holding one (``"{\"clientId\": …}"``) is unwrapped once."""
    try:
        value: Any = json.loads(text)
        if isinstance(value, str):
            value = json.loads(value)
    except ValueError, RecursionError:
        return None
    return value if isinstance(value, dict) else None


def _form_fields(request: Request, raw: bytes, content_type: str) -> dict[str, str] | None:
    """The form's fields (last value of each, files ignored), or ``None`` when it does not parse."""
    if content_type == "multipart/form-data":
        try:
            parser = MultiPartParser(
                request.META, io.BytesIO(raw), request._request.upload_handlers, settings.DEFAULT_CHARSET
            )
            data, _files = parser.parse()
        except MultiPartParserError:
            return None
    else:
        data = QueryDict(raw, encoding=settings.DEFAULT_CHARSET)
    return {str(key): str(data.get(key, "")) for key in data}


def _from_form(fields: dict[str, str]) -> dict[str, Any] | None:
    if len(fields) == 1:
        ((name, value),) = fields.items()
        whole = _json_object(value) or (_json_object(name) if not value else None)
        if whole is not None:
            return whole
    document: dict[str, Any] = {}
    status: dict[str, Any] = {}
    for name, value in fields.items():
        if name.startswith("status[") and name.endswith("]") and name[7:-1] in STATUS_KEYS:
            status[name[7:-1]] = value
        elif name == "status":
            document["status"] = _json_object(value) or value
        else:
            document[name] = value
    if status:
        document["status"] = status
    return document or None


def notification(request: Request, raw: bytes) -> dict[str, Any] | None:
    """BTS's notification document from any of the body shapes of the module docstring, else ``None``."""
    content_type = (request.content_type or "").split(";")[0].strip().lower()
    if content_type in ("multipart/form-data", "application/x-www-form-urlencoded"):
        fields = _form_fields(request, raw, content_type)
        return _from_form(fields) if fields else None
    return _json_object(raw.decode("utf-8", errors="replace"))


def _first(payload: dict[str, Any], *names: str) -> object:
    for name in names:
        value = payload.get(name)
        if value not in (None, ""):
            return value
    return None


def _status_ref(status: object) -> str:
    """``status.code`` or else ``status.id`` (a bare status as it is), for the event key."""
    if isinstance(status, dict):
        value = _first(status, "code", "id")
    else:
        value = status
    if isinstance(value, bool) or not isinstance(value, int | str):
        return ""
    return " ".join(str(value).split())


class BtsWebhookView(WebhookAPIView):
    @extend_schema(
        operation_id="webhook_bts_status",
        description="BTS status notification (JSON or form-data, customer contract §2.13.3), authenticated "
        "by `?token=<BTS_WEBHOOK_SECRET>` or the `X-GD-Webhook-Token` header; the status is re-tracked with "
        "our tracking number.",
        parameters=[OpenApiParameter("token", OpenApiTypes.STR, required=False)],
        request={
            "application/json": OpenApiTypes.OBJECT,
            "multipart/form-data": OpenApiTypes.OBJECT,
            "application/x-www-form-urlencoded": OpenApiTypes.OBJECT,
        },
        responses={
            200: OpenApiTypes.OBJECT,
            401: OpenApiTypes.OBJECT,
            413: OpenApiTypes.OBJECT,
            503: OpenApiTypes.OBJECT,
        },
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
        length = str(request.META.get("CONTENT_LENGTH") or "0")
        if (length.isdigit() and int(length) > MAX_BODY_BYTES) or len(request.body) > MAX_BODY_BYTES:
            logger.warning("bts.webhook_too_large %s", {"bytes": length})
            return Response(REFUSED, status=413)
        raw_bytes = request.body
        raw = raw_bytes.decode("utf-8", errors="replace")
        payload = notification(request, raw_bytes)
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
        body = payload or {}
        order_ref = bts_protocol.order_ref_of(
            _first(body, "clientId", "client_id"), str(getattr(settings, "BTS_CLIENT_ID_PREFIX", ""))
        )
        their_order = bts_protocol.bts_code(_first(body, "orderId", "order_id"))
        their_number = str(their_order) if their_order is not None and their_order > 0 else ""
        shipment = delivery_selectors.shipment_info(int(order_ref)) if order_ref else None
        if shipment is None and their_number:
            shipment = delivery_selectors.shipment_by_tracking(their_number)
        if (
            shipment is None
            or not shipment.tracking_number
            or shipment.state not in (Shipment.State.CREATED, Shipment.State.CANCELLED)
        ):
            logger.warning(
                "bts.webhook_unknown_parcel %s", {"client_id": order_ref, "order_id": their_number or None}
            )
            return self._stored(request, body, raw, f"{uuid.uuid4().hex}", IGNORED, started, "")
        order_id = shipment.order_id
        if their_number and their_number != shipment.tracking_number:
            logger.warning("bts.webhook_tracking_mismatch %s", {"order_id": order_id})
        status = _status_ref(body.get("status") if "status" in body else _first(body, "statusCode"))
        date = _first(body, "date", "eventTime", "event_time", "time")
        event_time = " ".join(str(date).split()) if isinstance(date, str | int) else ""
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
            tasks.alert_if_gone(order_id, parcel, shipment.bts_status)
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
