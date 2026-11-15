"""Delivery quotes (plan decision 4, §3.4): the prices shown to a customer, persisted so checkout accepts a
quote id and never a client price.

``commerce.quoting`` asks the carriers (outside any transaction) and stores every available Yandex option
through :func:`create_quote`; a later ``delivery/calculate/`` for the same customer, address snapshot, method,
weight and Yandex class reuses an unexpired, unused row (:func:`reusable_quote`) instead of asking Yandex
again. Checkout locks the row inside its transaction (:func:`lock_for_checkout`, no network) and marks it used
with the order (:func:`mark_used`); a quote lost to a race raises :class:`QuoteUnusable`.

Only ids, codes and amounts go into ``meta`` (zone id, taxi class); the address snapshot is the text the
customer saved plus the pin — never a phone.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

from django.db import transaction

from glowderma.core import enums
from glowderma.delivery.models import DeliveryQuote

QUOTE_TTL = timedelta(minutes=15)
# documented class limits (Yandex claims/create ``items[].weight``: courier up to 10 kg, express up to 20 kg);
# a class without a documented limit (``cargo``) is not limited here
CLASS_WEIGHT_LIMIT_G: dict[str, int] = {"courier": 10_000, "express": 20_000}

REASON_NOT_FOUND = "not_found"
REASON_USED = "used"
REASON_EXPIRED = "expired"
REASON_ADDRESS_CHANGED = "address_changed"


class QuoteUnusable(Exception):
    """The quote cannot pay for an order: ``reason`` is one of the ``REASON_*`` codes."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True, slots=True)
class AddressSnapshot:
    """What a quote was computed for: the saved address (``address_id``; ``None`` for a bare region/city
    pair), its text, its pin and its BTS codes."""

    address_id: int | None
    text: str
    latitude: Decimal | None
    longitude: Decimal | None
    region_code: str = ""
    city_code: str = ""

    @property
    def has_pin(self) -> bool:
        return self.latitude is not None and self.longitude is not None

    def same_place(self, other: AddressSnapshot) -> bool:
        """Same saved address, same text, same pin (the BTS codes do not change a Yandex price)."""
        return (
            self.address_id == other.address_id
            and self.text == other.text
            and self.latitude == other.latitude
            and self.longitude == other.longitude
        )


@dataclass(frozen=True, slots=True)
class QuoteRow:
    id: UUID
    customer_id: int
    address: AddressSnapshot
    provider: str
    method: str
    carrier_price: int
    customer_fee: int
    weight_g: int
    eta_from: datetime | None
    eta_to: datetime | None
    scheduled: bool
    created_at: datetime
    expires_at: datetime
    used_at: datetime | None
    order_id: int | None
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def taxi_class(self) -> str:
        return str(self.meta.get("taxi_class") or "")

    def usable_at(self, now: datetime) -> bool:
        return self.used_at is None and now < self.expires_at


def _row(quote: DeliveryQuote) -> QuoteRow:
    return QuoteRow(
        id=quote.pk,
        customer_id=quote.customer_id,
        address=AddressSnapshot(
            address_id=quote.address_id,
            text=quote.address_text,
            latitude=quote.latitude,
            longitude=quote.longitude,
            region_code=quote.region_code,
            city_code=quote.city_code,
        ),
        provider=quote.provider,
        method=quote.method,
        carrier_price=quote.carrier_price,
        customer_fee=quote.customer_fee,
        weight_g=quote.weight_g,
        eta_from=quote.eta_from,
        eta_to=quote.eta_to,
        scheduled=quote.scheduled,
        created_at=quote.created_at,
        expires_at=quote.expires_at,
        used_at=quote.used_at,
        order_id=quote.order_id,
        meta=dict(quote.meta) if isinstance(quote.meta, dict) else {},
    )


def create_quote(
    customer_id: int,
    *,
    address: AddressSnapshot,
    method: str,
    carrier_price: int,
    customer_fee: int,
    weight_g: int,
    eta_from: datetime | None,
    eta_to: datetime | None,
    scheduled: bool,
    meta: dict[str, Any],
    now: datetime,
    provider: str = enums.DeliveryProvider.YANDEX,
) -> QuoteRow:
    """Store one available option (valid :data:`QUOTE_TTL` from ``now``); its own short transaction."""
    quote = DeliveryQuote.objects.create(
        customer_id=customer_id,
        address_id=address.address_id,
        address_text=address.text[:300],
        latitude=address.latitude,
        longitude=address.longitude,
        region_code=address.region_code,
        city_code=address.city_code,
        provider=provider,
        method=method,
        carrier_price=carrier_price,
        customer_fee=customer_fee,
        weight_g=weight_g,
        eta_from=eta_from,
        eta_to=eta_to,
        scheduled=scheduled,
        meta=meta,
        created_at=now,
        expires_at=now + QUOTE_TTL,
    )
    return _row(quote)


def reusable_quote(
    customer_id: int,
    *,
    address: AddressSnapshot,
    method: str,
    weight_g: int,
    taxi_class: str,
    now: datetime,
    provider: str = enums.DeliveryProvider.YANDEX,
) -> QuoteRow | None:
    """The newest unexpired, unused quote of the customer for the same address snapshot, method, weight and
    Yandex class (so a repeated ``delivery/calculate/`` does not ask Yandex again), else ``None``."""
    candidates = DeliveryQuote.objects.filter(
        customer_id=customer_id,
        provider=provider,
        method=method,
        weight_g=weight_g,
        address_id=address.address_id,
        address_text=address.text[:300],
        latitude=address.latitude,
        longitude=address.longitude,
        used_at__isnull=True,
        expires_at__gt=now,
        meta__taxi_class=taxi_class,
    ).order_by("-created_at")
    quote = candidates.first()
    return _row(quote) if quote is not None else None


def get_quote(quote_id: UUID, customer_id: int) -> QuoteRow | None:
    """The customer's quote (another customer's id reads as ``None``)."""
    quote = DeliveryQuote.objects.filter(pk=quote_id, customer_id=customer_id).first()
    return _row(quote) if quote is not None else None


def lock_for_checkout(
    quote_id: UUID, *, customer_id: int, address: AddressSnapshot, now: datetime
) -> QuoteRow:
    """Inside the caller's checkout transaction: lock the quote row and check it can still pay for an order
    to ``address`` at ``now`` (the customer's, unused, unexpired, same address snapshot). Raises
    :class:`QuoteUnusable`; no network."""
    if not transaction.get_connection().in_atomic_block:
        raise RuntimeError("lock_for_checkout runs inside the checkout transaction")
    quote = DeliveryQuote.objects.select_for_update().filter(pk=quote_id, customer_id=customer_id).first()
    if quote is None:
        raise QuoteUnusable(REASON_NOT_FOUND)
    row = _row(quote)
    if row.used_at is not None:
        raise QuoteUnusable(REASON_USED)
    if now >= row.expires_at:
        raise QuoteUnusable(REASON_EXPIRED)
    if not row.address.same_place(address):
        raise QuoteUnusable(REASON_ADDRESS_CHANGED)
    return row


def mark_used(quote_id: UUID, *, order_id: int, at: datetime) -> None:
    """The locked quote paid for ``order_id`` (same transaction as the order); a second use raises
    :class:`QuoteUnusable`."""
    updated = DeliveryQuote.objects.filter(pk=quote_id, used_at__isnull=True).update(
        used_at=at, order_id=order_id
    )
    if updated != 1:
        raise QuoteUnusable(REASON_USED)


__all__ = [
    "CLASS_WEIGHT_LIMIT_G",
    "QUOTE_TTL",
    "REASON_ADDRESS_CHANGED",
    "REASON_EXPIRED",
    "REASON_NOT_FOUND",
    "REASON_USED",
    "AddressSnapshot",
    "QuoteRow",
    "QuoteUnusable",
    "create_quote",
    "get_quote",
    "lock_for_checkout",
    "mark_used",
    "reusable_quote",
]
