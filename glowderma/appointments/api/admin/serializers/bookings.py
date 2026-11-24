"""The admin booking bodies: create (a customer or a walk-in), reschedule and cancel."""

from __future__ import annotations

from typing import Any

from rest_framework import serializers

from glowderma.accounts.models import Customer
from glowderma.appointments.api.admin.serializers.messages import MESSAGES
from glowderma.appointments.services import WalkIn
from glowderma.core import enums
from glowderma.core.api.exceptions import field_error
from glowderma.core.api.fields import AwareDateTimeField, LenientPhoneField
from glowderma.professionals.models import Professional, Service


class AppointmentCreateSerializer(serializers.Serializer[dict[str, Any]]):
    """``POST appointments/`` (T10): ``specialist_id``, ``service_id`` (one of that specialist's active
    services), ``customer_id`` | ``phone``, ``start`` (aware, 5-minute grid, inside working hours), ``note``
    (≤ 300). A ``phone`` no customer holds books a walk-in: ``name`` is then required and the customer is
    created with the booking (no password, no consent: they sign in later with an SMS code to that phone)."""

    specialist_id = serializers.IntegerField()
    service_id = serializers.IntegerField()
    location_id = serializers.IntegerField(min_value=1, max_value=2_147_483_647)
    customer_id = serializers.IntegerField(required=False)
    phone = LenientPhoneField(required=False)
    name = serializers.CharField(
        min_length=2,
        max_length=150,
        required=False,
        help_text="The new customer's name, required when no customer has the phone (a walk-in).",
    )
    start = AwareDateTimeField()
    # Optional in the contract: no schema ``default`` (a generator would type the key as always present);
    # ``validate`` supplies the runtime default "".
    note = serializers.CharField(max_length=300, allow_blank=True, required=False)

    def validate_specialist_id(self, value: int) -> Professional:
        professional = Professional.objects.filter(pk=value, is_active=True).first()
        if professional is None:
            raise serializers.ValidationError(
                [field_error("does_not_exist", MESSAGES["specialist_not_found"])]
            )
        return professional

    def validate_service_id(self, value: int) -> Service:
        return active_service(value)

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        attrs.setdefault("note", "")
        name = attrs.pop("name", "")
        has_id, has_phone = "customer_id" in attrs, "phone" in attrs
        if has_id == has_phone:
            code = "required" if not has_id else "invalid"
            message = None if not has_id else MESSAGES["one_customer_ref"]
            raise serializers.ValidationError({"customer_id": [field_error(code, message)]})
        customer: Customer | WalkIn | None
        if has_id:
            active = Customer.objects.filter(status=enums.CustomerStatus.ACTIVE)
            customer = active.filter(pk=attrs.pop("customer_id")).first()
            if customer is None:
                raise serializers.ValidationError(
                    {"customer_id": [field_error("does_not_exist", MESSAGES["customer_id_not_found"])]}
                )
        else:
            customer = customer_by_phone(attrs.pop("phone"), name)
        attrs["customer"] = customer
        attrs["professional"] = attrs.pop("specialist_id")
        attrs["service"] = attrs.pop("service_id")
        return attrs


def customer_by_phone(phone: str, name: str) -> Customer | WalkIn:
    """The active customer holding ``phone``, else a :class:`WalkIn` to create with the booking (``name``
    required then: 400 ``name``). A phone held only by a blocked or deleted account is 400 ``phone``."""
    customer = Customer.objects.filter(status=enums.CustomerStatus.ACTIVE, phone=phone).first()
    if customer is not None:
        return customer
    if Customer.objects.filter(phone=phone).exists():
        raise serializers.ValidationError(
            {"phone": [field_error("customer_not_found", MESSAGES["customer_inactive"])]}
        )
    if not name:
        raise serializers.ValidationError({"name": [field_error("required", MESSAGES["walk_in_name"])]})
    return WalkIn(phone=phone, name=name)


def active_service(pk: int) -> Service:
    service = Service.objects.filter(pk=pk, is_active=True).first()
    if service is None:
        raise serializers.ValidationError([field_error("does_not_exist", MESSAGES["service_not_found"])])
    return service


class AppointmentRescheduleSerializer(serializers.Serializer[dict[str, Any]]):
    """``POST appointments/{id}/reschedule/``: ``start`` (same slot rules), optional ``specialist_id`` and
    ``service_id`` (required with another specialist: every service belongs to one specialist)."""

    start = AwareDateTimeField()
    location_id = serializers.IntegerField(min_value=1, max_value=2_147_483_647)
    specialist_id = serializers.IntegerField(required=False)
    service_id = serializers.IntegerField(
        required=False,
        help_text=(
            "One of the target specialist's active services; required when moving to another specialist, "
            "omitted = keep the booked service (and its duration and price)."
        ),
    )

    def validate_service_id(self, value: int) -> Service:
        return active_service(value)

    def validate_specialist_id(self, value: int) -> Professional:
        professional = Professional.objects.filter(pk=value, is_active=True).first()
        if professional is None:
            raise serializers.ValidationError(
                [field_error("does_not_exist", MESSAGES["specialist_not_found"])]
            )
        return professional


class AppointmentCancelSerializer(serializers.Serializer[dict[str, Any]]):
    """``POST appointments/{id}/cancel/``: optional ``reason`` (≤ 300), kept as the staff note."""

    reason = serializers.CharField(max_length=300, allow_blank=True, required=False)  # no schema default

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        attrs.setdefault("reason", "")
        return attrs


__all__ = [
    "AppointmentCancelSerializer",
    "AppointmentCreateSerializer",
    "AppointmentRescheduleSerializer",
    "active_service",
    "customer_by_phone",
]
