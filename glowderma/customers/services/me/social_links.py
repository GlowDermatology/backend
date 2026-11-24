"""Linking and unlinking a social identity of the signed-in customer (customer contract §2.2.8)."""

from __future__ import annotations

from typing import Any

from django.db import IntegrityError, transaction

from glowderma.accounts import services as account_services
from glowderma.accounts.models import Customer, CustomerSocialIdentity
from glowderma.core import enums
from glowderma.core.api.exceptions import (
    LastSignInMethod,
    SocialIdentityLinked,
    SocialProviderLinked,
)
from glowderma.customers.services.common import identities, trail
from glowderma.customers.services.me import contacts, reauth
from glowderma.customers.services.social import apple_revocation, tokens
from glowderma.integrations.oauth import registry as oauth_registry


def link_social(
    customer_id: int,
    provider: str,
    token: str,
    token_type: str | None,
    nonce: str | None,
    authorization_code: str | None,
    *,
    password: str | None,
    verification_token: str | None,
    fid: str,
    sid: str | account_services.NoSession,
    request: Any = None,
) -> None:
    """``POST me/social/`` (customer contract §2.2.8). The slow provider checks run before any lock; under
    the lock the account must still be active, the session open and the re-authentication current, so an
    identity never lands on an account deleted, reset or taken over meanwhile (C§6.17). An Apple token
    exchanged for a refused link is revoked."""
    oauth_registry.get_app_verifier(provider)  # 404 first
    snapshot = Customer.objects.get(pk=customer_id)
    needed = reauth.reauthenticate(
        snapshot, password=password, verification_token=verification_token, sid=sid
    )
    claims = tokens.verify_social_token(provider, token, token_type, nonce, fid=fid)
    sealed = client_id = ""
    if provider == enums.CustomerSocialProvider.APPLE:
        sealed, client_id = tokens.exchange_apple_code(authorization_code, claims)
    label = tokens.PROVIDER_LABELS[provider]
    try:
        with transaction.atomic():
            customer = reauth.lock_reauthenticated(customer_id, sid, snapshot, verification_token, needed)
            if CustomerSocialIdentity.objects.filter(customer_id=customer_id, provider=provider).exists():
                raise SocialProviderLinked(details={"provider": provider}, params={"provider": label})
            if CustomerSocialIdentity.objects.filter(provider=provider, subject=claims.subject).exists():
                raise SocialIdentityLinked(details={"provider": provider}, params={"provider": label})
            try:
                account_services.link_customer_identity(
                    customer_id,
                    provider=provider,
                    subject=claims.subject,
                    email=claims.email,
                    sealed_token=sealed,
                    client_id=client_id,
                )
            except IntegrityError as exc:
                raise SocialIdentityLinked(
                    details={"provider": provider}, params={"provider": label}
                ) from exc
            trail.notify_account_security(
                customer_id, enums.AccountSecurityEvent.SOCIAL_LINKED, provider=provider
            )
            trail.audit_customer(
                "customer.social_linked", customer, request, {"provider": provider, "via": "settings"}
            )
    except Exception:
        if sealed:
            apple_revocation.revoke_refused_apple_token(
                apple_revocation.RefusedAppleToken(sealed, client_id, customer_id, claims.subject)
            )
        raise


def unlink_social(
    customer_id: int,
    provider: str,
    *,
    password: str | None,
    verification_token: str | None,
    sid: str | account_services.NoSession,
    request: Any = None,
) -> None:
    """``DELETE me/social/{provider}/`` (customer contract §2.2.8): re-authentication first, then the
    idempotent no-op, ``last_sign_in_method``, the unlink (Apple revoked) and the notice."""
    snapshot = Customer.objects.get(pk=customer_id)
    needed = reauth.reauthenticate(
        snapshot, password=password, verification_token=verification_token, sid=sid
    )
    with transaction.atomic():
        customer = reauth.lock_reauthenticated(customer_id, sid, snapshot, verification_token, needed)
        identity = CustomerSocialIdentity.objects.filter(customer_id=customer_id, provider=provider).first()
        if identity is None:
            return
        if (
            not contacts.password_signs_in(customer)
            and CustomerSocialIdentity.objects.filter(customer_id=customer_id).count() <= 1
        ):
            raise LastSignInMethod(details={"provider": provider})
        identities.unlink_identity(identity, reason="customer", customer=customer, request=request)
        trail.notify_account_security(
            customer_id, enums.AccountSecurityEvent.SOCIAL_UNLINKED, provider=provider
        )


__all__ = [
    "link_social",
    "unlink_social",
]
