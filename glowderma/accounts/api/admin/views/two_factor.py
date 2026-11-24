"""The admin panel's optional TOTP MFA (``auth/mfa/``) and the sign-in's second step."""

from __future__ import annotations

from typing import Any, cast

from rest_framework.request import Request
from rest_framework.response import Response

from glowderma.accounts import mfa, services
from glowderma.accounts.api.admin.payloads import access_payload, me_payload, mfa_status_payload
from glowderma.accounts.api.admin.schemas.two_factor import (
    MFA_CONFIRM_POST,
    MFA_DISABLE_POST,
    MFA_RECOVERY_CODES_POST,
    MFA_SETUP_POST,
    MFA_STATUS_GET,
    MFA_VERIFY_POST,
)
from glowderma.accounts.api.admin.serializers import (
    MfaConfirmSerializer,
    MfaDisableSerializer,
    MfaPasswordSerializer,
    MfaRegenerateSerializer,
    MfaVerifySerializer,
)
from glowderma.accounts.api.base import PublicAuthAPIView, WorkforceAPIView
from glowderma.accounts.api.throttles import (
    MfaVerifyIPThrottle,
)
from glowderma.accounts.models import StaffUser


def _session_id(request: Request) -> Any:
    return cast(dict[str, Any], request.auth)["sid"]


class MfaStatusView(WorkforceAPIView):
    @MFA_STATUS_GET
    def get(self, request: Request) -> Response:
        return Response(mfa_status_payload(cast(StaffUser, request.user)))


class MfaSetupView(WorkforceAPIView):
    @MFA_SETUP_POST
    def post(self, request: Request) -> Response:
        user = cast(StaffUser, request.user)
        mfa.check_password_lockout(user)
        body = MfaPasswordSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        mfa.check_current_password(user, body.validated_data["current_password"])
        setup = mfa.begin_setup(user)
        return Response(
            {"device_id": setup.device_id, "otpauth_uri": setup.otpauth_uri, "manual_key": setup.manual_key}
        )


class MfaConfirmView(WorkforceAPIView):
    @MFA_CONFIRM_POST
    def post(self, request: Request) -> Response:
        body = MfaConfirmSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        codes = mfa.confirm_setup(
            cast(StaffUser, request.user),
            body.validated_data["device_id"],
            body.validated_data["code"],
            session_id=_session_id(request),
            request=request,
        )
        return Response({"recovery_codes": codes})


class MfaDisableView(WorkforceAPIView):
    @MFA_DISABLE_POST
    def post(self, request: Request) -> Response:
        user = cast(StaffUser, request.user)
        mfa.check_password_lockout(user)
        body = MfaDisableSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        mfa.check_current_password(user, body.validated_data["current_password"])
        mfa.disable(user, code_or_recovery=body.validated_data["code_or_recovery"], request=request)
        return Response(mfa_status_payload(user))


class MfaRecoveryCodesView(WorkforceAPIView):
    @MFA_RECOVERY_CODES_POST
    def post(self, request: Request) -> Response:
        user = cast(StaffUser, request.user)
        mfa.check_password_lockout(user)
        body = MfaRegenerateSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        mfa.check_current_password(user, body.validated_data["current_password"])
        codes = mfa.regenerate_recovery_codes(user, code=body.validated_data["code"], request=request)
        return Response({"recovery_codes": codes})


class MfaVerifyView(PublicAuthAPIView):
    throttle_classes = [MfaVerifyIPThrottle]

    @MFA_VERIFY_POST
    def post(self, request: Request) -> Response:
        body = MfaVerifySerializer(data=request.data)
        body.is_valid(raise_exception=True)
        data = body.validated_data
        session, refresh = services.complete_mfa_login(
            data["challenge"],
            code=data.get("code") or None,
            recovery_code=data.get("recovery_code") or None,
            device_label=request.META.get("HTTP_USER_AGENT", "")[:100],
            request=request,
        )
        user = cast(StaffUser, session.staff)
        return Response({**access_payload(session), "refresh": refresh, "user": me_payload(user, session)})


__all__ = [
    "MfaConfirmView",
    "MfaDisableView",
    "MfaRecoveryCodesView",
    "MfaSetupView",
    "MfaStatusView",
    "MfaVerifyView",
]
