"""Background work of ``accounts``, one subject per module: the outbox handlers (``otp_delivery``:
``sms.send``, ``email.send``; ``email_notices``: ``email.notice``; ``apple_revocation``: ``apple.revoke`` and
its dead hook; ``staff_password_links``: ``email.staff_password_link`` and its dead hook;
``session_replaced_push``: the legacy ``push.session_replaced`` drain) and the ``accounts.cleanup_auth`` job
(``auth_cleanup``).

Handlers register when their module is imported: ``AccountsConfig.ready()`` and Celery's autodiscovery import
this package, and importing it imports every handler module below. Every module logs under
``glowderma.accounts.tasks``.
"""

from glowderma.accounts.tasks.apple_revocation import (
    AppleRevokeNotSent,
    apple_revoke_dead,
    revoke_apple_token,
)
from glowderma.accounts.tasks.auth_cleanup import (
    SESSION_RETENTION,
    SMS_RETENTION,
    cleanup_auth,
)
from glowderma.accounts.tasks.email_notices import (
    send_email_notice,
)
from glowderma.accounts.tasks.message_rendering import (
    email_parts,
    sms_text,
    staff_link_parts,
)
from glowderma.accounts.tasks.otp_delivery import (
    send_email_code,
    send_sms,
)
from glowderma.accounts.tasks.outbox_payloads import (
    read_payload,
)
from glowderma.accounts.tasks.session_replaced_push import (
    blank_dead_session_replaced_token,
    push_session_replaced,
)
from glowderma.accounts.tasks.staff_password_links import (
    STAFF_LINK_SEALED_KEYS,
    blank_dead_staff_password_link,
    send_staff_password_link,
)

__all__ = [
    "SESSION_RETENTION",
    "SMS_RETENTION",
    "STAFF_LINK_SEALED_KEYS",
    "AppleRevokeNotSent",
    "apple_revoke_dead",
    "blank_dead_session_replaced_token",
    "blank_dead_staff_password_link",
    "cleanup_auth",
    "email_parts",
    "push_session_replaced",
    "read_payload",
    "revoke_apple_token",
    "send_email_code",
    "send_email_notice",
    "send_sms",
    "send_staff_password_link",
    "sms_text",
    "staff_link_parts",
]
