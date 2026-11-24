"""OpenAPI declarations shared by the customer ``appointments`` views: the header parameter lists."""

from __future__ import annotations

from glowderma.core.api.schema import (
    ACCEPT_LANGUAGE_HEADER,
    DEVICE_HEADERS,
    OPTIONAL_DEVICE_HEADERS,
)

HEADERS = [*DEVICE_HEADERS, ACCEPT_LANGUAGE_HEADER]


PUBLIC_HEADERS = [*OPTIONAL_DEVICE_HEADERS, ACCEPT_LANGUAGE_HEADER]


__all__ = [
    "HEADERS",
    "PUBLIC_HEADERS",
]
