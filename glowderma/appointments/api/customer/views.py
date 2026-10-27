"""Customer appointment routes (customer contract §2.11).

**O**: ``specialists/``, ``specialists/{id}/``, ``services/``, ``specialists/{id}/slots/``.
**C**: ``POST appointments/`` (**I**), ``GET me/appointments/`` (**P**), ``GET me/appointments/{id}/``,
``POST appointments/{id}/cancel/`` (**I**). The rules live in ``appointments.services`` / ``selectors``;
appointments of another customer or before the history horizon answer 404.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, cast

from django.conf import settings
from django.http import Http404
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import mixins
from rest_framework.exceptions import ValidationError
from rest_framework.request import Request
from rest_framework.response import Response

from glowderma.accounts import services as account_services
from glowderma.accounts.api.customer_base import CustomerAPIView, CustomerGenericViewSet, PublicAPIView
from glowderma.accounts.models import Customer
from glowderma.appointments import selectors, services
from glowderma.appointments.api.customer.serializers import (
    BookingSerializer,
    CancelSerializer,
    CustomerAppointmentSerializer,
    FreeSlotsSerializer,
    ServiceListSerializer,
    SlotsQuerySerializer,
    SpecialistListSerializer,
    SpecialistSerializer,
)
from glowderma.core import enums, timeutils
from glowderma.core.api.exceptions import field_error
from glowderma.core.api.schema import (
    ACCEPT_LANGUAGE_HEADER,
    CUSTOMER_ERRORS,
    DEVICE_HEADERS,
    IDEMPOTENCY_KEY_HEADER,
    OPTIONAL_DEVICE_HEADERS,
    PUBLIC_CUSTOMER_ERRORS,
)
from glowderma.core.idempotency import idempotent
from glowderma.professionals.selectors import format_clock

HEADERS = [*DEVICE_HEADERS, ACCEPT_LANGUAGE_HEADER]
PUBLIC_HEADERS = [*OPTIONAL_DEVICE_HEADERS, ACCEPT_LANGUAGE_HEADER]


def _customer(request: Request) -> Customer:
    return cast(Customer, request.user)


# --- O: the booking catalogue -----------------------------------------------------------------------------


class SpecialistListView(PublicAPIView):
    @extend_schema(
        operation_id="customer_specialists_list",
        description="Active specialists by position and name, with their active services.",
        parameters=PUBLIC_HEADERS,
        responses={200: SpecialistListSerializer, **PUBLIC_CUSTOMER_ERRORS},
    )
    def get(self, request: Request) -> Response:
        context = {"lang": self.language}
        return Response(SpecialistListSerializer({"results": selectors.specialists()}, context=context).data)


class SpecialistDetailView(PublicAPIView):
    @extend_schema(
        operation_id="customer_specialists_retrieve",
        description="One active specialist (404 when inactive or unknown).",
        parameters=PUBLIC_HEADERS,
        responses={200: SpecialistSerializer, **PUBLIC_CUSTOMER_ERRORS},
    )
    def get(self, request: Request, pk: int) -> Response:
        professional = selectors.specialist(pk)
        if professional is None:
            raise Http404
        return Response(SpecialistSerializer(professional, context={"lang": self.language}).data)


class SpecialistBySlugView(PublicAPIView):
    @extend_schema(
        operation_id="customer_specialists_by_slug_retrieve",
        description="One active specialist by its website slug (B-W1; 404 when inactive or unknown).",
        parameters=PUBLIC_HEADERS,
        responses={200: SpecialistSerializer, **PUBLIC_CUSTOMER_ERRORS},
    )
    def get(self, request: Request, slug: str) -> Response:
        professional = selectors.specialist_by_slug(slug)
        if professional is None:
            raise Http404
        return Response(SpecialistSerializer(professional, context={"lang": self.language}).data)


class ServiceListView(PublicAPIView):
    @extend_schema(
        operation_id="customer_services_list",
        description="Active services offered by at least one active specialist, with their price range.",
        parameters=PUBLIC_HEADERS,
        responses={200: ServiceListSerializer, **PUBLIC_CUSTOMER_ERRORS},
    )
    def get(self, request: Request) -> Response:
        context = {"lang": self.language}
        return Response(ServiceListSerializer({"results": selectors.service_offers()}, context=context).data)


def _slot_label(value: datetime) -> str:
    return format_clock(value.astimezone(timeutils.TASHKENT).time())


class SpecialistSlotsView(PublicAPIView):
    @extend_schema(
        operation_id="customer_specialist_slots_list",
        description=(
            "Free start times of one specialist for one service on one day (grid relative to the start of "
            "the working hours; lead time, blocks, the break and open appointments removed)."
        ),
        parameters=[
            *PUBLIC_HEADERS,
            OpenApiParameter("date", str, OpenApiParameter.QUERY, required=True, description="YYYY-MM-DD"),
            OpenApiParameter("service_id", int, OpenApiParameter.QUERY, required=True),
        ],
        responses={200: FreeSlotsSerializer, **PUBLIC_CUSTOMER_ERRORS},
    )
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
        link = selectors.offered_link(professional.pk, query.validated_data["service_id"])
        if link is None:
            raise ValidationError(
                {"service_id": [field_error("does_not_exist", message_key="field.service_not_offered")]}
            )
        free = selectors.free_slots(professional, link, day, now)
        duration = timedelta(minutes=free.duration_min)
        payload: dict[str, Any] = {
            "date": free.day,
            "specialist_id": free.professional_id,
            "service_id": free.service_id,
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


# --- C: booking, own appointments, cancel -----------------------------------------------------------------


def _appointment_response(request: Request, view: Any, appointment_id: int, status: int = 200) -> Response:
    customer = _customer(request)
    appointment = selectors.customer_appointment(customer.pk, appointment_id, horizon=customer.history_from)
    if appointment is None:  # pragma: no cover - the caller just wrote it
        raise Http404
    context = {"lang": view.language, "now": timeutils.now()}
    return Response(CustomerAppointmentSerializer(appointment, context=context).data, status=status)


class AppointmentCreateView(CustomerAPIView):
    @extend_schema(
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
    @idempotent()
    def post(self, request: Request) -> Response:
        account_services.require_phone(_customer(request))  # hybrid identity, C§2.2.10
        body = BookingSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        data = body.validated_data
        appointment = services.request_by_customer(
            _customer(request).pk,
            data["specialist_id"],
            data["service_id"],
            data["start"],
            data.get("note") or "",
            request=request,
        )
        return _appointment_response(request, self, appointment.pk, status=201)


class AppointmentCancelView(CustomerAPIView):
    @extend_schema(
        operation_id="customer_appointments_cancel",
        description=(
            "Cancel an own `requested`/`confirmed` appointment before `BOOKING_CANCEL_MIN_LEAD_MINUTES`; "
            "else 409 `not_cancellable`. The slot is free again; no push."
        ),
        parameters=[*HEADERS, IDEMPOTENCY_KEY_HEADER],
        request=CancelSerializer,
        responses={200: CustomerAppointmentSerializer, **CUSTOMER_ERRORS},
    )
    @idempotent()
    def post(self, request: Request, pk: int) -> Response:
        customer = _customer(request)
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
        customer = _customer(self.request)
        scope = self.request.query_params.get("scope", enums.AppointmentScope.UPCOMING)
        if scope not in enums.AppointmentScope.values:
            raise ValidationError({"scope": [field_error("invalid_choice")]})
        return selectors.customer_appointments(customer.pk, scope, horizon=customer.history_from)

    @extend_schema(
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
    def list(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        return super().list(request, *args, **kwargs)

    @extend_schema(
        operation_id="customer_my_appointments_retrieve",
        description="One own appointment (404 for another customer's or one before the history horizon).",
        parameters=HEADERS,
        responses={200: CustomerAppointmentSerializer, **CUSTOMER_ERRORS},
    )
    def retrieve(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        customer = _customer(request)
        appointment = selectors.customer_appointment(
            customer.pk, int(kwargs["pk"]), horizon=customer.history_from
        )
        if appointment is None:
            raise Http404
        return Response(self.get_serializer(appointment).data)
