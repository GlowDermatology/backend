"""OpenAPI declarations for admin views of `reporting` for the shell: ``GET dashboard/`` and ``GET
nav-counts/`` (§2.10), ``GET audit-log/`` and ``GET audit-log/facets/`` (§2.13)."""

from __future__ import annotations

from drf_spectacular.utils import extend_schema

from glowderma.core.api.schema import ADMIN_ERRORS, EXPORT_PARAM
from glowderma.reporting.api.admin.serializers import (
    AuditEntrySerializer,
    AuditFacetsSerializer,
    AuditLogQuerySerializer,
    DashboardQuerySerializer,
    DashboardSerializer,
    NavCountsSerializer,
)

AUDIT_LOG_LIST = extend_schema(
    operation_id="admin_audit_log_list",
    parameters=[AuditLogQuerySerializer, EXPORT_PARAM],
    responses={200: AuditEntrySerializer(many=True), **ADMIN_ERRORS},
)

AUDIT_FACETS_GET = extend_schema(
    operation_id="admin_audit_log_facets",
    description=(
        "What the audit log shows the signed-in staff member: ordinary staff see business entries and "
        "their own sign-ins and exports, without IP addresses; managers add staff management and every "
        "staff sign-in, with IPs; superusers see everything. Entries outside the tier are simply absent "
        "from `audit-log/` and its export (a filter on them answers an empty list, never 403)."
    ),
    responses={200: AuditFacetsSerializer, **ADMIN_ERRORS},
)

NAV_COUNTS_GET = extend_schema(
    operation_id="admin_nav_counts", responses={200: NavCountsSerializer, **ADMIN_ERRORS}
)

DASHBOARD_GET = extend_schema(
    operation_id="admin_dashboard",
    parameters=[DashboardQuerySerializer],
    responses={200: DashboardSerializer, **ADMIN_ERRORS},
)


__all__ = [
    "AUDIT_FACETS_GET",
    "AUDIT_LOG_LIST",
    "DASHBOARD_GET",
    "NAV_COUNTS_GET",
]
