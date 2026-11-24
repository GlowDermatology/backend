"""The reason rule the admin ``commerce`` bodies share and its refusal texts."""

from __future__ import annotations

from glowderma.core.uzsource import N_

REASON_MIN_LENGTH = 3


REASON_MAX_LENGTH = 500


MESSAGE_REASON = N_("Sababni kiriting.")


MESSAGE_REASON_MIN = N_("Kamida 3 ta belgi kiriting.")


MESSAGE_REASON_MAX = N_("500 belgidan oshmasin.")


__all__ = [
    "MESSAGE_REASON",
    "MESSAGE_REASON_MAX",
    "MESSAGE_REASON_MIN",
    "REASON_MAX_LENGTH",
    "REASON_MIN_LENGTH",
]
