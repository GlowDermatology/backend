"""OpenAPI declarations for ``POST webhooks/bts/`` (customer contract §2.13.3): BTS status notifications.

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
health)."""

from __future__ import annotations

from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema

BTS_WEBHOOK_POST = extend_schema(
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


__all__ = [
    "BTS_WEBHOOK_POST",
]
