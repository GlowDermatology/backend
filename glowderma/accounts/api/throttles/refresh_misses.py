"""``auth/refresh/``'s budget of refresh tokens that match no session (customer contract §1.8)."""

from __future__ import annotations

from typing import Any

from rest_framework.request import Request

from glowderma.accounts.api.throttles.base import ClientIPThrottle


class RefreshUnknownThrottle(ClientIPThrottle):
    """``auth/refresh/`` per client IP, counting **only** refresh tokens that match no session.

    ``allow_request`` refuses once the window is full but never counts; the refresh view calls
    :meth:`record_miss` when the presented token matched nothing, so junk-token rotation cannot bypass the
    per-token ``refresh`` key while honest refreshes never use this budget.
    """

    scope = "refresh_unknown"
    # Set by SimpleRateThrottle.__init__ from the parsed rate (not declared by the stubs).
    num_requests: int
    duration: int

    def _key(self, request: Request, view: Any) -> str:
        key = self.get_cache_key(request, view)
        if key is None:  # per client IP: the key always exists
            raise RuntimeError("refresh_unknown has no cache key")
        return key

    def _recent(self, key: str) -> list[float]:
        now = self.timer()
        return [moment for moment in self.cache.get(key, []) if moment > now - self.duration]

    def allow_request(self, request: Request, view: Any) -> bool:
        if self.rate is None:
            return True
        self.key = self._key(request, view)
        self.now = self.timer()
        self.history = self._recent(self.key)
        if len(self.history) >= self.num_requests:
            return self.throttle_failure()
        return True

    def record_miss(self, request: Request, view: Any = None) -> None:
        key = self._key(request, view)
        history = self._recent(key)
        history.insert(0, self.timer())
        self.cache.set(key, history, self.duration)


__all__ = ["RefreshUnknownThrottle"]
