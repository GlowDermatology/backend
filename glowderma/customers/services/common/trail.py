"""What a customer account change leaves behind: the audit row and the ``push.account_security`` notice."""

from __future__ import annotations

from typing import Any

from glowderma.accounts import topics
from glowderma.accounts.models import Customer
from glowderma.core import audit, outbox


def audit_customer(action: str, customer: Customer | None, request: Any, after: Any = None) -> None:
    audit.record(
        action=action,
        entity_type="customer",
        entity_id=str(customer.pk) if customer is not None else "",
        actor=customer,
        actor_type="customer",
        request=request,
        after=after,
    )


def notify_account_security(
    customer_id: int, event: str, *, provider: str = "", farewell_tokens: list[str] | tuple[str, ...] = ()
) -> None:
    payload = topics.account_security_payload(
        customer_id, event, provider=provider, farewell_tokens=farewell_tokens
    )
    outbox.enqueue(
        topics.PUSH_ACCOUNT_SECURITY,
        payload,
        dedupe_key=topics.account_security_key(customer_id, event, payload["event_id"]),
    )


__all__ = [
    "audit_customer",
    "notify_account_security",
]
