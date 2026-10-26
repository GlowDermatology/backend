"""``get_eskiz_port()`` and the OTP code generator (customer contract §2.14.1).

``settings.ESKIZ_MODE`` selects the adapter: the process-wide :class:`.fake.FakeEskiz`, or
:class:`.live.LiveEskiz` built from the ``ESKIZ_*`` settings (its token lives in the cache, not the object).
:func:`new_code` returns ``settings.ESKIZ_FAKE_CODE`` only under the local/test settings
(``DEV_FEATURES_ALLOWED``) with the fake adapter — the production settings refuse the variable at boot
anyway — and six random digits from ``secrets`` otherwise.
"""

from __future__ import annotations

import secrets

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

from glowderma.core import integration_modes

from .fake import FakeEskiz
from .live import LiveEskiz
from .ports import EskizPort

CODE_LENGTH = 6

# One fake per process so a test (or a local run) can read ``FakeEskiz.sent`` across handler calls.
_fake = FakeEskiz()


def get_eskiz_port() -> EskizPort:
    if settings.ESKIZ_MODE == "fake":
        return _fake
    if settings.ESKIZ_MODE == "live":
        return LiveEskiz.from_settings()
    raise ImproperlyConfigured(f"Unknown ESKIZ_MODE {settings.ESKIZ_MODE!r}")


def fake_eskiz() -> FakeEskiz:
    return _fake


def new_code() -> str:
    fixed = str(getattr(settings, "ESKIZ_FAKE_CODE", "") or "")
    if fixed and integration_modes.dev_features_allowed() and settings.ESKIZ_MODE == "fake":
        return fixed
    return "".join(secrets.choice("0123456789") for _ in range(CODE_LENGTH))
