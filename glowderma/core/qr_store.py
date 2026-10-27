"""Single-use loyalty QR keys in Redis (customer contract §2.9.3, §2.10).

Both the issue (``put``) and the cashier's resolve (``take``, ``GETDEL``) use the **raw** redis-py client of
the default cache with the literal key prefix ``gd:loyalty:qr:`` — never ``cache.set``, whose ``KEY_PREFIX``
and version would make the two sides disagree. ``put`` keeps at most ``max_live`` keys per customer (the list
``gd:loyalty:qr:live:{customer_id}``; issuing one more deletes the oldest) and ``revoke_all`` deletes every
key of a customer (logout, session replaced, account deleted). It lives in ``core`` so that ``accounts``
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

_PUT_SCRIPT = """
if not redis.call('SET', KEYS[1], ARGV[1], 'NX', 'EX', ARGV[2]) then return 0 end
redis.call('RPUSH', KEYS[2], KEYS[1])
redis.call('EXPIRE', KEYS[2], ARGV[2])
while redis.call('LLEN', KEYS[2]) > tonumber(ARGV[3]) do
  local oldest = redis.call('LPOP', KEYS[2])
  redis.call('DEL', oldest)
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

    def put(self, key: str, list_key: str, value: str, ttl: int, max_live: int) -> bool:
        with self.lock:
            now = time.monotonic()
            if self._live(key, now) is not None:
                return False
            self.values[key] = (value, now + ttl)
            items, expiry = self.lists.get(list_key, ([], 0.0))
            items = (items if expiry > now else []) + [key]
            while len(items) > max_live:
                self.values.pop(items.pop(0), None)
            self.lists[list_key] = (items, now + ttl)
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


def put(key_hash: str, customer_id: int, ttl: int, max_live: int) -> bool:
    """Store ``gd:loyalty:qr:{key_hash}`` → ``"{customer_id}:{issued_at_epoch}"`` (``SET NX EX ttl``).

    Returns ``False`` when the key already exists (a 128-bit collision: the caller draws a new token). The
    customer's live list keeps the newest ``max_live`` keys; older keys are deleted.
    """
    if ttl < 1 or max_live < 1:
        raise ValueError("ttl and max_live must be positive")
    key = token_key(key_hash)
    value = f"{customer_id}:{int(time.time())}"
    client = _redis(key)
    if client is None:
        return _local.put(key, live_key(customer_id), value, ttl, max_live)
    return bool(client.eval(_PUT_SCRIPT, 2, key, live_key(customer_id), value, ttl, max_live))


def take(key_hash: str) -> str | None:
    """Atomically read and delete a key (``GETDEL``): the stored ``"{customer_id}:{issued_at_epoch}"`` once,
    ``None`` for an unknown, expired or already used key."""
    key = token_key(key_hash)
    client = _redis(key)
    if client is None:
        return _local.take(key)
    raw = client.getdel(key)
    if raw is None:
        return None
    return raw.decode("utf-8") if isinstance(raw, bytes) else str(raw)


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
