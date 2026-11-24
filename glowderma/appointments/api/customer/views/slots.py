"""A specialist's free slots (O)."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from django.conf import settings
from django.http import Http404
from rest_framework.exceptions import ValidationError
from rest_framework.request import Request
from rest_framework.response import Response

from glowderma.accounts.api.customer_base import PublicAPIView
from glowderma.appointments import selectors
from glowderma.appointments.api.customer.schemas.slots import (
    SPECIALIST_AVAILABILITY_GET,
    SPECIALIST_SLOTS_GET,
)
from glowderma.appointments.api.customer.serializers import (
    AvailabilityQuerySerializer,
    FreeSlotsSerializer,
    SlotsQuerySerializer,
    SpecialistAvailabilityListSerializer,
)
from glowderma.core import timeutils
from glowderma.core.api.exceptions import field_error
from glowderma.professionals.selectors import assigned_locations, format_clock


def _slot_label(value: datetime) -> str:
    return format_clock(value.astimezone(timeutils.TASHKENT).time())


class SpecialistSlotsView(PublicAPIView):
    @SPECIALIST_SLOTS_GET
    def get(self, request: Request, pk: int) -> Response:
        professional = selectors.specialist(pk)
        if professional is None:
            raise Http404
        query = SlotsQuerySerializer(data=request.query_params.dict())
        query.is_valid(raise_exception=True)
        day = query.validated_data["date"]
        now = timeutils.now()
        if not selectors.customer_today(now) <= day <= selectors.booking_horizon_end(now):
            raise ValidationError(
                {
                    "date": [
                        field_error(
                            "out_of_range",
                            message_key="field.booking_horizon",
                            params={"n": int(settings.BOOKING_HORIZON_DAYS)},
                        )
                    ]
                }
            )
        service = selectors.offered_service(professional.pk, query.validated_data["service_id"])
        if service is None:
            raise ValidationError(
                {"service_id": [field_error("does_not_exist", message_key="field.service_not_offered")]}
            )
        location_id = query.validated_data["location_id"]
        if location_id not in {item.pk for item in assigned_locations(professional, active_only=True)}:
            raise ValidationError(
                {
                    "location_id": [
                        field_error("does_not_exist", message_key="field.booking_location_not_assigned")
                    ]
                }
            )
        free = selectors.free_slots(professional, service, day, now, location_id=location_id)
        duration = timedelta(minutes=free.duration_min)
        payload: dict[str, Any] = {
            "date": free.day,
            "specialist_id": free.professional_id,
            "service_id": free.service_id,
            "location_id": free.location_id,
            "duration_min": free.duration_min,
            "price": free.price,
            "slot_step_min": free.slot_step_min,
            "working_hours": (
                None
                if free.working_hours is None
                else {"start": free.working_hours[0], "end": free.working_hours[1]}
            ),
            "slots": [
                {
                    "start": _slot_label(start),
                    "end": _slot_label(start + duration),
                    "start_at": start,
                }
                for start in free.slots
            ],
        }
        return Response(FreeSlotsSerializer(payload).data)


class SpecialistAvailabilityView(PublicAPIView):
    """The storefront's "next free slot" badges: every listed specialist at once."""

    public_cache_seconds = 60

    @SPECIALIST_AVAILABILITY_GET
    def get(self, request: Request) -> Response:
        query = AvailabilityQuerySerializer(data=request.query_params.dict())
        query.is_valid(raise_exception=True)
        now = timeutils.now()
        rows = selectors.specialists_availability(query.validated_data["days"], now)
        payload = {
            "generated_at": now,
            "results": [
                {
                    "specialist_id": row.specialist_id,
                    "next_slot": (
                        None
                        if row.next_slot is None
                        else {
                            "start": row.next_slot.start,
                            "location_id": row.next_slot.location_id,
                            "service_id": row.next_slot.service_id,
                        }
                    ),
                    "slots_today": row.slots_today,
                }
                for row in rows
            ],
        }
        return Response(SpecialistAvailabilityListSerializer(payload).data)


__all__ = [
    "SpecialistAvailabilityView",
    "SpecialistSlotsView",
]
