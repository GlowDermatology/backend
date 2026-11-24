"""``get_sms_port()`` and the OTP code generator (customer contract §2.14.1).

``settings.DEVSMS_MODE`` selects the adapter: the process-wide :class:`.fake.FakeSms`, or
:class:`.live.LiveDevsms` built from the ``DEVSMS_*`` settings. :func:`new_code` returns
``settings.OTP_FAKE_CODE`` only under the local/test settings (``DEV_FEATURES_ALLOWED``) with the fake adapter
— the production settings refuse the variable at boot anyway — and six random digits from ``secrets``
otherwise (DevSMS's universal OTP takes 4–8 digits).
"""

from __future__ import annotations

import secrets

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

from glowderma.core import integration_modes
from glowderma.integrations.devsms.fake import FakeSms
from glowderma.integrations.devsms.live import LiveDevsms
from glowderma.integrations.devsms.ports import SmsPort

CODE_LENGTH = 6

# One fake per process so a test (or a local run) can read ``FakeSms.sent`` across handler calls.
_fake = FakeSms()


def get_sms_port() -> SmsPort:
    if settings.DEVSMS_MODE == "fake":
        return _fake
    if settings.DEVSMS_MODE == "live":
        return LiveDevsms.from_settings()
    raise ImproperlyConfigured(f"Unknown DEVSMS_MODE {settings.DEVSMS_MODE!r}")


def fake_sms() -> FakeSms:
    return _fake


def new_code() -> str:
    fixed = str(getattr(settings, "OTP_FAKE_CODE", "") or "")
    if fixed and integration_modes.dev_features_allowed() and settings.DEVSMS_MODE == "fake":
        return fixed
    return "".join(secrets.choice("0123456789") for _ in range(CODE_LENGTH))


__all__ = [
    "CODE_LENGTH",
    "fake_sms",
    "get_sms_port",
    "new_code",
]
