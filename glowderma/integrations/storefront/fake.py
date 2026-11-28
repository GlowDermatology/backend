"""The in-memory storefront adapter: records each call (the newest :data:`KEEP` of them) and sends nothing.
Tests read :attr:`FakeStorefront.calls`; a deployment without the URL and the secret runs it as a no-op."""

from __future__ import annotations

from collections import deque
from collections.abc import Sequence

KEEP = 200


class FakeStorefront:
    def __init__(self) -> None:
        self.calls: deque[list[str]] = deque(maxlen=KEEP)

    def revalidate(self, tags: Sequence[str]) -> None:
        self.calls.append(list(tags))

    def tags(self) -> set[str]:
        """Every tag revalidated since the last :meth:`clear`."""
        return {tag for call in self.calls for tag in call}

    def clear(self) -> None:
        self.calls.clear()


__all__ = [
    "FakeStorefront",
]
