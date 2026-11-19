"""In-memory KV v2 (``VAULT_MODE=fake``): per-path version history, process-wide, thread-safe.

Versions start at 1 and grow by one per write, like Vault's. :meth:`fail_next` scripts the next call to
raise (tests: Vault down during a rotation). :meth:`destroy` makes one version unreadable (Vault's
``destroy``). The registry never hands this adapter out under the production settings
(``vault_not_configured``).
"""

from __future__ import annotations

import threading
from collections.abc import Mapping

from glowderma.integrations.secret import Secret

from .ports import VaultError, VaultNotFound


class FakeVault:
    name = "fake"

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._versions: dict[str, list[dict[str, str] | None]] = {}
        self._fail: list[VaultError] = []
        self.writes = 0
        self.reads = 0

    def is_configured(self) -> bool:
        return True

    def reset(self) -> None:
        with self._lock:
            self._versions.clear()
            self._fail.clear()
            self.writes = 0
            self.reads = 0

    def fail_next(self, error: VaultError) -> None:
        with self._lock:
            self._fail.append(error)

    def _raise_scripted(self) -> None:
        if self._fail:
            raise self._fail.pop(0)

    def write(self, path: str, values: Mapping[str, str | Secret]) -> int:
        with self._lock:
            self._raise_scripted()
            plain = {
                key: value.reveal() if isinstance(value, Secret) else str(value)
                for key, value in values.items()
            }
            history = self._versions.setdefault(path, [])
            history.append(plain)
            self.writes += 1
            return len(history)

    def read(self, path: str, version: int | None = None) -> dict[str, Secret]:
        with self._lock:
            self._raise_scripted()
            self.reads += 1
            history = self._versions.get(path) or []
            number = len(history) if version is None else version
            stored = history[number - 1] if 1 <= number <= len(history) else None
            if stored is None:
                raise VaultNotFound(f"{path} version {version} not found")
            return {key: Secret(value) for key, value in stored.items()}

    def destroy(self, path: str, version: int) -> None:
        with self._lock:
            history = self._versions.get(path) or []
            if 1 <= version <= len(history):
                history[version - 1] = None

    def current_version(self, path: str) -> int:
        with self._lock:
            return len(self._versions.get(path) or [])
