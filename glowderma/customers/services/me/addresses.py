"""The signed-in customer's addresses (customer contract §2.2.7), under the same signed-in lock as the
profile."""

from __future__ import annotations

from typing import Any

from django.conf import settings
from django.db import transaction

from glowderma.accounts import services as account_services
from glowderma.accounts.models import Address
from glowderma.core.api.exceptions import (
    AddressLimit,
)


def add_address(customer_id: int, *, sid: str | account_services.NoSession, **fields: Any) -> Address:
    """``POST me/addresses/``: at most ``ADDRESS_MAX_PER_CUSTOMER`` (409 ``address_limit``)."""
    limit = int(settings.ADDRESS_MAX_PER_CUSTOMER)
    with transaction.atomic():
        account_services.lock_signed_in_customer(customer_id, sid)
        if Address.objects.filter(customer_id=customer_id).count() >= limit:
            raise AddressLimit(details={"limit": limit}, params={"limit": limit})
        return _reread(account_services.add_address(customer_id, **fields))


def update_address(
    customer_id: int, address_id: int, *, sid: str | account_services.NoSession, **fields: Any
) -> Address:
    """``PATCH me/addresses/{id}/``: the row re-read under the lock (404 when it was removed meanwhile)."""
    with transaction.atomic():
        account_services.lock_signed_in_customer(customer_id, sid)
        return _reread(account_services.update_customer_address(address_id, customer_id, **fields))


def set_default_address(
    customer_id: int, address_id: int, *, sid: str | account_services.NoSession
) -> Address:
    with transaction.atomic():
        account_services.lock_signed_in_customer(customer_id, sid)
        return _reread(account_services.set_customer_default_address(address_id, customer_id))


def _reread(address: Address) -> Address:
    """The stored row (database defaults and normalised decimals), read under the caller's customer-level
    lock: the view builds its response from it, never from a read after commit, which a concurrent ``DELETE
    me/addresses/{id}/`` or ``DELETE me/`` could answer with ``DoesNotExist`` (a 500)."""
    return Address.objects.get(pk=address.pk)


def remove_address(customer_id: int, address_id: int, *, sid: str | account_services.NoSession) -> None:
    with transaction.atomic():
        account_services.lock_signed_in_customer(customer_id, sid)
        account_services.remove_customer_address(address_id, customer_id)


__all__ = [
    "add_address",
    "remove_address",
    "set_default_address",
    "update_address",
]
