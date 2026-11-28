"""The storefront cache port: tell the website that the reads under some tags are stale.

The call is idempotent (expiring a tag twice costs one refetch), so the outbox may repeat it freely:

* :class:`StorefrontRefused` → the website answered with a refusal no retry can fix (a wrong secret, a
  website that does not serve the route, a tag it does not know): the message goes dead with the reason;
* any other exception → transient (the website restarting, a timeout): retried with backoff.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol


class StorefrontRefused(Exception):
    """The website refused the call for good (``detail`` says why, never the secret)."""


class StorefrontCache(Protocol):
    def revalidate(self, tags: Sequence[str]) -> None:
        """Expire the website's cached reads under ``tags`` (raises on failure)."""
        ...


__all__ = [
    "StorefrontCache",
    "StorefrontRefused",
]
