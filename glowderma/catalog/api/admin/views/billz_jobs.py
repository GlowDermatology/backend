"""The Billz import and its jobs (contract §2.3 last block, §2.14).

The views live here, not in ``integrations``: the admin view base (``accounts.api.base``) sits above
``integrations`` in the layer contract. The routes stay ``integrations/billz/import/`` and ``jobs/{id}/``."""

from __future__ import annotations

from rest_framework.exceptions import NotFound
from rest_framework.request import Request
from rest_framework.response import Response

from glowderma.accounts.api.base import AdminAPIView
from glowderma.catalog import services
from glowderma.catalog.api.admin.schemas.billz_jobs import BILLZ_IMPORT_POST, JOB_DETAIL_GET
from glowderma.core import enums
from glowderma.core.idempotency import idempotent
from glowderma.integrations import selectors as integration_selectors
from glowderma.integrations import services as job_services
from glowderma.integrations.api.admin.serializers import BillzImportSerializer, JobSerializer


class BillzImportView(AdminAPIView):
    @BILLZ_IMPORT_POST
    @idempotent(required=False)
    def post(self, request: Request) -> Response:
        services.require_live_billz()  # no job while Billz is fake under the production settings
        serializer = BillzImportSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        job, _created = job_services.start_job(
            enums.JobKind.BILLZ_IMPORT,
            {"mode": serializer.validated_data["mode"]},
            actor=request.user,
            request=request,
        )
        return Response(JobSerializer(job).data, status=202)


class JobDetailView(AdminAPIView):
    @JOB_DETAIL_GET
    def get(self, request: Request, pk: int) -> Response:
        job = integration_selectors.get_job(pk)
        if job is None:
            raise NotFound
        return Response(JobSerializer(job).data)


__all__ = [
    "BillzImportView",
    "JobDetailView",
]
