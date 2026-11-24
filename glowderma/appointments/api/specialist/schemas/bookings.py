"""OpenAPI declarations for the specialist's actions on an own appointment: confirm or complete, reschedule
and cancel."""

from __future__ import annotations

from drf_spectacular.utils import extend_schema

from glowderma.appointments.api.admin.schemas.common import ACTION_PARAM
from glowderma.appointments.api.admin.serializers import (
    AppointmentCancelSerializer,
)
from glowderma.appointments.api.specialist.serializers import (
    SpecialistAppointmentItemSerializer,
    SpecialistRescheduleSerializer,
)
from glowderma.core.api.schema import ADMIN_ERRORS, IDEMPOTENCY_KEY_HEADER

SPECIALIST_APPOINTMENT_ACTION_POST = extend_schema(
    operation_id="specialist_appointments_action",
    description=(
        "confirm / reject (a requested visit, before its start) or attend (a confirmed visit, from its "
        "start on); otherwise 409 invalid_transition."
    ),
    request=None,
    parameters=[ACTION_PARAM, IDEMPOTENCY_KEY_HEADER],
    responses={200: SpecialistAppointmentItemSerializer, **ADMIN_ERRORS},
)

SPECIALIST_APPOINTMENT_RESCHEDULE_POST = extend_schema(
    operation_id="specialist_appointments_reschedule",
    description=(
        "Move an own open appointment before its start (same slot rules as staff). Moving it to another "
        "clinic needs `customer_agreed`."
    ),
    request=SpecialistRescheduleSerializer,
    parameters=[IDEMPOTENCY_KEY_HEADER],
    responses={200: SpecialistAppointmentItemSerializer, **ADMIN_ERRORS},
)

SPECIALIST_APPOINTMENT_CANCEL_POST = extend_schema(
    operation_id="specialist_appointments_cancel",
    description="Cancel an own open appointment before its start; the customer is notified.",
    request=AppointmentCancelSerializer,
    parameters=[IDEMPOTENCY_KEY_HEADER],
    responses={200: SpecialistAppointmentItemSerializer, **ADMIN_ERRORS},
)


__all__ = [
    "SPECIALIST_APPOINTMENT_ACTION_POST",
    "SPECIALIST_APPOINTMENT_CANCEL_POST",
    "SPECIALIST_APPOINTMENT_RESCHEDULE_POST",
]
