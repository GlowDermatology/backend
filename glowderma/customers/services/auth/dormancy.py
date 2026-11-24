"""The dormant-account rule (customer contract §2.1.5): a reset of an account unused for long starts it
afresh."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from glowderma.accounts import services as account_services
from glowderma.accounts.models import Customer
from glowderma.commerce import services as commerce_services
from glowderma.core import timeutils
from glowderma.customers.services.common import identities, trail


def dormant_reset(customer_id: int, *, lang: str, last_activity: datetime | None, request: Any) -> None:
    """The dormant-account rule (customer contract §2.2.3, M2-A16), inside the caller's transaction and under
    its customer-level lock: the history horizon moves to now; name, image, addresses and cart are scrubbed;
    language becomes the request language; consent is cleared; every identity is unlinked (Apple revoked) and
    every device key cleared. Balance, ledger and purchases stay. Audit ``customer.history_reset``."""
    customer = Customer.objects.get(pk=customer_id)
    account_services.move_history_horizon(customer_id, at=timeutils.now())
    account_services.scrub_profile_for_dormant(customer_id, language=lang)  # image file deleted on commit
    identities.unlink_all_identities(customer, reason="dormant", request=request)
    account_services.clear_device_keys(customer_id)
    commerce_services.delete_cart_lines(customer_id)
    trail.audit_customer(
        "customer.history_reset",
        customer,
        request,
        {
            "reason": "dormant",
            "last_activity_at": timeutils.isoformat(last_activity) if last_activity else None,
        },
    )


__all__ = [
    "dormant_reset",
]
