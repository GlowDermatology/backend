"""OpenAPI declarations for click SHOP API callbacks (customer contract §2.13.1): ``webhooks/click/prepare/``
and ``…/complete/``.

Order of work (§2.13.0): the protocol checks that need no database (parameters → content type → IP → signature
→ action → service id → account reference), the advisory lock of the Click transaction, the inbox duplicate
check, ``commerce.services.lock_order`` before any state read, the state transition through a commerce service
that names the payment, and the ``WebhookInbox`` row last, all in one transaction. An unexpected exception
rolls everything back and answers HTTP 500 (Click retries). A Click port that is fake under the production
settings starts no transaction and honours no capture: every Prepare answers ``-1``; a Complete is parsed and
signature-checked, and only one naming a payment attached while Click was live runs the Complete steps, with
the capture always refused (``record_late_capture(case="fake_mode")``, a manual refund)."""

from __future__ import annotations

from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema

FORM_CONTENT_TYPE = "application/x-www-form-urlencoded"

SCHEMA = {
    "request": {FORM_CONTENT_TYPE: OpenApiTypes.OBJECT},
    "responses": {200: OpenApiTypes.OBJECT, 500: OpenApiTypes.OBJECT},
    "auth": [],
}

CLICK_PREPARE_POST = extend_schema(
    operation_id="webhook_click_prepare",
    description="Click SHOP API Prepare (action 0); provider-native body (customer contract §2.13.1).",
    **SCHEMA,  # type: ignore[arg-type]
)

CLICK_COMPLETE_POST = extend_schema(
    operation_id="webhook_click_complete",
    description="Click SHOP API Complete (action 1); provider-native body (customer contract §2.13.1).",
    **SCHEMA,  # type: ignore[arg-type]
)


__all__ = [
    "CLICK_COMPLETE_POST",
    "CLICK_PREPARE_POST",
    "FORM_CONTENT_TYPE",
    "SCHEMA",
]
