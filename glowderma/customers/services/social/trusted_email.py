"""One mailbox, one account: the address a trusted Google identity gives its account (customer contract
§2.1.8, §2.1.9)."""

from __future__ import annotations

from django.db import IntegrityError

from glowderma.accounts import services as account_services
from glowderma.accounts.models import Customer


def adopt_trusted_email(customer: Customer, email: str | None) -> None:
    """One mailbox, one account, completed: an account with a trusted Google identity (``trusted_email``,
    vouched by the provider) but no address of its own takes that address (``email_verified_at`` = now),
    unless another account holds it. Called under the caller's customer lock after a Google sign-in or a
    phone completion; a lost race for the address keeps the account without one rather than failing."""
    if customer.email or not email:
        return
    address = account_services.normalize_email(email)
    if Customer.objects.filter(email=address).exists():
        return
    try:
        account_services.set_customer_email(customer.pk, address)
    except IntegrityError:
        return
    customer.email = address


__all__ = [
    "adopt_trusted_email",
]
