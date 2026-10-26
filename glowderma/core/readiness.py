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

The loyalty app registers one more check the same way (``LoyaltyConfig.ready``):
:func:`cashback_policy_missing` — no cashback policy version is in force, so purchases earn nothing (gap
CB-9.2.4). Informational: shown to callers past the gate, it never fails readiness.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Protocol


class ProviderProbe(Protocol):
    def configured(self) -> dict[str, bool]: ...

    def last_syncs(self) -> dict[str, datetime | None]: ...


_probe: ProviderProbe | None = None
_cashback_policy_check: Callable[[], bool] | None = None


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


def register_cashback_policy_check(check: Callable[[], bool]) -> None:
    """Install the "no cashback policy in force" check (one per process; the last registration wins)."""
    global _cashback_policy_check
    _cashback_policy_check = check


def cashback_policy_missing() -> bool | None:
    """Whether no cashback policy version is in force now; ``None`` without a registered check."""
    return bool(_cashback_policy_check()) if _cashback_policy_check is not None else None


__all__ = [
    "ProviderProbe",
    "cashback_policy_missing",
    "last_syncs",
    "live_configured",
    "probe",
    "register",
    "register_cashback_policy_check",
]
