"""OpenAPI declarations for the admin boards (contract §2.9): the day board with its bookings, the range
boards, the pending summary and the booking form's meta."""

from __future__ import annotations

from drf_spectacular.utils import extend_schema

from glowderma.appointments.api.admin.serializers import (
    AppointmentCreateSerializer,
    AppointmentItemSerializer,
    AppointmentRangeSerializer,
    AppointmentsMetaSerializer,
    DayQuerySerializer,
    DayScheduleSerializer,
    PendingSummarySerializer,
    RangeQuerySerializer,
)
from glowderma.core.api.schema import ADMIN_ERRORS

APPOINTMENTS_GET = extend_schema(
    operation_id="admin_appointments_day",
    parameters=[DayQuerySerializer],
    responses={200: DayScheduleSerializer, **ADMIN_ERRORS},
)

APPOINTMENTS_POST = extend_schema(
    operation_id="admin_appointments_create",
    description=(
        "Staff booking (requested_via=admin, created directly as confirmed). The slot must sit on a "
        "5-minute grid in the future, inside the specialist's hours for that day (a date override wins), "
        "outside blocked periods and the recurring break, and must not overlap another open appointment "
        "(a concurrent booking of the same slot answers 409 conflict)."
    ),
    request=AppointmentCreateSerializer,
    responses={201: AppointmentItemSerializer, **ADMIN_ERRORS},
)

APPOINTMENT_RANGE_GET = extend_schema(
    operation_id="admin_appointments_range",
    description=(
        "The week and month boards: per Tashkent day of `from`…`to` (inclusive) the status counts of the "
        "appointments starting that day, whether any listed specialist works and their combined hours. "
        "`detail=counts` (default) allows up to 42 days; `detail=full` (up to 7 days) adds every "
        "specialist's day schedule and the appointments in the day-item shape. Specialists follow the "
        "day board's rule over the whole range (active ones, plus inactive ones with an appointment in "
        "it); `specialist_id` narrows specialists, counts and appointments to one."
    ),
    parameters=[RangeQuerySerializer],
    responses={200: AppointmentRangeSerializer, **ADMIN_ERRORS},
)

PENDING_SUMMARY_GET = extend_schema(
    operation_id="admin_appointments_pending_summary",
    responses={200: PendingSummarySerializer, **ADMIN_ERRORS},
)

APPOINTMENTS_META_GET = extend_schema(
    operation_id="admin_appointments_meta",
    description=(
        "What the admin booking dialog offers: active specialists (by position, then name) with "
        "their active services (the duration and price a booking would snapshot) and the slot grid "
        "in minutes."
    ),
    responses={200: AppointmentsMetaSerializer, **ADMIN_ERRORS},
)


__all__ = [
    "APPOINTMENTS_GET",
    "APPOINTMENTS_META_GET",
    "APPOINTMENTS_POST",
    "APPOINTMENT_RANGE_GET",
    "PENDING_SUMMARY_GET",
]
