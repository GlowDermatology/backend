"""Admin views of `professionals` (contract "Mutaxassislar va xizmatlar"; gaps FND-1 and FND-7).

Specialists (list / retrieve / PATCH, photo, their own services, weekly hours, date overrides, blocked
periods) and the clinic locations. Every service belongs to one specialist (there is no global catalogue);
a service is deleted only if it was never booked, else deactivated. Every write
goes through :mod:`glowderma.professionals.services` (audited) and honours an optional ``Idempotency-Key``.
The customer catalogue and the free slots read the same rows on every request (there is no slot cache), so
a change is bookable — or no longer offered — at once. ``specialists/{id}/conflicts/`` is declared by
``appointments`` (it renders appointments).
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Any, cast

from django.db.models import QuerySet
from drf_spectacular.utils import extend_schema, inline_serializer
from rest_framework import mixins, serializers
from rest_framework.exceptions import NotFound, ValidationError
from rest_framework.parsers import MultiPartParser
from rest_framework.request import Request
from rest_framework.response import Response

from glowderma.accounts import selectors as account_selectors
from glowderma.accounts.api.admin.serializers import (
    StaffCredentialsSerializer,
    StaffMemberSerializer,
    WorkforceInviteSerializer,
)
from glowderma.accounts.api.admin.views import staff_credentials
from glowderma.accounts.api.base import AdminAPIView, AdminGenericViewSet
from glowderma.accounts.models import StaffUser
from glowderma.accounts.permissions import IsManager
from glowderma.core import labels, timeutils
from glowderma.core.api.exceptions import field_error
from glowderma.core.api.schema import ADMIN_ERRORS, EXPORT_PARAM, IDEMPOTENCY_KEY_HEADER
from glowderma.core.exports import ExportMixin
from glowderma.core.idempotency import idempotent
from glowderma.core.uzsource import N_, tr
from glowderma.professionals import bookings, selectors, services
from glowderma.professionals.api.admin.serializers import (
    BlockedPeriodCreateSerializer,
    BlockedPeriodSerializer,
    ClinicLocationCreateSerializer,
    ClinicLocationPatchSerializer,
    ClinicLocationSerializer,
    ClinicLocationsSerializer,
    ClinicServiceWriteSerializer,
    ClinicSpecialistPatchSerializer,
    ClinicSpecialistSerializer,
    HoursOverrideCreateSerializer,
    HoursOverrideSerializer,
    ScheduleRangeQuerySerializer,
    SpecialistListQuerySerializer,
    WorkingWeekSerializer,
)
from glowderma.professionals.models import (
    BlockedPeriod,
    Location,
    Professional,
    Service,
    WorkingHours,
    WorkingHoursOverride,
)

# Export column "Ish kunlari" (rendered with ``tr`` in the export's language).
WEEKDAY_SHORT = (N_("Du"), N_("Se"), N_("Ch"), N_("Pa"), N_("Ju"), N_("Sh"), N_("Ya"))


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
        self.upcoming = _upcoming(page or [])  # one query for the page
        return page

    def get_serializer_context(self) -> dict[str, Any]:
        return {**super().get_serializer_context(), "upcoming": getattr(self, "upcoming", {})}

    def get_export_rows(self, items: Iterable[Professional]) -> Iterable[Sequence[Any]]:
        rows = list(items)
        upcoming = _upcoming(rows)
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

    @extend_schema(
        operation_id="admin_specialists_list",
        parameters=[SpecialistListQuerySerializer, EXPORT_PARAM],
        responses={200: ClinicSpecialistSerializer(many=True), **ADMIN_ERRORS},
    )
    def list(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        return super().list(request, *args, **kwargs)

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
        operation_id="admin_specialists_services_create",
        description=(
            "A new service of this specialist with the price and duration a booking snapshots; appended "
            "after its others. A name the specialist already uses answers 400 `unique`; at most 50 services."
        ),
        request=ClinicServiceWriteSerializer,
        responses={201: ClinicSpecialistSerializer, **ADMIN_ERRORS},
    )
    @idempotent(required=False)
    def post(self, request: Request, pk: int) -> Response:
        _professional_or_404(pk)
        serializer = ClinicServiceWriteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        services.create_specialist_service(
            pk, dict(serializer.validated_data), actor=request.user, request=request
        )
        return Response(specialist_body(pk), status=201)


class SpecialistServiceView(AdminAPIView):
    @extend_schema(
        operation_id="admin_specialists_services_partial_update",
        description=(
            "Every field optional; texts merge leaf by leaf. `is_active: false` stops offering the service "
            "at once; booked appointments keep their price and duration. Another specialist's service is 404."
        ),
        request=ClinicServiceWriteSerializer,
        responses={200: ClinicSpecialistSerializer, **ADMIN_ERRORS},
    )
    @idempotent(required=False)
    def patch(self, request: Request, pk: int, service_id: int) -> Response:
        _professional_or_404(pk)
        serializer = ClinicServiceWriteSerializer(data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        services.update_specialist_service(
            pk, service_id, dict(serializer.validated_data), actor=request.user, request=request
        )
        return Response(specialist_body(pk))

    @extend_schema(
        operation_id="admin_specialists_services_destroy",
        description=(
            "Delete a service that was never booked. One with appointments (any status) answers 409 "
            "`conflict`: deactivate it instead (`is_active: false`)."
        ),
        responses={204: None, **ADMIN_ERRORS},
    )
    @idempotent(required=False)
    def delete(self, request: Request, pk: int, service_id: int) -> Response:
        _professional_or_404(pk)
        services.delete_specialist_service(pk, service_id, actor=request.user, request=request)
        return Response(status=204)


class SpecialistInviteCredentialsSerializer(StaffCredentialsSerializer):
    specialist_id = serializers.IntegerField(help_text="The draft profile created with the account.")


class SpecialistInviteView(AdminAPIView):
    permission_classes = [IsManager]

    @extend_schema(
        operation_id="admin_specialist_invite",
        description=(
            "Staff → Add employee → Specialist: creates the Specialist account and its private draft profile "
            "together (the profile is named after the account; the public name can be changed later). An "
            "email already in use → 400 `unique` on `email`, nothing is created. Resend the invitation with "
            "`staff/{id}/send-password-link/` or `staff/{id}/reset-password/`: same account, same profile."
        ),
        parameters=[IDEMPOTENCY_KEY_HEADER],
        request=WorkforceInviteSerializer,
        responses={201: SpecialistInviteCredentialsSerializer, **ADMIN_ERRORS},
    )
    @idempotent(sealed=("temporary_password",))
    def post(self, request: Request) -> Response:
        body = WorkforceInviteSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        user, password, professional = services.invite_specialist(
            dict(body.validated_data), actor=cast(StaffUser, request.user), request=request
        )
        credentials = staff_credentials(user, password, body.validated_data["delivery"])
        return Response({**credentials, "specialist_id": professional.pk}, status=201)


class SpecialistRoleChangeSerializer(serializers.Serializer[dict[str, Any]]):
    role = serializers.ChoiceField(choices=("staff", "specialist"))
    confirmed = serializers.BooleanField(help_text="Must be `true`: the role change is a confirmed action.")

    def validate_confirmed(self, value: bool) -> bool:
        if not value:
            raise serializers.ValidationError(field_error("required"))
        return value


class SpecialistRoleChangeResultSerializer(serializers.Serializer[dict[str, Any]]):
    account = StaffMemberSerializer()
    specialist_id = serializers.IntegerField(
        allow_null=True, help_text="The profile the account owns; `null` for an account that never had one."
    )
    upcoming_appointments = serializers.IntegerField(
        help_text="→ staff: requested/confirmed visits ahead on the paused profile, for staff to handle."
    )


class SpecialistAccountRoleView(AdminAPIView):
    permission_classes = [IsManager]

    @extend_schema(
        operation_id="admin_staff_specialist_role",
        description=(
            "Staff → Edit: `specialist` converts an eligible Staff or Manager account (its draft profile is "
            "created, or the profile it already owns is reused unpublished); `staff` ends specialist access "
            "and unpublishes the profile, keeping it and its appointments on the account. Sessions are "
            "revoked on a change; the role the account already has changes nothing. The caller's own "
            "account, an inactive account, a superuser, operations-console access or the last active "
            "manager → 409 `conflict`."
        ),
        parameters=[IDEMPOTENCY_KEY_HEADER],
        request=SpecialistRoleChangeSerializer,
        responses={200: SpecialistRoleChangeResultSerializer, **ADMIN_ERRORS},
    )
    @idempotent()
    def post(self, request: Request, pk: int) -> Response:
        body = SpecialistRoleChangeSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        _, profile, upcoming = services.set_specialist_account_role(
            pk, body.validated_data["role"], actor=cast(StaffUser, request.user), request=request
        )
        member = account_selectors.staff_member(pk)
        if member is None:  # pragma: no cover - the service just locked the row
            raise NotFound
        return Response(
            {
                "account": StaffMemberSerializer(member).data,
                "specialist_id": None if profile is None else profile.pk,
                "upcoming_appointments": upcoming,
            }
        )


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


# --- the location -------------------------------------------------------------------------------------------


class ClinicsView(AdminAPIView):
    @extend_schema(
        operation_id="admin_clinics_list",
        responses={200: ClinicLocationsSerializer, **ADMIN_ERRORS},
    )
    def get(self, request: Request) -> Response:
        return Response(ClinicLocationsSerializer({"results": Location.objects.all()}).data)

    @extend_schema(
        operation_id="admin_clinics_create",
        request=ClinicLocationCreateSerializer,
        responses={201: ClinicLocationSerializer, **ADMIN_ERRORS},
    )
    @idempotent(required=False)
    def post(self, request: Request) -> Response:
        serializer = ClinicLocationCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        location = services.create_location(
            dict(serializer.validated_data), actor=request.user, request=request
        )
        return Response(ClinicLocationSerializer(location).data, status=201)


class ClinicView(AdminAPIView):
    @extend_schema(
        operation_id="admin_clinics_retrieve",
        responses={200: ClinicLocationSerializer, **ADMIN_ERRORS},
    )
    def get(self, request: Request, pk: int) -> Response:
        location = Location.objects.filter(pk=pk).first()
        if location is None:
            raise NotFound
        return Response(ClinicLocationSerializer(location).data)

    @extend_schema(
        operation_id="admin_clinics_partial_update",
        description="Deactivate after reassigning active specialists and resolving upcoming visits.",
        request=ClinicLocationPatchSerializer,
        responses={200: ClinicLocationSerializer, **ADMIN_ERRORS},
    )
    @idempotent(required=False)
    def patch(self, request: Request, pk: int) -> Response:
        serializer = ClinicLocationPatchSerializer(data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        location = services.update_location(
            pk, dict(serializer.validated_data), actor=request.user, request=request
        )
        return Response(ClinicLocationSerializer(location).data)
