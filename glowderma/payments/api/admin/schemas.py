"""OpenAPI declarations for admin views of `payments`: ``GET payments/`` (contract §2.12, P E, export
``tolovlar``)."""

from __future__ import annotations

from drf_spectacular.utils import extend_schema

from glowderma.core.api.schema import ADMIN_ERRORS, EXPORT_PARAM
from glowderma.payments.api.admin.serializers import PaymentListQuerySerializer, PaymentSerializer

PAYMENT_LIST = extend_schema(
    operation_id="admin_payments_list",
    parameters=[PaymentListQuerySerializer, EXPORT_PARAM],
    responses={200: PaymentSerializer(many=True), **ADMIN_ERRORS},
)


__all__ = [
    "PAYMENT_LIST",
]
