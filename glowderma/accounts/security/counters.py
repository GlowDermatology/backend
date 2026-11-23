"""The atomic expiring counter every lockout and budget counts with (Redis MULTI, LocMem fallback)."""

from __future__ import annotations

from django.core.cache import caches
from django.core.cache.backends.redis import RedisCache


def increment_counter(key: str, timeout: int, *, sliding: bool = False) -> int:
    """Add one to the counter ``key`` and return it. Its ``timeout`` runs from the first increment (a fixed
    window), or with ``sliding`` from the latest one."""
    backend = caches["default"]
    if isinstance(backend, RedisCache):
        raw_key = backend.make_and_validate_key(key)
        client = backend._cache.get_client(raw_key, write=True)
        with client.pipeline(transaction=True) as pipeline:
            pipeline.incr(raw_key)
            if sliding:
                pipeline.expire(raw_key, timeout)
            else:
                pipeline.expire(raw_key, timeout, nx=True)
            return int(pipeline.execute()[0])
    # LocMem's increment checks expiry while holding its lock and never creates a missing key.
    backend.add(key, 0, timeout=timeout)
    try:
        count = int(backend.incr(key))
    except ValueError:
        if backend.add(key, 1, timeout=timeout):
            return 1
        count = int(backend.incr(key))
    if sliding:
        backend.touch(key, timeout)
    return count


__all__ = ["increment_counter"]
