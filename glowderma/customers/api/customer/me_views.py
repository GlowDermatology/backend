"""``me/*`` of the customer API (customer contract §2.2): profile, deletion, consent, image, device, password,
social identities and addresses. Every query starts from the signed-in customer; another customer's address
id answers 404.

``Idempotency-Key`` (C§1.7): required on ``POST me/password/``, ``POST me/social/`` and ``POST
me/addresses/``, optional and honoured when sent on every other write here. A refusal rolls the key's
transaction back; what a refusal keeps on purpose lives outside it: the re-authentication guard counters
(cache) and the revocation of an Apple token a refused link exchanged
(:func:`customers.services.revoke_refused_apple_tokens`)."""

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
    OPTIONAL_IDEMPOTENCY_KEY_HEADER,
)
from glowderma.core.idempotency import idempotent
from glowderma.customers import selectors, services
from glowderma.customers.api.customer import serializers as s
from glowderma.customers.api.customer.auth_views import client_ip, require_social_available
from glowderma.customers.api.customer.schema import DeleteBodyAutoSchema
from glowderma.integrations.oauth.registry import CUSTOMER_PROVIDERS

PARAMETERS = [*DEVICE_HEADERS, ACCEPT_LANGUAGE_HEADER]
I_PARAMETERS = [*PARAMETERS, IDEMPOTENCY_KEY_HEADER]
O_PARAMETERS = [*PARAMETERS, OPTIONAL_IDEMPOTENCY_KEY_HEADER]
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

    @property
    def sid(self) -> str:
        """The request's session id: every write re-checks it under the customer-level lock."""
        return str(cast(dict[str, Any], self.request.auth)["sid"])

    def me(self) -> dict[str, Any]:
        return selectors.me_payload(Customer.objects.get(pk=self.customer.pk), fid=self.fid)


class MeView(MeBase):
    schema = DeleteBodyAutoSchema()  # DELETE me/ carries the re-authentication body

    @extend_schema(
        operation_id="customer_me_retrieve",
        parameters=PARAMETERS,
        responses={200: s.CustomerMeSerializer, **CUSTOMER_ERRORS},
    )
    def get(self, request: Request) -> Response:
        return Response(self.me())

    @extend_schema(
        operation_id="customer_me_partial_update",
        parameters=O_PARAMETERS,
        request=s.MePatchSerializer,
        responses={200: s.CustomerMeSerializer, **CUSTOMER_ERRORS},
    )
    @idempotent(required=False)
    def patch(self, request: Request) -> Response:
        serializer = s.MePatchSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        services.update_profile(self.customer.pk, dict(serializer.validated_data), fid=self.fid, sid=self.sid)
        return Response(self.me())

    @extend_schema(
        operation_id="customer_me_destroy",
        parameters=O_PARAMETERS,
        request=s.ReauthSerializer,
        responses={204: None, **CUSTOMER_ERRORS},
    )
    @idempotent(required=False)
    def delete(self, request: Request) -> Response:
        serializer = s.ReauthSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        services.delete_account(
            self.customer.pk,
            password=data.get("password"),
            verification_token=data.get("verification_token"),
            sid=self.sid,
            request=request,
        )
        return Response(status=204)


class ConsentView(MeBase):
    @extend_schema(
        operation_id="customer_me_consent",
        parameters=O_PARAMETERS,
        request=s.ConsentSerializer,
        responses={200: s.CustomerMeSerializer, **CUSTOMER_ERRORS},
    )
    @idempotent(required=False)
    def post(self, request: Request) -> Response:
        serializer = s.ConsentSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        services.accept_consent(
            self.customer.pk,
            terms_version=serializer.validated_data["terms_version"],
            ip=client_ip(request),
            sid=self.sid,
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
        parameters=O_PARAMETERS,
        request={
            "multipart/form-data": {
                "type": "object",
                "properties": {"file": {"type": "string", "format": "binary"}},
            }
        },
        responses={200: s.ImageUrlSerializer, **CUSTOMER_ERRORS},
    )
    @idempotent(required=False)
    def put(self, request: Request) -> Response:
        upload = request.FILES.get("file")
        if upload is None:
            raise ValidationError({"file": [field_error("required")]})
        return Response({"image_url": services.set_image(self.customer.pk, upload, sid=self.sid)})

    @extend_schema(
        operation_id="customer_me_image_destroy",
        parameters=O_PARAMETERS,
        responses={204: None, **CUSTOMER_ERRORS},
    )
    @idempotent(required=False)
    def delete(self, request: Request) -> Response:
        services.delete_image(self.customer.pk, sid=self.sid)
        return Response(status=204)


class DeviceView(MeBase):
    @extend_schema(
        operation_id="customer_me_device_update",
        parameters=O_PARAMETERS,
        request=s.DeviceSerializer,
        responses={200: s.DeviceStateSerializer, **CUSTOMER_ERRORS},
    )
    @idempotent(required=False)
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
            sid=self.sid,
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
    def post(self, request: Request) -> Response:
        # The Idempotency-Key transaction rolls back with a refusal: an Apple token exchanged for it is
        # revoked after that transaction ended, so the revocation is not rolled back with it.
        with services.revoke_refused_apple_tokens():
            return self.link(request)

    @idempotent()
    def link(self, request: Request) -> Response:
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
            sid=self.sid,
            request=request,
        )
        return Response(self.me())


class SocialUnlinkView(MeBase):
    schema = DeleteBodyAutoSchema()  # DELETE carries the re-authentication body

    @extend_schema(
        operation_id="customer_me_social_unlink",
        parameters=[*O_PARAMETERS, PROVIDER_PARAM],
        request=s.ReauthSerializer,
        responses={200: s.CustomerMeSerializer, **CUSTOMER_ERRORS},
    )
    @idempotent(required=False)
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
            sid=self.sid,
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
        address = services.add_address(self.customer.pk, sid=self.sid, **serializer.validated_data)
        return Response(selectors.address_payload(address), status=201)


class AddressDetailView(MeBase):
    """The address row is re-read under the customer-level lock by the service (404 when it is not the
    caller's, or another request removed it meanwhile)."""

    @extend_schema(
        operation_id="customer_me_addresses_partial_update",
        parameters=[*O_PARAMETERS, ADDRESS_ID_PARAM],
        request=s.AddressWriteSerializer,
        responses={200: s.CustomerAddressSerializer, **CUSTOMER_ERRORS},
    )
    @idempotent(required=False)
    def patch(self, request: Request, pk: int) -> Response:
        if not Address.objects.filter(pk=pk, customer_id=self.customer.pk).exists():
            raise NotFound  # 404 before validation for a foreign or unknown id
        serializer = s.AddressWriteSerializer(data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        updated = services.update_address(self.customer.pk, pk, sid=self.sid, **serializer.validated_data)
        return Response(selectors.address_payload(updated))

    @extend_schema(
        operation_id="customer_me_addresses_destroy",
        parameters=[*O_PARAMETERS, ADDRESS_ID_PARAM],
        responses={204: None, **CUSTOMER_ERRORS},
    )
    @idempotent(required=False)
    def delete(self, request: Request, pk: int) -> Response:
        services.remove_address(self.customer.pk, pk, sid=self.sid)
        return Response(status=204)


class AddressDefaultView(MeBase):
    @extend_schema(
        operation_id="customer_me_addresses_default",
        parameters=[*O_PARAMETERS, ADDRESS_ID_PARAM],
        request=None,
        responses={200: s.CustomerAddressSerializer, **CUSTOMER_ERRORS},
    )
    @idempotent(required=False)
    def post(self, request: Request, pk: int) -> Response:
        address = services.set_default_address(self.customer.pk, pk, sid=self.sid)
        return Response(selectors.address_payload(address))
