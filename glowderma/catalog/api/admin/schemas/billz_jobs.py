"""OpenAPI declarations for the Billz import and its jobs (contract §2.3 last block, §2.14).

The views live here, not in ``integrations``: the admin view base (``accounts.api.base``) sits above
``integrations`` in the layer contract. The routes stay ``integrations/billz/import/`` and ``jobs/{id}/``."""

from __future__ import annotations

from drf_spectacular.utils import extend_schema

from glowderma.core.api.schema import ADMIN_ERRORS
from glowderma.integrations.api.admin.serializers import BillzImportSerializer, JobSerializer

BILLZ_IMPORT_POST = extend_schema(
    operation_id="admin_billz_import_create",
    description=(
        "Start a Billz import job (single-flight: a queued or running `billz_import` job is returned "
        "instead of a new one). Poll `jobs/{id}/`."
    ),
    request=BillzImportSerializer,
    responses={202: JobSerializer, **ADMIN_ERRORS},
)

JOB_DETAIL_GET = extend_schema(
    operation_id="admin_jobs_retrieve", responses={200: JobSerializer, **ADMIN_ERRORS}
)


__all__ = [
    "BILLZ_IMPORT_POST",
    "JOB_DETAIL_GET",
]
