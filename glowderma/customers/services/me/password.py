"""Changing (or first setting) the signed-in customer's password (customer contract §2.2.9)."""

from __future__ import annotations

from typing import Any

from django.db import transaction

from glowderma.accounts import services as account_services
from glowderma.accounts.models import Customer
from glowderma.core import enums
from glowderma.core.api.exceptions import (
    CustomerError,
)
from glowderma.customers.services.common import identifiers, passwords, trail
from glowderma.customers.services.me import reauth


def change_password(
    customer_id: int,
    *,
    current_password: str | None,
    verification_token: str | None,
    password: str,
    sid: str,
    request: Any = None,
) -> None:
    """``POST me/password/`` (customer contract §2.2.9): set or change the password while signed in; the
    current session, its device key and every identity are kept; other sessions → ``password_changed``.
    Under the lock the caller's session must still be open (a reset revokes it) and the checked hash
    current, so a racing reset's new password is never overwritten. An account without a phone or an e-mail
    (a phoneless social sign-up) cannot sign in with a password: 409 ``phone_or_email_required``."""
    snapshot = Customer.objects.get(pk=customer_id)
    if not identifiers.customer_identifier(snapshot):
        raise CustomerError("phone_or_email_required", 409)
    needed = reauth.reauthenticate(
        snapshot,
        password=current_password,
        verification_token=verification_token,
        secret_field="current_password",  # noqa: S106 - a field name
        sid=sid,
    )
    passwords.validate_customer_password(
        password, identifiers.customer_identifier(snapshot), current_hash=snapshot.password
    )
    first = not snapshot.password
    with transaction.atomic():
        customer = reauth.lock_reauthenticated(
            customer_id,
            sid,
            snapshot,
            verification_token,
            needed,
            secret_field="current_password",  # noqa: S106 - a field name
        )
        account_services.set_customer_password(customer, password)
        farewell = account_services.sign_out_customer(
            customer_id, enums.SessionRevokeReason.PASSWORD_CHANGED, except_sid=sid
        )
        trail.notify_account_security(
            customer_id, enums.AccountSecurityEvent.PASSWORD_CHANGED, farewell_tokens=farewell
        )
        trail.audit_customer("customer.password_changed", customer, request, {"first_password": first})
        trail.alert_by_email(customer_id, "password_changed")


__all__ = [
    "change_password",
]
