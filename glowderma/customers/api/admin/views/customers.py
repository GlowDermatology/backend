"""The admin customer list (P E) and card (admin contract §2.6)."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Any

from django.db.models import QuerySet
from rest_framework import mixins
from rest_framework.exceptions import NotFound
from rest_framework.request import Request
from rest_framework.response import Response

from glowderma.accounts.api.base import AdminGenericViewSet
from glowderma.accounts.models import Customer
from glowderma.core import labels
from glowderma.core.exports import ExportMixin, format_export_datetime
from glowderma.core.uzsource import N_
from glowderma.customers import selectors
from glowderma.customers.api.admin.schemas.customers import CUSTOMER_LIST, CUSTOMER_RETRIEVE
from glowderma.customers.api.admin.serializers import (
    CustomerCardSerializer,
    CustomerListQuerySerializer,
)
from glowderma.notifications.api.admin.serializers import CustomerListItemSerializer


class CustomerViewSet(ExportMixin, mixins.ListModelMixin, AdminGenericViewSet):
    serializer_class = CustomerListItemSerializer
    export_name = N_("mijozlar")
    export_resource = "customers"
    export_headers = labels.CUSTOMER_EXPORT_HEADERS
    export_filter_keys = frozenset(CustomerListQuerySerializer().fields)

    def get_queryset(self) -> QuerySet[Customer]:
        query = CustomerListQuerySerializer(data=self.request.query_params.dict())
        query.is_valid(raise_exception=True)
        params = query.validated_data
        return selectors.customers_list(
            search=params.get("search"),
            status=params.get("status"),
            segment_id=params.get("segment"),
            ordering=params["ordering"],
        )

    def get_export_rows(self, items: Iterable[Customer]) -> Iterable[Sequence[Any]]:
        for customer in items:
            percent = getattr(customer, "percent", None)
            yield (
                customer.pk,
                customer.name,
                customer.phone or customer.email or "",  # hybrid identity: the e-mail of a phoneless account
                format_export_datetime(customer.registered_at),
                int(getattr(customer, "lifetime_total", 0)),
                "" if percent is None else f"{percent:.2f}",
                int(getattr(customer, "balance", 0)),
                labels.text(labels.CUSTOMER_STATUS, customer.status),
            )

    @CUSTOMER_LIST
    def list(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        return super().list(request, *args, **kwargs)

    @CUSTOMER_RETRIEVE
    def retrieve(self, request: Request, pk: int) -> Response:
        card = selectors.customer_card(int(pk))
        if card is None:
            raise NotFound()
        return Response(CustomerCardSerializer(card).data)


__all__ = [
    "CustomerViewSet",
]
