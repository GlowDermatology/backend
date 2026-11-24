"""Unlinking a customer's social identities (one, or all of them on a reset or a deletion)."""

from __future__ import annotations

from typing import Any

from glowderma.accounts import services as account_services
from glowderma.accounts.models import Customer, CustomerSocialIdentity
from glowderma.customers.services.common import trail


def unlink_identity(
    identity: CustomerSocialIdentity, *, reason: str, customer: Customer, request: Any
) -> None:
    trail.audit_customer(
        "customer.social_unlinked", customer, request, {"provider": identity.provider, "reason": reason}
    )
    account_services.unlink_customer_identity(identity)


def unlink_all_identities(customer: Customer, *, reason: str, request: Any) -> None:
    for identity in list(CustomerSocialIdentity.objects.filter(customer_id=customer.pk).order_by("pk")):
        unlink_identity(identity, reason=reason, customer=customer, request=request)


__all__ = [
    "unlink_all_identities",
    "unlink_identity",
]
