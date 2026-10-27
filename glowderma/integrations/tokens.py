"""Provider bearer tokens shared by every worker process (Eskiz, BTS): kept **only** in the Django cache
(Redis DB 0 in production) and fetched single-flight across processes through a ``cache.add`` lock.

* :meth:`SharedToken.get` returns the cached token, or fetches one under the lock (the first caller logs in,
  the others wait for its result and never log in themselves).
* :meth:`SharedToken.renew` is called after a provider refused a token (HTTP 401): it fetches a new token
  unless another worker already replaced the refused one, so a burst of 401s causes one login.

Keys are scoped to the account: ``gd:{provider}:token:{sha256(base_url|login)[:16]}`` (and ``…:token-lock:…``,
``…:token-refused:…``), so rotated credentials or another host never reuse a token fetched for other ones.

**Bounds.** The lock lives :data:`LOCK_SECONDS` (45 s) and a fetch gets at most half of it (its
:class:`~glowderma.integrations.httpclient.Deadline` is the caller's, capped at ``LOCK_SECONDS / 2``), so the
lock never expires while its owner is still logging in; it is released only by its owner (compare-and-delete).

**A refused login** (the adapter's ``fetch`` raises :class:`LoginRefused`) is remembered for
:data:`REFUSED_SECONDS` (60 s): every call in that window fails fast with :class:`LoginRefused` instead of
logging in again with credentials the provider refuses (account lockout risk).

**A cache that is down** (Redis unreachable) fails **closed**: no call logs in on its own. A process-local
fallback keeps one token per account in memory and logs in single-flight under a process lock, at most once
per token lifetime (plus one renewal after a 401, at most once a minute); beyond that the call raises
:class:`TokenBusy` (the adapter's "unavailable, nothing sent").

The token never reaches the database, a log line or an exception message.
"""

from __future__ import annotations

import hashlib
import logging
import threading
import time
import uuid
from collections.abc import Callable
from typing import Any

from django.core.cache import cache

from .httpclient import Deadline

logger = logging.getLogger(__name__)

LOCK_SECONDS = 45  # the lock outlives a fetch, which is bounded to LOCK_SECONDS / 2
POLL_SECONDS = 0.05
REFUSED_SECONDS = 60  # a refused login is not repeated within this window
LOCAL_RENEW_SECONDS = 60  # without a cache: at most one renewal per account per minute

type Fetch = Callable[[str | None, Deadline], str]


class TokenBusy(Exception):
    """No token could be had before this call's deadline (another worker held the login lock, or the cache is
    down and this process already logged in): the caller maps it to "unavailable, nothing sent"."""


class LoginRefused(Exception):
    """The provider refused our credentials (raised by an adapter's ``fetch``, then remembered for
    :data:`REFUSED_SECONDS`): the caller maps it to "unavailable, nothing sent"."""


class _CacheDown(Exception):
    pass


def account_scope(*parts: str) -> str:
    """The key suffix of one account: ``sha256("|".join(parts))[:16]`` (never the login itself)."""
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:16]


class _Local:
    """The process-local fallback of one account (used only while the cache is down)."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.token: str | None = None
        self.expires = 0.0
        self.logged_in_at: float | None = None
        self.refused_until = 0.0


_locals: dict[str, _Local] = {}
_locals_lock = threading.Lock()


def _local(key: str) -> _Local:
    with _locals_lock:
        return _locals.setdefault(key, _Local())


def reset_local() -> None:
    """Forget every process-local fallback token (tests)."""
    with _locals_lock:
        _locals.clear()


class SharedToken:
    """One provider account's token: ``fetch(stale, deadline)`` logs in (or refreshes ``stale``) and returns
    it; ``scope`` is :func:`account_scope` of the account (base URL and login)."""

    def __init__(self, name: str, *, ttl_seconds: int, fetch: Fetch, scope: str = "") -> None:
        self.name = name
        suffix = scope or account_scope(name)
        self.key = f"gd:{name}:token:{suffix}"
        self.lock_key = f"gd:{name}:token-lock:{suffix}"
        self.refused_key = f"gd:{name}:token-refused:{suffix}"
        self.ttl_seconds = ttl_seconds
        self.fetch = fetch

    # --- cache primitives (each raises _CacheDown when the cache cannot be reached) ------------------------

    def _read(self, key: str) -> Any:
        try:
            return cache.get(key)
        except Exception as exc:  # noqa: BLE001 - any backend error means "no cache"
            raise _CacheDown from exc

    def _cached(self) -> str | None:
        value = self._read(self.key)
        return value if isinstance(value, str) and value else None

    def _store(self, token: str) -> None:
        try:
            cache.set(self.key, token, timeout=self.ttl_seconds)
        except Exception:  # noqa: BLE001 - the token is still returned; the next call fetches again
            logger.warning("%s token cache unwritable", self.name)

    def _lock(self, owner: str) -> bool:
        try:
            return bool(cache.add(self.lock_key, owner, timeout=LOCK_SECONDS))
        except Exception as exc:  # noqa: BLE001
            raise _CacheDown from exc

    def _unlock(self, owner: str) -> None:
        """Delete the lock only if this call still owns it (atomic on Redis; the lock expires anyway)."""
        try:
            if not _compare_and_delete(self.lock_key, owner) and cache.get(self.lock_key) == owner:
                cache.delete(self.lock_key)
        except Exception:  # noqa: BLE001 - the lock expires on its own
            logger.warning("%s token lock not released", self.name)

    def _refused(self) -> bool:
        return bool(self._read(self.refused_key))

    def _remember_refusal(self) -> None:
        try:
            cache.set(self.refused_key, 1, timeout=REFUSED_SECONDS)
        except Exception:  # noqa: BLE001
            _local(self.key).refused_until = time.monotonic() + REFUSED_SECONDS

    # --- public ---------------------------------------------------------------------------------------------

    def get(self, deadline: Deadline) -> str:
        return self._token(None, deadline)

    def renew(self, stale: str, deadline: Deadline) -> str:
        """A fresh token after ``stale`` was refused; another worker's newer token is reused as it is."""
        return self._token(stale, deadline)

    def forget(self) -> None:
        """Drop the cached token and a remembered refusal (tests, credential rotation)."""
        try:
            cache.delete_many([self.key, self.refused_key])
        except Exception:  # noqa: BLE001
            logger.warning("%s token cache unwritable", self.name)
        with _locals_lock:
            _locals.pop(self.key, None)

    def _token(self, stale: str | None, deadline: Deadline) -> str:
        try:
            if self._refused():
                raise LoginRefused(f"{self.name} refused our credentials less than a minute ago")
            current = self._cached()
            if current and current != stale:
                return current
            return self._acquire(stale, deadline)
        except _CacheDown:
            logger.warning("%s token cache unavailable; using the process-local token", self.name)
            return self._fallback(stale, deadline)

    def _bounded(self, deadline: Deadline) -> Deadline:
        return Deadline(min(deadline.remaining(), LOCK_SECONDS / 2))

    def _fetch(self, stale: str | None, deadline: Deadline) -> str:
        try:
            return self.fetch(stale, self._bounded(deadline))
        except LoginRefused:
            self._remember_refusal()
            raise

    def _acquire(self, stale: str | None, deadline: Deadline) -> str:
        owner = uuid.uuid4().hex
        while True:
            if self._lock(owner):
                try:
                    current = self._cached()
                    if current and current != stale:
                        return current  # another worker fetched it between our read and the lock
                    token = self._fetch(stale, deadline)
                    self._store(token)
                    return token
                finally:
                    self._unlock(owner)
            current = self._cached()
            if current and current != stale:
                return current
            if self._refused():
                raise LoginRefused(f"{self.name} refused our credentials")
            if deadline.remaining() <= POLL_SECONDS:
                raise TokenBusy(f"{self.name} token is being fetched by another worker")
            time.sleep(POLL_SECONDS)

    def _fallback(self, stale: str | None, deadline: Deadline) -> str:
        """No cache: one token per account in this process, logged in single-flight and rarely."""
        local = _local(self.key)
        if not local.lock.acquire(timeout=max(0.0, deadline.remaining())):
            raise TokenBusy(f"{self.name} token is being fetched in this process")
        try:
            now = time.monotonic()
            if local.refused_until > now:
                raise LoginRefused(f"{self.name} refused our credentials less than a minute ago")
            if local.token and local.expires > now and local.token != stale:
                return local.token
            last = local.logged_in_at
            renewal = stale is not None and stale == local.token
            allowed = last is None or now - last >= (LOCAL_RENEW_SECONDS if renewal else self.ttl_seconds)
            if not allowed:
                raise TokenBusy(f"{self.name} token cache is down and this process logged in already")
            local.logged_in_at = now
            try:
                token = self.fetch(stale, self._bounded(deadline))
            except LoginRefused:
                local.refused_until = time.monotonic() + REFUSED_SECONDS
                raise
            local.token, local.expires = token, time.monotonic() + self.ttl_seconds
            return token
        finally:
            local.lock.release()


_COMPARE_AND_DELETE = """
if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('del', KEYS[1]) else return 0 end
"""


def _compare_and_delete(key: str, owner: str) -> bool:
    """Atomic "delete ``key`` if it still holds ``owner``" on Django's Redis cache; ``False`` when the backend
    is not Redis (the caller falls back to get + delete, which only a lock expiring in between can race)."""
    backend: Any = getattr(cache, "_cache", None)
    get_client = getattr(backend, "get_client", None)
    serializer = getattr(backend, "_serializer", None)
    make_key = getattr(cache, "make_and_validate_key", None)
    if get_client is None or serializer is None or make_key is None:
        return False
    full_key = make_key(key)
    client = get_client(full_key, write=True)
    client.eval(_COMPARE_AND_DELETE, 1, full_key, serializer.dumps(owner))
    return True
