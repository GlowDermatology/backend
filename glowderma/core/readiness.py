"""Provider readiness for ``GET health/?ready=1`` (admin contract §2.15, docs/ops/monitoring.md).

``core`` sits below ``integrations``, so the health view cannot ask the adapters itself: the integrations app
registers one :class:`ProviderProbe` at startup (``IntegrationsConfig.ready``) and the view calls it.

* :meth:`ProviderProbe.configured` — ``{provider: configured}`` for every provider whose mode is ``live``:
  the live adapter can be built and its ``is_configured()`` (where the port has one) says yes. It reads
  settings only, so the view calls it on every readiness probe: a live provider that is not configured makes
  readiness answer 503 for every caller.
* :meth:`ProviderProbe.last_syncs` — ``{provider: last successful sync}`` of the providers that sync (Billz
  catalog sync, BTS polling; ``IntegrationState.last_sync_at``). One query; shown only to callers past the
  admin network gate, with the configured flags.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol


class ProviderProbe(Protocol):
    def configured(self) -> dict[str, bool]: ...

    def last_syncs(self) -> dict[str, datetime | None]: ...


_probe: ProviderProbe | None = None


def register(probe: ProviderProbe) -> None:
    """Install the provider probe (one per process; the last registration wins)."""
    global _probe
    _probe = probe


def probe() -> ProviderProbe | None:
    """The registered probe, ``None`` before ``IntegrationsConfig.ready`` ran."""
    return _probe


def live_configured() -> dict[str, bool]:
    """``{provider: configured}`` of the live providers (``{}`` without a probe)."""
    return dict(_probe.configured()) if _probe is not None else {}


def last_syncs() -> dict[str, datetime | None]:
    """``{provider: last successful sync}`` of the syncing providers (``{}`` without a probe)."""
    return dict(_probe.last_syncs()) if _probe is not None else {}


__all__ = ["ProviderProbe", "last_syncs", "live_configured", "probe", "register"]
