"""``get_fcm_port()``: the adapter ``settings.FCM_MODE`` selects — the process-wide :class:`.fake.FakeFcm`, or
:class:`.live.LiveFcm` built from ``FCM_PROJECT_ID`` / ``FCM_CREDENTIALS_FILE`` (its access token is cached
per process in :data:`.live.access_tokens`, not on the object)."""

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

from .fake import FakeFcm
from .live import LiveFcm
from .ports import FcmPort

# One fake per process so a test (or a local run) can inspect ``FakeFcm.sent`` across handler calls.
_fake = FakeFcm()


def get_fcm_port() -> FcmPort:
    if settings.FCM_MODE == "fake":
        return _fake
    if settings.FCM_MODE == "live":
        return LiveFcm.from_settings()
    raise ImproperlyConfigured(f"Unknown FCM_MODE {settings.FCM_MODE!r}")
