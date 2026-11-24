"""Signed-in re-authentication (customer contract §1.8, §2.2.3, §2.2.8, §2.2.9): a fresh session, the
password or an SMS verification token."""

from __future__ import annotations

from datetime import timedelta

from django.conf import settings

from glowderma.accounts import services as account_services
from glowderma.accounts.models import AuthSession, Customer
from glowderma.accounts.security.login_lockout import LoginGuard
from glowderma.core import enums, timeutils
from glowderma.core.api.exceptions import (
    ReauthRequired,
    VerificationInvalid,
)
from glowderma.customers.services.common import identifiers, passwords


def session_is_fresh(sid: str | account_services.NoSession | None) -> bool:
    """The request's session signed in within ``CUSTOMER_REAUTH_FRESH_SECONDS`` (C§2.2.10): the sign-in that
    opened it is recent enough to stand for a re-authentication. ``created_at`` is the sign-in instant (a
    refresh rotation keeps it)."""
    if sid is None or isinstance(sid, account_services.NoSession):
        return False
    window = int(settings.CUSTOMER_REAUTH_FRESH_SECONDS)
    if window <= 0:
        return False
    since = timeutils.now() - timedelta(seconds=window)
    return AuthSession.objects.filter(
        pk=sid, audience="customer", revoked_at__isnull=True, created_at__gte=since
    ).exists()


def _own_verification_kwargs(customer: Customer, verification_token: str) -> dict[str, str]:
    """``phone=`` / ``email=`` of the account's own target a ``reset_password`` token was sent to (its phone,
    or its e-mail address — hybrid identity); a token of anything else → 400 ``verification_invalid``."""
    row = account_services.verification_row(verification_token, purpose=enums.OtpPurpose.RESET_PASSWORD)
    if customer.phone and row.phone == customer.phone:
        return {"phone": customer.phone}
    if customer.email and row.email and row.email == customer.email:
        return {"email": customer.email}
    raise VerificationInvalid()


def reauthenticate(
    customer: Customer,
    *,
    password: str | None,
    verification_token: str | None,
    secret_field: str = "password",  # noqa: S107 - a field name, not a secret
    sid: str | account_services.NoSession | None = None,
) -> bool:
    """The password checks of a signed-in customer: ``customer_reauth`` guard (checked and reset), each
    failure also counted in ``customer_login`` (never checked). A social-only account needs a
    ``reset_password`` verification token of its own phone (or, hybrid identity, of its own e-mail address),
    which is only checked here and consumed by the caller under the customer-level lock. Returns whether a
    verification token must be consumed.

    An account with a password may give that same token instead of the password ("forgot the password",
    C§2.2.10): a code of its own phone or e-mail is exactly the proof ``auth/reset-password/`` accepts to
    replace the password, so it adds no way in. A given password is always the one checked.

    Hybrid identity (C§2.2.10): only an account with no password, phone or e-mail (a phoneless social
    sign-up) has nothing to re-authenticate with — a session signed in within
    ``CUSTOMER_REAUTH_FRESH_SECONDS`` passes, anything else answers 409 ``reauth_required`` (the app signs in
    with the provider again). Every other account proves itself with its password or a code of its own phone
    or e-mail, however fresh the session: a social sign-in alone (no new-device step, M2-A10) must never add
    a way in to a password account. Every failure also counts against the account in ``customer_login``."""
    if customer.password:
        if not password and verification_token and identifiers.customer_identifier(customer):
            _own_verification_kwargs(customer, verification_token)
            return True
        if not password:
            raise passwords.field_refusal(secret_field, "required")
        guard = LoginGuard()
        key = str(customer.pk)
        guard.check(identifiers.REAUTH_SCOPE, key)
        if not account_services.check_customer_password(customer, password):
            guard.record_failure(identifiers.REAUTH_SCOPE, key)
            guard.record_failure(identifiers.LOGIN_SCOPE, identifiers.customer_guard_key(customer.pk))
            identifier = identifiers.customer_identifier(customer)
            if identifier:
                guard.record_failure(identifiers.LOGIN_SCOPE, identifiers.phone_guard_key(identifier))
            raise passwords.field_refusal(secret_field, "password_incorrect", "field.password_incorrect")
        guard.reset(identifiers.REAUTH_SCOPE, key)
        return False
    if not identifiers.customer_identifier(customer):
        if session_is_fresh(sid):
            return False
        raise ReauthRequired()
    if not verification_token:
        raise passwords.field_refusal("verification_token", "required")
    _own_verification_kwargs(customer, verification_token)
    return True


def _consume_reauth(customer: Customer, verification_token: str | None, needed: bool) -> None:
    if needed:
        token = str(verification_token)
        account_services.consume_verification(
            token,
            purpose=enums.OtpPurpose.RESET_PASSWORD,
            **_own_verification_kwargs(customer, token),
        )


def lock_reauthenticated(
    customer_id: int,
    sid: str | account_services.NoSession,
    snapshot: Customer,
    verification_token: str | None,
    needed: bool,
    *,
    secret_field: str = "password",  # noqa: S107 - a field name, not a secret
) -> Customer:
    """Take the signed-in lock (:func:`accounts.services.lock_signed_in_customer`: active customer, open
    session) and re-check the re-authentication against the locked row: the hash the password was checked
    against (or its absence, for a verification token) is still the stored one — a reset or another password
    change committing meanwhile wins. Then consume the verification token (Customer → SmsCode, C§2.7.1)."""
    customer = account_services.lock_signed_in_customer(customer_id, sid)
    if customer.password != snapshot.password:
        if not snapshot.password:
            raise passwords.field_refusal(secret_field, "required")
        if needed:  # proved by a code, not the password: a password change meanwhile wins, ask anew
            raise VerificationInvalid()
        raise passwords.field_refusal(secret_field, "password_incorrect", "field.password_incorrect")
    _consume_reauth(customer, verification_token, needed)
    return customer


__all__ = [
    "lock_reauthenticated",
    "reauthenticate",
    "session_is_fresh",
]
