"""OpenAPI declarations for the admin booking actions: confirm or complete, reschedule and cancel."""

from __future__ import annotations

from drf_spectacular.utils import extend_schema

from glowderma.appointments.api.admin.schemas.common import ACTION_PARAM
from glowderma.appointments.api.admin.serializers import (
    AppointmentCancelSerializer,
    AppointmentItemSerializer,
    AppointmentRescheduleSerializer,
)
from glowderma.core.api.schema import ADMIN_ERRORS, IDEMPOTENCY_KEY_HEADER

APPOINTMENT_ACTION_POST = extend_schema(
    operation_id="admin_appointments_action",
    description=(
        "confirm / reject (from requested) or attend (from confirmed); otherwise 409 invalid_transition."
    ),
    parameters=[ACTION_PARAM, IDEMPOTENCY_KEY_HEADER],
    request=None,
    responses={200: AppointmentItemSerializer, **ADMIN_ERRORS},
)

APPOINTMENT_RESCHEDULE_POST = extend_schema(
    operation_id="admin_appointments_reschedule",
    description=(
        "Move an open (requested|confirmed) appointment; the status is kept. Same slot rules as the "
        "creation. Moving to another specialist needs `service_id`, one of that specialist's active "
        "services; a changed service re-snapshots duration and price."
    ),
    request=AppointmentRescheduleSerializer,
    responses={200: AppointmentItemSerializer, **ADMIN_ERRORS},
)

APPOINTMENT_CANCEL_POST = extend_schema(
    operation_id="admin_appointments_cancel",
    description=(
        "Cancel an open (requested|confirmed) appointment; the slot is freed and the customer is pushed."
    ),
    request=AppointmentCancelSerializer,
    responses={200: AppointmentItemSerializer, **ADMIN_ERRORS},
)


__all__ = [
    "APPOINTMENT_ACTION_POST",
    "APPOINTMENT_CANCEL_POST",
    "APPOINTMENT_RESCHEDULE_POST",
]
