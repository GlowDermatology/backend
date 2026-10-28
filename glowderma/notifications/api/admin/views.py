"""Admin views of `notifications` (contract §2.8: notifications, meta, templates, segments, members)."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Any, cast

from django.core.files.uploadedfile import UploadedFile
from django.db.models import QuerySet
from django.db.models.functions import Coalesce
from django.http import QueryDict
from drf_spectacular.utils import extend_schema, inline_serializer
from rest_framework import mixins, serializers
from rest_framework.exceptions import NotFound
from rest_framework.parsers import MultiPartParser
from rest_framework.request import Request
from rest_framework.response import Response

from glowderma.accounts.api.base import AdminAPIView, AdminGenericViewSet
from glowderma.accounts.models import Customer
from glowderma.core import enums, labels
from glowderma.core.api.parsers import JSONParser
from glowderma.core.api.schema import ADMIN_ERRORS, EXPORT_PARAM, IDEMPOTENCY_KEY_HEADER
from glowderma.core.exports import ExportMixin, format_export_datetime
from glowderma.core.idempotency import idempotent, refresh_after_commit
from glowderma.loyalty import selectors as loyalty_selectors
from glowderma.notifications import selectors, services
from glowderma.notifications.api.admin.serializers import (
    CustomerListItemSerializer,
    NotificationCreateSerializer,
    NotificationListQuerySerializer,
    NotificationMetaSerializer,
    NotificationRecordSerializer,
    SegmentMembersInputSerializer,
    SegmentSerializer,
    SegmentWriteSerializer,
    StatusTemplateSerializer,
    TemplateUpdateSerializer,
    record_state,
)
from glowderma.notifications.models import Campaign, CustomerSegment

NBSP = " "


def format_number(value: int) -> str:
    """The FE ``formatNumber``: NBSP thousands separator."""
    return f"{value:,}".replace(",", NBSP)


def format_phone(phone: str) -> str:
    """``+998901234567`` → ``+998 90 123 45 67`` (the FE ``formatPhone``)."""
    if len(phone) == 13 and phone.startswith("+998"):
        return f"{phone[:4]} {phone[4:6]} {phone[6:9]} {phone[9:11]} {phone[11:]}"
    return phone


def audience_label(campaign: Campaign) -> str:
    """Contract §5.5 ``audienceLabel``."""
    if campaign.audience_type == enums.AudienceType.ALL:
        return f"Barcha · {format_number(campaign.recipient_count)}"
    if campaign.audience_type == enums.AudienceType.SEGMENT:
        return f"{campaign.audience_segment_name} · {format_number(campaign.recipient_count)}"
    return f"Bitta mijoz · {format_phone(campaign.audience_phone)}"


def form_to_nested(data: Any) -> dict[str, Any]:
    """Un-dot multipart keys (``audience.type`` → ``{"audience": {"type": …}}``); files are left out."""
    if not isinstance(data, QueryDict):
        return dict(data) if isinstance(data, dict) else {}
    nested: dict[str, Any] = {}
    for key in data:
        value: Any = data.get(key)
        if isinstance(value, UploadedFile):
            continue
        target = nested
        parts = key.split(".")
        for part in parts[:-1]:
            child = target.get(part)
            if not isinstance(child, dict):
                child = {}
                target[part] = child
            target = child
        target[parts[-1]] = value
    return nested


NotificationFormSerializer = inline_serializer(
    name="NotificationForm",
    fields={
        "kind": serializers.ChoiceField(choices=enums.NotificationKind.values),
        "title": serializers.CharField(min_length=3, max_length=60),
        "body": serializers.CharField(min_length=5, max_length=180),
        "image": serializers.FileField(required=False),
        "link.kind": serializers.ChoiceField(choices=enums.LinkKind.values, required=False),
        "link.product_id": serializers.IntegerField(required=False),
        "link.url": serializers.CharField(required=False, max_length=500),
        "audience.type": serializers.ChoiceField(choices=enums.AudienceType.values),
        "audience.segment_id": serializers.IntegerField(required=False),
        "audience.phone": serializers.CharField(required=False),
        "scheduled_at": serializers.DateTimeField(required=False),
    },
)


# --- notifications ------------------------------------------------------------------------------------------


def _link_label(campaign: Campaign) -> str:
    """Export column ``Havola`` (gap ADM-10.1): ``mahsulot #<id>`` (else the product name kept at creation),
    the URL itself for kind ``url``, else empty — like the banners export."""
    if campaign.link_kind == enums.LinkKind.URL:
        return campaign.link_url
    if campaign.link_kind == enums.LinkKind.PRODUCT:
        return (
            f"mahsulot #{campaign.link_product_id}"
            if campaign.link_product_id
            else campaign.link_product_label
        )
    return ""


class NotificationViewSet(ExportMixin, mixins.ListModelMixin, AdminGenericViewSet):
    serializer_class = NotificationRecordSerializer
    export_name = "bildirishnomalar"
    export_filter_keys = frozenset(NotificationListQuerySerializer().fields)
    export_resource = "notifications"
    export_headers = labels.NOTIFICATION_EXPORT_HEADERS

    def get_parsers(self) -> list[Any]:
        # Contract §2.8: ``POST notifications/`` is multipart (anything else is a 415); GET has no body.
        # ``self.request`` is unset while drf-spectacular introspects the view.
        request = getattr(self, "request", None)
        return [MultiPartParser()] if request is not None and request.method == "POST" else [JSONParser()]

    def get_queryset(self) -> QuerySet[Campaign]:
        query = NotificationListQuerySerializer(data=self.request.query_params.dict())
        query.is_valid(raise_exception=True)
        params = query.validated_data
        return selectors.filter_campaigns(
            selectors.campaign_queryset(), state=params.get("state"), kind=params.get("kind")
        ).order_by(Coalesce("scheduled_at", "created_at").desc(), "-id")

    def get_export_rows(self, items: Iterable[Campaign]) -> Iterable[Sequence[Any]]:
        for campaign in items:
            yield (
                campaign.pk,
                format_export_datetime(campaign.scheduled_at or campaign.created_at),
                labels.NOTIFICATION_KIND[campaign.kind],
                campaign.title,
                audience_label(campaign),
                labels.NOTIFICATION_STATE[record_state(campaign)],
                "" if campaign.sent is None else campaign.sent,
                "" if campaign.delivered is None else campaign.delivered,
                _link_label(campaign),
            )

    @extend_schema(
        operation_id="admin_notifications_list",
        parameters=[NotificationListQuerySerializer, EXPORT_PARAM],
        responses={200: NotificationRecordSerializer(many=True), **ADMIN_ERRORS},
    )
    def list(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        return super().list(request, *args, **kwargs)

    @extend_schema(
        operation_id="admin_notifications_create",
        description=(
            "Multipart with dotted keys (`link.kind`, `audience.type`, …) and an optional `image` (JPEG, PNG "
            "or WebP, ≤ 5 MiB). Without `scheduled_at` the send starts at once; the 201 body is the campaign "
            "as stored after the commit (`queued`, or `sent` when the send already ran inside the request). "
            "With it the campaign waits for its time (state `scheduled`)."
        ),
        parameters=[IDEMPOTENCY_KEY_HEADER],
        request={"multipart/form-data": NotificationFormSerializer},
        responses={201: NotificationRecordSerializer, **ADMIN_ERRORS},
    )
    @idempotent()
    def create(self, request: Request) -> Response:
        serializer = NotificationCreateSerializer(data=form_to_nested(request.data))
        serializer.is_valid(raise_exception=True)
        campaign = services.create_campaign(
            serializer.to_input(), request.FILES.get("image"), actor=request.user, request=request
        )

        # The 201 body is the campaign as stored when the response is built (contract §2.8): re-read after
        # the transaction committed and its on-commit dispatch ran (an eager send already finished it).
        def body() -> Any:
            return NotificationRecordSerializer(selectors.get_campaign(campaign.pk)).data

        return refresh_after_commit(Response(body(), status=201), body)


class NotificationCancelView(AdminAPIView):
    @extend_schema(
        operation_id="admin_notifications_cancel",
        description="Cancel a `scheduled` campaign before its send starts (state `cancelled`, audited "
        "`notification.cancelled`). Idempotent: an already cancelled campaign answers 200 unchanged. 409 "
        "`conflict` once the send started (`queued` / `sent`); 404 for an unknown id.",
        request=None,
        responses={200: NotificationRecordSerializer, **ADMIN_ERRORS},
    )
    @idempotent(required=False)
    def post(self, request: Request, pk: int) -> Response:
        campaign = services.cancel_campaign(pk, actor=request.user, request=request)
        return Response(NotificationRecordSerializer(selectors.get_campaign(campaign.pk)).data)


class NotificationMetaView(AdminAPIView):
    @extend_schema(
        operation_id="admin_notifications_meta", responses={200: NotificationMetaSerializer, **ADMIN_ERRORS}
    )
    def get(self, request: Request) -> Response:
        return Response(NotificationMetaSerializer(selectors.meta()).data)


# --- templates ----------------------------------------------------------------------------------------------


class TemplateView(AdminAPIView):
    def _template(self, status: str) -> Any:
        template = selectors.get_template(status)
        if template is None:
            raise NotFound
        return template

    @extend_schema(
        operation_id="admin_notification_templates_retrieve",
        responses={200: StatusTemplateSerializer, **ADMIN_ERRORS},
    )
    def get(self, request: Request, status: str) -> Response:
        return Response(StatusTemplateSerializer(self._template(status)).data)

    @extend_schema(
        operation_id="admin_notification_templates_update",
        request=TemplateUpdateSerializer,
        responses={200: StatusTemplateSerializer, **ADMIN_ERRORS},
    )
    @idempotent(required=False)
    def put(self, request: Request, status: str) -> Response:
        self._template(status)
        serializer = TemplateUpdateSerializer(data=cast(dict[str, Any], request.data))
        serializer.is_valid(raise_exception=True)
        services.update_template(
            status, serializer.validated_data["texts"], actor=request.user, request=request
        )
        return Response(StatusTemplateSerializer(self._template(status)).data)


# --- segments -----------------------------------------------------------------------------------------------


def _segment_or_404(pk: int) -> CustomerSegment:
    segment = CustomerSegment.objects.filter(pk=pk).first()
    if segment is None:
        raise NotFound
    return segment


class SegmentViewSet(ExportMixin, mixins.ListModelMixin, AdminGenericViewSet):
    serializer_class = SegmentSerializer
    export_name = "guruhlar"
    export_filter_keys = frozenset[str]()  # no filters
    export_resource = "segments"
    export_headers = labels.SEGMENT_EXPORT_HEADERS

    def get_queryset(self) -> QuerySet[CustomerSegment]:
        return CustomerSegment.objects.order_by("id")

    def get_export_rows(self, items: Iterable[CustomerSegment]) -> Iterable[Sequence[Any]]:
        for segment in items:
            stats = selectors.segment_stats(segment)
            yield (segment.pk, segment.name, stats["member_count"], stats["size"])

    @extend_schema(
        operation_id="admin_segments_list",
        parameters=[EXPORT_PARAM],
        responses={200: SegmentSerializer(many=True), **ADMIN_ERRORS},
    )
    def list(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        return super().list(request, *args, **kwargs)

    @extend_schema(
        operation_id="admin_segments_create",
        request=SegmentWriteSerializer,
        responses={201: SegmentSerializer, **ADMIN_ERRORS},
    )
    @idempotent(required=False)
    def create(self, request: Request) -> Response:
        serializer = SegmentWriteSerializer(data=cast(dict[str, Any], request.data))
        serializer.is_valid(raise_exception=True)
        rules = serializer.validated_data.get("rules", {})
        segment = services.create_segment(
            name=serializer.validated_data["name"],
            min_online_orders=rules.get("min_online_orders"),
            min_lifetime_amount=rules.get("min_lifetime_amount"),
            actor=request.user,
            request=request,
        )
        return Response(SegmentSerializer(segment).data, status=201)

    @extend_schema(operation_id="admin_segments_retrieve", responses={200: SegmentSerializer, **ADMIN_ERRORS})
    def retrieve(self, request: Request, pk: int) -> Response:
        return Response(SegmentSerializer(_segment_or_404(pk)).data)

    @extend_schema(
        operation_id="admin_segments_partial_update",
        request=SegmentWriteSerializer,
        responses={200: SegmentSerializer, **ADMIN_ERRORS},
    )
    @idempotent(required=False)
    def partial_update(self, request: Request, pk: int) -> Response:
        segment = _segment_or_404(pk)
        serializer = SegmentWriteSerializer(
            data=cast(dict[str, Any], request.data), instance=segment, partial=True
        )
        serializer.is_valid(raise_exception=True)
        updated = services.update_segment(
            segment, serializer.to_changes(), actor=request.user, request=request
        )
        return Response(SegmentSerializer(updated).data)

    @extend_schema(operation_id="admin_segments_destroy", responses={204: None, **ADMIN_ERRORS})
    @idempotent(required=False)
    def destroy(self, request: Request, pk: int) -> Response:
        services.delete_segment(_segment_or_404(pk), actor=request.user, request=request)
        return Response(status=204)


def _percent_cell(percent: Any) -> str:
    return "" if percent is None else f"{percent:.2f}"


class SegmentMembersViewSet(ExportMixin, mixins.ListModelMixin, AdminGenericViewSet):
    serializer_class = CustomerListItemSerializer
    export_name = "guruh-azolari"
    export_filter_keys = frozenset[str]()  # no filters
    export_resource = "segment_members"
    export_headers = labels.SEGMENT_MEMBER_EXPORT_HEADERS

    def get_queryset(self) -> QuerySet[Customer]:
        segment = _segment_or_404(int(self.kwargs["pk"]))
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
                labels.CUSTOMER_STATUS[customer.status],
            )

    @extend_schema(
        operation_id="admin_segment_members_list",
        parameters=[EXPORT_PARAM],
        responses={200: CustomerListItemSerializer(many=True), **ADMIN_ERRORS},
    )
    def list(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        return super().list(request, *args, **kwargs)

    @extend_schema(
        operation_id="admin_segment_members_add",
        request=SegmentMembersInputSerializer,
        responses={200: SegmentSerializer, **ADMIN_ERRORS},
    )
    @idempotent(required=False)
    def add(self, request: Request, pk: int) -> Response:
        segment = _segment_or_404(pk)
        serializer = SegmentMembersInputSerializer(data=cast(dict[str, Any], request.data))
        serializer.is_valid(raise_exception=True)
        services.add_members(
            segment, serializer.validated_data["customer_ids"], actor=request.user, request=request
        )
        return Response(SegmentSerializer(segment).data)


class SegmentMemberDetailView(AdminAPIView):
    @extend_schema(
        operation_id="admin_segment_members_remove",
        description="Removes a manual membership; a rule-only member answers 409 conflict.",
        responses={204: None, **ADMIN_ERRORS},
    )
    @idempotent(required=False)
    def delete(self, request: Request, pk: int, customer_id: int) -> Response:
        services.remove_member(_segment_or_404(pk), customer_id, actor=request.user, request=request)
        return Response(status=204)
