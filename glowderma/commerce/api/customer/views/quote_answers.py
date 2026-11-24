"""The delivery options of a quote as the customer API renders them."""

from __future__ import annotations

from typing import Any

from glowderma.commerce import quoting
from glowderma.commerce.api.customer.serializers import (
    CustomerDeliveryOptionSerializer,
)


def options_data(options: list[quoting.DeliveryOption]) -> list[dict[str, Any]]:
    rows: Any = [option.as_dict() for option in options]
    serializer: Any = CustomerDeliveryOptionSerializer(rows, many=True)
    return list(serializer.data)


__all__ = [
    "options_data",
]
