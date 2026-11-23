"""The per-address failure budget of the ops console's sign-in steps (``IpFailureBudget``).

``IpFailureBudget`` adds a per-address budget to the console's sign-in steps: at most ``OPS_IP_FAILURE_LIMIT``
failed attempts per ``OPS_IP_FAILURE_WINDOW_SECONDS`` from one client address (an IPv6 client by its /64),
counted separately for the password step and for the code steps; an attempt is reserved before it is checked
(concurrent guesses share the budget) and given back when it succeeds. Beyond the budget the address is
refused (429) until the window ends, audited ``console.ip_locked`` and alerted.
"""

from __future__ import annotations

import ipaddress
import math

from django.conf import settings
from django.core.cache import cache
from django.http import HttpRequest
from rest_framework.exceptions import Throttled

from glowderma.accounts.security import counters
from glowderma.core import timeutils
from glowderma.core.clientip import peer_ip


class IpThrottled(Throttled):
    """The client address used up its failure budget (``IpFailureBudget``), not the account its ladder."""


def client_bucket(request: HttpRequest) -> str:
    """The budget key of the request's client: its address as the console's network gate reads it
    (``peer_ip``: the edge's ``X-Forwarded-For`` hop), an IPv6 address by its /64 (one host holds a whole
    /64)."""
    raw = peer_ip(request)
    try:
        address = ipaddress.ip_address(raw)
    except ValueError:
        return "unknown"
    if address.version == 6:
        return str(ipaddress.ip_network(f"{address}/64", strict=False))
    return str(address)


class IpFailureBudget:
    """At most ``OPS_IP_FAILURE_LIMIT`` failed attempts of one ``step`` (``password``, ``code``) per client
    address per ``OPS_IP_FAILURE_WINDOW_SECONDS`` (module doc)."""

    def __init__(self, step: str) -> None:
        self.step = step

    def _count_key(self, bucket: str) -> str:
        return f"auth:ipfail:{self.step}:{bucket}"

    def _lock_key(self, bucket: str) -> str:
        return f"auth:iplock:{self.step}:{bucket}"

    def reserve(self, bucket: str) -> None:
        """Count one attempt against the budget (raise :class:`IpThrottled` while it is used up)."""
        lock_key = self._lock_key(bucket)
        expiry = cache.get(lock_key)
        now = timeutils.now().timestamp()
        if expiry is not None:
            wait = math.ceil(float(expiry) - now)
            if wait > 0:
                raise IpThrottled(wait=wait)
            cache.delete(lock_key)
        window = int(settings.OPS_IP_FAILURE_WINDOW_SECONDS)
        if counters.increment_counter(self._count_key(bucket), window) > int(settings.OPS_IP_FAILURE_LIMIT):
            cache.set(lock_key, now + window, timeout=window)
            raise IpThrottled(wait=window)

    def refund(self, bucket: str) -> None:
        """Give a reserved attempt back: it succeeded."""
        try:
            cache.decr(self._count_key(bucket))
        except ValueError:  # the window ended meanwhile: nothing to give back
            pass


__all__ = ["IpFailureBudget", "IpThrottled", "client_bucket"]
