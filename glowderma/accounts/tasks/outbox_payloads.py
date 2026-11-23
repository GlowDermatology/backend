"""Reading an outbox message's payload: sealed fields are opened, the rest passes through."""

from __future__ import annotations

from typing import Any

from glowderma.core.models import OutboxMessage


def read_payload(message: OutboxMessage) -> dict[str, Any]:
    return dict(message.payload) if isinstance(message.payload, dict) else {}


__all__ = [
    "read_payload",
]
