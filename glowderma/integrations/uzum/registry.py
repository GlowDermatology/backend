"""``get_uzum(scenario)``: the Uzum adapter of ``settings.UZUM_MODE`` (default ``fake``, never
``INTEGRATIONS_MODE``): :class:`.fake.FakeUzum` over the order's scenario, or :class:`.live.LiveUzum` built
from the settings (the scenario is ignored)."""

from collections.abc import Mapping
from typing import Any

from django.conf import settings

from .fake import FakeUzum
from .live import LiveUzum
from .ports import UzumCheckoutPort


def uzum_mode() -> str:
    return str(getattr(settings, "UZUM_MODE", "fake") or "fake")


def get_uzum(scenario: Mapping[str, Any] | None = None) -> UzumCheckoutPort:
    mode = uzum_mode()
    if mode == "fake":
        return FakeUzum(scenario)
    if mode == "live":
        return LiveUzum.from_settings()
    raise ValueError(f"Unknown UZUM_MODE {mode!r}")
