from typing import Any

from drf_spectacular.utils import extend_schema, extend_schema_field, inline_serializer
from rest_framework import serializers
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.parsers import MultiPartParser
from rest_framework.request import Request
from rest_framework.response import Response

from glowderma.accounts.api.base import SpecialistAPIView
from glowderma.core.api.exceptions import field_error
from glowderma.core.api.schema import ADMIN_ERRORS, IDEMPOTENCY_KEY_HEADER
from glowderma.core.idempotency import idempotent
from glowderma.core.uzsource import N_
from glowderma.professionals import selectors, services
from glowderma.professionals.api.admin.serializers import (
    BlockedPeriodCreateSerializer,
    BlockedPeriodSerializer,
    ClinicLocationSerializer,
    ClinicServiceWriteSerializer,
    ClinicSpecialistPatchSerializer,
    ClinicSpecialistSerializer,
    HoursOverrideCreateSerializer,
    HoursOverrideSerializer,
    ScheduleRangeQuerySerializer,
    WorkingDaySerializer,
    WorkingWeekSerializer,
)
from glowderma.professionals.api.admin.views import specialist_body
from glowderma.professionals.models import Professional


def require_active_profile(user_id: int, *, lock: bool = False) -> Professional:
    """The caller's own profile (draft or published). Every ``specialist/`` route resolves the profile from
    the signed-in account only, so a specialist can never address another specialist's rows."""
    professional = selectors.active_specialist_account(user_id, lock=lock)
    if professional is None:
        raise PermissionDenied(N_("Mutaxassis jadvali hozir mavjud emas."))
    return professional


class SpecialistProfileAPIView(SpecialistAPIView):
    account_id: int
    professional: Professional

    def initial(self, request: Request, *args: Any, **kwargs: Any) -> None:
        super().initial(request, *args, **kwargs)
        account_id = request.user.pk
        if not isinstance(account_id, int):
            raise PermissionDenied
        self.account_id = account_id
        self.professional = require_active_profile(account_id)


class SpecialistOwnProfileSerializer(ClinicSpecialistSerializer):
    """``SpecialistOwnProfile``: the ``ClinicSpecialist`` of the signed-in specialist plus the clinics staff
    assigned (read-only for the specialist)."""

    clinics = serializers.SerializerMethodField(
        help_text="Assigned clinics, primary first, inactive ones included (`is_active`)."
    )

    @extend_schema_field(ClinicLocationSerializer(many=True))
    def get_clinics(self, obj: Professional) -> list[dict[str, Any]]:
        return [ClinicLocationSerializer(location).data for location in selectors.assigned_locations(obj)]


def own_profile_body(professional_id: int) -> dict[str, Any]:
    """The ``SpecialistOwnProfile`` of a fresh read (the profile, service and weekly-hours writes return
    it)."""
    return specialist_body(professional_id, serializer=SpecialistOwnProfileSerializer)


class SpecialistAvailabilitySerializer(serializers.Serializer[dict[str, Any]]):
    clinics = ClinicLocationSerializer(
        many=True, help_text="Assigned clinics, primary first, inactive ones included (`is_active`)."
    )
    working_hours = WorkingDaySerializer(many=True)
    overrides = HoursOverrideSerializer(many=True)
    blocks = BlockedPeriodSerializer(many=True)


class AvailabilityView(SpecialistProfileAPIView):
    @extend_schema(
        operation_id="specialist_availability",
        parameters=[ScheduleRangeQuerySerializer],
        responses={200: SpecialistAvailabilitySerializer, **ADMIN_ERRORS},
    )
    def get(self, request: Request) -> Response:
        query = ScheduleRangeQuerySerializer(data=request.query_params.dict())
        query.is_valid(raise_exception=True)
        professional = self.professional
        return Response(
            SpecialistAvailabilitySerializer(
                {
                    "clinics": selectors.assigned_locations(professional),
                    "working_hours": professional.working_hours.order_by("weekday"),
                    "overrides": selectors.overrides_of(professional, **query.validated_data),
                    "blocks": selectors.blocks_of(professional, **query.validated_data),
                }
            ).data
        )


class WeeklyHoursView(SpecialistProfileAPIView):
    @extend_schema(
        operation_id="specialist_weekly_hours_update",
        description=(
            "The whole week at the assigned clinics: a weekday left out is not a working day. Appointments "
            "already booked outside the new hours are kept (see `specialist/conflicts/`)."
        ),
        request=WorkingWeekSerializer,
        parameters=[IDEMPOTENCY_KEY_HEADER],
        responses={200: SpecialistOwnProfileSerializer, **ADMIN_ERRORS},
    )
    @idempotent(required=False)
    def put(self, request: Request) -> Response:
        body = WorkingWeekSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        professional = require_active_profile(self.account_id, lock=True)
        services.set_working_week(
            professional.pk, body.validated_data["days"], actor=request.user, request=request
        )
        return Response(own_profile_body(professional.pk))


class OverridesView(SpecialistProfileAPIView):
    @extend_schema(
        operation_id="specialist_override_create",
        request=HoursOverrideCreateSerializer,
        parameters=[IDEMPOTENCY_KEY_HEADER],
        responses={201: HoursOverrideSerializer, **ADMIN_ERRORS},
    )
    @idempotent(required=False)
    def post(self, request: Request) -> Response:
        return self.save(request)

    @extend_schema(
        operation_id="specialist_override_update",
        request=HoursOverrideCreateSerializer,
        parameters=[IDEMPOTENCY_KEY_HEADER],
        responses={200: HoursOverrideSerializer, **ADMIN_ERRORS},
    )
    @idempotent(required=False)
    def put(self, request: Request, pk: int) -> Response:
        return self.save(request, pk)

    def save(self, request: Request, pk: int | None = None) -> Response:
        body = HoursOverrideCreateSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        professional = require_active_profile(self.account_id, lock=True)
        row = services.create_override(
            professional.pk, dict(body.validated_data), override_id=pk, actor=request.user, request=request
        )
        return Response(HoursOverrideSerializer(row).data, status=201 if pk is None else 200)

    @extend_schema(
        operation_id="specialist_override_delete",
        parameters=[IDEMPOTENCY_KEY_HEADER],
        responses={204: None, **ADMIN_ERRORS},
    )
    @idempotent(required=False)
    def delete(self, request: Request, pk: int) -> Response:
        professional = require_active_profile(self.account_id, lock=True)
        services.delete_override(professional.pk, pk, actor=request.user, request=request)
        return Response(status=204)


class BlocksView(SpecialistProfileAPIView):
    @extend_schema(
        operation_id="specialist_block_create",
        request=BlockedPeriodCreateSerializer,
        parameters=[IDEMPOTENCY_KEY_HEADER],
        responses={201: BlockedPeriodSerializer, **ADMIN_ERRORS},
    )
    @idempotent(required=False)
    def post(self, request: Request) -> Response:
        return self.save(request)

    @extend_schema(
        operation_id="specialist_block_update",
        request=BlockedPeriodCreateSerializer,
        parameters=[IDEMPOTENCY_KEY_HEADER],
        responses={200: BlockedPeriodSerializer, **ADMIN_ERRORS},
    )
    @idempotent(required=False)
    def put(self, request: Request, pk: int) -> Response:
        return self.save(request, pk)

    def save(self, request: Request, pk: int | None = None) -> Response:
        body = BlockedPeriodCreateSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        professional = require_active_profile(self.account_id, lock=True)
        row = services.create_block(
            professional.pk, dict(body.validated_data), block_id=pk, actor=request.user, request=request
        )
        return Response(BlockedPeriodSerializer(row).data, status=201 if pk is None else 200)

    @extend_schema(
        operation_id="specialist_block_delete",
        parameters=[IDEMPOTENCY_KEY_HEADER],
        responses={204: None, **ADMIN_ERRORS},
    )
    @idempotent(required=False)
    def delete(self, request: Request, pk: int) -> Response:
        professional = require_active_profile(self.account_id, lock=True)
        services.delete_block(professional.pk, pk, actor=request.user, request=request)
        return Response(status=204)


OWN_PROFILE_IGNORED = ("location_id", "location_ids", "position")


class OwnProfileSerializer(ClinicSpecialistPatchSerializer):
    """``PATCH specialist/profile/`` (validated ``partial``): the staff profile fields without the clinics
    and the site order, which stay with staff (sent anyway, they are ignored). Texts merge leaf by leaf;
    ``is_active`` publishes or unpublishes under the staff rules (``force`` confirms an unpublish despite
    upcoming visits)."""

    def get_fields(self) -> dict[str, Any]:
        fields = super().get_fields()
        for name in OWN_PROFILE_IGNORED:
            del fields[name]
        return fields


class OwnProfileView(SpecialistProfileAPIView):
    @extend_schema(
        operation_id="specialist_profile", responses={200: SpecialistOwnProfileSerializer, **ADMIN_ERRORS}
    )
    def get(self, request: Request) -> Response:
        return Response(own_profile_body(self.professional.pk))

    @extend_schema(
        operation_id="specialist_profile_update",
        description=(
            "Every field optional; texts merge leaf by leaf; `location_id`, `location_ids` and `position` "
            "are ignored. `is_active: true` with `publish_blockers` left answers 409 `conflict` naming them; "
            "`is_active: false` with requested/confirmed appointments ahead answers 409 unless `force: true` "
            "(the appointments are kept either way)."
        ),
        request=OwnProfileSerializer,
        parameters=[IDEMPOTENCY_KEY_HEADER],
        responses={200: SpecialistOwnProfileSerializer, **ADMIN_ERRORS},
    )
    @idempotent(required=False)
    def patch(self, request: Request) -> Response:
        body = OwnProfileSerializer(
            data=request.data, partial=True, context={"professional_id": self.professional.pk}
        )
        body.is_valid(raise_exception=True)
        data = dict(body.validated_data)
        force = bool(data.pop("force", False))
        services.update_own_profile(
            self.professional.pk, data, force=force, actor=request.user, request=request
        )
        return Response(own_profile_body(self.professional.pk))


class OwnServicesView(SpecialistProfileAPIView):
    @extend_schema(
        operation_id="specialist_services_create",
        description=(
            "A new own service with the price and duration a booking snapshots; appended after the others. "
            "A name already used answers 400 `unique`; at most 50 services."
        ),
        request=ClinicServiceWriteSerializer,
        parameters=[IDEMPOTENCY_KEY_HEADER],
        responses={201: SpecialistOwnProfileSerializer, **ADMIN_ERRORS},
    )
    @idempotent(required=False)
    def post(self, request: Request) -> Response:
        body = ClinicServiceWriteSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        services.create_specialist_service(
            self.professional.pk, dict(body.validated_data), actor=request.user, request=request
        )
        return Response(own_profile_body(self.professional.pk), status=201)


class OwnServiceView(SpecialistProfileAPIView):
    @extend_schema(
        operation_id="specialist_services_partial_update",
        description=(
            "Every field optional; texts merge leaf by leaf. `is_active: false` stops offering the service "
            "at once; booked appointments keep their price and duration. Another specialist's service is 404."
        ),
        request=ClinicServiceWriteSerializer,
        parameters=[IDEMPOTENCY_KEY_HEADER],
        responses={200: SpecialistOwnProfileSerializer, **ADMIN_ERRORS},
    )
    @idempotent(required=False)
    def patch(self, request: Request, pk: int) -> Response:
        body = ClinicServiceWriteSerializer(data=request.data, partial=True)
        body.is_valid(raise_exception=True)
        services.update_specialist_service(
            self.professional.pk, pk, dict(body.validated_data), actor=request.user, request=request
        )
        return Response(own_profile_body(self.professional.pk))

    @extend_schema(
        operation_id="specialist_services_destroy",
        description=(
            "Delete an own service that was never booked. One with appointments (any status) answers 409 "
            "`conflict`: deactivate it instead (`is_active: false`)."
        ),
        parameters=[IDEMPOTENCY_KEY_HEADER],
        responses={204: None, **ADMIN_ERRORS},
    )
    @idempotent(required=False)
    def delete(self, request: Request, pk: int) -> Response:
        services.delete_specialist_service(self.professional.pk, pk, actor=request.user, request=request)
        return Response(status=204)


class OwnPhotoView(SpecialistProfileAPIView):
    parser_classes = [MultiPartParser]

    @extend_schema(
        operation_id="specialist_profile_photo_update",
        description="One multipart part `file` (JPEG, PNG or WebP, ≤ 5 MiB); replaces the current photo.",
        request={
            "multipart/form-data": inline_serializer(
                name="OwnPhotoUpload", fields={"file": serializers.FileField()}
            )
        },
        responses={200: SpecialistOwnProfileSerializer, **ADMIN_ERRORS},
    )
    @idempotent(required=False)
    def put(self, request: Request) -> Response:
        upload = request.FILES.get("file")
        if upload is None:
            raise ValidationError({"file": [field_error("required")]})
        services.set_specialist_photo(self.professional.pk, upload, actor=request.user, request=request)
        return Response(own_profile_body(self.professional.pk))

    @extend_schema(operation_id="specialist_profile_photo_destroy", responses={204: None, **ADMIN_ERRORS})
    @idempotent(required=False)
    def delete(self, request: Request) -> Response:
        services.delete_specialist_photo(self.professional.pk, actor=request.user, request=request)
        return Response(status=204)
