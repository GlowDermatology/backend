"""OpenAPI declarations for ``POST pos/cashback/resolve-qr/`` (customer contract §2.10): a cashier scans the
customer's personal QR.

The token is consumed (``GETDEL``) by the first scan; every failure answers the same 409 ``qr_invalid`` (no
oracle). The response names neither the customer nor our id: an opaque per-device ``customer_ref``, the phone
(what Billz needs) and its mask, the available cashback and the tier percent. Every resolve is audited
(``pos.qr_resolved`` / ``pos.qr_rejected``, actor type ``integration``)."""

from __future__ import annotations

from drf_spectacular.utils import extend_schema

from glowderma.core.api.schema import ACCEPT_LANGUAGE_HEADER, CustomerErrorEnvelopeSerializer
from glowderma.loyalty.api.customer.serializers import ResolvedQrSerializer, ResolveQrSerializer

POS_ERRORS = dict.fromkeys((400, 401, 409, 429), CustomerErrorEnvelopeSerializer)

RESOLVE_QR_POST = extend_schema(
    operation_id="pos_cashback_resolve_qr",
    description=(
        "Resolve a scanned personal QR token (single use). Every failure answers 409 `qr_invalid` with "
        "the same body. `customer_ref` is opaque and stable per (POS device, customer)."
    ),
    parameters=[ACCEPT_LANGUAGE_HEADER],
    request=ResolveQrSerializer,
    responses={200: ResolvedQrSerializer, **POS_ERRORS},
)


__all__ = [
    "POS_ERRORS",
    "RESOLVE_QR_POST",
]
