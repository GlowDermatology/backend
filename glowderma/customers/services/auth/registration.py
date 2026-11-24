"""Customer registration (customer contract §2.1.3), including the reactivation of a deleted account."""

from __future__ import annotations

from typing import Any

from django.db import IntegrityError, transaction

from glowderma.accounts import services as account_services
from glowderma.accounts.models import Customer
from glowderma.core import enums, timeutils
from glowderma.core.api.exceptions import (
    EmailRegistered,
    PhoneRegistered,
)
from glowderma.customers.services.common import identifiers, passwords, published_terms, results, trail


def reactivate_account(customer: Customer, *, name: str, language: str) -> Customer:
    """Reactivate a deleted row (same id, history horizon at ``reactivated_at``) after scrubbing anything a
    write racing its deletion left on it."""
    account_services.scrub_deleted_for_reactivation(customer.pk)
    customer = account_services.reactivate_customer(customer, name=name)
    account_services.move_history_horizon(customer.pk, at=customer.reactivated_at or timeutils.now())
    account_services.set_customer_language(customer.pk, language)
    return customer


def register(
    verification_token: str,
    *,
    password: str,
    name: str,
    terms_version: str,
    language: str,
    device: results.DeviceContext,
    request: Any = None,
) -> results.SignIn:
    """``auth/set-password/``: a new customer, or the reactivation of a deleted one (same id, history horizon
    at ``reactivated_at``); password, consent, the first session with a device key; audit
    ``customer.registered``. The verification token says what the account is created with: a phone, or an
    e-mail address (hybrid identity: verified by the code, no phone until the first checkout or booking; a
    deleted account's address was scrubbed, so an e-mail registration never reactivates)."""
    terms = published_terms.require_published_terms()
    row = account_services.verification_row(verification_token, purpose=enums.OtpPurpose.REGISTER)
    identifier = account_services.row_identifier(row)
    registered = EmailRegistered if row.email else PhoneRegistered
    passwords.validate_customer_password(password, identifier)
    published_terms.check_terms_version(terms_version, terms)
    with transaction.atomic():
        # Lock order Customer → SmsCode (C§2.7.1), as every flow that consumes a verification token.
        customer = account_services.lock_customer_by_phone(identifier)
        account_services.consume_verification(
            verification_token, purpose=enums.OtpPurpose.REGISTER, **identifiers.target_kwargs(identifier)
        )
        reactivated = False
        if customer is not None and customer.status == enums.CustomerStatus.ACTIVE:
            raise registered()
        if customer is None:
            try:
                customer = account_services.create_customer(
                    row.phone or None, email=row.email or None, name=name, language=language
                )
            except IntegrityError as exc:  # a concurrent registration won
                raise registered() from exc
        elif row.email:
            # Reactivation is by phone only (A1); a deleted account's address was scrubbed, so a row still
            # holding it here is a scrub race: refused, never revived through an e-mail code.
            raise registered()
        else:
            customer = reactivate_account(customer, name=name, language=language)
            reactivated = True
        account_services.set_customer_password(customer, password)
        account_services.stamp_consent(customer.pk, terms_version=terms_version, ip=device.ip)
        started = account_services.start_customer_session(
            customer.pk, device.fid, device.platform, device.app_version, issue_device_key=True
        )
        trail.audit_customer(
            "customer.registered",
            customer,
            request,
            {"reactivated": reactivated, "terms_version": terms_version},
        )
        return results.SignIn(started, customer.pk, is_new_customer=not reactivated, reactivated=reactivated)


__all__ = [
    "reactivate_account",
    "register",
]
