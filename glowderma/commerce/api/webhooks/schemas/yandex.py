"""OpenAPI declarations for ``POST webhooks/yandex-delivery/`` (Yandex Delivery plan §3.7, decision 10): claim
status callbacks.

**What Yandex sends** (``claims/create`` → ``callback_properties.callback_url``, docs rev. r22073422): a POST
to the URL we registered for the claim with ``updated_ts=<ISO-8601>&claim_id=<claim id>`` appended by
concatenation — no status, no body we rely on, **no signature**. The mechanism is documented as deprecated, "a
push ahead of polling": the 60 s ``bulk_info`` poll (``commerce.poll_yandex``) is the reliable path.

So the callback is only a hint. Each claim registers ``YANDEX_DELIVERY_CALLBACK_URL?ref=<order>-<hmac>&``
(:mod:`glowderma.commerce.yandex_ref`): the ``ref`` is verified in constant time first (a missing or malformed
ref → 400, a wrong HMAC → 403, both with no detail and a bounded ``rejected:`` inbox row); ``claim_id`` must
be one of **that** order's claims (else the call is acknowledged and ignored); the ``WebhookInbox`` event key
``"{claim_id}:{updated_ts}"`` drops repeats; then ``yandex.sync_claim`` is enqueued (a fresh ``claims/info`` →
``commerce.services.apply_yandex_claim``). No Yandex call runs in the request and every verified call answers
200 (Yandex stops retrying on 200). Refused while the fake runs under the production settings
(``callbacks_refused("yandex")``, 403). The ``ref`` never reaches a log or a stored row: gunicorn and the edge
log no query string and ``WebhookInbox.path`` has none; the stored payload is ``{claim_id, updated_ts}``."""

from __future__ import annotations

from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema

YANDEX_DELIVERY_WEBHOOK_POST = extend_schema(
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


__all__ = [
    "YANDEX_DELIVERY_WEBHOOK_POST",
]
