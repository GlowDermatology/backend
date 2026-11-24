"""OpenAPI declarations for payme Merchant API (JSON-RPC 2.0) callbacks (customer contract §2.13.2):
``webhooks/payme/``.

Every answer is HTTP 200 with a JSON-RPC body; amounts are tiyin and exist only here; times are epoch ms.
Basic auth with ``PAYME_KEY`` first (``-32504``), then the JSON-RPC envelope, then the method — each method
takes the advisory lock of its Payme transaction and ``commerce.services.lock_order`` before reading state,
and the ``WebhookInbox`` row is written last (every delivery its own row; read-only methods without the raw
body). An unexpected exception rolls everything back and answers ``-32400`` — also one raised before the
dispatch (auth, peek, parse, a refusal's inbox row), so the endpoint never answers anything but HTTP 200 with
a JSON-RPC body. A Payme port that is fake under the production settings answers ``-32504`` to every call
before anything is read."""

from __future__ import annotations

from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema

PAYME_POST = extend_schema(
    operation_id="webhook_payme_rpc",
    description="Payme Merchant API (JSON-RPC 2.0); HTTP 200 with a JSON-RPC body (customer contract "
    "§2.13.2).",
    request={"application/json": OpenApiTypes.OBJECT},
    responses={200: OpenApiTypes.OBJECT},
    auth=[],
)

PAYME_GET = extend_schema(exclude=True)

PAYME_OPTIONS = extend_schema(exclude=True)


__all__ = [
    "PAYME_GET",
    "PAYME_OPTIONS",
    "PAYME_POST",
]
