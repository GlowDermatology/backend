"""``get_click(scenario)``: the Click adapter of ``settings.CLICK_MODE`` (default ``INTEGRATIONS_MODE``):
:class:`.fake.FakeClick` over the order's scenario, or :class:`.live.LiveClick` built from the settings (the
scenario is ignored)."""

from collections.abc import Mapping
from typing import Any

from django.conf import settings

from .fake import FakeClick
from .live import LiveClick
from .ports import ClickPort


def click_mode() -> str:
    return str(getattr(settings, "CLICK_MODE", None) or settings.INTEGRATIONS_MODE)


def get_click(scenario: Mapping[str, Any] | None = None) -> ClickPort:
    mode = click_mode()
    if mode == "fake":
        return FakeClick(scenario)
    if mode == "live":
        return LiveClick.from_settings()
    raise ValueError(f"Unknown CLICK_MODE {mode!r}")
