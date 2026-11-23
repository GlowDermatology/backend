"""An address over its sign-in failure budget: audited, logged and alerted (the address is in the audit row,
never in the alert)."""

from __future__ import annotations

import logging

from django.http import HttpRequest

from glowderma.accounts.console import events
from glowderma.core import alerts

logger = logging.getLogger("glowderma.accounts.console")


def record_ip_locked(request: HttpRequest, step: str, wait: int, email: str = "") -> None:
    """Audit and alert an address over its budget (the address is in the audit row, never in the alert)."""
    events.record("console.ip_locked", request, email=email, step=step, wait=wait)
    logger.warning("console.ip_locked step=%s wait=%s", step, wait)
    alerts.alert("console.ip_locked", {"step": step, "wait": wait}, scope=step)


__all__ = [
    "record_ip_locked",
]
