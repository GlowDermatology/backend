"""``get_vault()``: the KV v2 adapter ``settings.VAULT_MODE`` selects.

``fake`` → the process-wide in-memory :class:`.fake.FakeVault`, refused under the production settings
(``core.integration_modes.fake_in_production("vault")`` → :class:`.ports.VaultNotConfigured`: a key typed
into the console must never live only in one process's memory); ``live`` → :class:`.live.LiveVault` from the
``VAULT_*`` settings.
"""

from __future__ import annotations

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

from glowderma.core import integration_modes

from .fake import FakeVault
from .live import LiveVault
from .ports import SecretStore, VaultNotConfigured

PROVIDER = "vault"

_fake = FakeVault()


def get_vault() -> SecretStore:
    mode = settings.VAULT_MODE
    if mode == "fake":
        if integration_modes.fake_in_production(PROVIDER):
            raise VaultNotConfigured("fake Vault refused under the production settings")
        return _fake
    if mode == "live":
        return LiveVault.from_settings()
    raise ImproperlyConfigured(f"Unknown VAULT_MODE {mode!r}")


def fake_vault() -> FakeVault:
    return _fake
