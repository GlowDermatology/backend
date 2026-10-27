"""Customer loyalty routes (customer contract §2.9): ``me/cashback/``, ``me/cashback/ledger/``, ``me/qr/``.

Every query starts from the signed-in customer; the views only shape requests and responses (the numbers come
from ``loyalty.selectors``, the token from ``loyalty.qr``).
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any, cast

from django.conf import settings
from drf_spectacular.utils import extend_schema
from rest_framework import mixins
from rest_framework.request import Request
from rest_framework.response import Response

from glowderma.accounts.api.customer_base import CustomerAPIView, CustomerGenericViewSet
from glowderma.accounts.api.throttles import CustomerRateThrottle, QrThrottle
from glowderma.accounts.models import Customer
from glowderma.core import timeutils
from glowderma.core.api.schema import ACCEPT_LANGUAGE_HEADER, CUSTOMER_ERRORS, DEVICE_HEADERS
from glowderma.loyalty import qr, selectors
from glowderma.loyalty.api.customer.serializers import (
    CashbackSummarySerializer,
    LedgerItemSerializer,
    QrTokenSerializer,
)

HEADERS = [*DEVICE_HEADERS, ACCEPT_LANGUAGE_HEADER]


def request_customer(request: Request) -> Customer:
    """The signed-in customer (``IsActiveCustomer`` already checked the type and status)."""
    return cast(Customer, request.user)


class CashbackView(CustomerAPIView):
    @extend_schema(
        operation_id="customer_cashback_retrieve",
        description=(
            "Balance, active holds (with their effective expiry), lifetime total, tier and next tier under "
            "the active policy, the policy bands and the QR token lifetime."
        ),
        parameters=HEADERS,
        responses={200: CashbackSummarySerializer, **CUSTOMER_ERRORS},
    )
    def get(self, request: Request) -> Response:
        summary = selectors.cashback_summary(request_customer(request).pk, timeutils.now())
        payload: dict[str, Any] = {**asdict(summary), "qr_ttl_seconds": int(settings.QR_TOKEN_TTL_SECONDS)}
        return Response(CashbackSummarySerializer(payload).data)


class CashbackLedgerViewSet(mixins.ListModelMixin, CustomerGenericViewSet):
    serializer_class = LedgerItemSerializer
    page_size_options = (20, 50)
    default_page_size = 20

    @extend_schema(
        operation_id="customer_cashback_ledger_list",
        description=(
            "The customer's own ledger, newest first. Entries before the history horizon keep their amounts "
            "but show `order_id: null` and the subtitle of the previous account; the staff reason of an "
            "adjustment is never shown."
        ),
        parameters=HEADERS,
        responses={200: LedgerItemSerializer(many=True), **CUSTOMER_ERRORS},
    )
    def list(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        customer = request_customer(request)
        page = self.paginate_queryset(selectors.customer_ledger_page(customer.pk))
        lang = self.language
        horizon = customer.history_from
        items = [asdict(selectors.ledger_item_view(entry, lang, horizon)) for entry in page or []]
        return self.get_paginated_response(LedgerItemSerializer(items, many=True).data)


class QrView(CustomerAPIView):
    throttle_classes = [CustomerRateThrottle, QrThrottle]

    @extend_schema(
        operation_id="customer_qr_retrieve",
        description=(
            "A fresh single-use personal QR token (60 s; at most two live tokens per customer). Request a "
            "new one every `refresh_in_seconds` and whenever the screen regains focus."
        ),
        parameters=HEADERS,
        responses={200: QrTokenSerializer, **CUSTOMER_ERRORS},
    )
    def get(self, request: Request) -> Response:
        token = qr.issue_qr(request_customer(request).pk)
        return Response(QrTokenSerializer(asdict(token)).data)
