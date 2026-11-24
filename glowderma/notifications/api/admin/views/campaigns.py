"""The admin campaigns: list (with its export), retrieve, create, update and delete."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Any

from django.db.models import QuerySet
from django.db.models.functions import Coalesce
from rest_framework import mixins
from rest_framework.exceptions import NotFound
from rest_framework.parsers import MultiPartParser
from rest_framework.request import Request
from rest_framework.response import Response

from glowderma.accounts.api.base import AdminGenericViewSet
from glowderma.core import labels
from glowderma.core.api.parsers import JSONParser
from glowderma.core.exports import ExportMixin, format_export_datetime
from glowderma.core.idempotency import idempotent
from glowderma.core.uzsource import N_
from glowderma.notifications import selectors, services
from glowderma.notifications.api.admin.schemas.campaigns import (
    NOTIFICATION_CREATE,
    NOTIFICATION_DESTROY,
    NOTIFICATION_LIST,
    NOTIFICATION_PARTIAL_UPDATE,
    NOTIFICATION_RETRIEVE,
)
from glowderma.notifications.api.admin.serializers import (
    NotificationCreateSerializer,
    NotificationListQuerySerializer,
    NotificationPatchMetaSerializer,
    NotificationRecordSerializer,
)
from glowderma.notifications.api.admin.views.common import audience_label, link_label, record_body
from glowderma.notifications.api.admin.views.forms import MULTIPART_METHODS, form_to_nested, merged_form
from glowderma.notifications.models import Campaign


class NotificationViewSet(ExportMixin, mixins.ListModelMixin, AdminGenericViewSet):
    serializer_class = NotificationRecordSerializer
    export_name = N_("bildirishnomalar")
    export_filter_keys = frozenset(NotificationListQuerySerializer().fields)
    export_resource = "notifications"
    export_headers = labels.NOTIFICATION_EXPORT_HEADERS

    def get_parsers(self) -> list[Any]:
        # Contract §2.8: ``POST`` / ``PATCH notifications/`` are multipart (anything else is a 415); GET and
        # DELETE have no body. ``self.request`` is unset while drf-spectacular introspects the view.
        request = getattr(self, "request", None)
        if request is not None and request.method in MULTIPART_METHODS:
            return [MultiPartParser()]
        return [JSONParser()]

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
                labels.text(labels.NOTIFICATION_KIND, campaign.kind),
                campaign.title,
                audience_label(campaign),
                labels.text(labels.NOTIFICATION_STATE, campaign.state),
                "" if campaign.sent is None else campaign.sent,
                "" if campaign.delivered is None else campaign.delivered,
                link_label(campaign),
            )

    @NOTIFICATION_LIST
    def list(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        return super().list(request, *args, **kwargs)

    @NOTIFICATION_CREATE
    @idempotent()
    def create(self, request: Request) -> Response:
        serializer = NotificationCreateSerializer(data=form_to_nested(request.data))
        serializer.is_valid(raise_exception=True)
        campaign = services.create_draft(
            serializer.to_input(), request.FILES.get("image"), actor=request.user, request=request
        )
        return Response(record_body(campaign.pk), status=201)

    @NOTIFICATION_RETRIEVE
    def retrieve(self, request: Request, pk: int) -> Response:
        campaign = selectors.get_campaign(pk)
        if campaign is None:
            raise NotFound
        return Response(NotificationRecordSerializer(campaign).data)

    @NOTIFICATION_PARTIAL_UPDATE
    @idempotent(required=False)
    def partial_update(self, request: Request, pk: int) -> Response:
        campaign = selectors.get_campaign(pk)
        if campaign is None:
            raise NotFound
        submitted = form_to_nested(request.data)
        meta = NotificationPatchMetaSerializer(data=submitted)
        meta.is_valid(raise_exception=True)
        serializer = NotificationCreateSerializer(
            data=merged_form(campaign, submitted),
            context={"check_scheduled_at": "scheduled_at" in submitted},  # a kept time may have passed
        )
        serializer.is_valid(raise_exception=True)
        services.update_draft(
            campaign.pk,
            serializer.to_input(),
            request.FILES.get("image"),
            remove_image=bool(meta.validated_data.get("remove_image")),
            expected_revision=int(meta.validated_data["expected_revision"]),
            actor=request.user,
            request=request,
        )
        return Response(record_body(campaign.pk))

    @NOTIFICATION_DESTROY
    @idempotent(required=False)
    def destroy(self, request: Request, pk: int) -> Response:
        services.delete_draft(pk, actor=request.user, request=request)
        return Response(status=204)


__all__ = [
    "NotificationViewSet",
]
