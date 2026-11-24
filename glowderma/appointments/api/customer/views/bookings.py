"""Booking, the customer's own appointments and cancel (C)."""

from __future__ import annotations

from typing import Any

from django.http import Http404
from rest_framework import mixins
from rest_framework.exceptions import ValidationError
from rest_framework.request import Request
from rest_framework.response import Response

from glowderma.accounts import services as account_services
from glowderma.accounts.api.customer_base import CustomerAPIView, CustomerGenericViewSet
from glowderma.appointments import selectors, services
from glowderma.appointments.api.customer.schemas.bookings import (
    APPOINTMENT_CANCEL_POST,
    APPOINTMENT_CREATE_POST,
    MY_APPOINTMENTS_LIST,
    MY_APPOINTMENTS_RETRIEVE,
)
from glowderma.appointments.api.customer.serializers import (
    BookingSerializer,
    CancelSerializer,
    CustomerAppointmentSerializer,
)
from glowderma.appointments.api.customer.views.common import signed_in_customer
from glowderma.core import enums, timeutils
from glowderma.core.api.exceptions import field_error
from glowderma.core.idempotency import idempotent


def _appointment_response(request: Request, view: Any, appointment_id: int, status: int = 200) -> Response:
    customer = signed_in_customer(request)
    appointment = selectors.customer_appointment(customer.pk, appointment_id, horizon=customer.history_from)
    if appointment is None:  # pragma: no cover - the caller just wrote it
        raise Http404
    context = {"lang": view.language, "now": timeutils.now()}
    return Response(CustomerAppointmentSerializer(appointment, context=context).data, status=status)


class AppointmentCreateView(CustomerAPIView):
    @APPOINTMENT_CREATE_POST
    @idempotent()
    def post(self, request: Request) -> Response:
        account_services.require_phone(signed_in_customer(request))  # hybrid identity, C§2.2.10
        body = BookingSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        data = body.validated_data
        appointment = services.request_by_customer(
            signed_in_customer(request).pk,
            data["specialist_id"],
            data["service_id"],
            data["start"],
            data.get("note") or "",
            location_id=data["location_id"],
            request=request,
        )
        return _appointment_response(request, self, appointment.pk, status=201)


class AppointmentCancelView(CustomerAPIView):
    @APPOINTMENT_CANCEL_POST
    @idempotent()
    def post(self, request: Request, pk: int) -> Response:
        customer = signed_in_customer(request)
        body = CancelSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        if selectors.customer_appointment(customer.pk, pk, horizon=customer.history_from) is None:
            raise Http404
        services.cancel_by_customer(
            pk,
            customer.pk,
            reason=body.validated_data.get("reason") or "",
            request=request,
            account_deleted=False,  # the lead rule always applies to the customer route
        )
        return _appointment_response(request, self, pk)


class MyAppointmentsViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin, CustomerGenericViewSet):
    serializer_class = CustomerAppointmentSerializer
    page_size_options = (20, 50)
    default_page_size = 20

    def get_serializer_context(self) -> dict[str, Any]:
        return {**super().get_serializer_context(), "lang": self.language, "now": timeutils.now()}

    def get_queryset(self) -> Any:
        customer = signed_in_customer(self.request)
        scope = self.request.query_params.get("scope", enums.AppointmentScope.UPCOMING)
        if scope not in enums.AppointmentScope.values:
            raise ValidationError({"scope": [field_error("invalid_choice")]})
        return selectors.customer_appointments(customer.pk, scope, horizon=customer.history_from)

    @MY_APPOINTMENTS_LIST
    def list(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        return super().list(request, *args, **kwargs)

    @MY_APPOINTMENTS_RETRIEVE
    def retrieve(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        customer = signed_in_customer(request)
        appointment = selectors.customer_appointment(
            customer.pk, int(kwargs["pk"]), horizon=customer.history_from
        )
        if appointment is None:
            raise Http404
        return Response(self.get_serializer(appointment).data)


__all__ = [
    "AppointmentCancelView",
    "AppointmentCreateView",
    "MyAppointmentsViewSet",
]
