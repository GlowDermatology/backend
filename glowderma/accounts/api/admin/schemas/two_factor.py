"""OpenAPI declarations for the admin panel's optional TOTP MFA (``auth/mfa/``) and the sign-in's second
step."""

from __future__ import annotations

from drf_spectacular.utils import extend_schema

from glowderma.accounts.api.admin.schemas.common import ERRORS
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
from glowderma.core.api.schema import (
    ADMIN_ERRORS,
)

MFA_STATUS_GET = extend_schema(
    operation_id="admin_auth_mfa_status",
    description="The caller's admin panel MFA: on or off, and the unused recovery codes.",
    responses={200: MfaStatusSerializer, **ADMIN_ERRORS},
)

MFA_SETUP_POST = extend_schema(
    operation_id="admin_auth_mfa_setup",
    description="Start enabling the admin panel MFA (re-enter the current password): a new pending "
    "TOTP device (an earlier pending one is replaced) as an `otpauth://` URI for the QR code and a "
    "manual key. Wrong password → 400 `password_incorrect` on `current_password` (the "
    "change-password lockout); already on → 409 `conflict`.",
    request=MfaPasswordSerializer,
    responses={200: MfaSetupSerializer, **ADMIN_ERRORS},
)

MFA_CONFIRM_POST = extend_schema(
    operation_id="admin_auth_mfa_confirm",
    description="Confirm the pending device with its first code: MFA is on, the current session counts "
    "as verified, and the ten recovery codes are returned once. Wrong code or not the caller's pending "
    "device → 400 `mfa_code_invalid`; already on → 409; 429 while the code lockout runs.",
    request=MfaConfirmSerializer,
    responses={200: MfaRecoveryCodesSerializer, **ADMIN_ERRORS},
)

MFA_DISABLE_POST = extend_schema(
    operation_id="admin_auth_mfa_disable",
    description="Turn the admin panel MFA off: the current password plus a code or a recovery code. "
    "Wrong password → 400 `password_incorrect`; wrong code → 400 `mfa_code_invalid`; off → 409.",
    request=MfaDisableSerializer,
    responses={200: MfaStatusSerializer, **ADMIN_ERRORS},
)

MFA_RECOVERY_CODES_POST = extend_schema(
    operation_id="admin_auth_mfa_recovery_codes",
    description="Ten new recovery codes (the old ones stop working), returned once: the current password "
    "plus a TOTP code. Errors as `auth/mfa/disable/`.",
    request=MfaRegenerateSerializer,
    responses={200: MfaRecoveryCodesSerializer, **ADMIN_ERRORS},
)

MFA_VERIFY_POST = extend_schema(
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


__all__ = [
    "MFA_CONFIRM_POST",
    "MFA_DISABLE_POST",
    "MFA_RECOVERY_CODES_POST",
    "MFA_SETUP_POST",
    "MFA_STATUS_GET",
    "MFA_VERIFY_POST",
]
