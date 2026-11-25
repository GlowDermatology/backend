"""Customer password reset (customer contract §2.1.5)."""

from __future__ import annotations

from typing import Any

from django.db import transaction

from glowderma.accounts import services as account_services
from glowderma.core import enums, timeutils
from glowderma.core.api.exceptions import (
    EmailNotRegistered,
    PhoneNotRegistered,
)
from glowderma.customers import selectors
from glowderma.customers.services.auth import dormancy
from glowderma.customers.services.common import identifiers, identities, passwords, results, trail


def reset_password(
    verification_token: str, *, password: str, device: results.DeviceContext, request: Any = None
) -> results.SignIn:
    """``auth/reset-password/`` (customer contract §2.1.5): the unauthenticated recovery, by a code sent to
    the phone or to the e-mail address of the account (hybrid identity)."""
    row = account_services.verification_row(verification_token, purpose=enums.OtpPurpose.RESET_PASSWORD)
    identifier = account_services.row_identifier(row)
    not_registered = EmailNotRegistered if row.email else PhoneNotRegistered
    customer = account_services.active_customer_by_phone(identifier)
    if customer is None:
        raise not_registered()
    passwords.validate_customer_password(password, identifier, current_hash=customer.password)
    with transaction.atomic():
        # Lock order Customer → SmsCode (C§2.7.1): the same order as DELETE me/ and me/password/, which
        # consume a reset_password token too.
        customer = account_services.lock_customer(customer.pk)
        account_services.consume_verification(
            verification_token,
            purpose=enums.OtpPurpose.RESET_PASSWORD,
            **identifiers.target_kwargs(identifier),
        )
        if customer.status != enums.CustomerStatus.ACTIVE:
            raise not_registered()
        # The dormant rule protects recycled phone numbers (M2-A16); e-mail addresses are not recycled.
        dormant, last_activity = (
            selectors.is_dormant(customer.pk, now=timeutils.now()) if row.phone else (False, None)
        )
        account_services.set_customer_password(customer, password)
        farewell = account_services.sign_out_customer(
            customer.pk, enums.SessionRevokeReason.PASSWORD_CHANGED, keep_fid=device.fid
        )
        identities.unlink_all_identities(customer, reason="password_reset", request=request)
        account_services.clear_device_keys(customer.pk, except_fid=device.fid)
        if dormant:
            dormancy.dormant_reset(
                customer.pk, lang=device.lang, last_activity=last_activity, request=request
            )
        started = account_services.start_customer_session(
            customer.pk, device.fid, device.platform, device.app_version, issue_device_key=True
        )
        trail.notify_account_security(
            customer.pk,
            enums.AccountSecurityEvent.PASSWORD_RESET,
            farewell_tokens=[*farewell, *started.farewell_tokens],
        )
        trail.audit_customer("customer.password_reset", customer, request)
        trail.alert_by_email(customer.pk, "password_reset")
        return results.SignIn(started, customer.pk)


__all__ = [
    "reset_password",
]
