"""Single-use loyalty QR keys in Redis (customer contract §2.9.3, §2.10).

Both the issue (``put``) and the cashier's resolve (``take``, ``GETDEL``) use the **raw** redis-py client of
the default cache with the literal key prefix ``gd:loyalty:qr:`` — never ``cache.set``, whose ``KEY_PREFIX``
and version would make the two sides disagree. ``put`` keeps at most ``max_live`` keys per signed-in
session (the list ``gd:loyalty:qr:live:{customer_id}:s:{session_id}``; issuing one more deletes the oldest),
so the customer's devices (B-W1: several sessions at once) never cancel each other's QR. Every key is also
listed under the customer (``gd:loyalty:qr:live:{customer_id}``, capped at ``CUSTOMER_LIST_CAP``, oldest
dropped — far older than a token's TTL under the ``qr`` throttle). ``revoke_session`` deletes the keys of one
session (logout, a session revoked from the sessions list) and ``revoke_all`` every key of a customer
(sign-out everywhere, password change, account deleted). Each value names its session
(``"{customer_id}:{issued_at_epoch}:{session_id}"``), so a resolve can refuse a token whose session ended
even when the on-commit revocation was lost. It lives in ``core`` so that ``accounts``
(revocation) and ``loyalty`` (issue and resolve) can both use it. Without a Redis cache (LocMem tests) an
in-process store behind a lock keeps the same semantics, including the atomic single use.
"""

from __future__ import annotations

import threading
import time
from typing import Any

from django.core.cache import caches
from django.core.cache.backends.redis import RedisCache

PREFIX = "gd:loyalty:qr:"
LIVE_PREFIX = f"{PREFIX}live:"

CUSTOMER_LIST_CAP = 64

_PUT_SCRIPT = """
if not redis.call('SET', KEYS[1], ARGV[1], 'NX', 'EX', ARGV[2]) then return 0 end
redis.call('RPUSH', KEYS[2], KEYS[1])
redis.call('EXPIRE', KEYS[2], ARGV[2])
while redis.call('LLEN', KEYS[2]) > tonumber(ARGV[3]) do
  local oldest = redis.call('LPOP', KEYS[2])
  redis.call('DEL', oldest)
end
redis.call('RPUSH', KEYS[3], KEYS[1])
redis.call('EXPIRE', KEYS[3], ARGV[2])
while redis.call('LLEN', KEYS[3]) > tonumber(ARGV[4]) do
  redis.call('LPOP', KEYS[3])
end
return 1
"""
_REVOKE_SCRIPT = """
local keys = redis.call('LRANGE', KEYS[1], 0, -1)
for _, key in ipairs(keys) do redis.call('DEL', key) end
redis.call('DEL', KEYS[1])
return #keys
"""


def token_key(key_hash: str) -> str:
    return f"{PREFIX}{key_hash}"


def live_key(customer_id: int | str) -> str:
    return f"{LIVE_PREFIX}{customer_id}"


def session_live_key(customer_id: int | str, session_id: str) -> str:
    return f"{LIVE_PREFIX}{customer_id}:s:{session_id}"


def _redis(key: str) -> Any | None:
    """The raw redis-py client of the default cache, or ``None`` when the cache is not Redis."""
    backend = caches["default"]
    if isinstance(backend, RedisCache):
        return backend._cache.get_client(key, write=True)
    return None


class _LocalStore:
    """The LocMem stand-in: values with a monotonic expiry and per-customer lists, all behind one lock."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.values: dict[str, tuple[str, float]] = {}
        self.lists: dict[str, tuple[list[str], float]] = {}

    def _live(self, key: str, now: float) -> str | None:
        entry = self.values.get(key)
        if entry is None:
            return None
        if entry[1] <= now:
            del self.values[key]
            return None
        return entry[0]

    def _append(self, list_key: str, key: str, cap: int, now: float, ttl: int, *, drop: bool) -> None:
        items, expiry = self.lists.get(list_key, ([], 0.0))
        items = (items if expiry > now else []) + [key]
        while len(items) > cap:
            oldest = items.pop(0)
            if drop:
                self.values.pop(oldest, None)
        self.lists[list_key] = (items, now + ttl)

    def put(self, key: str, lists: tuple[str, str], value: str, ttl: int, max_live: int) -> bool:
        with self.lock:
            now = time.monotonic()
            if self._live(key, now) is not None:
                return False
            self.values[key] = (value, now + ttl)
            session_list, customer_list = lists
            self._append(session_list, key, max_live, now, ttl, drop=True)
            self._append(customer_list, key, CUSTOMER_LIST_CAP, now, ttl, drop=False)
            return True

    def take(self, key: str) -> str | None:
        with self.lock:
            value = self._live(key, time.monotonic())
            self.values.pop(key, None)
            return value

    def revoke_all(self, list_key: str) -> int:
        with self.lock:
            items, _expiry = self.lists.pop(list_key, ([], 0.0))
            for key in items:
                self.values.pop(key, None)
            return len(items)

    def clear(self) -> None:
        with self.lock:
            self.values.clear()
            self.lists.clear()


_local = _LocalStore()


def put(key_hash: str, customer_id: int, ttl: int, max_live: int, *, session_id: str = "") -> bool:
    """Store ``gd:loyalty:qr:{key_hash}`` → ``"{customer_id}:{issued_at_epoch}:{session_id}"`` (``SET NX
    EX ttl``).

    Returns ``False`` when the key already exists (a 128-bit collision: the caller draws a new token). The
    session's live list keeps the newest ``max_live`` keys; older keys of that session are deleted.
    """
    if ttl < 1 or max_live < 1:
        raise ValueError("ttl and max_live must be positive")
    key = token_key(key_hash)
    value = f"{customer_id}:{int(time.time())}:{session_id}"
    session_list = session_live_key(customer_id, session_id)
    customer_list = live_key(customer_id)
    client = _redis(key)
    if client is None:
        return _local.put(key, (session_list, customer_list), value, ttl, max_live)
    return bool(
        client.eval(_PUT_SCRIPT, 3, key, session_list, customer_list, value, ttl, max_live, CUSTOMER_LIST_CAP)
    )


def take(key_hash: str) -> str | None:
    """Atomically read and delete a key (``GETDEL``): the stored value (:func:`put`) once,
    ``None`` for an unknown, expired or already used key."""
    key = token_key(key_hash)
    client = _redis(key)
    if client is None:
        return _local.take(key)
    raw = client.getdel(key)
    if raw is None:
        return None
    return raw.decode("utf-8") if isinstance(raw, bytes) else str(raw)


def revoke_session(customer_id: int, session_id: str) -> int:
    """Delete every live key one session issued; returns how many keys its live list named."""
    list_key = session_live_key(customer_id, session_id)
    client = _redis(list_key)
    if client is None:
        return _local.revoke_all(list_key)
    return int(client.eval(_REVOKE_SCRIPT, 1, list_key))


def revoke_all(customer_id: int) -> int:
    """Delete every live key of the customer; returns how many keys the live list named."""
    list_key = live_key(customer_id)
    client = _redis(list_key)
    if client is None:
        return _local.revoke_all(list_key)
    return int(client.eval(_REVOKE_SCRIPT, 1, list_key))


def clear_local() -> None:
    """Empty the in-process stand-in (tests)."""
    _local.clear()
