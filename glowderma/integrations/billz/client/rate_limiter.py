"""The integration-wide Billz rate limiter (slots won with ``cache.add``) and the ``Retry-After`` reading."""

from __future__ import annotations

import time
from collections.abc import Callable
from email.utils import parsedate_to_datetime

import httpx
from django.core.cache import cache as default_cache
from django.core.cache.backends.base import BaseCache

from glowderma.integrations.billz.client.config import BATCH
from glowderma.integrations.billz.client.shared_cache import SafeCache
from glowderma.integrations.billz.ports import (
    BillzOutOfTime,
)

RATE_PREFIX = "billz:rate"


RETRY_AFTER_CAP_SECONDS = 60.0


class RateLimiter:
    """At most ``rate`` request starts per second across every process sharing ``cache`` (module doc)."""

    def __init__(
        self,
        rate: float,
        *,
        cache: BaseCache | SafeCache | None = None,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
        prefix: str = RATE_PREFIX,
        max_wait: float = BATCH.limiter_wait,
    ) -> None:
        if rate <= 0:
            raise ValueError("rate must be positive")
        self.interval = 1.0 / rate
        self.cache = SafeCache(cache if cache is not None else default_cache)
        self.clock = clock
        self.sleep = sleep
        self.prefix = prefix
        self.max_wait = max_wait

    @property
    def _pause_key(self) -> str:
        return f"{self.prefix}:pause"

    def acquire(self, *, max_wait: float | None = None, wait_on_pause: bool = True) -> float:
        """Wait for a slot of our own and return the instant the request may start.

        Never waits longer than ``max_wait`` (default: the limiter's own); ``wait_on_pause=False`` fails at
        once while a 429 pause is in force. Either refusal is :class:`BillzUnavailable` (nothing was sent).
        """
        wait = self.max_wait if max_wait is None else max_wait
        if wait <= 0:
            raise BillzOutOfTime("Billz rate limit: no time left to wait for a slot", not_applied=True)
        deadline = self.clock() + wait
        while True:
            now = self.clock()
            paused_until = self.cache.get(self._pause_key)
            if isinstance(paused_until, (int, float)) and paused_until > now:
                if not wait_on_pause or paused_until > deadline:
                    raise BillzOutOfTime(
                        "Billz rate limit: paused longer than the limiter waits", not_applied=True
                    )
                self.sleep(paused_until - now)
                continue
            first = int(now / self.interval) + 1  # the next slot boundary: never mid-slot
            last = int(deadline / self.interval)
            ttl = int(wait) + 5
            for slot in range(first, last + 1):
                if self.cache.add(f"{self.prefix}:{slot}", 1, timeout=ttl):
                    start = slot * self.interval
                    self.sleep(max(0.0, start - self.clock()))
                    return start
            raise BillzOutOfTime(
                "Billz rate limit: every slot the limiter may wait for is taken", not_applied=True
            )

    def pause(self, seconds: float) -> None:
        """Hold every worker back for ``seconds`` (a 429 answer). Not atomic: two 429s at once may keep the
        shorter pause, which only means the next request meets another 429 and pauses again."""
        until = self.clock() + seconds
        current = self.cache.get(self._pause_key)
        if isinstance(current, (int, float)) and current >= until:
            return
        self.cache.set(self._pause_key, until, timeout=int(seconds) + 2)


def retry_after_seconds(response: httpx.Response, *, now: float) -> float | None:
    """``Retry-After`` as seconds (delta or HTTP date), capped; ``None`` when absent or unreadable."""
    value = response.headers.get("Retry-After", "").strip()
    if not value:
        return None
    try:
        seconds = float(value)
    except ValueError:
        try:
            seconds = parsedate_to_datetime(value).timestamp() - now
        except TypeError, ValueError:
            return None
    if seconds != seconds:  # NaN
        return None
    return min(max(seconds, 0.0), RETRY_AFTER_CAP_SECONDS)


__all__ = [
    "RATE_PREFIX",
    "RETRY_AFTER_CAP_SECONDS",
    "RateLimiter",
    "retry_after_seconds",
]
