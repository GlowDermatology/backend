"""OpenAPI declarations for customer loyalty routes (customer contract §2.9): ``me/cashback/``,
``me/cashback/ledger/``, ``me/qr/``.

Every query starts from the signed-in customer; the views only shape requests and responses (the numbers come
from ``loyalty.selectors``, the token from ``loyalty.qr``)."""

from __future__ import annotations

from drf_spectacular.utils import extend_schema

from glowderma.core.api.schema import ACCEPT_LANGUAGE_HEADER, CUSTOMER_ERRORS, DEVICE_HEADERS
from glowderma.loyalty.api.customer.serializers import (
    CashbackSummarySerializer,
    LedgerItemSerializer,
    QrTokenSerializer,
)

HEADERS = [*DEVICE_HEADERS, ACCEPT_LANGUAGE_HEADER]

CASHBACK_GET = extend_schema(
    operation_id="customer_cashback_retrieve",
    description=(
        "Balance, active holds (with their effective expiry), lifetime total, tier and next tier under "
        "the active policy, the policy bands and the QR token lifetime."
    ),
    parameters=HEADERS,
    responses={200: CashbackSummarySerializer, **CUSTOMER_ERRORS},
)

CASHBACK_LEDGER_LIST = extend_schema(
    operation_id="customer_cashback_ledger_list",
    description=(
        "The customer's own ledger, newest first. Entries before the history horizon keep their amounts "
        "but show `order_id: null` and the subtitle of the previous account; the staff reason of an "
        "adjustment is never shown."
    ),
    parameters=HEADERS,
    responses={200: LedgerItemSerializer(many=True), **CUSTOMER_ERRORS},
)

QR_GET = extend_schema(
    operation_id="customer_qr_retrieve",
    description=(
        "A fresh single-use personal QR token (60 s; at most two live tokens per customer). Request a "
        "new one every `refresh_in_seconds` and whenever the screen regains focus. 409 `phone_required` "
        "while the account has no verified phone (`me/phone/`)."
    ),
    parameters=HEADERS,
    responses={200: QrTokenSerializer, **CUSTOMER_ERRORS},
)


__all__ = [
    "CASHBACK_GET",
    "CASHBACK_LEDGER_LIST",
    "HEADERS",
    "QR_GET",
]
