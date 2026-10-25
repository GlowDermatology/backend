"""Admin views of `reporting` for the shell: ``GET dashboard/`` and ``GET nav-counts/`` (§2.10), ``GET
audit-log/`` (§2.13)."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Any, cast

from django.db.models import QuerySet
from drf_spectacular.utils import extend_schema
from rest_framework import mixins
from rest_framework.request import Request
from rest_framework.response import Response

from glowderma.accounts.api.base import AdminAPIView, AdminGenericViewSet
from glowderma.core import labels, timeutils
from glowderma.core.api.schema import ADMIN_ERRORS, EXPORT_PARAM
from glowderma.core.exports import ExportMixin, format_export_datetime
from glowderma.core.models import AuditEntry
from glowderma.reporting import selectors
from glowderma.reporting.api.admin.serializers import (
    AuditEntrySerializer,
    AuditLogQuerySerializer,
    DashboardQuerySerializer,
    DashboardSerializer,
    NavCountsSerializer,
)


class AuditLogViewSet(ExportMixin, mixins.ListModelMixin, AdminGenericViewSet):
    serializer_class = AuditEntrySerializer
    export_name = "harakatlar-jurnali"
    export_filter_keys = frozenset(AuditLogQuerySerializer().fields)
    export_resource = "audit-log"
    export_headers = labels.AUDIT_LOG_EXPORT_HEADERS

    def get_queryset(self) -> QuerySet[AuditEntry]:
        query = AuditLogQuerySerializer(data=self.request.query_params.dict())
        query.is_valid(raise_exception=True)
        return selectors.audit_log(cast(selectors.AuditLogFilters, dict(query.validated_data)))

    def get_export_rows(self, items: Iterable[AuditEntry]) -> Iterable[Sequence[Any]]:
        for entry in items:
            actor = selectors.audit_actor(entry)
            yield (
                entry.pk,
                format_export_datetime(entry.created_at),
                actor["display_name"] if actor is not None else "",
                entry.action,
                entry.entity_type,
                entry.entity_id,
                entry.ip or "",
                entry.request_id,
            )

    @extend_schema(
        operation_id="admin_audit_log_list",
        parameters=[AuditLogQuerySerializer, EXPORT_PARAM],
        responses={200: AuditEntrySerializer(many=True), **ADMIN_ERRORS},
    )
    def list(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        return super().list(request, *args, **kwargs)


class NavCountsView(AdminAPIView):
    @extend_schema(operation_id="admin_nav_counts", responses={200: NavCountsSerializer, **ADMIN_ERRORS})
    def get(self, request: Request) -> Response:
        return Response(NavCountsSerializer(selectors.nav_counts()).data)


class DashboardView(AdminAPIView):
    @extend_schema(
        operation_id="admin_dashboard",
        parameters=[DashboardQuerySerializer],
        responses={200: DashboardSerializer, **ADMIN_ERRORS},
    )
    def get(self, request: Request) -> Response:
        query = DashboardQuerySerializer(data=request.query_params.dict())
        query.is_valid(raise_exception=True)
        params = query.validated_data
        now = timeutils.now()
        window = selectors.dashboard_window(
            params["range"], timeutils.local_date(now), params.get("from"), params.get("to")
        )
        return Response(DashboardSerializer(selectors.dashboard(window, now)).data)
