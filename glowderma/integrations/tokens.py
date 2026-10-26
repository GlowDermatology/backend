"""Provider bearer tokens shared by every worker process (Eskiz, BTS): kept **only** in the Django cache
(Redis DB 0 in production) and fetched single-flight across processes through a ``cache.add`` lock.

* :meth:`SharedToken.get` returns the cached token, or fetches one under the lock (the first caller logs in,
  the others wait for its result and never log in themselves).
* :meth:`SharedToken.renew` is called after a provider refused a token (HTTP 401): it fetches a new token
  unless another worker already replaced the refused one, so a burst of 401s causes one login.

The token never reaches the database, a log line or an exception message. A cache that is down (Redis
unreachable) degrades to a login per call rather than failing the call: tokens are a cache, not state.
"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Callable

from django.core.cache import cache

from .httpclient import Deadline

logger = logging.getLogger(__name__)

LOCK_SECONDS = 20  # longer than one bounded fetch (TOTAL_TIMEOUT_SECONDS) so a slow login keeps the lock
POLL_SECONDS = 0.05


class TokenBusy(Exception):
    """Another worker held the login lock until this call's deadline: the caller maps it to "unavailable"."""


class SharedToken:
    """One provider credential's token: ``fetch(stale)`` logs in (or refreshes ``stale``) and returns it."""

    def __init__(self, name: str, *, ttl_seconds: int, fetch: Callable[[str | None], str]) -> None:
        self.name = name
        self.key = f"gd:{name}:token"
        self.lock_key = f"gd:{name}:token-lock"
        self.ttl_seconds = ttl_seconds
        self.fetch = fetch

    def _cached(self) -> str | None:
        try:
            value = cache.get(self.key)
        except Exception:  # noqa: BLE001 - a cache outage must not fail the provider call
            logger.warning("%s token cache unreadable", self.name)
            return None
        return value if isinstance(value, str) and value else None

    def get(self, deadline: Deadline) -> str:
        return self._cached() or self._acquire(None, deadline)

    def renew(self, stale: str, deadline: Deadline) -> str:
        """A fresh token after ``stale`` was refused; another worker's newer token is reused as it is."""
        current = self._cached()
        if current and current != stale:
            return current
        return self._acquire(stale, deadline)

    def _store(self, token: str) -> None:
        try:
            cache.set(self.key, token, timeout=self.ttl_seconds)
        except Exception:  # noqa: BLE001
            logger.warning("%s token cache unwritable", self.name)

    def _lock(self, owner: str) -> bool | None:
        """``True`` when this call holds the lock, ``False`` when another one does, ``None`` without a
        cache."""
        try:
            return bool(cache.add(self.lock_key, owner, timeout=LOCK_SECONDS))
        except Exception:  # noqa: BLE001
            logger.warning("%s token lock unavailable; fetching without it", self.name)
            return None

    def _unlock(self, owner: str) -> None:
        try:
            if cache.get(self.lock_key) == owner:
                cache.delete(self.lock_key)
        except Exception:  # noqa: BLE001 - the lock expires on its own
            logger.warning("%s token lock not released", self.name)

    def _acquire(self, stale: str | None, deadline: Deadline) -> str:
        owner = uuid.uuid4().hex
        while True:
            locked = self._lock(owner)
            if locked is None:  # no cache at all: every call fetches its own token
                token = self.fetch(stale)
                return token
            if locked:
                try:
                    current = self._cached()
                    if current and current != stale:
                        return current  # another worker fetched it between our read and the lock
                    token = self.fetch(stale)
                    self._store(token)
                    return token
                finally:
                    self._unlock(owner)
            current = self._cached()
            if current and current != stale:
                return current
            if deadline.remaining() <= POLL_SECONDS:
                raise TokenBusy(f"{self.name} token is being fetched by another worker")
            time.sleep(POLL_SECONDS)

    def forget(self) -> None:
        """Drop the cached token (tests, credential rotation)."""
        try:
            cache.delete(self.key)
        except Exception:  # noqa: BLE001
            logger.warning("%s token cache unwritable", self.name)
