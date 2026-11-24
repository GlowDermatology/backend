"""``me/`` of the customer API (customer contract §2.2): profile, deletion, consent, image, device and
password. Every query starts from the signed-in customer."""

from __future__ import annotations

from typing import Any, cast

from rest_framework.exceptions import ValidationError
from rest_framework.parsers import MultiPartParser
from rest_framework.request import Request
from rest_framework.response import Response

from glowderma.accounts import services as account_services
from glowderma.accounts.authentication import DeviceHeaders
from glowderma.core import timeutils
from glowderma.core.api.exceptions import field_error
from glowderma.core.idempotency import idempotent
from glowderma.customers import services
from glowderma.customers.api.customer import serializers as s
from glowderma.customers.api.customer.schemas.delete_body import DeleteBodyAutoSchema
from glowderma.customers.api.customer.schemas.me import (
    CONSENT_POST,
    DEVICE_PUT,
    IMAGE_DELETE,
    IMAGE_PUT,
    ME_DELETE,
    ME_GET,
    ME_PATCH,
    PASSWORD_POST,
)
from glowderma.customers.api.customer.views.common import MeBase, client_ip


class MeView(MeBase):
    schema = DeleteBodyAutoSchema()  # DELETE me/ carries the re-authentication body

    @ME_GET
    def get(self, request: Request) -> Response:
        return Response(self.me())

    @ME_PATCH
    @idempotent(required=False)
    def patch(self, request: Request) -> Response:
        serializer = s.MePatchSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        services.update_profile(self.customer.pk, dict(serializer.validated_data), fid=self.fid, sid=self.sid)
        return Response(self.me())

    @ME_DELETE
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
    @CONSENT_POST
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

    @IMAGE_PUT
    @idempotent(required=False)
    def put(self, request: Request) -> Response:
        upload = request.FILES.get("file")
        if upload is None:
            raise ValidationError({"file": [field_error("required")]})
        return Response(services.set_image(self.customer.pk, upload, sid=self.sid))

    @IMAGE_DELETE
    @idempotent(required=False)
    def delete(self, request: Request) -> Response:
        services.delete_image(self.customer.pk, sid=self.sid)
        return Response(status=204)


class DeviceView(MeBase):
    @DEVICE_PUT
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
    @PASSWORD_POST
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


__all__ = [
    "ConsentView",
    "DeviceView",
    "ImageView",
    "MeView",
    "PasswordView",
]
