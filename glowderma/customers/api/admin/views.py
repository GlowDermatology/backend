"""Admin views of `customers` (contract §2.6): the list (P E), the card and the cashback adjustment (I)."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Any, cast

from django.db.models import QuerySet
from drf_spectacular.utils import extend_schema
from rest_framework import mixins
from rest_framework.exceptions import NotFound
from rest_framework.request import Request
from rest_framework.response import Response

from glowderma.accounts.api.base import AdminAPIView, AdminGenericViewSet
from glowderma.accounts.models import Customer
from glowderma.core import labels
from glowderma.core.api.exceptions import ContractValidationError
from glowderma.core.api.schema import ADMIN_ERRORS, EXPORT_PARAM, IDEMPOTENCY_KEY_HEADER
from glowderma.core.exports import ExportMixin, format_export_datetime
from glowderma.core.idempotency import idempotent
from glowderma.customers import selectors, services
from glowderma.customers.api.admin.serializers import (
    CashbackAdjustmentDirectionSerializer,
    CashbackAdjustmentResultSerializer,
    CashbackAdjustmentSerializer,
    CustomerCardSerializer,
    CustomerListQuerySerializer,
    CustomerRowSerializer,
)
from glowderma.loyalty.services import LoyaltyValidationError
from glowderma.notifications.api.admin.serializers import CustomerListItemSerializer


class CustomerViewSet(ExportMixin, mixins.ListModelMixin, AdminGenericViewSet):
    serializer_class = CustomerRowSerializer
    export_name = "mijozlar"
    export_resource = "customers"
    export_headers = labels.CUSTOMER_EXPORT_HEADERS

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
                customer.phone,
                format_export_datetime(customer.registered_at),
                int(getattr(customer, "lifetime_total", 0)),
                "" if percent is None else f"{percent:.2f}",
                int(getattr(customer, "balance", 0)),
                labels.CUSTOMER_STATUS[customer.status],
            )

    @extend_schema(
        operation_id="admin_customers_list",
        description=(
            "`search`: the name contains the term, or (≥ 3 digits) the phone contains its digits. `segment`: "
            "segment membership (rules or manual, active customers only). Orderings tie by `id`."
        ),
        parameters=[CustomerListQuerySerializer, EXPORT_PARAM],
        responses={200: CustomerListItemSerializer(many=True), **ADMIN_ERRORS},
    )
    def list(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        return super().list(request, *args, **kwargs)

    @extend_schema(
        operation_id="admin_customers_retrieve",
        description='The customer card of an active or deleted customer (deleted: name `""`, `deleted_at`).',
        responses={200: CustomerCardSerializer, **ADMIN_ERRORS},
    )
    def retrieve(self, request: Request, pk: int) -> Response:
        card = selectors.customer_card(int(pk))
        if card is None:
            raise NotFound()
        return Response(CustomerCardSerializer(card).data)


class CashbackAdjustmentView(AdminAPIView):
    @extend_schema(
        operation_id="admin_customers_cashback_adjustments_create",
        description=(
            'Checks in order: `amount` 1..10 000 000 ("Summani kiriting."), `reason` trimmed 3..300 '
            '("Sababni kiriting."), then for `subtract` `amount ≤ available` under the account lock '
            '("Balans manfiy bo‘lib qoladi."). Allowed for deleted customers.'
        ),
        parameters=[IDEMPOTENCY_KEY_HEADER],
        request=CashbackAdjustmentSerializer,
        responses={201: CashbackAdjustmentResultSerializer, **ADMIN_ERRORS},
    )
    @idempotent()
    def post(self, request: Request, pk: int) -> Response:
        body: Any = request.data
        data: dict[str, Any] = body if isinstance(body, dict) else {}
        direction = CashbackAdjustmentDirectionSerializer(
            data={k: v for k, v in data.items() if k == "direction"}
        )
        if not selectors.customer_exists(int(pk)):
            raise NotFound()
        direction.is_valid(raise_exception=True)
        try:
            outcome = services.adjust_cashback(
                int(pk),
                direction=direction.validated_data["direction"],
                amount=data.get("amount"),
                reason=data.get("reason"),
                actor_id=cast(int, request.user.pk),
                request=request,
            )
        except services.CustomerNotFound as exc:
            raise NotFound() from exc
        except LoyaltyValidationError as exc:
            raise ContractValidationError(exc.errors, message=exc.message) from exc
        return Response(CashbackAdjustmentResultSerializer(outcome).data, status=201)
