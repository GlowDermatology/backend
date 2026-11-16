"""``POST webhooks/yandex-delivery/`` (Yandex Delivery plan §3.7, decision 10): claim status callbacks.

**What Yandex sends** (``claims/create`` → ``callback_properties.callback_url``, docs rev. r22073422): a POST
to the URL we registered for the claim with ``updated_ts=<ISO-8601>&claim_id=<claim id>`` appended by
concatenation — no status, no body we rely on, **no signature**. The mechanism is documented as deprecated, "a
push ahead of polling": the 60 s ``bulk_info`` poll (``commerce.poll_yandex``) is the reliable path.

So the callback is only a hint. Each claim registers ``YANDEX_DELIVERY_CALLBACK_URL?ref=<order>-<hmac>&``
(:mod:`glowderma.commerce.yandex_ref`): the ``ref`` is verified in constant time first (a missing or malformed
ref → 400, a wrong HMAC → 403, both with no detail and a bounded ``rejected:`` inbox row); ``claim_id`` must
be one of **that** order's claims (else the call is acknowledged and ignored); the ``WebhookInbox`` event key
``"{claim_id}:{updated_ts}"`` drops repeats; then ``yandex.sync_claim`` is enqueued (a fresh ``claims/info``
→ ``commerce.services.apply_yandex_claim``). No Yandex call runs in the request and every verified call
answers 200 (Yandex stops retrying on 200). Refused while the fake runs under the production settings
(``callbacks_refused("yandex")``, 403). The ``ref`` never reaches a log or a stored row: gunicorn and the edge
log no query string and ``WebhookInbox.path`` has none; the stored payload is ``{claim_id, updated_ts}``.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from django.db import transaction
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework.request import Request
from rest_framework.response import Response

from glowderma.accounts.api.customer_base import WebhookAPIView
from glowderma.commerce import topics, yandex_ref
from glowderma.commerce.api.webhooks import inbox
from glowderma.core import integration_modes, outbox, timeutils
from glowderma.delivery import selectors as delivery_selectors
from glowderma.integrations.yandex_delivery import protocol as yandex_protocol

logger = logging.getLogger(__name__)
PROVIDER = "yandex"
OK: dict[str, Any] = {"ok": True}
IGNORED: dict[str, Any] = {"ok": True, "ignored": True}
REFUSED: dict[str, Any] = {"ok": False}
UPDATED_TS_MAX = 40


class YandexDeliveryWebhookView(WebhookAPIView):
    parser_classes = []  # the body is never read

    @extend_schema(
        operation_id="webhook_yandex_delivery_status",
        description="Yandex Delivery claim callback (an unauthenticated hint): `?ref=<order>-<hmac>` (our "
        "own reference, registered per claim) plus the `updated_ts` and `claim_id` Yandex appends; answers "
        "200 for a verified ref and re-reads the claim asynchronously.",
        parameters=[
            OpenApiParameter("ref", OpenApiTypes.STR, required=True),
            OpenApiParameter("claim_id", OpenApiTypes.STR, required=False),
            OpenApiParameter("updated_ts", OpenApiTypes.STR, required=False),
        ],
        request=None,
        responses={200: OpenApiTypes.OBJECT, 400: OpenApiTypes.OBJECT, 403: OpenApiTypes.OBJECT},
        auth=[],
    )
    def post(self, request: Request) -> Response:
        started = time.monotonic()
        try:
            return self._handle(request, started)
        except Exception:
            logger.exception("yandex.webhook failed; everything rolled back")
            return Response(REFUSED, status=500)

    def _handle(self, request: Request, started: float) -> Response:
        if integration_modes.callbacks_refused(PROVIDER):
            inbox.store_rejected(
                PROVIDER, request, None, status=403, body=REFUSED, error_code="fake_mode", started=started
            )
            return Response(REFUSED, status=403)
        ref = str(request.query_params.get(yandex_ref.QUERY_KEY) or "")
        order_id = yandex_ref.verify(ref)
        if order_id is None:
            status = 403 if ref and "-" in ref else 400
            inbox.store_rejected(
                PROVIDER, request, None, status=status, body=REFUSED, error_code="bad_ref", started=started
            )
            return Response(REFUSED, status=status)
        claim_id = str(request.query_params.get("claim_id") or "")[:64]
        updated_ts = " ".join(str(request.query_params.get("updated_ts") or "").split())[:UPDATED_TS_MAX]
        payload = {"claim_id": claim_id, "updated_ts": updated_ts}
        entity = f"order:{order_id}"
        claim = (
            delivery_selectors.claim_by_claim_id(order_id, claim_id)
            if yandex_protocol.valid_claim_id(claim_id)
            else None
        )
        if claim is None:
            logger.warning("yandex.webhook_foreign_claim %s", {"order_id": order_id})
            inbox.store_rejected(
                PROVIDER,
                request,
                payload,
                status=200,
                body=IGNORED,
                error_code="foreign_claim",
                started=started,
            )
            return Response(IGNORED)
        event_key = f"{claim_id}:{updated_ts}"
        with transaction.atomic():
            if inbox.exists(PROVIDER, event_key) is not None:
                inbox.store_delivery(
                    PROVIDER,
                    event_key,
                    request,
                    payload,
                    status=200,
                    body=OK,
                    outcome=inbox.OUTCOME_DUPLICATE,
                    entity_ref=entity,
                    started=started,
                )
                return Response(OK)
            outbox.enqueue(
                topics.YANDEX_SYNC_CLAIM,
                {"order_id": order_id, "claim_id": claim_id, "updated_ts": updated_ts},
                dedupe_key=topics.yandex_sync_claim_key(claim_id, updated_ts or timeutils.now().isoformat()),
            )
            inbox.store_delivery(
                PROVIDER,
                event_key,
                request,
                payload,
                status=200,
                body=OK,
                outcome=inbox.OUTCOME_ACCEPTED,
                entity_ref=entity,
                started=started,
            )
        return Response(OK)
