"""Completing a social sign-in with the verified phone (customer contract §2.1.9): link the identity to the
account of that phone, or register one."""

from __future__ import annotations

import logging
from typing import Any

from django.conf import settings
from django.core.cache import cache
from django.db import IntegrityError, transaction

from glowderma.accounts import services as account_services
from glowderma.accounts.models import Customer, CustomerSocialIdentity, SmsCode
from glowderma.accounts.security.login_lockout import LoginGuard
from glowderma.core import enums, timeutils
from glowderma.core.api.exceptions import (
    EmailRegistered,
    PhoneRegistered,
    SocialIdentityLinked,
    SocialProviderLinked,
    SocialTicketInvalid,
    VerificationInvalid,
)
from glowderma.customers import selectors
from glowderma.customers.services.auth import dormancy, registration
from glowderma.customers.services.common import identifiers, passwords, published_terms, results, trail
from glowderma.customers.services.social import apple_revocation, sign_in, tickets, tokens

logger = logging.getLogger("glowderma.customers.services")


def complete_social(
    ticket: str,
    verification_token: str | None,
    *,
    password: str | None,
    name: str | None,
    terms_version: str | None,
    language: str | None,
    device: results.DeviceContext,
    request: Any = None,
) -> results.SignIn:
    """``auth/social/complete/`` (customer contract §2.1.9). A final refusal (409 ``social_provider_linked``
    / ``social_identity_linked``) drops the waiting Apple token and revokes it in its own transaction (never
    while its subject is linked, C§2.1.8); other refusals keep it for a retry of the same ticket.

    Hybrid identity: the ``link_social`` verification token proves a phone (SMS) or an e-mail address
    (e-mail code) and links the identity to that account, or creates one with it. Without a verification
    token the identity signs up a new account **without a phone** (name and terms required): the phone is
    asked at the first checkout, payment, booking or QR (409 ``phone_required``); nothing is ever merged
    into an existing account this way."""
    social = tickets.read_social_ticket(ticket, fid=device.fid)
    row: SmsCode | None = None
    if verification_token:
        row = account_services.verification_row(verification_token, purpose=enums.OtpPurpose.LINK_SOCIAL)
        if cache.get(account_services.social_otp_key(row.pk)) != social.jti:
            raise VerificationInvalid()
    apple: dict[str, str] | None = None
    if social.provider == enums.CustomerSocialProvider.APPLE:
        stored = cache.get(tickets.SOCIAL_APPLE_KEY.format(jti=social.jti))
        if not isinstance(stored, dict) or not stored.get("sealed"):
            raise SocialTicketInvalid()
        apple = stored
    identifier = account_services.row_identifier(row) if row is not None else ""
    customer = (
        Customer.objects.filter(**identifiers.target_kwargs(identifier)).first() if identifier else None
    )
    active = customer is not None and customer.status == enums.CustomerStatus.ACTIVE
    terms = None
    if not active:
        if not name or len(name.strip()) < 2:
            raise (
                passwords.field_refusal("name", "required")
                if not name
                else passwords.field_refusal("name", "min_length", "field.name_min")
            )
        if not terms_version:
            raise passwords.field_refusal("terms_version", "required")
        terms = published_terms.require_published_terms()
        published_terms.check_terms_version(terms_version, terms)
    checked_hash = ""  # the stored hash the password was checked against ("" = none checked)
    if active and customer is not None and customer.password:
        if not password:
            raise passwords.field_refusal("password", "required")
        guard = LoginGuard()
        key = identifiers.phone_guard_key(identifier)
        account_key = identifiers.customer_guard_key(customer.pk)
        guard.check(identifiers.LOGIN_SCOPE, key)
        guard.check(identifiers.LOGIN_SCOPE, account_key)
        if not account_services.check_customer_password(customer, password):
            guard.record_failure(identifiers.LOGIN_SCOPE, key)
            guard.record_failure(identifiers.LOGIN_SCOPE, account_key)
            raise passwords.field_refusal("password", "password_incorrect", "field.password_incorrect")
        guard.reset(identifiers.LOGIN_SCOPE, key)
        guard.reset(identifiers.LOGIN_SCOPE, account_key)
        checked_hash = customer.password
    used_key = tickets.SOCIAL_TICKET_USED_KEY.format(jti=social.jti)
    if not cache.add(used_key, 1, int(settings.SOCIAL_TICKET_TTL_SECONDS)):
        raise SocialTicketInvalid()
    try:
        result = _complete_social(
            social,
            verification_token or "",
            identifier,
            active,
            apple,
            name,
            terms_version,
            language,
            device,
            request,
            checked_hash=checked_hash,
        )
    except SocialProviderLinked, SocialIdentityLinked:
        # Final for this ticket: the phone's account has this provider, or the identity is linked elsewhere.
        # The waiting Apple token is dropped and revoked (unless its subject is linked: C§2.1.8).
        _best_effort(cache.delete, used_key)
        if apple is not None:
            _best_effort(cache.delete, tickets.SOCIAL_APPLE_KEY.format(jti=social.jti))
            apple_revocation.revoke_refused_apple_token(
                apple_revocation.RefusedAppleToken(
                    apple["sealed"],
                    apple.get("client_id", ""),
                    customer.pk if customer is not None else None,
                    social.subject,
                )
            )
        raise
    except Exception:
        _best_effort(cache.delete, used_key)  # never replaces the original error
        raise
    # Everything committed: a Redis error here must not lose the tokens the client is about to receive.
    _best_effort(cache.delete, tickets.SOCIAL_APPLE_KEY.format(jti=social.jti))
    return result


def _best_effort(operation: Any, *args: Any) -> None:
    try:
        operation(*args)
    except Exception:
        logger.warning("cache cleanup failed: %s", getattr(operation, "__name__", operation), exc_info=True)


def _trusted_signup_email(social: tickets.SocialTicket, identifier: str) -> str | None:
    """The address a sign-up without a verification token keeps: the provider-vouched one of the ticket
    (``trusted``), unless another account holds it (it would have signed in by it, so that account appeared
    meanwhile: the new account keeps no address rather than failing). ``None`` with a verified phone (its
    account is created with the phone) or an untrusted ticket."""
    if identifier or not social.trusted or not social.email:
        return None
    email = account_services.normalize_email(social.email)
    if Customer.objects.filter(email=email).exists():
        return None
    return email


def _complete_social(
    social: tickets.SocialTicket,
    verification_token: str,
    identifier: str,
    active: bool,
    apple: dict[str, str] | None,
    name: str | None,
    terms_version: str | None,
    language: str | None,
    device: results.DeviceContext,
    request: Any,
    *,
    checked_hash: str,
) -> results.SignIn:
    with transaction.atomic():
        # Lock order Customer → SmsCode (C§2.7.1). No identifier: a phoneless sign-up (nothing to lock).
        customer = account_services.lock_customer_by_phone(identifier) if identifier else None
        if identifier:
            account_services.consume_verification(
                verification_token,
                purpose=enums.OtpPurpose.LINK_SOCIAL,
                **identifiers.target_kwargs(identifier),
            )
        created = reactivated = False
        if customer is not None and customer.status == enums.CustomerStatus.ACTIVE:
            if not active:  # registered meanwhile: the name/terms branch no longer applies
                raise EmailRegistered() if identifiers.is_email(identifier) else PhoneRegistered()
            if customer.password != checked_hash:
                # A password was set or changed after the unlocked check (C§2.1.9, §6.16: the SMS code plus
                # the password when one is set): the client retries with the current password.
                if not checked_hash:
                    raise passwords.field_refusal("password", "required")
                raise passwords.field_refusal("password", "password_incorrect", "field.password_incorrect")
            if not customer.password and not identifiers.is_email(
                identifier
            ):  # recycled numbers only (M2-A16)
                dormant, last_activity = selectors.is_dormant(customer.pk, now=timeutils.now())
                if dormant:
                    dormancy.dormant_reset(
                        customer.pk, lang=device.lang, last_activity=last_activity, request=request
                    )
        else:
            if active:  # deleted meanwhile
                raise VerificationInvalid()
            if customer is None:
                email = (
                    identifier
                    if identifiers.is_email(identifier)
                    else _trusted_signup_email(social, identifier)
                )
                try:
                    customer = account_services.create_customer(
                        identifier if identifier and not identifiers.is_email(identifier) else None,
                        email=email,
                        name=(name or "").strip(),
                        language=language or device.lang,
                    )
                except IntegrityError as exc:
                    raise (EmailRegistered() if email else PhoneRegistered()) from exc
                created = True
            elif identifiers.is_email(identifier):  # reactivation is by phone only (A1)
                raise EmailRegistered()
            else:
                customer = registration.reactivate_account(
                    customer, name=(name or "").strip(), language=language or device.lang
                )
                reactivated = True
            account_services.stamp_consent(customer.pk, terms_version=str(terms_version), ip=device.ip)
        label = tokens.PROVIDER_LABELS[social.provider]
        if CustomerSocialIdentity.objects.filter(customer_id=customer.pk, provider=social.provider).exists():
            raise SocialProviderLinked(details={"provider": social.provider}, params={"provider": label})
        if CustomerSocialIdentity.objects.filter(provider=social.provider, subject=social.subject).exists():
            raise SocialIdentityLinked(details={"provider": social.provider}, params={"provider": label})
        try:
            account_services.link_customer_identity(
                customer.pk,
                provider=social.provider,
                subject=social.subject,
                email=social.email,
                sealed_token=apple["sealed"] if apple else "",
                client_id=apple["client_id"] if apple else "",
                signed_in=True,
            )
        except IntegrityError as exc:
            raise SocialIdentityLinked(
                details={"provider": social.provider}, params={"provider": label}
            ) from exc
        if social.trusted:
            sign_in.adopt_trusted_email(customer, social.email)
        started = account_services.start_customer_session(
            customer.pk,
            device.fid,
            device.platform,
            device.app_version,
            issue_device_key=True,
            auth_method=social.provider,
        )
        if not created and not reactivated:
            trail.notify_account_security(
                customer.pk,
                enums.AccountSecurityEvent.SOCIAL_LINKED,
                provider=social.provider,
                farewell_tokens=started.farewell_tokens,
            )
        trail.audit_customer("customer.social_linked", customer, request, {"provider": social.provider})
        if created or reactivated:
            trail.audit_customer(
                "customer.registered",
                customer,
                request,
                {"reactivated": reactivated, "terms_version": terms_version},
            )
        return results.SignIn(started, customer.pk, is_new_customer=created, reactivated=reactivated)


__all__ = [
    "complete_social",
]
