"""The delivery exception queue (Yandex Delivery plan §3.9): list, open on an order and resolve."""

from __future__ import annotations

from typing import Any, cast

from django.db.models import QuerySet
from rest_framework import mixins
from rest_framework.exceptions import NotFound
from rest_framework.request import Request
from rest_framework.response import Response

from glowderma.accounts.api.base import AdminAPIView, AdminGenericViewSet
from glowderma.commerce.api.admin.schemas.delivery_issues import (
    DELIVERY_ISSUE_LIST,
    DELIVERY_ISSUE_RESOLVE_POST,
    ORDER_ISSUE_CREATE_POST,
)
from glowderma.commerce.api.admin.serializers import (
    DeliveryIssueQuerySerializer,
    DeliveryIssueSerializer,
    OpenIssueSerializer,
    ResolveIssueSerializer,
)
from glowderma.commerce.api.admin.views.common import actor_id
from glowderma.commerce.models import Order
from glowderma.core import enums
from glowderma.core.api.exceptions import Conflict
from glowderma.core.idempotency import idempotent
from glowderma.core.uzsource import N_
from glowderma.delivery import selectors as delivery_selectors
from glowderma.delivery import services as delivery_services
from glowderma.delivery.models import DeliveryIssue

MESSAGE_PICKUP_ISSUE = N_("Olib ketiladigan buyurtma uchun yetkazish muammosi ochilmaydi.")


MESSAGE_ISSUE_RESOLVED = N_("Bu muammo allaqachon hal qilingan.")


def _issue_response(issue_id: int, status: int = 200) -> Response:
    row = DeliveryIssue.objects.select_related("resolved_by").filter(pk=issue_id).first()
    if row is None:
        raise NotFound
    return Response(DeliveryIssueSerializer(row).data, status=status)


class DeliveryIssueViewSet(mixins.ListModelMixin, AdminGenericViewSet):
    serializer_class = DeliveryIssueSerializer

    def get_queryset(self) -> QuerySet[DeliveryIssue]:
        query = DeliveryIssueQuerySerializer(data=self.request.query_params.dict())
        query.is_valid(raise_exception=True)
        return delivery_selectors.issues_queryset(
            cast(delivery_selectors.IssueFilters, dict(query.validated_data))
        )

    @DELIVERY_ISSUE_LIST
    def list(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        return super().list(request, *args, **kwargs)


class OrderIssueCreateView(AdminAPIView):
    @ORDER_ISSUE_CREATE_POST
    @idempotent()
    def post(self, request: Request, pk: int) -> Response:
        # the Order lock first (lock order Order → …), inside the idempotent transaction
        fulfilment = (
            Order.objects.select_for_update().filter(pk=pk).values_list("fulfilment", flat=True).first()
        )
        if fulfilment is None:
            raise NotFound
        body = OpenIssueSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        if fulfilment != enums.FulfilmentMethod.DELIVERY:
            raise Conflict(MESSAGE_PICKUP_ISSUE)
        issue_id, created = delivery_services.open_incident(
            pk,
            note=body.validated_data["note"],
            urgent=bool(body.validated_data["urgent"]),
            actor_id=actor_id(request),
            request=request,
        )
        return _issue_response(issue_id, status=201 if created else 200)


class DeliveryIssueResolveView(AdminAPIView):
    @DELIVERY_ISSUE_RESOLVE_POST
    @idempotent()
    def post(self, request: Request, pk: int) -> Response:
        if not DeliveryIssue.objects.filter(pk=pk).exists():
            raise NotFound
        body = ResolveIssueSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        try:
            changed = delivery_services.resolve_issue(
                pk, actor_id=actor_id(request), note=body.validated_data["note"], request=request
            )
        except delivery_services.IssueNotFound as exc:
            raise NotFound from exc
        if not changed:
            raise Conflict(MESSAGE_ISSUE_RESOLVED)
        return _issue_response(pk)


__all__ = [
    "MESSAGE_ISSUE_RESOLVED",
    "MESSAGE_PICKUP_ISSUE",
    "DeliveryIssueResolveView",
    "DeliveryIssueViewSet",
    "OrderIssueCreateView",
]
