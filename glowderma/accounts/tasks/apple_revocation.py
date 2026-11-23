"""The ``apple.revoke`` handler and its dead-letter hook: revoking a Sign in with Apple grant after an unlink
or a deletion."""

from __future__ import annotations

import logging
from typing import Any

from glowderma.accounts import services, topics
from glowderma.accounts.tasks import outbox_payloads
from glowderma.core import integration_modes, outbox, secretbox, timeutils
from glowderma.core.models import OutboxMessage
from glowderma.integrations.oauth.ports import OAuthExchangeError
from glowderma.integrations.oauth.registry import get_apple_service

logger = logging.getLogger("glowderma.accounts.tasks")


class AppleRevokeNotSent(RuntimeError):
    """The revocation was not sent: OAuth runs its fake adapter under the production settings."""


def _apple_grant(payload: dict[str, Any]) -> str:
    """The log reference of an ``apple.revoke`` grant: identity, customer and ``pii_ref`` of Apple's ``sub``
    (never the subject itself or the token)."""
    identity_id, customer_id = payload.get("identity_id"), payload.get("customer_id")
    return (
        f"identity_id={identity_id} customer_id={customer_id} subject_ref={payload.get('subject_ref') or '-'}"
    )


@outbox.register(topics.APPLE_REVOKE)
def revoke_apple_token(message: OutboxMessage) -> None:
    """Revoke a sealed Apple refresh token at ``https://appleid.apple.com/auth/revoke`` with the ``client_id``
    it was issued to (customer contract §2.1.8). Transport errors and Apple misconfiguration answers
    (``unavailable``) propagate (outbox retry, then ``apple.revoke_dead``); an Apple 400 for a token that is
    already invalid is final with a WARNING. After a final outcome the sealed token and the subject are
    blanked in the stored payload (``customer_id`` and ``subject_ref`` stay for the logs).

    A grant whose Apple ``sub`` is linked to a customer again at delivery (the account re-linked the same
    Apple ID, or the refused flow's subject was linked meanwhile) is skipped as final: Apple's revocation ends
    the user's whole authorization of the app, which would invalidate the linked identity's own token.

    While OAuth is fake under the production settings (C§6.14) the fake is never called — it would report a
    revocation that did not happen: ERROR ``apple.revoke_not_sent`` and the handler raises, so the sealed
    token stays in the payload, the outbox retries (OAuth may be live again by then) and a message that
    never gets through ends ``dead`` with the ``apple.revoke_dead`` alert (staff revoke by hand, finding the
    grant by ``customer_id`` and ``subject``).
    """
    payload = outbox_payloads.read_payload(message)
    sealed = str(payload.get("sealed_token") or "")
    if not sealed:
        return
    grant = _apple_grant(payload)
    if services.apple_subject_linked(str(payload.get("subject") or "")):
        logger.info("apple.revoke skipped %s (the subject is linked again; final)", grant)
    else:
        _revoke_apple_grant(payload, sealed, grant)
    OutboxMessage.objects.filter(pk=message.pk).update(
        payload={**payload, "sealed_token": "", "subject": ""}, updated_at=timeutils.now()
    )


def _revoke_apple_grant(payload: dict[str, Any], sealed: str, grant: str) -> None:
    if integration_modes.fake_in_production("oauth"):
        logger.error("apple.revoke_not_sent %s (OAuth is fake under the production settings)", grant)
        raise AppleRevokeNotSent(f"apple.revoke not sent: {grant}")
    client_id = str(payload.get("client_id") or "")
    try:
        refresh_token = services.open_apple_refresh(sealed, client_id=client_id)
    except secretbox.SecretboxError:
        logger.error("apple.revoke token does not open %s", grant)
        return
    try:
        get_apple_service(require_available=False).revoke(refresh_token, client_id=client_id)
    except OAuthExchangeError as exc:
        if exc.kind == "unavailable":
            raise
        logger.warning("apple.revoke refused %s (final)", grant)


@outbox.on_dead(topics.APPLE_REVOKE)
def apple_revoke_dead(message: OutboxMessage) -> None:
    """Apple stayed unreachable until the last attempt: ERROR ``apple.revoke_dead`` with the grant's
    reference (alert; staff revoke the token in the Apple developer account)."""
    logger.error("apple.revoke_dead %s", _apple_grant(outbox_payloads.read_payload(message)))


# --- feat/admin-ux: staff invite / password link e-mails (admin contract §2.1, §2.16) ----------------------


__all__ = [
    "AppleRevokeNotSent",
    "revoke_apple_token",
    "apple_revoke_dead",
]
