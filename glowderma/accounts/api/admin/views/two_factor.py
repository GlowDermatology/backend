"""The admin panel's optional TOTP MFA (``auth/mfa/``) and the sign-in's second step."""

from __future__ import annotations

from typing import Any, cast

from drf_spectacular.utils import extend_schema
from rest_framework.request import Request
from rest_framework.response import Response

from glowderma.accounts import mfa, services
from glowderma.accounts.api.admin.payloads import access_payload, me_payload, mfa_status_payload
from glowderma.accounts.api.admin.serializers import (
    LoginResponseSerializer,
    MfaConfirmSerializer,
    MfaDisableSerializer,
    MfaPasswordSerializer,
    MfaRecoveryCodesSerializer,
    MfaRegenerateSerializer,
    MfaSetupSerializer,
    MfaStatusSerializer,
    MfaVerifySerializer,
)
from glowderma.accounts.api.admin.views.schema import ERRORS
from glowderma.accounts.api.base import PublicAuthAPIView, WorkforceAPIView
from glowderma.accounts.api.throttles import (
    MfaVerifyIPThrottle,
)
from glowderma.accounts.models import StaffUser
from glowderma.core.api.schema import (
    ADMIN_ERRORS,
)


def _session_id(request: Request) -> Any:
    return cast(dict[str, Any], request.auth)["sid"]


class MfaStatusView(WorkforceAPIView):
    @extend_schema(
        operation_id="admin_auth_mfa_status",
        description="The caller's admin panel MFA: on or off, and the unused recovery codes.",
        responses={200: MfaStatusSerializer, **ADMIN_ERRORS},
    )
    def get(self, request: Request) -> Response:
        return Response(mfa_status_payload(cast(StaffUser, request.user)))


class MfaSetupView(WorkforceAPIView):
    @extend_schema(
        operation_id="admin_auth_mfa_setup",
        description="Start enabling the admin panel MFA (re-enter the current password): a new pending "
        "TOTP device (an earlier pending one is replaced) as an `otpauth://` URI for the QR code and a "
        "manual key. Wrong password → 400 `password_incorrect` on `current_password` (the "
        "change-password lockout); already on → 409 `conflict`.",
        request=MfaPasswordSerializer,
        responses={200: MfaSetupSerializer, **ADMIN_ERRORS},
    )
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
    @extend_schema(
        operation_id="admin_auth_mfa_confirm",
        description="Confirm the pending device with its first code: MFA is on, the current session counts "
        "as verified, and the ten recovery codes are returned once. Wrong code or not the caller's pending "
        "device → 400 `mfa_code_invalid`; already on → 409; 429 while the code lockout runs.",
        request=MfaConfirmSerializer,
        responses={200: MfaRecoveryCodesSerializer, **ADMIN_ERRORS},
    )
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
    @extend_schema(
        operation_id="admin_auth_mfa_disable",
        description="Turn the admin panel MFA off: the current password plus a code or a recovery code. "
        "Wrong password → 400 `password_incorrect`; wrong code → 400 `mfa_code_invalid`; off → 409.",
        request=MfaDisableSerializer,
        responses={200: MfaStatusSerializer, **ADMIN_ERRORS},
    )
    def post(self, request: Request) -> Response:
        user = cast(StaffUser, request.user)
        mfa.check_password_lockout(user)
        body = MfaDisableSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        mfa.check_current_password(user, body.validated_data["current_password"])
        mfa.disable(user, code_or_recovery=body.validated_data["code_or_recovery"], request=request)
        return Response(mfa_status_payload(user))


class MfaRecoveryCodesView(WorkforceAPIView):
    @extend_schema(
        operation_id="admin_auth_mfa_recovery_codes",
        description="Ten new recovery codes (the old ones stop working), returned once: the current password "
        "plus a TOTP code. Errors as `auth/mfa/disable/`.",
        request=MfaRegenerateSerializer,
        responses={200: MfaRecoveryCodesSerializer, **ADMIN_ERRORS},
    )
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

    @extend_schema(
        operation_id="admin_auth_mfa_verify",
        description="Public (no bearer): the sign-in's second step. The challenge of `auth/login/` or "
        "`auth/oauth/{provider}/` plus exactly one of `code` / `recovery_code` → the normal sign-in answer "
        "(session `auth_method` = the first step's). Wrong code → 400 `mfa_code_invalid` (5 attempts per "
        "challenge); unknown, expired (5 min), used or exhausted challenge, or a password change / "
        "deactivation / MFA reset since → 400 `mfa_challenge_invalid` (sign in again); 429 while the "
        "account's code lockout runs or for the per-IP limit.",
        auth=[],
        request=MfaVerifySerializer,
        responses={200: LoginResponseSerializer, **ERRORS},
    )
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
