"""``get_fcm_port()``: the adapter ``settings.FCM_MODE`` selects (``fake`` in M1; ``live`` comes with M3)."""

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

from .fake import FakeFcm
from .ports import FcmPort

# One fake per process so a test (or a local run) can inspect ``FakeFcm.sent`` across handler calls.
_fake = FakeFcm()


def get_fcm_port() -> FcmPort:
    if settings.FCM_MODE == "fake":
        return _fake
    raise ImproperlyConfigured("FCM_MODE=live is not available before M3 (set FCM_MODE=fake)")
