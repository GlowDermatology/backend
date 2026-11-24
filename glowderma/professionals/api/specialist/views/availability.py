"""The specialist's availability: the weekly hours, the date overrides and the blocked periods."""

from __future__ import annotations

from rest_framework.request import Request
from rest_framework.response import Response

from glowderma.core.idempotency import idempotent
from glowderma.professionals import selectors, services
from glowderma.professionals.api.admin.serializers import (
    BlockedPeriodCreateSerializer,
    BlockedPeriodSerializer,
    HoursOverrideCreateSerializer,
    HoursOverrideSerializer,
    ScheduleRangeQuerySerializer,
    WorkingWeekSerializer,
)
from glowderma.professionals.api.specialist.base import SpecialistProfileAPIView, require_active_profile
from glowderma.professionals.api.specialist.payloads import own_profile_body
from glowderma.professionals.api.specialist.schemas.availability import (
    AVAILABILITY_GET,
    BLOCKS_DELETE,
    BLOCKS_POST,
    BLOCKS_PUT,
    OVERRIDES_DELETE,
    OVERRIDES_POST,
    OVERRIDES_PUT,
    WEEKLY_HOURS_PUT,
)
from glowderma.professionals.api.specialist.serializers import (
    SpecialistAvailabilitySerializer,
)


class AvailabilityView(SpecialistProfileAPIView):
    @AVAILABILITY_GET
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
    @WEEKLY_HOURS_PUT
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
    @OVERRIDES_POST
    @idempotent(required=False)
    def post(self, request: Request) -> Response:
        return self.save(request)

    @OVERRIDES_PUT
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

    @OVERRIDES_DELETE
    @idempotent(required=False)
    def delete(self, request: Request, pk: int) -> Response:
        professional = require_active_profile(self.account_id, lock=True)
        services.delete_override(professional.pk, pk, actor=request.user, request=request)
        return Response(status=204)


class BlocksView(SpecialistProfileAPIView):
    @BLOCKS_POST
    @idempotent(required=False)
    def post(self, request: Request) -> Response:
        return self.save(request)

    @BLOCKS_PUT
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

    @BLOCKS_DELETE
    @idempotent(required=False)
    def delete(self, request: Request, pk: int) -> Response:
        professional = require_active_profile(self.account_id, lock=True)
        services.delete_block(professional.pk, pk, actor=request.user, request=request)
        return Response(status=204)


__all__ = [
    "AvailabilityView",
    "BlocksView",
    "OverridesView",
    "WeeklyHoursView",
]
