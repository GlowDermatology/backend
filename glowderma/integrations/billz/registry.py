"""``get_billz(known)``: the Billz adapter of ``settings.BILLZ_MODE`` (default ``INTEGRATIONS_MODE``).

``fake`` returns :class:`FakeBillz` over the caller's ``KnownProducts`` view; ``live`` is M3 work and
raises ``NotImplementedError("M3")`` for now, so a misconfigured environment fails loudly at the first use
instead of silently importing nothing.
"""

from django.conf import settings

from .fake import FakeBillz
from .ports import BillzPort, KnownProducts


def billz_mode() -> str:
    return str(getattr(settings, "BILLZ_MODE", None) or settings.INTEGRATIONS_MODE)


def get_billz(known: KnownProducts | None = None) -> BillzPort:
    mode = billz_mode()
    if mode == "fake":
        return FakeBillz(known)
    if mode == "live":
        raise NotImplementedError("M3")
    raise ValueError(f"Unknown BILLZ_MODE {mode!r}")
