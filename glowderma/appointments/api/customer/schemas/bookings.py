"""OpenAPI declarations for booking, the customer's own appointments and cancel (C)."""

from __future__ import annotations

from drf_spectacular.utils import OpenApiParameter, extend_schema

from glowderma.appointments.api.customer.schemas.common import HEADERS
from glowderma.appointments.api.customer.serializers import (
    BookingSerializer,
    CancelSerializer,
    CustomerAppointmentSerializer,
)
from glowderma.core import enums
from glowderma.core.api.schema import (
    CUSTOMER_ERRORS,
    IDEMPOTENCY_KEY_HEADER,
)

APPOINTMENT_CREATE_POST = extend_schema(
    operation_id="customer_appointments_create",
    description=(
        "Book a slot (`requested`; staff confirm it). 409 `phone_required` first while the account has "
        "no verified phone (`me/phone/`). Checks in order: specialist, service, consent, "
        "open appointments limit, then the start (aware, lead time, horizon, grid, hours, busy, own "
        "overlap); a lost race answers 409 `slot_taken`."
    ),
    parameters=[*HEADERS, IDEMPOTENCY_KEY_HEADER],
    request=BookingSerializer,
    responses={201: CustomerAppointmentSerializer, **CUSTOMER_ERRORS},
)

APPOINTMENT_CANCEL_POST = extend_schema(
    operation_id="customer_appointments_cancel",
    description=(
        "Cancel an own `requested`/`confirmed` appointment before `BOOKING_CANCEL_MIN_LEAD_MINUTES`; "
        "else 409 `not_cancellable`. The slot is free again; no push."
    ),
    parameters=[*HEADERS, IDEMPOTENCY_KEY_HEADER],
    request=CancelSerializer,
    responses={200: CustomerAppointmentSerializer, **CUSTOMER_ERRORS},
)

MY_APPOINTMENTS_LIST = extend_schema(
    operation_id="customer_my_appointments_list",
    description=(
        "`upcoming` (default): open appointments starting now or later, by start; `past`: the rest, "
        "newest first. Limited to the history horizon."
    ),
    parameters=[
        *HEADERS,
        OpenApiParameter(
            "scope", str, OpenApiParameter.QUERY, required=False, enum=enums.AppointmentScope.values
        ),
    ],
    responses={200: CustomerAppointmentSerializer(many=True), **CUSTOMER_ERRORS},
)

MY_APPOINTMENTS_RETRIEVE = extend_schema(
    operation_id="customer_my_appointments_retrieve",
    description="One own appointment (404 for another customer's or one before the history horizon).",
    parameters=HEADERS,
    responses={200: CustomerAppointmentSerializer, **CUSTOMER_ERRORS},
)


__all__ = [
    "APPOINTMENT_CANCEL_POST",
    "APPOINTMENT_CREATE_POST",
    "MY_APPOINTMENTS_LIST",
    "MY_APPOINTMENTS_RETRIEVE",
]
