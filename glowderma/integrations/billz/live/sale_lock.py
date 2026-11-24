"""The per-order lock of a Billz sale push, held in the shared cache."""

from __future__ import annotations

import logging
import uuid

from glowderma.integrations.billz.client import SafeCache
from glowderma.integrations.billz.ports import (
    BillzUnavailable,
)

logger = logging.getLogger("glowderma.integrations.billz.live")


SALE_LOCK_KEY = "billz:sale:lock:{order_uuid}"


SALE_LOCK_TTL = 600


class SaleLock:
    """The per-sale cache lock, owned by one worker: released (and renewed) only by its owner (review S4)."""

    def __init__(self, cache: SafeCache, key: str, ttl: int) -> None:
        self.cache = cache
        self.key = key
        self.ttl = ttl
        self.owner = uuid.uuid4().hex

    def acquire(self) -> bool:
        return self.cache.add(self.key, self.owner, timeout=self.ttl)

    def owned(self) -> bool:
        return bool(self.cache.get(self.key) == self.owner)

    def renew(self) -> None:
        """Still ours → extend it; otherwise stop before anything irreversible is sent."""
        if not self.owned():
            raise BillzUnavailable(f"the sale lock {self.key} expired under this worker", not_applied=True)
        self.cache.touch(self.key, timeout=self.ttl)

    def release(self) -> None:
        try:
            if self.owned():
                self.cache.delete(self.key)
        except BillzUnavailable:  # the cache went away: the lock expires on its own
            logger.warning("Could not release the Billz sale lock %s", self.key)


__all__ = [
    "SALE_LOCK_KEY",
    "SALE_LOCK_TTL",
    "SaleLock",
]
