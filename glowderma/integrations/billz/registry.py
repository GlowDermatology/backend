"""``get_billz(known, scenario)``: the Billz adapter of ``settings.BILLZ_MODE`` (default: INTEGRATIONS_MODE).

``fake`` returns :class:`FakeBillz` over the caller's ``KnownProducts`` view and the order's fake scenario
(``metadata["fake"]["billz"]``, sale push only); ``live`` returns :class:`LiveBillz` built from the
``BILLZ_*`` settings (it ignores both arguments). Any other mode is a configuration error.
"""

from collections.abc import Mapping
from typing import Any

from django.conf import settings

from .fake import FakeBillz
from .live import LiveBillz
from .ports import BillzPort, KnownProducts


def billz_mode() -> str:
    return str(getattr(settings, "BILLZ_MODE", None) or settings.INTEGRATIONS_MODE)


def get_billz(known: KnownProducts | None = None, scenario: Mapping[str, Any] | None = None) -> BillzPort:
    mode = billz_mode()
    if mode == "fake":
        return FakeBillz(known, scenario)
    if mode == "live":
        return LiveBillz.from_settings()
    raise ValueError(f"Unknown BILLZ_MODE {mode!r}")
