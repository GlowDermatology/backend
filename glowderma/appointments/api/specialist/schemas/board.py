"""OpenAPI declarations for the specialist's boards: the day board with its bookings, the range, the meta and
the pending summary."""

from __future__ import annotations

from drf_spectacular.utils import extend_schema

from glowderma.appointments.api.admin.serializers import (
    AppointmentsMetaSerializer,
    DayQuerySerializer,
    PendingSummarySerializer,
)
from glowderma.appointments.api.specialist.serializers import (
    SpecialistAppointmentCreateSerializer,
    SpecialistAppointmentDaySerializer,
    SpecialistAppointmentItemSerializer,
    SpecialistAppointmentRangeSerializer,
    SpecialistRangeQuerySerializer,
)
from glowderma.core.api.schema import ADMIN_ERRORS, IDEMPOTENCY_KEY_HEADER

SPECIALIST_APPOINTMENTS_GET = extend_schema(
    operation_id="specialist_appointments_day",
    description="The caller's schedule and own appointments of `date` (default today, Tashkent).",
    parameters=[DayQuerySerializer],
    responses={200: SpecialistAppointmentDaySerializer, **ADMIN_ERRORS},
)

SPECIALIST_APPOINTMENTS_POST = extend_schema(
    operation_id="specialist_appointments_create",
    description=(
        "Book a visit in the caller's own calendar (a phone call or a walk-in), created directly as "
        "confirmed: one of the caller's active services at one of their active clinics, with the staff "
        "slot rules (5-minute grid, future, inside the hours of that day and clinic, outside blocked "
        "periods, no overlap: a concurrent booking of the slot answers 409 conflict). A phone no "
        "customer holds creates the customer from `phone` and `name`. The profile must be published (409 "
        "conflict otherwise). Recorded like a staff booking (`requested_via=admin`) with the specialist "
        "as the actor."
    ),
    request=SpecialistAppointmentCreateSerializer,
    parameters=[IDEMPOTENCY_KEY_HEADER],
    responses={201: SpecialistAppointmentItemSerializer, **ADMIN_ERRORS},
)

SPECIALIST_APPOINTMENT_RANGE_GET = extend_schema(
    operation_id="specialist_appointments_range",
    description=(
        "The caller's week and month boards in the admin range shape: `detail=counts` (default) up to 42 "
        "days, `detail=full` up to 7 days. The caller's profile is always listed, even as a draft."
    ),
    parameters=[SpecialistRangeQuerySerializer],
    responses={200: SpecialistAppointmentRangeSerializer, **ADMIN_ERRORS},
)

SPECIALIST_APPOINTMENTS_META_GET = extend_schema(
    operation_id="specialist_appointments_meta",
    description=(
        "The admin booking-dialog meta for the caller only: their active services and active clinics. "
        "`specialists` is empty while the profile is unpublished."
    ),
    responses={200: AppointmentsMetaSerializer, **ADMIN_ERRORS},
)

SPECIALIST_PENDING_SUMMARY_GET = extend_schema(
    operation_id="specialist_appointments_pending_summary",
    description="The admin pending summary over the caller's own requested appointments.",
    responses={200: PendingSummarySerializer, **ADMIN_ERRORS},
)


__all__ = [
    "SPECIALIST_APPOINTMENTS_GET",
    "SPECIALIST_APPOINTMENTS_META_GET",
    "SPECIALIST_APPOINTMENTS_POST",
    "SPECIALIST_APPOINTMENT_RANGE_GET",
    "SPECIALIST_PENDING_SUMMARY_GET",
]
