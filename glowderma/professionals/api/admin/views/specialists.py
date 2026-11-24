"""The admin specialists list (with its export), card and PATCH."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Any

from django.db.models import QuerySet
from rest_framework import mixins
from rest_framework.request import Request
from rest_framework.response import Response

from glowderma.accounts.api.base import AdminGenericViewSet
from glowderma.core import labels
from glowderma.core.exports import ExportMixin
from glowderma.core.idempotency import idempotent
from glowderma.core.uzsource import N_, tr
from glowderma.professionals import selectors, services
from glowderma.professionals.api.admin.payloads import specialist_body, upcoming_counts
from glowderma.professionals.api.admin.schemas.specialists import (
    SPECIALIST_LIST,
    SPECIALIST_PARTIAL_UPDATE,
    SPECIALIST_RETRIEVE,
)
from glowderma.professionals.api.admin.serializers import (
    ClinicSpecialistPatchSerializer,
    ClinicSpecialistSerializer,
    SpecialistListQuerySerializer,
)
from glowderma.professionals.api.admin.views.common import professional_or_404, query_params
from glowderma.professionals.models import (
    Professional,
    Service,
    WorkingHours,
)

# Export column "Ish kunlari" (rendered with ``tr`` in the export's language).
WEEKDAY_SHORT = (N_("Du"), N_("Se"), N_("Ch"), N_("Pa"), N_("Ju"), N_("Sh"), N_("Ya"))


class SpecialistViewSet(ExportMixin, mixins.ListModelMixin, AdminGenericViewSet):
    serializer_class = ClinicSpecialistSerializer
    queryset = Professional.objects.none()  # the schema's model; rows come from get_queryset
    export_name = N_("mutaxassislar")
    export_resource = "specialists"
    export_headers = labels.SPECIALIST_EXPORT_HEADERS
    export_filter_keys = frozenset(SpecialistListQuerySerializer().fields)

    def get_queryset(self) -> QuerySet[Professional]:
        query = SpecialistListQuerySerializer(data=query_params(self.request))
        query.is_valid(raise_exception=True)
        return selectors.filter_specialists(selectors.specialist_queryset(), dict(query.validated_data))

    def paginate_queryset(self, queryset: Any) -> list[Any] | None:
        page = super().paginate_queryset(queryset)
        self.upcoming = upcoming_counts(page or [])  # one query for the page
        return page

    def get_serializer_context(self) -> dict[str, Any]:
        return {**super().get_serializer_context(), "upcoming": getattr(self, "upcoming", {})}

    def get_export_rows(self, items: Iterable[Professional]) -> Iterable[Sequence[Any]]:
        rows = list(items)
        upcoming = upcoming_counts(rows)
        for professional in rows:
            owned: list[Service] = getattr(professional, "service_rows", [])
            week: list[WorkingHours] = getattr(professional, "week_rows", [])
            offered = [service.short_name_uz for service in owned if service.is_active]
            yield (
                professional.pk,
                professional.name,
                labels.text(labels.SPECIALIST_ROLE, professional.role),
                "" if professional.experience_years is None else professional.experience_years,
                ", ".join(offered),
                ", ".join(tr(WEEKDAY_SHORT[row.weekday]) for row in week),
                upcoming[professional.pk],
                labels.active_label(professional.is_active),
            )

    @SPECIALIST_LIST
    def list(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        return super().list(request, *args, **kwargs)

    @SPECIALIST_RETRIEVE
    def retrieve(self, request: Request, pk: int) -> Response:
        professional_or_404(pk)
        return Response(specialist_body(pk))

    @SPECIALIST_PARTIAL_UPDATE
    @idempotent(required=False)
    def partial_update(self, request: Request, pk: int) -> Response:
        professional_or_404(pk)
        serializer = ClinicSpecialistPatchSerializer(
            data=request.data, partial=True, context={"professional_id": pk}
        )
        serializer.is_valid(raise_exception=True)
        data = dict(serializer.validated_data)
        force = bool(data.pop("force", False))
        services.update_specialist(pk, data, force=force, actor=request.user, request=request)
        return Response(specialist_body(pk))


__all__ = [
    "WEEKDAY_SHORT",
    "SpecialistViewSet",
]
