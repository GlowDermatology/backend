"""OpenAPI declarations for ``auth/*`` of the customer API (customer contract §2.1): OTP, registration, login
with the new-device code, reset, rotating refresh and logout."""

from __future__ import annotations

from drf_spectacular.utils import extend_schema

from glowderma.core.api.schema import (
    CUSTOMER_ERRORS,
    DEVICE_KEY_HEADER,
)
from glowderma.customers.api.customer import serializers as s
from glowderma.customers.api.customer.schemas.common import AUTH_TOKENS_OR_NEXT_STEP, PARAMETERS

SEND_CODE_POST = extend_schema(
    operation_id="customer_auth_send_code",
    auth=[],
    parameters=PARAMETERS,
    request=s.SendCodeSerializer,
    responses={200: s.CodeIssueSerializer, **CUSTOMER_ERRORS},
)

VERIFY_CODE_POST = extend_schema(
    operation_id="customer_auth_verify_code",
    auth=[],
    parameters=PARAMETERS,
    request=s.VerifyCodeSerializer,
    responses={200: s.VerificationSerializer, **CUSTOMER_ERRORS},
)

SET_PASSWORD_POST = extend_schema(
    operation_id="customer_auth_set_password",
    auth=[],
    parameters=PARAMETERS,
    request=s.SetPasswordSerializer,
    responses={201: s.AuthTokensSerializer, **CUSTOMER_ERRORS},
)

LOGIN_POST = extend_schema(
    operation_id="customer_auth_login",
    auth=[],
    parameters=[*PARAMETERS, DEVICE_KEY_HEADER],
    request=s.CustomerLoginSerializer,
    responses={200: AUTH_TOKENS_OR_NEXT_STEP, **CUSTOMER_ERRORS},
)

RESET_PASSWORD_POST = extend_schema(
    operation_id="customer_auth_reset_password",
    auth=[],
    parameters=PARAMETERS,
    request=s.ResetPasswordSerializer,
    responses={200: s.AuthTokensSerializer, **CUSTOMER_ERRORS},
)

REFRESH_POST = extend_schema(
    operation_id="customer_auth_refresh",
    auth=[],
    parameters=PARAMETERS,
    request=s.CustomerRefreshSerializer,
    responses={200: s.RefreshResponseSerializer, **CUSTOMER_ERRORS},
)

LOGOUT_POST = extend_schema(
    operation_id="customer_auth_logout",
    auth=[],
    parameters=PARAMETERS,
    request=s.CustomerLogoutSerializer,
    responses={204: None, **CUSTOMER_ERRORS},
)


__all__ = [
    "LOGIN_POST",
    "LOGOUT_POST",
    "REFRESH_POST",
    "RESET_PASSWORD_POST",
    "SEND_CODE_POST",
    "SET_PASSWORD_POST",
    "VERIFY_CODE_POST",
]
