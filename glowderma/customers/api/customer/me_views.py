"""``me/*`` of the customer API (customer contract §2.2): profile, deletion, consent, image, device, password,
social identities and addresses. Every query starts from the signed-in customer; another customer's address
id answers 404."""

from __future__ import annotations

from typing import Any, cast

from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework.exceptions import NotFound, ValidationError
from rest_framework.parsers import MultiPartParser
from rest_framework.request import Request
from rest_framework.response import Response

from glowderma.accounts import services as account_services
from glowderma.accounts.api.customer_base import CustomerAPIView
from glowderma.accounts.authentication import DeviceHeaders
from glowderma.accounts.models import Address, Customer
from glowderma.core import timeutils
from glowderma.core.api.exceptions import field_error
from glowderma.core.api.schema import (
    ACCEPT_LANGUAGE_HEADER,
    CUSTOMER_ERRORS,
    DEVICE_HEADERS,
    IDEMPOTENCY_KEY_HEADER,
)
from glowderma.core.idempotency import idempotent
from glowderma.customers import selectors, services
from glowderma.customers.api.customer import serializers as s
from glowderma.customers.api.customer.auth_views import client_ip, require_social_available
from glowderma.integrations.oauth.registry import CUSTOMER_PROVIDERS

PARAMETERS = [*DEVICE_HEADERS, ACCEPT_LANGUAGE_HEADER]
I_PARAMETERS = [*PARAMETERS, IDEMPOTENCY_KEY_HEADER]
PROVIDER_PARAM = OpenApiParameter(
    name="provider", type=OpenApiTypes.STR, location=OpenApiParameter.PATH, enum=list(CUSTOMER_PROVIDERS)
)
ADDRESS_ID_PARAM = OpenApiParameter(name="id", type=OpenApiTypes.INT, location=OpenApiParameter.PATH)


class MeBase(CustomerAPIView):
    @property
    def customer(self) -> Customer:
        return cast(Customer, self.request.user)

    @property
    def fid(self) -> str:
        return cast(DeviceHeaders, self.device).fid

    def me(self) -> dict[str, Any]:
        return selectors.me_payload(Customer.objects.get(pk=self.customer.pk), fid=self.fid)


class MeView(MeBase):
    @extend_schema(
        operation_id="customer_me_retrieve",
        parameters=PARAMETERS,
        responses={200: s.CustomerMeSerializer, **CUSTOMER_ERRORS},
    )
    def get(self, request: Request) -> Response:
        return Response(self.me())

    @extend_schema(
        operation_id="customer_me_partial_update",
        parameters=PARAMETERS,
        request=s.MePatchSerializer,
        responses={200: s.CustomerMeSerializer, **CUSTOMER_ERRORS},
    )
    def patch(self, request: Request) -> Response:
        serializer = s.MePatchSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        services.update_profile(self.customer.pk, dict(serializer.validated_data), fid=self.fid)
        return Response(self.me())

    @extend_schema(
        operation_id="customer_me_destroy",
        parameters=PARAMETERS,
        request=s.ReauthSerializer,
        responses={204: None, **CUSTOMER_ERRORS},
    )
    def delete(self, request: Request) -> Response:
        serializer = s.ReauthSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        services.delete_account(
            self.customer.pk,
            password=data.get("password"),
            verification_token=data.get("verification_token"),
            request=request,
        )
        return Response(status=204)


class ConsentView(MeBase):
    @extend_schema(
        operation_id="customer_me_consent",
        parameters=PARAMETERS,
        request=s.ConsentSerializer,
        responses={200: s.CustomerMeSerializer, **CUSTOMER_ERRORS},
    )
    def post(self, request: Request) -> Response:
        serializer = s.ConsentSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        services.accept_consent(
            self.customer.pk,
            terms_version=serializer.validated_data["terms_version"],
            ip=client_ip(request),
            request=request,
        )
        return Response(self.me())


class ImageView(MeBase):
    def get_parsers(self) -> list[Any]:
        return (
            [MultiPartParser()]
            if self.request is None or self.request.method == "PUT"
            else super().get_parsers()
        )

    @extend_schema(
        operation_id="customer_me_image_update",
        parameters=PARAMETERS,
        request={
            "multipart/form-data": {
                "type": "object",
                "properties": {"file": {"type": "string", "format": "binary"}},
            }
        },
        responses={200: s.ImageUrlSerializer, **CUSTOMER_ERRORS},
    )
    def put(self, request: Request) -> Response:
        upload = request.FILES.get("file")
        if upload is None:
            raise ValidationError({"file": [field_error("required")]})
        return Response({"image_url": services.set_image(self.customer.pk, upload)})

    @extend_schema(
        operation_id="customer_me_image_destroy",
        parameters=PARAMETERS,
        responses={204: None, **CUSTOMER_ERRORS},
    )
    def delete(self, request: Request) -> Response:
        services.delete_image(self.customer.pk)
        return Response(status=204)


class DeviceView(MeBase):
    @extend_schema(
        operation_id="customer_me_device_update",
        parameters=PARAMETERS,
        request=s.DeviceSerializer,
        responses={200: s.DeviceStateSerializer, **CUSTOMER_ERRORS},
    )
    def put(self, request: Request) -> Response:
        serializer = s.DeviceSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        headers = cast(DeviceHeaders, self.device)
        device = account_services.register_device(
            self.customer.pk,
            headers.fid,
            fcm_token=data.get("fcm_token"),
            push_enabled=data["push_enabled"],
            model=data.get("model", ""),
            os=data.get("os", ""),
            locale=data.get("locale", ""),
            platform=headers.platform,
            app_version=headers.app_version,
        )
        return Response(
            {
                "fid": device.fid,
                "platform": device.platform,
                "model": device.model,
                "os": device.os,
                "app_version": device.app_version,
                "push_enabled": device.push_enabled,
                "last_active_at": timeutils.isoformat(device.last_active_at),
            }
        )


class PasswordView(MeBase):
    @extend_schema(
        operation_id="customer_me_password",
        parameters=I_PARAMETERS,
        request=s.MePasswordSerializer,
        responses={200: s.CustomerMeSerializer, **CUSTOMER_ERRORS},
    )
    @idempotent()
    def post(self, request: Request) -> Response:
        serializer = s.MePasswordSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        services.change_password(
            self.customer.pk,
            current_password=data.get("current_password"),
            verification_token=data.get("verification_token"),
            password=data["password"],
            sid=str(cast(dict[str, Any], request.auth)["sid"]),
            request=request,
        )
        return Response(self.me())


class SocialLinkView(MeBase):
    @extend_schema(
        operation_id="customer_me_social_link",
        parameters=I_PARAMETERS,
        request=s.MeSocialLinkSerializer,
        responses={200: s.CustomerMeSerializer, **CUSTOMER_ERRORS},
    )
    @idempotent()
    def post(self, request: Request) -> Response:
        require_social_available(request.data.get("provider") if isinstance(request.data, dict) else None)
        serializer = s.MeSocialLinkSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        services.link_social(
            self.customer.pk,
            data["provider"],
            data["token"],
            data.get("token_type"),
            data.get("nonce"),
            data.get("authorization_code"),
            password=data.get("password"),
            verification_token=data.get("verification_token"),
            fid=self.fid,
            request=request,
        )
        return Response(self.me())


class SocialUnlinkView(MeBase):
    @extend_schema(
        operation_id="customer_me_social_unlink",
        parameters=[*PARAMETERS, PROVIDER_PARAM],
        request=s.ReauthSerializer,
        responses={200: s.CustomerMeSerializer, **CUSTOMER_ERRORS},
    )
    def delete(self, request: Request, provider: str) -> Response:
        if provider not in CUSTOMER_PROVIDERS:
            raise NotFound
        serializer = s.ReauthSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        services.unlink_social(
            self.customer.pk,
            provider,
            password=data.get("password"),
            verification_token=data.get("verification_token"),
            request=request,
        )
        return Response(self.me())


class AddressListView(MeBase):
    @extend_schema(
        operation_id="customer_me_addresses_list",
        parameters=PARAMETERS,
        responses={200: s.CustomerAddressListSerializer, **CUSTOMER_ERRORS},
    )
    def get(self, request: Request) -> Response:
        rows = selectors.customer_addresses(self.customer.pk)
        return Response({"results": [selectors.address_payload(row) for row in rows]})

    @extend_schema(
        operation_id="customer_me_addresses_create",
        parameters=I_PARAMETERS,
        request=s.AddressWriteSerializer,
        responses={201: s.CustomerAddressSerializer, **CUSTOMER_ERRORS},
    )
    @idempotent()
    def post(self, request: Request) -> Response:
        serializer = s.AddressWriteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        address = services.add_address(self.customer.pk, **serializer.validated_data)
        address.refresh_from_db()
        return Response(selectors.address_payload(address), status=201)


class AddressLookup(MeBase):
    def address(self, pk: int) -> Address:
        address = Address.objects.filter(pk=pk, customer_id=self.customer.pk).first()
        if address is None:
            raise NotFound
        return address


class AddressDetailView(AddressLookup):
    @extend_schema(
        operation_id="customer_me_addresses_partial_update",
        parameters=[*PARAMETERS, ADDRESS_ID_PARAM],
        request=s.AddressWriteSerializer,
        responses={200: s.CustomerAddressSerializer, **CUSTOMER_ERRORS},
    )
    def patch(self, request: Request, pk: int) -> Response:
        address = self.address(pk)
        serializer = s.AddressWriteSerializer(data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        updated = account_services.update_address(address, **serializer.validated_data)
        updated.refresh_from_db()
        return Response(selectors.address_payload(updated))

    @extend_schema(
        operation_id="customer_me_addresses_destroy",
        parameters=[*PARAMETERS, ADDRESS_ID_PARAM],
        responses={204: None, **CUSTOMER_ERRORS},
    )
    def delete(self, request: Request, pk: int) -> Response:
        account_services.remove_address(self.address(pk))
        return Response(status=204)


class AddressDefaultView(AddressLookup):
    @extend_schema(
        operation_id="customer_me_addresses_default",
        parameters=[*PARAMETERS, ADDRESS_ID_PARAM],
        request=None,
        responses={200: s.CustomerAddressSerializer, **CUSTOMER_ERRORS},
    )
    def post(self, request: Request, pk: int) -> Response:
        address = account_services.set_default_address(self.address(pk))
        address.refresh_from_db()
        return Response(selectors.address_payload(address))
