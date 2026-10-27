"""Admin views of `professionals` (contract "Mutaxassislar va xizmatlar"; gaps FND-1 and FND-7).

Specialists (list / create / retrieve / PATCH, photo, offered services, weekly hours, date overrides, blocked
periods), services (list / create / retrieve / PATCH; never deleted) and the clinic location. Every write
goes through :mod:`glowderma.professionals.services` (audited) and honours an optional ``Idempotency-Key``.
The customer catalogue and the free slots read the same rows on every request (there is no slot cache), so
a change is bookable — or no longer offered — at once. ``specialists/{id}/conflicts/`` is declared by
``appointments`` (it renders appointments).
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Any

from django.db.models import QuerySet
from drf_spectacular.utils import extend_schema, inline_serializer
from rest_framework import mixins, serializers
from rest_framework.exceptions import NotFound, ValidationError
from rest_framework.parsers import MultiPartParser
from rest_framework.request import Request
from rest_framework.response import Response

from glowderma.accounts.api.base import AdminAPIView, AdminGenericViewSet
from glowderma.core import labels, timeutils
from glowderma.core.api.exceptions import field_error
from glowderma.core.api.schema import ADMIN_ERRORS, EXPORT_PARAM
from glowderma.core.exports import ExportMixin
from glowderma.core.idempotency import idempotent
from glowderma.professionals import bookings, selectors, services
from glowderma.professionals.api.admin.serializers import (
    BlockedPeriodCreateSerializer,
    BlockedPeriodSerializer,
    ClinicLocationPatchSerializer,
    ClinicLocationSerializer,
    ClinicServiceSerializer,
    ClinicServiceWriteSerializer,
    ClinicSpecialistPatchSerializer,
    ClinicSpecialistSerializer,
    ClinicSpecialistWriteSerializer,
    HoursOverrideCreateSerializer,
    HoursOverrideSerializer,
    OfferedServicesSerializer,
    ScheduleRangeQuerySerializer,
    ServiceListQuerySerializer,
    SpecialistListQuerySerializer,
    WorkingWeekSerializer,
)
from glowderma.professionals.models import (
    BlockedPeriod,
    Professional,
    ProfessionalService,
    Service,
    WorkingHours,
    WorkingHoursOverride,
)

WEEKDAY_SHORT = ("Du", "Se", "Ch", "Pa", "Ju", "Sh", "Ya")


def query_params(request: Request) -> dict[str, str]:
    # A plain dict: DRF reads a missing BooleanField of a QueryDict as ``False``.
    return request.query_params.dict()


def _upcoming(professionals: Iterable[Professional]) -> dict[int, int]:
    return bookings.upcoming_counts([professional.pk for professional in professionals], timeutils.now())


def specialist_body(professional_id: int) -> dict[str, Any]:
    """The ``ClinicSpecialist`` of a fresh read (writes return it)."""
    professional = selectors.get_specialist(professional_id)
    if professional is None:  # pragma: no cover - the write just locked the row
        raise NotFound
    context = {"upcoming": _upcoming([professional])}
    return dict(ClinicSpecialistSerializer(professional, context=context).data)


def _professional_or_404(pk: int) -> Professional:
    professional = Professional.objects.filter(pk=pk).first()
    if professional is None:
        raise NotFound
    return professional


# --- specialists --------------------------------------------------------------------------------------------


class SpecialistViewSet(ExportMixin, mixins.ListModelMixin, AdminGenericViewSet):
    serializer_class = ClinicSpecialistSerializer
    queryset = Professional.objects.none()  # the schema's model; rows come from get_queryset
    export_name = "mutaxassislar"
    export_resource = "specialists"
    export_headers = labels.SPECIALIST_EXPORT_HEADERS
    export_filter_keys = frozenset(SpecialistListQuerySerializer().fields)

    def get_queryset(self) -> QuerySet[Professional]:
        query = SpecialistListQuerySerializer(data=query_params(self.request))
        query.is_valid(raise_exception=True)
        return selectors.filter_specialists(selectors.specialist_queryset(), dict(query.validated_data))

    def paginate_queryset(self, queryset: Any) -> list[Any] | None:
        page = super().paginate_queryset(queryset)
        self.upcoming = _upcoming(page or [])  # one query for the page
        return page

    def get_serializer_context(self) -> dict[str, Any]:
        return {**super().get_serializer_context(), "upcoming": getattr(self, "upcoming", {})}

    def get_export_rows(self, items: Iterable[Professional]) -> Iterable[Sequence[Any]]:
        rows = list(items)
        upcoming = _upcoming(rows)
        for professional in rows:
            links: list[ProfessionalService] = getattr(professional, "offered_links", [])
            week: list[WorkingHours] = getattr(professional, "week_rows", [])
            offered = [
                link.service.short_name_uz for link in links if link.is_active and link.service.is_active
            ]
            yield (
                professional.pk,
                professional.name,
                labels.SPECIALIST_ROLE[professional.role],
                "" if professional.experience_years is None else professional.experience_years,
                ", ".join(offered),
                ", ".join(WEEKDAY_SHORT[row.weekday] for row in week),
                upcoming[professional.pk],
                labels.active_label(professional.is_active),
            )

    @extend_schema(
        operation_id="admin_specialists_list",
        parameters=[SpecialistListQuerySerializer, EXPORT_PARAM],
        responses={200: ClinicSpecialistSerializer(many=True), **ADMIN_ERRORS},
    )
    def list(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        return super().list(request, *args, **kwargs)

    @extend_schema(
        operation_id="admin_specialists_create",
        description=(
            "Create a specialist (inactive unless `is_active: true`; the location defaults to the clinic). "
            "Add the offered services and the working hours, then activate."
        ),
        request=ClinicSpecialistWriteSerializer,
        responses={201: ClinicSpecialistSerializer, **ADMIN_ERRORS},
    )
    @idempotent(required=False)
    def create(self, request: Request) -> Response:
        serializer = ClinicSpecialistWriteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        professional = services.create_specialist(
            dict(serializer.validated_data), actor=request.user, request=request
        )
        return Response(specialist_body(professional.pk), status=201)

    @extend_schema(
        operation_id="admin_specialists_retrieve", responses={200: ClinicSpecialistSerializer, **ADMIN_ERRORS}
    )
    def retrieve(self, request: Request, pk: int) -> Response:
        _professional_or_404(pk)
        return Response(specialist_body(pk))

    @extend_schema(
        operation_id="admin_specialists_partial_update",
        description=(
            "Every field optional; texts merge leaf by leaf. `is_active: false` with requested/confirmed "
            "appointments ahead answers 409 `conflict` naming their count unless `force: true` (the "
            "appointments are kept either way)."
        ),
        request=ClinicSpecialistPatchSerializer,
        responses={200: ClinicSpecialistSerializer, **ADMIN_ERRORS},
    )
    @idempotent(required=False)
    def partial_update(self, request: Request, pk: int) -> Response:
        _professional_or_404(pk)
        serializer = ClinicSpecialistPatchSerializer(
            data=request.data, partial=True, context={"professional_id": pk}
        )
        serializer.is_valid(raise_exception=True)
        data = dict(serializer.validated_data)
        force = bool(data.pop("force", False))
        services.update_specialist(pk, data, force=force, actor=request.user, request=request)
        return Response(specialist_body(pk))


class SpecialistPhotoView(AdminAPIView):
    parser_classes = [MultiPartParser]

    @extend_schema(
        operation_id="admin_specialists_photo_update",
        description="One multipart part `file` (JPEG, PNG or WebP, ≤ 5 MiB); replaces the current photo.",
        request={
            "multipart/form-data": inline_serializer(
                name="SpecialistPhotoUpload", fields={"file": serializers.FileField()}
            )
        },
        responses={200: ClinicSpecialistSerializer, **ADMIN_ERRORS},
    )
    @idempotent(required=False)
    def put(self, request: Request, pk: int) -> Response:
        _professional_or_404(pk)
        upload = request.FILES.get("file")
        if upload is None:
            raise ValidationError({"file": [field_error("required")]})
        services.set_specialist_photo(pk, upload, actor=request.user, request=request)
        return Response(specialist_body(pk))

    @extend_schema(operation_id="admin_specialists_photo_destroy", responses={204: None, **ADMIN_ERRORS})
    @idempotent(required=False)
    def delete(self, request: Request, pk: int) -> Response:
        _professional_or_404(pk)
        services.delete_specialist_photo(pk, actor=request.user, request=request)
        return Response(status=204)


class SpecialistServicesView(AdminAPIView):
    @extend_schema(
        operation_id="admin_specialists_services_update",
        description=(
            "The exact set of offered services with the price and duration a booking snapshots; a service "
            "left out stops being offered. Existing appointments keep their snapshots."
        ),
        request=OfferedServicesSerializer,
        responses={200: ClinicSpecialistSerializer, **ADMIN_ERRORS},
    )
    @idempotent(required=False)
    def put(self, request: Request, pk: int) -> Response:
        _professional_or_404(pk)
        serializer = OfferedServicesSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        services.set_offered_services(
            pk, serializer.validated_data["services"], actor=request.user, request=request
        )
        return Response(specialist_body(pk))


class SpecialistWorkingHoursView(AdminAPIView):
    @extend_schema(
        operation_id="admin_specialists_working_hours_update",
        description=(
            "The whole week: a weekday left out is not a working day. Appointments already booked outside "
            "the new hours are kept (see `specialists/{id}/conflicts/`)."
        ),
        request=WorkingWeekSerializer,
        responses={200: ClinicSpecialistSerializer, **ADMIN_ERRORS},
    )
    @idempotent(required=False)
    def put(self, request: Request, pk: int) -> Response:
        _professional_or_404(pk)
        serializer = WorkingWeekSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        services.set_working_week(pk, serializer.validated_data["days"], actor=request.user, request=request)
        return Response(specialist_body(pk))


class SpecialistOverrideViewSet(mixins.ListModelMixin, AdminGenericViewSet):
    serializer_class = HoursOverrideSerializer
    queryset = WorkingHoursOverride.objects.none()  # the schema's model; rows come from get_queryset

    def get_queryset(self) -> QuerySet[WorkingHoursOverride]:
        if getattr(self, "swagger_fake_view", False):
            return WorkingHoursOverride.objects.none()
        professional = _professional_or_404(self.kwargs["pk"])
        query = ScheduleRangeQuerySerializer(data=query_params(self.request))
        query.is_valid(raise_exception=True)
        return selectors.overrides_of(professional, **query.validated_data)

    @extend_schema(
        operation_id="admin_specialists_overrides_list",
        description="Date overrides by date; default from today (Tashkent).",
        parameters=[ScheduleRangeQuerySerializer],
        responses={200: HoursOverrideSerializer(many=True), **ADMIN_ERRORS},
    )
    def list(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        return super().list(request, *args, **kwargs)

    @extend_schema(
        operation_id="admin_specialists_overrides_create",
        description="One date's own hours, or a day off (`start` and `end` null); one per date.",
        request=HoursOverrideCreateSerializer,
        responses={201: HoursOverrideSerializer, **ADMIN_ERRORS},
    )
    @idempotent(required=False)
    def create(self, request: Request, pk: int) -> Response:
        _professional_or_404(pk)
        serializer = HoursOverrideCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        row = services.create_override(
            pk, dict(serializer.validated_data), actor=request.user, request=request
        )
        return Response(HoursOverrideSerializer(row).data, status=201)

    @extend_schema(operation_id="admin_specialists_overrides_destroy", responses={204: None, **ADMIN_ERRORS})
    @idempotent(required=False)
    def destroy(self, request: Request, pk: int, override_id: int) -> Response:
        _professional_or_404(pk)
        services.delete_override(pk, override_id, actor=request.user, request=request)
        return Response(status=204)


class SpecialistBlockViewSet(mixins.ListModelMixin, AdminGenericViewSet):
    serializer_class = BlockedPeriodSerializer
    queryset = BlockedPeriod.objects.none()  # the schema's model; rows come from get_queryset

    def get_queryset(self) -> QuerySet[BlockedPeriod]:
        if getattr(self, "swagger_fake_view", False):
            return BlockedPeriod.objects.none()
        professional = _professional_or_404(self.kwargs["pk"])
        query = ScheduleRangeQuerySerializer(data=query_params(self.request))
        query.is_valid(raise_exception=True)
        return selectors.blocks_of(professional, **query.validated_data)

    @extend_schema(
        operation_id="admin_specialists_blocks_list",
        description="Blocked periods touching the days `date_from` … `date_to`, by start; default today on.",
        parameters=[ScheduleRangeQuerySerializer],
        responses={200: BlockedPeriodSerializer(many=True), **ADMIN_ERRORS},
    )
    def list(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        return super().list(request, *args, **kwargs)

    @extend_schema(
        operation_id="admin_specialists_blocks_create",
        description="An absolute busy/off range; it may not overlap another block of the specialist.",
        request=BlockedPeriodCreateSerializer,
        responses={201: BlockedPeriodSerializer, **ADMIN_ERRORS},
    )
    @idempotent(required=False)
    def create(self, request: Request, pk: int) -> Response:
        _professional_or_404(pk)
        serializer = BlockedPeriodCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        block = services.create_block(
            pk, dict(serializer.validated_data), actor=request.user, request=request
        )
        return Response(BlockedPeriodSerializer(block).data, status=201)

    @extend_schema(operation_id="admin_specialists_blocks_destroy", responses={204: None, **ADMIN_ERRORS})
    @idempotent(required=False)
    def destroy(self, request: Request, pk: int, block_id: int) -> Response:
        _professional_or_404(pk)
        services.delete_block(pk, block_id, actor=request.user, request=request)
        return Response(status=204)


# --- services -----------------------------------------------------------------------------------------------


class ServiceViewSet(ExportMixin, mixins.ListModelMixin, AdminGenericViewSet):
    serializer_class = ClinicServiceSerializer
    queryset = Service.objects.none()  # the schema's model; rows come from get_queryset
    export_name = "xizmatlar"
    export_resource = "services"
    export_headers = labels.SERVICE_EXPORT_HEADERS
    export_filter_keys = frozenset(ServiceListQuerySerializer().fields)

    def get_queryset(self) -> QuerySet[Service]:
        query = ServiceListQuerySerializer(data=query_params(self.request))
        query.is_valid(raise_exception=True)
        return selectors.filter_services(selectors.service_queryset(), dict(query.validated_data))

    def get_export_rows(self, items: Iterable[Service]) -> Iterable[Sequence[Any]]:
        for service in items:
            yield (
                service.pk,
                service.name_uz,
                service.short_name_uz,
                labels.SERVICE_CATEGORY[service.category],
                service.default_duration_min,
                "" if service.default_price is None else service.default_price,
                int(getattr(service, "specialist_count", 0)),
                labels.active_label(service.is_active),
            )

    def _service(self, pk: int) -> Service:
        service = selectors.get_service(pk)
        if service is None:
            raise NotFound
        return service

    @extend_schema(
        operation_id="admin_services_list",
        parameters=[ServiceListQuerySerializer, EXPORT_PARAM],
        responses={200: ClinicServiceSerializer(many=True), **ADMIN_ERRORS},
    )
    def list(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        return super().list(request, *args, **kwargs)

    @extend_schema(
        operation_id="admin_services_create",
        request=ClinicServiceWriteSerializer,
        responses={201: ClinicServiceSerializer, **ADMIN_ERRORS},
    )
    @idempotent(required=False)
    def create(self, request: Request) -> Response:
        serializer = ClinicServiceWriteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        service = services.create_service(
            dict(serializer.validated_data), actor=request.user, request=request
        )
        return Response(ClinicServiceSerializer(self._service(service.pk)).data, status=201)

    @extend_schema(
        operation_id="admin_services_retrieve", responses={200: ClinicServiceSerializer, **ADMIN_ERRORS}
    )
    def retrieve(self, request: Request, pk: int) -> Response:
        return Response(ClinicServiceSerializer(self._service(pk)).data)

    @extend_schema(
        operation_id="admin_services_partial_update",
        description=(
            "Every field optional; texts merge leaf by leaf. There is no delete: `is_active: false` retires "
            "a service (it leaves the customer catalogue at once; its appointments are kept)."
        ),
        request=ClinicServiceWriteSerializer,
        responses={200: ClinicServiceSerializer, **ADMIN_ERRORS},
    )
    @idempotent(required=False)
    def partial_update(self, request: Request, pk: int) -> Response:
        self._service(pk)
        serializer = ClinicServiceWriteSerializer(data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        services.update_service(pk, dict(serializer.validated_data), actor=request.user, request=request)
        return Response(ClinicServiceSerializer(self._service(pk)).data)


# --- the location -------------------------------------------------------------------------------------------


class LocationView(AdminAPIView):
    @extend_schema(
        operation_id="admin_location_retrieve",
        description="The clinic (one location, decision P4); 404 until the first PATCH entered it.",
        responses={200: ClinicLocationSerializer, **ADMIN_ERRORS},
    )
    def get(self, request: Request) -> Response:
        location = selectors.the_location()
        if location is None:
            raise NotFound
        return Response(ClinicLocationSerializer(location).data)

    @extend_schema(
        operation_id="admin_location_partial_update",
        description="Texts merge leaf by leaf; the first PATCH creates the clinic and needs `name.uz`.",
        request=ClinicLocationPatchSerializer,
        responses={200: ClinicLocationSerializer, **ADMIN_ERRORS},
    )
    @idempotent(required=False)
    def patch(self, request: Request) -> Response:
        serializer = ClinicLocationPatchSerializer(data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        location = services.update_location(
            dict(serializer.validated_data), actor=request.user, request=request
        )
        return Response(ClinicLocationSerializer(location).data)
