"""Customer inbox routes (customer contract §2.12): the feed with its unread badge, read one / all, and the
delivery acknowledgement of the app's push handler. Every query starts from the signed-in customer and stays
inside the history horizon; another customer's row answers 404 (read) or is ignored (ack). The three POSTs
take an optional ``Idempotency-Key`` (customer contract §1.7: absolute writes, a retry converges)."""

from __future__ import annotations

from typing import Any, cast

from drf_spectacular.utils import extend_schema
from rest_framework import mixins
from rest_framework.request import Request
from rest_framework.response import Response

from glowderma.accounts.api.customer_base import CustomerAPIView, CustomerGenericViewSet
from glowderma.accounts.models import Customer
from glowderma.core.api.pagination import EnvelopePagination
from glowderma.core.api.schema import ACCEPT_LANGUAGE_HEADER, CUSTOMER_ERRORS, DEVICE_HEADERS
from glowderma.core.idempotency import idempotent
from glowderma.notifications import selectors, services
from glowderma.notifications.api.customer.serializers import (
    AckRequestSerializer,
    AckSerializer,
    InboxItemSerializer,
    ReadAllSerializer,
    ReadSerializer,
    UnreadCountSerializer,
)

HEADERS = [*DEVICE_HEADERS, ACCEPT_LANGUAGE_HEADER]


def _customer(request: Request) -> Customer:
    return cast(Customer, request.user)


class InboxPagination(EnvelopePagination):
    """The envelope plus one top-level ``unread_count`` (customer contract §1.5)."""

    unread_count = 0

    def paginate_queryset(self, queryset: Any, request: Request, view: Any = None) -> list[Any]:
        customer = _customer(request)
        self.unread_count = selectors.unread_count(customer.pk, customer.history_from)
        return super().paginate_queryset(queryset, request, view)

    def get_paginated_response(self, data: Any) -> Response:
        response = super().get_paginated_response(data)
        response.data["unread_count"] = self.unread_count
        return response

    def get_paginated_response_schema(self, schema: dict[str, Any]) -> dict[str, Any]:
        envelope = super().get_paginated_response_schema(schema)
        envelope["properties"]["unread_count"] = {"type": "integer"}
        envelope["required"] = [*envelope["required"], "unread_count"]
        return envelope


class InboxViewSet(mixins.ListModelMixin, CustomerGenericViewSet):
    serializer_class = InboxItemSerializer
    pagination_class = InboxPagination
    page_size_options = (20, 50)
    default_page_size = 20

    def get_queryset(self) -> Any:
        customer = _customer(self.request)
        return selectors.inbox_page(customer.pk, customer.history_from)

    @extend_schema(
        operation_id="customer_notifications_list",
        description=(
            "The inbox, newest first, inside the history horizon; also the rows whose push could not be "
            "sent. `unread_count` is the badge."
        ),
        parameters=HEADERS,
        responses={200: InboxItemSerializer(many=True), **CUSTOMER_ERRORS},
    )
    def list(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        return super().list(request, *args, **kwargs)


class UnreadCountView(CustomerAPIView):
    @extend_schema(
        operation_id="customer_notifications_unread_count",
        description="The unread badge (polling).",
        parameters=HEADERS,
        responses={200: UnreadCountSerializer, **CUSTOMER_ERRORS},
    )
    def get(self, request: Request) -> Response:
        customer = _customer(request)
        return Response({"unread_count": selectors.unread_count(customer.pk, customer.history_from)})


class ReadView(CustomerAPIView):
    @extend_schema(
        operation_id="customer_notifications_read",
        description="Mark one row read (idempotent); reading implies delivery (counted like an ack).",
        parameters=HEADERS,
        request=None,
        responses={200: ReadSerializer, **CUSTOMER_ERRORS},
    )
    @idempotent(required=False)
    def post(self, request: Request, pk: int) -> Response:
        customer = _customer(request)
        result = services.mark_read(customer.pk, pk, horizon=customer.history_from)
        return Response(ReadSerializer(result).data)


class ReadAllView(CustomerAPIView):
    @extend_schema(
        operation_id="customer_notifications_read_all",
        description="Mark every unread row read (each acknowledged first).",
        parameters=HEADERS,
        request=None,
        responses={200: ReadAllSerializer, **CUSTOMER_ERRORS},
    )
    @idempotent(required=False)
    def post(self, request: Request) -> Response:
        customer = _customer(request)
        result = services.mark_all_read(customer.pk, horizon=customer.history_from)
        return Response(ReadAllSerializer(result).data)


class AckView(CustomerAPIView):
    @extend_schema(
        operation_id="customer_notifications_ack",
        description=(
            "Delivery acknowledgement from the push handler (1..50 ids). Unknown ids and ids of other "
            "customers are ignored silently; a retry never counts twice."
        ),
        parameters=HEADERS,
        request=AckRequestSerializer,
        responses={200: AckSerializer, **CUSTOMER_ERRORS},
    )
    @idempotent(required=False)
    def post(self, request: Request) -> Response:
        customer = _customer(request)
        body = AckRequestSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        result = services.acknowledge(
            customer.pk, body.validated_data["inbox_ids"], horizon=customer.history_from
        )
        return Response({"acknowledged": result.acknowledged})
