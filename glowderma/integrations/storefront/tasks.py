"""The ``storefront.revalidate`` outbox handler (``core.storefront``): one call with the message's tags.

Imported by ``IntegrationsConfig.ready`` so every process that runs ``core.dispatch_outbox`` knows it."""

from __future__ import annotations

from glowderma.core import outbox, storefront
from glowderma.core.models import OutboxMessage
from glowderma.integrations.storefront import registry
from glowderma.integrations.storefront.ports import StorefrontRefused


@outbox.register(storefront.TOPIC)
def revalidate(message: OutboxMessage) -> None:
    tags = [tag for tag in message.payload.get("tags", []) if tag in storefront.TAGS]
    if not tags:
        return
    try:
        registry.get_storefront_cache().revalidate(tags)
    except StorefrontRefused as exc:
        raise outbox.PermanentFailure(f"storefront refused the revalidation: {exc}") from exc


__all__ = [
    "revalidate",
]
