"""OpenAPI declarations for ``me/phone/*`` and ``me/email/*`` of the customer API (customer contract §2.2):
attaching a verified phone or e-mail."""

from __future__ import annotations

from drf_spectacular.utils import extend_schema

from glowderma.core.api.schema import (
    CUSTOMER_ERRORS,
)
from glowderma.customers.api.customer import serializers as s
from glowderma.customers.api.customer.schemas.common import PARAMETERS

PHONE_CODE_POST = extend_schema(
    operation_id="customer_me_phone_code",
    description=(
        "Send an `attach_phone` SMS code to the phone a phoneless account wants to attach (every OTP "
        "limit of `auth/send-code/`). 409 `conflict` when the account has a phone, `phone_taken` when "
        "another account (active or deleted) holds the number: no merge, the customer signs in with that "
        "phone."
    ),
    parameters=PARAMETERS,
    request=s.MePhoneCodeSerializer,
    responses={200: s.CodeIssueSerializer, **CUSTOMER_ERRORS},
)

PHONE_POST = extend_schema(
    operation_id="customer_me_phone_attach",
    description=(
        "Attach the phone with its SMS code. Re-authentication first: the `password`, or instead (none, "
        "or a forgotten one) a `reset_password` `verification_token` of the account's own phone "
        "or e-mail; only an account with no password, phone or e-mail passes on a session signed in "
        "within 10 minutes, else `reauth_required`. "
        "→ 200 `Me` with the phone set (checkout, pay, booking and the QR open up)."
    ),
    parameters=PARAMETERS,
    request=s.MePhoneSerializer,
    responses={200: s.CustomerMeSerializer, **CUSTOMER_ERRORS},
)

EMAIL_CODE_POST = extend_schema(
    operation_id="customer_me_email_code",
    description=(
        "Send a `verify_email` code to the address to attach (or to replace the current one). 409 "
        "`conflict` when it already is the account's address, `email_taken` when another account has it."
    ),
    parameters=PARAMETERS,
    request=s.MeEmailCodeSerializer,
    responses={200: s.CodeIssueSerializer, **CUSTOMER_ERRORS},
)

EMAIL_POST = extend_schema(
    operation_id="customer_me_email_attach",
    description=(
        "Attach (or replace) the verified e-mail address with its code; re-authentication as "
        "`me/phone/`. → 200 `Me`."
    ),
    parameters=PARAMETERS,
    request=s.MeEmailSerializer,
    responses={200: s.CustomerMeSerializer, **CUSTOMER_ERRORS},
)


__all__ = [
    "EMAIL_CODE_POST",
    "EMAIL_POST",
    "PHONE_CODE_POST",
    "PHONE_POST",
]
