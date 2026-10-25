"""Admin views of `payments`: ``GET payments/`` (contract §2.12, P E, export ``tolovlar``)."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Any, cast

from django.db.models import QuerySet
from drf_spectacular.utils import extend_schema
from rest_framework import mixins
from rest_framework.request import Request
from rest_framework.response import Response

from glowderma.accounts.api.base import AdminGenericViewSet
from glowderma.core import labels
from glowderma.core.api.schema import ADMIN_ERRORS, EXPORT_PARAM
from glowderma.core.exports import ExportMixin, format_export_datetime
from glowderma.payments import selectors
from glowderma.payments.api.admin.serializers import PaymentListQuerySerializer, PaymentSerializer
from glowderma.payments.models import Payment


class PaymentViewSet(ExportMixin, mixins.ListModelMixin, AdminGenericViewSet):
    serializer_class = PaymentSerializer
    export_name = "tolovlar"
    export_resource = "payments"
    export_headers = labels.PAYMENT_EXPORT_HEADERS

    def get_queryset(self) -> QuerySet[Payment]:
        query = PaymentListQuerySerializer(data=self.request.query_params.dict())
        query.is_valid(raise_exception=True)
        return selectors.payments_list(cast(selectors.PaymentFilters, dict(query.validated_data)))

    def get_serializer(self, *args: Any, **kwargs: Any) -> Any:
        instance = args[0] if args else kwargs.get("instance")
        if instance is not None:
            rows = list(instance) if kwargs.get("many") else [instance]
            kwargs.setdefault("context", self.get_serializer_context())
            kwargs["context"]["refunds"] = selectors.refunds_by_payment(row.pk for row in rows)
        return super().get_serializer(*args, **kwargs)

    def get_export_rows(self, items: Iterable[Payment]) -> Iterable[Sequence[Any]]:
        rows = list(items)
        refunds = selectors.refunds_by_payment(payment.pk for payment in rows)
        for payment in rows:
            customer = payment.order.customer
            newest = refunds.get(payment.pk, [])
            yield (
                payment.pk,
                payment.order_id,
                customer.name,
                customer.phone,
                labels.PROVIDER[payment.provider],
                labels.PAYMENT_STATUS[payment.status],
                payment.amount,
                payment.provider_txn_id,
                format_export_datetime(payment.created_at),
                format_export_datetime(payment.confirmed_at),
                labels.REFUND_STATE[newest[0].state] if newest else "",
            )

    @extend_schema(
        operation_id="admin_payments_list",
        parameters=[PaymentListQuerySerializer, EXPORT_PARAM],
        responses={200: PaymentSerializer(many=True), **ADMIN_ERRORS},
    )
    def list(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        return super().list(request, *args, **kwargs)
