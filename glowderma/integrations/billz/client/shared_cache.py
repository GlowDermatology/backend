"""The Django cache as the Billz adapter uses it: Redis being down is ``BillzUnavailable``, never a crash."""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from typing import Any

from django.core.cache.backends.base import BaseCache
from redis.exceptions import RedisError

from glowderma.core import alerts
from glowderma.integrations.billz.ports import (
    BillzUnavailable,
)

logger = logging.getLogger("glowderma.integrations.billz.client")


# What a cache backend raises when Redis is unreachable or slow (redis-py's errors; a socket error of
# another backend). Nothing else is caught: Celery's SoftTimeLimitExceeded, a bug or an interrupt must
# propagate as what it is — never turn into "nothing was sent" (review NEW-1).
CACHE_BACKEND_ERRORS: tuple[type[BaseException], ...] = (RedisError, OSError, ConnectionError, TimeoutError)


class SafeCache:
    """The cache as the adapter uses it: a backend failure (Redis down) is :class:`BillzUnavailable`.

    Only the operations the adapter needs; ``timeout`` is always passed explicitly (Django reads ``None`` as
    "never expires"). Only :data:`CACHE_BACKEND_ERRORS` are mapped; the error says ``not_applied`` (no cache
    operation sends anything to Billz) — :meth:`BillzClient._exchange` clears that flag when the failing
    operation ran after a request that Billz may have applied.
    """

    def __init__(self, cache: BaseCache | SafeCache) -> None:
        self.backend: BaseCache = cache.backend if isinstance(cache, SafeCache) else cache

    def _run(self, operation: str, call: Callable[[], Any]) -> Any:
        try:
            return call()
        except CACHE_BACKEND_ERRORS as exc:
            logger.error("billz.cache_unavailable operation=%s error=%s", operation, type(exc).__name__)
            alerts.alert("billz.cache_unavailable", {"operation": operation, "error": type(exc).__name__})
            raise BillzUnavailable("Billz: shared cache unavailable", not_applied=True) from exc

    def get(self, key: str, default: Any = None) -> Any:
        return self._run("get", lambda: self.backend.get(key, default))

    def set(self, key: str, value: Any, *, timeout: float) -> None:
        self._run("set", lambda: self.backend.set(key, value, timeout=timeout))

    def add(self, key: str, value: Any, *, timeout: float) -> bool:
        return bool(self._run("add", lambda: self.backend.add(key, value, timeout=timeout)))

    def delete(self, key: str) -> None:
        self._run("delete", lambda: self.backend.delete(key))

    def set_many(self, data: Mapping[str, Any], *, timeout: float) -> None:
        self._run("set_many", lambda: self.backend.set_many(dict(data), timeout=timeout))

    def touch(self, key: str, *, timeout: float) -> bool:
        return bool(self._run("touch", lambda: self.backend.touch(key, timeout=timeout)))

    def incr(self, key: str, delta: int = 1) -> int:
        return int(self._run("incr", lambda: self.backend.incr(key, delta)))


__all__ = [
    "CACHE_BACKEND_ERRORS",
    "SafeCache",
]
