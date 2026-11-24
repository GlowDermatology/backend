"""Customer social sign-in (customer contract §2.1.8): a linked identity or a trusted e-mail signs in,
anything else asks for the phone."""

from __future__ import annotations

import secrets
from typing import Any

from django.conf import settings
from django.core import signing
from django.core.cache import cache
from django.db import IntegrityError, transaction

from glowderma.accounts import services as account_services
from glowderma.accounts.models import CustomerSocialIdentity
from glowderma.core import enums
from glowderma.core.pii import pii_ref
from glowderma.customers.services.common import results, trail
from glowderma.customers.services.social import tickets, tokens, trusted_email
from glowderma.integrations.oauth.ports import AppTokenClaims


def social_sign_in(
    provider: str,
    token: str,
    token_type: str | None,
    nonce: str | None,
    authorization_code: str | None,
    *,
    device: results.DeviceContext,
    request: Any = None,
) -> results.SignIn | results.PhoneStep:
    """``auth/social/``: a linked identity of an active customer signs in (no new-device SMS, M2-A10), an
    unknown one gets the ``verify_phone`` ticket (Apple's code is exchanged now, the sealed token waits in
    Redis for the completion).

    Accepted residual: a ticket that is never completed leaves its Apple token unrevoked when the cache
    entry expires (``SOCIAL_TICKET_TTL_SECONDS``); a completion refused for good revokes it
    (:func:`complete_social`).
    """
    claims = tokens.verify_social_token(provider, token, token_type, nonce, fid=device.fid)
    identity = (
        CustomerSocialIdentity.objects.select_related("customer")
        .filter(provider=provider, subject=claims.subject)
        .first()
    )
    sealed = client_id = ""
    if identity is not None and identity.customer.status == enums.CustomerStatus.ACTIVE:
        if provider == enums.CustomerSocialProvider.APPLE and not identity.provider_refresh_token:
            sealed, client_id = tokens.exchange_apple_code(authorization_code, claims)
        signed = _sign_in_linked(
            identity, provider, sealed, client_id, claims, device=device, request=request
        )
        if signed is not None:
            return signed
        # The identity was unlinked (reset, dormant rule, unlink) or the account deleted meanwhile: it is an
        # unknown identity now and gets the verify_phone ticket (an Apple code already exchanged is reused).
    if claims.trusted_email:
        signed = _sign_in_by_trusted_email(provider, claims, device=device, request=request)
        if signed is not None:
            return signed
    jti = secrets.token_hex(16)
    ttl = int(settings.SOCIAL_TICKET_TTL_SECONDS)
    if provider == enums.CustomerSocialProvider.APPLE:
        if not sealed:
            sealed, client_id = tokens.exchange_apple_code(authorization_code, claims)
        cache.set(tickets.SOCIAL_APPLE_KEY.format(jti=jti), {"sealed": sealed, "client_id": client_id}, ttl)
    ticket = signing.TimestampSigner(salt=tickets.SOCIAL_TICKET_SALT).sign_object(
        {
            "provider": provider,
            "subject": claims.subject,
            "email": claims.email,
            "jti": jti,
            "fid": pii_ref(device.fid),
            "trusted": claims.trusted_email,
        }
    )
    return results.PhoneStep(ticket, provider, claims.email, ttl)


def _sign_in_by_trusted_email(
    provider: str, claims: AppTokenClaims, *, device: results.DeviceContext, request: Any
) -> results.SignIn | None:
    """One mailbox, one account: an unknown identity whose provider vouches for its address
    (``trusted_email``, Google's verified Gmail / Workspace rule) is linked to the active account holding
    that address and signs in, with no ticket, code or password — whoever holds the mailbox can already
    reset that account's password by e-mail. Every account address was proven by a code, so no one can
    pre-register someone else's address. ``None`` (the ticket flow runs) when no active account holds the
    address or that account already has an identity of this provider; nothing is written then.
    Lock order Customer → identity → devices → sessions (C§2.7.1)."""
    email = account_services.normalize_email(claims.email)
    candidate = account_services.active_customer_by_email(email)
    if candidate is None:
        return None
    with transaction.atomic():
        customer = account_services.lock_customer(candidate.pk)
        if customer.status != enums.CustomerStatus.ACTIVE or customer.email != email:
            return None
        if CustomerSocialIdentity.objects.filter(customer_id=customer.pk, provider=provider).exists():
            return None
        if CustomerSocialIdentity.objects.filter(provider=provider, subject=claims.subject).exists():
            return None
        try:
            account_services.link_customer_identity(
                customer.pk, provider=provider, subject=claims.subject, email=email, signed_in=True
            )
        except IntegrityError:
            return None
        started = account_services.start_customer_session(
            customer.pk,
            device.fid,
            device.platform,
            device.app_version,
            issue_device_key=True,
            auth_method=provider,
        )
        trail.notify_account_security(
            customer.pk,
            enums.AccountSecurityEvent.SOCIAL_LINKED,
            provider=provider,
            farewell_tokens=started.farewell_tokens,
        )
        trail.audit_customer(
            "customer.social_linked", customer, request, {"provider": provider, "method": "verified_email"}
        )
        trail.audit_customer("customer.login_succeeded", customer, request, {"method": provider})
        return results.SignIn(started, customer.pk)


def _sign_in_linked(
    identity: CustomerSocialIdentity,
    provider: str,
    sealed: str,
    client_id: str,
    claims: AppTokenClaims,
    *,
    device: results.DeviceContext,
    request: Any,
) -> results.SignIn | None:
    """The linked-identity sign-in under the customer-level lock first (C§2.7.1: Customer → identity →
    devices → sessions, the order of unlink, reset, the dormant rule and ``DELETE me/``), with the identity
    and the customer re-read under it; ``None`` when either changed meanwhile (nothing written)."""
    with transaction.atomic():
        customer = account_services.lock_customer(identity.customer_id)
        if customer.status != enums.CustomerStatus.ACTIVE:
            return None
        if not account_services.touch_identity_sign_in(
            identity.pk, customer.pk, sealed_token=sealed, client_id=client_id
        ):
            return None
        # Repairs social-only accounts created before the address was kept on sign-up. Never an account with
        # a phone or password: the identity alone must not add a way to reset it (review B-W2 #1).
        if claims.trusted_email and not customer.phone and not customer.password:
            trusted_email.adopt_trusted_email(customer, claims.email)
        started = account_services.start_customer_session(
            customer.pk,
            device.fid,
            device.platform,
            device.app_version,
            issue_device_key=True,
            auth_method=provider,
        )
        trail.audit_customer("customer.login_succeeded", customer, request, {"method": provider})
        return results.SignIn(started, customer.pk)


__all__ = [
    "social_sign_in",
]
