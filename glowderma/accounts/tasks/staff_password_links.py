"""The ``email.staff_password_link`` handler and its dead-letter hook: the staff invite and password-reset
links by e-mail."""

from __future__ import annotations

import logging
from typing import Any

from django.db import transaction

from glowderma.accounts import services, topics
from glowderma.accounts.models import StaffPasswordToken
from glowderma.accounts.tasks import message_rendering, outbox_payloads
from glowderma.core import outbox, secretbox, timeutils
from glowderma.core.models import OutboxMessage
from glowderma.integrations.email.ports import EmailMessage, EmailRejected, EmailUnavailable
from glowderma.integrations.email.registry import get_email_port

logger = logging.getLogger("glowderma.accounts.tasks")


STAFF_LINK_SEALED_KEYS = ("to", "link", "name")


def _blank_staff_link(message: OutboxMessage, payload: dict[str, Any]) -> None:
    OutboxMessage.objects.filter(pk=message.pk).update(
        payload={**payload, **dict.fromkeys(STAFF_LINK_SEALED_KEYS, "")}, updated_at=timeutils.now()
    )


@outbox.register(topics.STAFF_PASSWORD_LINK)
def send_staff_password_link(message: OutboxMessage) -> None:
    """E-mail one staff invite / password link (``email.staff_password_link``).

    Sent only while the link is still open (unused, unrevoked, unexpired, the account active): a link that
    was superseded or used meanwhile sends nothing. ``StaffPasswordToken.sent_at`` is claimed in its own
    transaction before the relay call, so a redelivery never sends twice; a relay that accepted nothing
    un-claims and re-raises (outbox retry, the sealed payload is kept); an unknown outcome keeps the claim and
    is never resent; a refusal clears the claim (``invite.sent_at`` stays null) and is final. After any final
    outcome the sealed address, link and name are blanked in the stored payload. Logs carry the outbox and
    link ids only — never the address, the name or the link."""
    payload = outbox_payloads.read_payload(message)
    if not payload.get("link"):
        return
    token_id = int(payload.get("link_id") or 0)
    aad = services.password_link_aad(token_id)
    try:
        address, link, name = (
            secretbox.open_(str(payload.get(key) or ""), aad=aad).decode("utf-8")
            for key in STAFF_LINK_SEALED_KEYS
        )
    except secretbox.SecretboxError:
        logger.error(
            "email.staff_password_link payload does not open outbox_id=%s link_id=%s", message.pk, token_id
        )
        _blank_staff_link(message, payload)
        return
    instant = timeutils.now()
    with transaction.atomic():
        claimed = StaffPasswordToken.objects.filter(
            pk=token_id,
            sent_at__isnull=True,
            used_at__isnull=True,
            revoked_at__isnull=True,
            expires_at__gt=instant,
            staff__is_active=True,
        ).update(sent_at=instant)
    if not claimed:
        logger.info("email.staff_password_link not sent: link closed or already sent link_id=%s", token_id)
        _blank_staff_link(message, payload)
        return
    subject, text, html = message_rendering.staff_link_parts(
        link=link, name=name, lang=str(payload.get("lang") or ""), purpose=str(payload.get("purpose") or "")
    )
    email = EmailMessage(
        to=address, subject=subject, text=text, html=html, reference=f"outbox_id={message.pk}"
    )
    try:
        get_email_port().send(email)
    except EmailUnavailable as exc:
        if not exc.sent:
            StaffPasswordToken.objects.filter(pk=token_id).update(sent_at=None)
            raise
        logger.warning("email.staff_password_link outcome unknown link_id=%s (not resent)", token_id)
    except EmailRejected:
        StaffPasswordToken.objects.filter(pk=token_id).update(sent_at=None)
        logger.warning("email.staff_password_link rejected link_id=%s", token_id)
    _blank_staff_link(message, payload)


@outbox.on_dead(topics.STAFF_PASSWORD_LINK)
def blank_dead_staff_password_link(message: OutboxMessage) -> None:
    """A link e-mail that died (the relay unreachable until the last attempt) keeps no address or link."""
    payload = outbox_payloads.read_payload(message)
    if any(payload.get(key) for key in STAFF_LINK_SEALED_KEYS):
        _blank_staff_link(message, payload)


__all__ = [
    "STAFF_LINK_SEALED_KEYS",
    "send_staff_password_link",
    "blank_dead_staff_password_link",
]
