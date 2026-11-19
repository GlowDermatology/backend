"""The Vault KV v2 port: write a new version of a secret, read one version.

Paths are relative to the KV v2 mount (``settings.VAULT_KV_MOUNT``), e.g. ``production/ai/gemini``. Values
come back wrapped in :class:`~glowderma.integrations.secret.Secret`; a write takes plain strings or
``Secret``s.

Errors (their messages carry the path and the HTTP status only, never a value):

* :class:`VaultUnavailable` — Vault (or its agent) could not be reached, answered 5xx, or refused the
  request (401/403: the agent's token or policy); ``sent`` is true when a write may have reached Vault;
* :class:`VaultNotFound` — no such secret, or the version does not exist / was deleted or destroyed;
* :class:`VaultNotConfigured` — the fake adapter under the production settings, or no proxy URL.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol

from glowderma.integrations.secret import Secret


class VaultError(Exception):
    code = "vault_unavailable"


class VaultUnavailable(VaultError):
    def __init__(self, detail: str = "", *, sent: bool = False) -> None:
        super().__init__(detail or "vault unavailable")
        self.sent = sent


class VaultNotFound(VaultError):
    code = "vault_not_found"


class VaultNotConfigured(VaultError):
    code = "vault_not_configured"


class SecretStore(Protocol):
    name: str

    def write(self, path: str, values: Mapping[str, str | Secret]) -> int:
        """Store ``values`` as a new version of ``path``; the new version number."""
        ...

    def read(self, path: str, version: int | None = None) -> dict[str, Secret]:
        """The values of ``version`` (``None`` = the current one)."""
        ...

    def is_configured(self) -> bool: ...
