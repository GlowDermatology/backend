"""The ``email.notice`` handler: account-security notices sent by e-mail."""

from __future__ import annotations

import logging

from django.template.loader import render_to_string

from glowderma.accounts import services, topics
from glowderma.accounts.tasks import outbox_payloads
from glowderma.core import i18n, outbox, secretbox, timeutils
from glowderma.core.models import OutboxMessage
from glowderma.integrations.email.ports import EmailMessage, EmailRejected, EmailUnavailable
from glowderma.integrations.email.registry import get_email_port

logger = logging.getLogger("glowderma.accounts.tasks")


@outbox.register(topics.EMAIL_NOTICE)
def send_email_notice(message: OutboxMessage) -> None:
    """A security notice e-mail (hybrid identity, C§2.2.10: the previous address of a replaced e-mail). The
    address is sealed in the payload and blanked after a definitive outcome; a relay that accepted nothing
    re-raises (outbox retry), an unknown outcome or a refusal is final. Logs carry the outbox id only."""
    payload = outbox_payloads.read_payload(message)
    sealed = str(payload.get("to") or "")
    if not sealed:
        return
    kind = str(payload.get("kind") or "")
    try:
        address = secretbox.open_(sealed, aad=services.EMAIL_NOTICE_AAD).decode("utf-8")
    except secretbox.SecretboxError:
        logger.error("email.notice address does not open outbox_id=%s", message.pk)
        address = ""
    if address and kind in services.EMAIL_NOTICE_KINDS:
        lang = i18n.normalize_language(payload.get("lang"))
        context = {
            "lang": lang,
            "subject": i18n.t(f"email.notice.{kind}.subject", lang),
            "body": i18n.t(f"email.notice.{kind}.body", lang),
            "footer": i18n.t("email.otp.footer", lang),
        }
        email = EmailMessage(
            to=address,
            subject=str(context["subject"]),
            text=render_to_string("accounts/email/notice.txt", context).strip() + "\n",
            html=render_to_string("accounts/email/notice.html", context),
            reference=f"outbox_id={message.pk}",
        )
        try:
            get_email_port().send(email)
        except EmailUnavailable as exc:
            if not exc.sent:
                raise
            logger.warning("email.notice outcome unknown outbox_id=%s (not resent)", message.pk)
        except EmailRejected:
            logger.warning("email.notice rejected outbox_id=%s", message.pk)
    OutboxMessage.objects.filter(pk=message.pk).update(
        payload={**payload, "to": ""}, updated_at=timeutils.now()
    )


__all__ = [
    "send_email_notice",
]
