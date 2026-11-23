"""Values shared by the accounts models and frozen into migrations (do not rename or move)."""

from __future__ import annotations

from datetime import datetime

from glowderma.core import timeutils

PHONE_E164_REGEX = r"^\+998\d{9}$"


def date_joined_default() -> datetime:
    return timeutils.now()


__all__ = ["PHONE_E164_REGEX", "date_joined_default"]
