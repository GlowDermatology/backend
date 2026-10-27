"""``get_bts(scenario)``: the BTS adapter of ``settings.BTS_MODE`` (default ``INTEGRATIONS_MODE``).

``fake`` returns :class:`FakeBts` over the order's scenario (``metadata["fake"]["bts"]``); ``live`` returns
:class:`.live.LiveBts` built from the ``BTS_*`` settings (the scenario is ignored).
"""

from collections.abc import Mapping
from typing import Any

from django.conf import settings

from .fake import FakeBts
from .live import LiveBts
from .ports import BtsPort


def bts_mode() -> str:
    return str(getattr(settings, "BTS_MODE", None) or settings.INTEGRATIONS_MODE)


def get_bts(scenario: Mapping[str, Any] | None = None) -> BtsPort:
    mode = bts_mode()
    if mode == "fake":
        return FakeBts(scenario)
    if mode == "live":
        return LiveBts.from_settings()
    raise ValueError(f"Unknown BTS_MODE {mode!r}")
