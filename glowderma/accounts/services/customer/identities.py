"""Customer social identities (customer contract §2.1.8): linking and unlinking, the Apple refresh token
sealed at rest and its revocation, and the replay guards of a social sign-in (nonce, seen ID token).
"""

from __future__ import annotations

import secrets
from typing import Any

from django.conf import settings
from django.core.cache import cache
from django.db import transaction

from glowderma.accounts import topics
from glowderma.accounts.models import CustomerSocialIdentity
from glowderma.accounts.services.common import hashing
from glowderma.core import enums, outbox, secretbox, timeutils
from glowderma.core.pii import pii_ref


def apple_client_id(identity: CustomerSocialIdentity | None = None) -> str:
    """The ``client_id`` an Apple refresh token is revoked with (customer contract §2.1.8).

    Apple binds a refresh token to the client it was issued to: the ID token's ``aud`` bundle id, which the
    code exchange used as ``client_id``. It is kept on the identity (``provider_client_id``, and bound to the
    sealed token as its associated data, :func:`seal_apple_refresh`), so the revocation uses the right
    ``client_id`` when ``APPLE_BUNDLE_IDS`` lists several bundle ids. An identity from before
    ``accounts.0007`` falls back to the first configured bundle id (``""`` when none).
    """
    if identity is not None and identity.provider_client_id:
        return identity.provider_client_id
    bundle_ids = list(settings.APPLE_BUNDLE_IDS)
    return str(bundle_ids[0]) if bundle_ids else ""


def apple_token_aad(client_id: str) -> bytes:
    """Associated data of a sealed Apple refresh token: its ``client_id`` (a token cannot be replayed under
    another bundle id, and the revocation payload's ``client_id`` is authenticated by the seal)."""
    return f"apple.refresh_token:{client_id}".encode()


def seal_apple_refresh(refresh_token: str, *, client_id: str) -> str:
    return secretbox.seal(refresh_token.encode("utf-8"), aad=apple_token_aad(client_id))


def open_apple_refresh(sealed: str, *, client_id: str) -> str:
    return secretbox.open_(sealed, aad=apple_token_aad(client_id)).decode("utf-8")


def enqueue_apple_revoke(identity: CustomerSocialIdentity) -> bool:
    """Queue ``apple.revoke`` for an Apple identity holding a sealed refresh token (customer contract §2.1.8).

    Called in the transaction that unlinks the identity (``DELETE me/social/apple/``, the reset, the dormant
    reset, ``DELETE me/``). Returns whether a message was queued.
    """
    if identity.provider != enums.CustomerSocialProvider.APPLE or not identity.provider_refresh_token:
        return False
    outbox.enqueue(
        topics.APPLE_REVOKE,
        {
            "identity_id": identity.pk,
            **apple_grant_reference(identity.customer_id, identity.subject),
            "client_id": apple_client_id(identity),
            "sealed_token": identity.provider_refresh_token,
        },
        dedupe_key=topics.apple_revoke_key(identity.pk),
    )
    return True


def apple_grant_reference(customer_id: int | None, subject: str) -> dict[str, Any]:
    """The non-secret reference of an Apple grant in an ``apple.revoke`` payload: the customer, Apple's
    ``sub`` (the handler skips a grant whose subject is linked again, and staff need it to find a grant
    whose message went dead; blanked with the token after a final outcome) and ``subject_ref``
    (``pii_ref``, the only form the log lines carry)."""
    return {
        "customer_id": customer_id,
        "subject": subject,
        "subject_ref": pii_ref(subject) if subject else "",
    }


def apple_subject_linked(subject: str) -> bool:
    """Whether an Apple identity with ``subject`` is linked to any customer. Apple's revocation ends the
    user's whole authorization of the app, not only the one token, so a token whose subject is linked keeps
    the linked identity's grant alive and must never be revoked on its own."""
    return bool(subject) and (
        CustomerSocialIdentity.objects.filter(
            provider=enums.CustomerSocialProvider.APPLE, subject=subject
        ).exists()
    )


SOCIAL_NONCE_KEY = "gd:social:nonce:{digest}"
SOCIAL_SEEN_KEY = "gd:social:seen:{digest}"


def issue_social_nonce(fid: str) -> tuple[str, int]:
    """A 256-bit nonce bound to this install (``gd:social:nonce:{sha256(nonce)}`` = ``pii_ref(fid)``)."""
    nonce = secrets.token_urlsafe(32)
    ttl = int(settings.SOCIAL_NONCE_TTL_SECONDS)
    cache.set(SOCIAL_NONCE_KEY.format(digest=hashing.hash_secret(nonce)), pii_ref(fid), ttl)
    return nonce, ttl


def consume_social_nonce(nonce: str, fid: str) -> bool:
    """Single use: the nonce must exist, be bound to ``fid``, and its deletion must succeed (a concurrent use
    of the same nonce deletes nothing and fails)."""
    if not nonce:
        return False
    key = SOCIAL_NONCE_KEY.format(digest=hashing.hash_secret(nonce))
    if cache.get(key) != pii_ref(fid):
        return False
    return bool(cache.delete(key))


def remember_id_token(provider: str, raw_token: str, *, expires_at: int) -> bool:
    """The replay cache (``gd:social:seen:{sha256(provider + ":" + token)}`` until ``exp``); ``False``
    when the
    token was seen before. Keyed by the raw token: Apple's token has no ``jti`` and two tokens of one second
    share every claim."""
    ttl = max(1, int(expires_at - timeutils.now().timestamp()))
    return bool(
        cache.add(SOCIAL_SEEN_KEY.format(digest=hashing.hash_secret(f"{provider}:{raw_token}")), 1, ttl)
    )


def link_customer_identity(
    customer_id: int,
    *,
    provider: str,
    subject: str,
    email: str,
    sealed_token: str = "",
    client_id: str = "",
    signed_in: bool = False,
) -> CustomerSocialIdentity:
    """Link a social identity (customer contract §2.1.9, §2.2.8). ``IntegrityError`` (inside its own
    savepoint) when the subject or the customer's provider slot was taken meanwhile."""
    with transaction.atomic():
        return CustomerSocialIdentity.objects.create(
            customer_id=customer_id,
            provider=provider,
            subject=subject,
            email_at_link=email[:254] if "@" in email else "",
            last_login_at=timeutils.now() if signed_in else None,
            provider_refresh_token=sealed_token,
            provider_client_id=client_id,
        )


def unlink_customer_identity(identity: CustomerSocialIdentity) -> None:
    """Delete one identity; an Apple identity holding a refresh token gets its ``apple.revoke``."""
    enqueue_apple_revoke(identity)
    identity.delete()


def touch_identity_sign_in(
    identity_id: int, customer_id: int, *, sealed_token: str = "", client_id: str = ""
) -> bool:
    """A linked identity signs in: lock it (after the customer-level lock), stamp ``last_login_at`` and store
    a newly exchanged Apple refresh token. ``False`` when the identity is gone (unlinked by a reset, the
    dormant rule or the customer meanwhile).

    A token exchanged while a concurrent sign-in of the same identity stored one first is dropped, not
    revoked: both belong to the same Apple authorization of the app, and Apple's revocation ends the whole
    authorization, so revoking the dropped one would invalidate the stored one too."""
    identity = (
        CustomerSocialIdentity.objects.select_for_update()
        .filter(pk=identity_id, customer_id=customer_id)
        .first()
    )
    if identity is None:
        return False
    fields: dict[str, Any] = {"last_login_at": timeutils.now()}
    if sealed_token and not identity.provider_refresh_token:
        fields.update(provider_refresh_token=sealed_token, provider_client_id=client_id)
    CustomerSocialIdentity.objects.filter(pk=identity_id).update(**fields)
    return True


def enqueue_apple_token_revoke(
    sealed_token: str, *, client_id: str, customer_id: int | None, subject: str
) -> bool:
    """Revoke an Apple refresh token the code exchange produced for a flow that was then refused (nothing
    linked): ``apple.revoke`` without an identity, deduplicated on the sealed token. Nothing is queued while
    an identity with the same ``subject`` is linked (:func:`apple_subject_linked`: revoking would end that
    identity's grant too); the handler re-checks at delivery. Returns whether a message was queued."""
    if not sealed_token or apple_subject_linked(subject):
        return False
    outbox.enqueue(
        topics.APPLE_REVOKE,
        {
            "identity_id": None,
            **apple_grant_reference(customer_id, subject),
            "client_id": client_id,
            "sealed_token": sealed_token,
        },
        dedupe_key=f"{topics.APPLE_REVOKE}:token:{hashing.hash_secret(sealed_token)[:32]}",
    )
    return True


__all__ = [
    "apple_client_id",
    "apple_token_aad",
    "seal_apple_refresh",
    "open_apple_refresh",
    "enqueue_apple_revoke",
    "apple_grant_reference",
    "apple_subject_linked",
    "SOCIAL_NONCE_KEY",
    "SOCIAL_SEEN_KEY",
    "issue_social_nonce",
    "consume_social_nonce",
    "remember_id_token",
    "link_customer_identity",
    "unlink_customer_identity",
    "touch_identity_sign_in",
    "enqueue_apple_token_revoke",
]
