"""``accounts.cleanup_auth``: the hourly retention job of one-time codes and ended sessions (customer contract
§6.4)."""

from __future__ import annotations

import logging
from datetime import timedelta

from celery import shared_task  # type: ignore[import-untyped]
from django.db import transaction
from django.db.models import Q

from glowderma.accounts import services, topics
from glowderma.accounts.models import AuthSession, SmsCode
from glowderma.core import timeutils
from glowderma.core.models import OutboxMessage

logger = logging.getLogger("glowderma.accounts.tasks")


# accounts.cleanup_auth retention (customer contract §6.4, plan §8).
SMS_RETENTION = timedelta(hours=24)
SESSION_RETENTION = timedelta(days=90)


@shared_task(name="accounts.cleanup_auth")  # type: ignore[untyped-decorator]
def cleanup_auth() -> dict[str, int]:
    """Hourly (beat ``accounts-cleanup-auth``): delete ``SmsCode`` rows (SMS and e-mail codes) older than
    24 h, ``done``/``dead`` ``sms.send``, ``email.send`` and ``email.staff_password_link`` outbox rows older
    than 24 h and customer sessions revoked or expired for more than 90 days (with them their user agent, IP
    and location — B-W1); retire
    every active device left without an open, unexpired session (an expired session never revoked: no push
    to a signed-out phone)."""
    instant = timeutils.now()
    codes, _ = SmsCode.objects.filter(created_at__lt=instant - SMS_RETENTION).delete()
    messages, _ = OutboxMessage.objects.filter(
        topic__in=(topics.SMS_SEND, topics.EMAIL_SEND, topics.STAFF_PASSWORD_LINK),
        status__in=("done", "dead"),
        updated_at__lt=instant - SMS_RETENTION,
    ).delete()
    horizon = instant - SESSION_RETENTION
    sessions, _ = (
        AuthSession.objects.filter(audience="customer")
        .filter(Q(revoked_at__lt=horizon) | Q(revoked_at__isnull=True, absolute_expires_at__lt=horizon))
        .delete()
    )
    customers = services.customers_with_sessionless_devices()
    for customer_id in customers:
        with transaction.atomic():  # the customer lock first, as every sign-in (C§2.7.1)
            services.lock_customer(customer_id)
            services.retire_sessionless_devices(customer_id)
    return {
        "sms_codes": codes,
        "outbox_messages": messages,
        "sessions": sessions,
        "customers_with_devices_retired": len(customers),
    }


# --- P1: sms.send and apple.revoke (customer contract §2.14.1, §2.1.8) --------------------------------------


__all__ = [
    "SMS_RETENTION",
    "SESSION_RETENTION",
    "cleanup_auth",
]
