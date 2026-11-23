"""Customer delivery addresses (customer contract §2.2.4): one default per customer and the address text
kept in sync on the customer row.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from django.db import transaction

from glowderma.accounts.models import Address, Customer
from glowderma.accounts.services.customer import locks
from glowderma.core import timeutils


def _sync_address_text(customer_id: int) -> str:
    """``Customer.address_text`` = the default address, else the most recently updated one, else ``""``."""
    address = (
        Address.objects.filter(customer_id=customer_id).order_by("-is_default", "-updated_at", "-id").first()
    )
    text = address.text if address is not None else ""
    Customer.objects.filter(pk=customer_id).update(address_text=text, updated_at=timeutils.now())
    return text


def _clear_other_defaults(customer_id: int, *, except_pk: int | None = None) -> None:
    """Clear the customer's current default (but ``except_pk``) before another address takes it: the partial
    unique constraint allows one default per customer."""
    defaults = Address.objects.filter(customer_id=customer_id, is_default=True)
    if except_pk is not None:
        defaults = defaults.exclude(pk=except_pk)
    defaults.update(is_default=False, updated_at=timeutils.now())


@transaction.atomic
def add_address(
    customer_id: int,
    *,
    text: str,
    label: str = "",
    is_default: bool = False,
    region_code: str = "",
    city_code: str = "",
    comment: str = "",
    latitude: Decimal | None = None,
    longitude: Decimal | None = None,
) -> Address:
    """Create an address of the customer and recompute the denormalised ``address_text``.

    The first address of a customer always becomes the default; ``is_default=True`` replaces the previous one
    inside the same transaction (the partial unique constraint forbids two).
    """
    locks.lock_customer(customer_id)
    if is_default or not Address.objects.filter(customer_id=customer_id).exists():
        _clear_other_defaults(customer_id)
        is_default = True
    address = Address.objects.create(
        customer_id=customer_id,
        text=text,
        label=label,
        is_default=is_default,
        region_code=region_code,
        city_code=city_code,
        comment=comment,
        latitude=latitude,
        longitude=longitude,
    )
    _sync_address_text(customer_id)
    return address


_UNSET: Any = object()


@transaction.atomic
def update_address(
    address: Address,
    *,
    text: str = _UNSET,
    label: str = _UNSET,
    region_code: str = _UNSET,
    city_code: str = _UNSET,
    comment: str = _UNSET,
    latitude: Decimal | None = _UNSET,
    longitude: Decimal | None = _UNSET,
    is_default: bool = _UNSET,
) -> Address:
    """Edit an address (M2 ``PATCH /me/addresses/{id}``) and recompute ``Customer.address_text``.

    Only the keyword arguments given change. ``is_default=True`` clears the previous default inside the same
    transaction; ``is_default=False`` on the current default leaves the customer without one (``address_text``
    then falls back to the most recently updated address). The write path every edit must take so the design
    01 §1.3 invariant (saving an address recomputes the denormalised text) holds.
    """
    locks.lock_customer(address.customer_id)
    changes = {
        name: value
        for name, value in (
            ("text", text),
            ("label", label),
            ("region_code", region_code),
            ("city_code", city_code),
            ("comment", comment),
            ("latitude", latitude),
            ("longitude", longitude),
            ("is_default", is_default),
        )
        if value is not _UNSET
    }
    if changes.get("is_default"):
        _clear_other_defaults(address.customer_id, except_pk=address.pk)
    for name, value in changes.items():
        setattr(address, name, value)
    address.save(update_fields=[*changes])  # TimeStampedModel adds updated_at
    _sync_address_text(address.customer_id)
    return address


@transaction.atomic
def set_default_address(address: Address) -> Address:
    """Make ``address`` the customer's default, clearing the previous one in the same transaction."""
    locks.lock_customer(address.customer_id)
    _clear_other_defaults(address.customer_id, except_pk=address.pk)
    if not address.is_default:
        address.is_default = True
        address.save(update_fields=["is_default"])
    _sync_address_text(address.customer_id)
    return address


@transaction.atomic
def remove_address(address: Address) -> None:
    """Delete one address; the denormalised text falls back to the next best address (or ``""``)."""
    customer_id = address.customer_id
    locks.lock_customer(customer_id)
    address.delete()
    _sync_address_text(customer_id)


def update_customer_address(address_id: int, customer_id: int, **changes: Any) -> Address:
    """``PATCH me/addresses/{id}/`` on the row re-read under the customer-level lock (404 when another
    request removed it meanwhile)."""
    return update_address(locks.lock_address(address_id, customer_id), **changes)


def set_customer_default_address(address_id: int, customer_id: int) -> Address:
    return set_default_address(locks.lock_address(address_id, customer_id))


def remove_customer_address(address_id: int, customer_id: int) -> None:
    remove_address(locks.lock_address(address_id, customer_id))


__all__ = [
    "add_address",
    "update_address",
    "set_default_address",
    "remove_address",
    "update_customer_address",
    "set_customer_default_address",
    "remove_customer_address",
]
