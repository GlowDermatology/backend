"""``get_payme(scenario)``: the Payme adapter of ``settings.PAYME_MODE`` (default ``INTEGRATIONS_MODE``):
:class:`.fake.FakePayme`, or :class:`.live.LivePayme` built from the settings (the scenario is ignored)."""

from collections.abc import Mapping
from typing import Any

from django.conf import settings

from .fake import FakePayme
from .live import LivePayme
from .ports import PaymePort


def payme_mode() -> str:
    return str(getattr(settings, "PAYME_MODE", None) or settings.INTEGRATIONS_MODE)


def get_payme(scenario: Mapping[str, Any] | None = None) -> PaymePort:
    mode = payme_mode()
    if mode == "fake":
        return FakePayme(scenario)
    if mode == "live":
        return LivePayme.from_settings()
    raise ValueError(f"Unknown PAYME_MODE {mode!r}")
