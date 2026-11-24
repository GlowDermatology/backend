"""A segment's members: list (with its export), add and remove."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Any, cast

from django.db.models import QuerySet
from rest_framework import mixins
from rest_framework.request import Request
from rest_framework.response import Response

from glowderma.accounts.api.base import AdminAPIView, AdminGenericViewSet
from glowderma.accounts.models import Customer
from glowderma.core import labels
from glowderma.core.exports import ExportMixin, format_export_datetime
from glowderma.core.idempotency import idempotent
from glowderma.core.uzsource import N_
from glowderma.loyalty import selectors as loyalty_selectors
from glowderma.notifications import selectors, services
from glowderma.notifications.api.admin.schemas.segment_members import (
    SEGMENT_MEMBER_DETAIL_DELETE,
    SEGMENT_MEMBERS_ADD,
    SEGMENT_MEMBERS_LIST,
)
from glowderma.notifications.api.admin.serializers import (
    CustomerListItemSerializer,
    SegmentMembersInputSerializer,
    SegmentSerializer,
)
from glowderma.notifications.api.admin.views.common import segment_or_404


def _percent_cell(percent: Any) -> str:
    return "" if percent is None else f"{percent:.2f}"


class SegmentMembersViewSet(ExportMixin, mixins.ListModelMixin, AdminGenericViewSet):
    serializer_class = CustomerListItemSerializer
    export_name = N_("guruh-azolari")
    export_filter_keys = frozenset[str]()  # no filters
    export_resource = "segment_members"
    export_headers = labels.SEGMENT_MEMBER_EXPORT_HEADERS

    def get_queryset(self) -> QuerySet[Customer]:
        segment = segment_or_404(int(self.kwargs["pk"]))
        loyalty = (
            loyalty_selectors.list_annotations()
        )  # lifetime_total / online_orders_count come from the rules
        return (
            selectors.segment_members(segment)
            .annotate(balance=loyalty["balance"], percent=loyalty["percent"])
            .order_by("-registered_at", "-id")
        )

    def get_export_rows(self, items: Iterable[Customer]) -> Iterable[Sequence[Any]]:
        for customer in items:
            yield (
                customer.pk,
                customer.name,
                customer.phone or customer.email or "",  # hybrid identity: the e-mail of a phoneless account
                format_export_datetime(customer.registered_at),
                int(getattr(customer, "lifetime_total", 0)),
                _percent_cell(getattr(customer, "percent", None)),
                int(getattr(customer, "balance", 0)),
                labels.text(labels.CUSTOMER_STATUS, customer.status),
            )

    @SEGMENT_MEMBERS_LIST
    def list(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        return super().list(request, *args, **kwargs)

    @SEGMENT_MEMBERS_ADD
    @idempotent(required=False)
    def add(self, request: Request, pk: int) -> Response:
        segment = segment_or_404(pk)
        serializer = SegmentMembersInputSerializer(data=cast(dict[str, Any], request.data))
        serializer.is_valid(raise_exception=True)
        services.add_members(
            segment, serializer.validated_data["customer_ids"], actor=request.user, request=request
        )
        return Response(SegmentSerializer(segment).data)


class SegmentMemberDetailView(AdminAPIView):
    @SEGMENT_MEMBER_DETAIL_DELETE
    @idempotent(required=False)
    def delete(self, request: Request, pk: int, customer_id: int) -> Response:
        services.remove_member(segment_or_404(pk), customer_id, actor=request.user, request=request)
        return Response(status=204)


__all__ = [
    "SegmentMemberDetailView",
    "SegmentMembersViewSet",
]
