from typing import Any

from drf_spectacular.utils import extend_schema
from rest_framework import serializers
from rest_framework.exceptions import PermissionDenied
from rest_framework.request import Request
from rest_framework.response import Response

from glowderma.accounts.api.base import SpecialistAPIView
from glowderma.core.api.schema import ADMIN_ERRORS, IDEMPOTENCY_KEY_HEADER
from glowderma.core.idempotency import idempotent
from glowderma.core.uzsource import N_
from glowderma.professionals import selectors, services
from glowderma.professionals.api.admin.serializers import (
    BlockedPeriodCreateSerializer,
    BlockedPeriodSerializer,
    HoursOverrideCreateSerializer,
    HoursOverrideSerializer,
    ScheduleRangeQuerySerializer,
    WorkingDaySerializer,
    WorkingWeekSerializer,
)
from glowderma.professionals.models import Location, Professional


def require_active_profile(user_id: int, *, lock: bool = False) -> Professional:
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
