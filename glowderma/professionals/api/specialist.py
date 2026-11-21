from typing import Any

from drf_spectacular.utils import extend_schema, inline_serializer
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
    ClinicSpecialistSerializer,
    HoursOverrideCreateSerializer,
    HoursOverrideSerializer,
    ScheduleRangeQuerySerializer,
    SpecialistTextSerializer,
    WorkingDaySerializer,
    WorkingWeekSerializer,
)
from glowderma.professionals.api.admin.views import specialist_body
from glowderma.professionals.models import Location, Professional


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


class ScheduleClinicSerializer(serializers.Serializer[Location]):
    id = serializers.IntegerField()
    name = serializers.CharField(source="name_uz")
    address = serializers.CharField(source="address_uz", allow_blank=True)


class SpecialistAvailabilitySerializer(serializers.Serializer[dict[str, Any]]):
    clinics = ScheduleClinicSerializer(many=True)
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
                    "clinics": selectors.assigned_locations(professional, active_only=True),
                    "working_hours": professional.working_hours.order_by("weekday"),
                    "overrides": selectors.overrides_of(professional, **query.validated_data),
                    "blocks": selectors.blocks_of(professional, **query.validated_data),
                }
            ).data
        )


class WeeklyHoursView(SpecialistProfileAPIView):
    @extend_schema(
        operation_id="specialist_weekly_hours_update",
        request=WorkingWeekSerializer,
        parameters=[IDEMPOTENCY_KEY_HEADER],
        responses={204: None, **ADMIN_ERRORS},
    )
    @idempotent()
    def put(self, request: Request) -> Response:
        body = WorkingWeekSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        professional = require_active_profile(self.account_id, lock=True)
        services.set_working_week(
            professional.pk, body.validated_data["days"], actor=request.user, request=request
        )
        return Response(status=204)


class OverridesView(SpecialistProfileAPIView):
    @extend_schema(
        operation_id="specialist_override_create",
        request=HoursOverrideCreateSerializer,
        parameters=[IDEMPOTENCY_KEY_HEADER],
        responses={204: None, **ADMIN_ERRORS},
    )
    @idempotent()
    def post(self, request: Request) -> Response:
        return self.save(request)

    @extend_schema(
        operation_id="specialist_override_update",
        request=HoursOverrideCreateSerializer,
        parameters=[IDEMPOTENCY_KEY_HEADER],
        responses={204: None, **ADMIN_ERRORS},
    )
    @idempotent()
    def put(self, request: Request, pk: int) -> Response:
        return self.save(request, pk)

    def save(self, request: Request, pk: int | None = None) -> Response:
        body = HoursOverrideCreateSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        professional = require_active_profile(self.account_id, lock=True)
        services.create_override(
            professional.pk, dict(body.validated_data), override_id=pk, actor=request.user, request=request
        )
        return Response(status=204)

    @extend_schema(
        operation_id="specialist_override_delete",
        parameters=[IDEMPOTENCY_KEY_HEADER],
        responses={204: None, **ADMIN_ERRORS},
    )
    @idempotent()
    def delete(self, request: Request, pk: int) -> Response:
        professional = require_active_profile(self.account_id, lock=True)
        services.delete_override(professional.pk, pk, actor=request.user, request=request)
        return Response(status=204)


class BlocksView(SpecialistProfileAPIView):
    @extend_schema(
        operation_id="specialist_block_create",
        request=BlockedPeriodCreateSerializer,
        parameters=[IDEMPOTENCY_KEY_HEADER],
        responses={204: None, **ADMIN_ERRORS},
    )
    @idempotent()
    def post(self, request: Request) -> Response:
        return self.save(request)

    @extend_schema(
        operation_id="specialist_block_update",
        request=BlockedPeriodCreateSerializer,
        parameters=[IDEMPOTENCY_KEY_HEADER],
        responses={204: None, **ADMIN_ERRORS},
    )
    @idempotent()
    def put(self, request: Request, pk: int) -> Response:
        return self.save(request, pk)

    def save(self, request: Request, pk: int | None = None) -> Response:
        body = BlockedPeriodCreateSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        professional = require_active_profile(self.account_id, lock=True)
        services.create_block(
            professional.pk, dict(body.validated_data), block_id=pk, actor=request.user, request=request
        )
        return Response(status=204)

    @extend_schema(
        operation_id="specialist_block_delete",
        parameters=[IDEMPOTENCY_KEY_HEADER],
        responses={204: None, **ADMIN_ERRORS},
    )
    @idempotent()
    def delete(self, request: Request, pk: int) -> Response:
        professional = require_active_profile(self.account_id, lock=True)
        services.delete_block(professional.pk, pk, actor=request.user, request=request)
        return Response(status=204)


class OwnProfileSerializer(serializers.Serializer[dict[str, Any]]):
    """``PATCH specialist/profile/``: the public name and the bio (languages merge one by one). Clinics,
    services, discipline and publication stay with staff."""

    name = serializers.CharField(min_length=2, max_length=150, required=False)
    bio = SpecialistTextSerializer(required=False, partial=True)


class OwnProfileView(SpecialistProfileAPIView):
    @extend_schema(
        operation_id="specialist_profile", responses={200: ClinicSpecialistSerializer, **ADMIN_ERRORS}
    )
    def get(self, request: Request) -> Response:
        return Response(specialist_body(self.professional.pk))

    @extend_schema(
        operation_id="specialist_profile_update",
        request=OwnProfileSerializer,
        parameters=[IDEMPOTENCY_KEY_HEADER],
        responses={200: ClinicSpecialistSerializer, **ADMIN_ERRORS},
    )
    @idempotent(required=False)
    def patch(self, request: Request) -> Response:
        body = OwnProfileSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        data = dict(body.validated_data)
        if "bio" in data:
            sent = request.data.get("bio") if isinstance(request.data, dict) else None
            data["bio"] = {key: value for key, value in data["bio"].items() if key in (sent or {})}
        services.update_own_profile(self.professional.pk, data, actor=request.user, request=request)
        return Response(specialist_body(self.professional.pk))


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
        responses={200: ClinicSpecialistSerializer, **ADMIN_ERRORS},
    )
    @idempotent(required=False)
    def put(self, request: Request) -> Response:
        upload = request.FILES.get("file")
        if upload is None:
            raise ValidationError({"file": [field_error("required")]})
        services.set_specialist_photo(self.professional.pk, upload, actor=request.user, request=request)
        return Response(specialist_body(self.professional.pk))

    @extend_schema(operation_id="specialist_profile_photo_destroy", responses={204: None, **ADMIN_ERRORS})
    @idempotent(required=False)
    def delete(self, request: Request) -> Response:
        services.delete_specialist_photo(self.professional.pk, actor=request.user, request=request)
        return Response(status=204)
