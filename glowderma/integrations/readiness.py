"""The provider probe of ``GET health/?ready=1`` (``core.readiness``; docs/ops/monitoring.md).

``configured()`` covers the providers whose mode is ``live``: the registry must build the live adapter (one
that refuses its settings raises ``ImproperlyConfigured`` / ``ValueError`` → not configured) and
``is_configured()`` must say yes (Billz: the key and the shop, cash box and payment-type ids; Payme: the
merchant id and key; Click: the three numeric ids, not ``00000``, and the secret key; BTS: login, password,
sender city code and, for a branch pickup, the branch code; Eskiz: email, password and sender; FCM: the
project id and an existing key file). Building an adapter reads settings only and sends nothing.
``last_syncs()`` reads ``IntegrationState.last_sync_at`` of the live providers that sync
(Billz catalog sync, BTS polling).
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from datetime import datetime

from glowderma.core import integration_modes
from glowderma.integrations.billz.registry import get_billz
from glowderma.integrations.bts.registry import get_bts
from glowderma.integrations.click.registry import get_click
from glowderma.integrations.eskiz.registry import get_eskiz_port
from glowderma.integrations.fcm.registry import get_fcm_port
from glowderma.integrations.models import IntegrationState
from glowderma.integrations.oauth.registry import STAFF_PROVIDERS, get_oauth_provider
from glowderma.integrations.payme.registry import get_payme

logger = logging.getLogger(__name__)

LIVE = "live"
# Providers whose IntegrationState.last_sync_at is a sync (Billz) or a polling run (BTS).
SYNCING_PROVIDERS: tuple[str, ...] = ("billz", "bts")
# provider → the adapters its live mode uses (built from settings; nothing is sent).
BUILDERS: dict[str, Callable[[], Iterable[object]]] = {
    "oauth": lambda: [get_oauth_provider(name) for name in STAFF_PROVIDERS],
    "billz": lambda: [get_billz()],
    "fcm": lambda: [get_fcm_port()],
    "click": lambda: [get_click()],
    "payme": lambda: [get_payme()],
    "bts": lambda: [get_bts()],
    "eskiz": lambda: [get_eskiz_port()],
}


def adapter_configured(provider: str) -> bool:
    """Whether the live adapter(s) of ``provider`` can be built and say they are configured."""
    builder = BUILDERS.get(provider)
    if builder is None:
        return False
    try:
        adapters = list(builder())
    except Exception as exc:  # noqa: BLE001 - no live adapter yet, or one that refuses its settings
        logger.debug("readiness: %s live adapter unavailable: %s", provider, type(exc).__name__)
        return False
    for adapter in adapters:
        check = getattr(adapter, "is_configured", None)
        if not callable(check):
            continue
        try:
            if not check():
                return False
        except Exception:  # noqa: BLE001 - a probe never raises
            return False
    return True


class IntegrationsProbe:
    """``core.readiness.ProviderProbe`` of the integrations app."""

    def configured(self) -> dict[str, bool]:
        return {
            provider: adapter_configured(provider)
            for provider, mode in integration_modes.provider_modes().items()
            if mode == LIVE
        }

    def last_syncs(self) -> dict[str, datetime | None]:
        live = [p for p in SYNCING_PROVIDERS if integration_modes.provider_mode(p) == LIVE]
        if not live:
            return {}
        rows = dict(
            IntegrationState.objects.filter(provider__in=live).values_list("provider", "last_sync_at")
        )
        return {provider: rows.get(provider) for provider in live}


PROBE = IntegrationsProbe()

__all__ = ["BUILDERS", "PROBE", "SYNCING_PROVIDERS", "IntegrationsProbe", "adapter_configured"]
