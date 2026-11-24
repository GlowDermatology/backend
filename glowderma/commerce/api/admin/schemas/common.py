"""OpenAPI declarations shared by the admin ``commerce`` views: the error map of the actions that may reach
a provider."""

from __future__ import annotations

from glowderma.core.api.schema import (
    ADMIN_ERRORS,
    ErrorEnvelopeSerializer,
)

ACTION_ERRORS = {**ADMIN_ERRORS, 503: ErrorEnvelopeSerializer}


__all__ = [
    "ACTION_ERRORS",
]
