"""``get_email_port()`` (hybrid identity, customer contract §2.14.4).

``settings.EMAIL_MODE`` selects the adapter: the process-wide :class:`.fake.FakeEmail` (``fake``) or
:class:`.smtp.SmtpEmail` built from the ``EMAIL_*`` settings (``smtp``). OTP codes come from
``integrations.eskiz.registry.new_code`` (one generator, so the fixed local/test code ``ESKIZ_FAKE_CODE``
applies to e-mail codes too while ``EMAIL_MODE=fake``; see :func:`new_code`).
"""

from __future__ import annotations

import secrets

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

from glowderma.core import integration_modes

from .fake import FakeEmail
from .ports import EmailPort
from .smtp import SmtpEmail

CODE_LENGTH = 6

_fake = FakeEmail()


def get_email_port() -> EmailPort:
    if settings.EMAIL_MODE == "fake":
        return _fake
    if settings.EMAIL_MODE in ("smtp", "live"):  # "live": the generic name of every provider's live mode
        return SmtpEmail.from_settings()
    raise ImproperlyConfigured(f"Unknown EMAIL_MODE {settings.EMAIL_MODE!r}")


def fake_email() -> FakeEmail:
    return _fake


def new_code() -> str:
    """Six random digits; the fixed ``ESKIZ_FAKE_CODE`` only under the local/test settings with the fake
    adapter (the production settings refuse the variable)."""
    fixed = str(getattr(settings, "ESKIZ_FAKE_CODE", "") or "")
    if fixed and integration_modes.dev_features_allowed() and settings.EMAIL_MODE == "fake":
        return fixed
    return "".join(secrets.choice("0123456789") for _ in range(CODE_LENGTH))
