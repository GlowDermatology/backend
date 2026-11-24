"""What a customer account change leaves behind: the audit row and the ``push.account_security`` notice."""

from __future__ import annotations

from typing import Any

from glowderma.accounts import services as account_services
from glowderma.accounts import topics
from glowderma.accounts.models import Customer, Device
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


def is_new_device(customer_id: int, fid: str) -> bool:
    """No device key of this install belongs to the account yet (read **before** the sign-in issues one)."""
    return not Device.objects.filter(customer_id=customer_id, fid=fid).exclude(key_hash="").exists()


def alert_by_email(customer_id: int, kind: str) -> None:
    """A security alert e-mail (``email.notice`` ``kind``: ``new_sign_in``, ``password_reset``,
    ``password_changed``) to the account's **verified** address, in its language; nothing without one. The
    text names no code, IP address or device id."""
    row = Customer.objects.filter(pk=customer_id).values("email", "email_verified_at", "language").first()
    if row is None or not row["email"] or row["email_verified_at"] is None:
        return
    account_services.enqueue_email_notice(str(row["email"]), lang=str(row["language"] or "uz"), kind=kind)


__all__ = [
    "alert_by_email",
    "audit_customer",
    "is_new_device",
    "notify_account_security",
]
