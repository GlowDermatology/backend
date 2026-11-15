"""``delivery.quotes`` (plan §3.4): stored quotes, their reuse, and the lock-and-use step of checkout."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest
from django.db import transaction

from glowderma.accounts.tests.factories import CustomerFactory
from glowderma.core import timeutils
from glowderma.delivery import quotes
from glowderma.delivery.models import DeliveryQuote
from glowderma.delivery.tests.factories import TASHKENT_PIN, OrderRowFactory

pytestmark = pytest.mark.django_db
ADDRESS = quotes.AddressSnapshot(
    address_id=7,
    text="Toshkent, Chilonzor tumani, 9-kvartal, 12-uy",
    latitude=TASHKENT_PIN[0],
    longitude=TASHKENT_PIN[1],
    region_code="10",
    city_code="1001",
)


def _create(customer_id: int, **over: object) -> quotes.QuoteRow:
    values: dict[str, object] = {
        "address": ADDRESS,
        "method": "standard",
        "carrier_price": 18_000,
        "customer_fee": 20_000,
        "weight_g": 600,
        "eta_from": None,
        "eta_to": None,
        "scheduled": False,
        "meta": {"zone_id": "tashkent", "taxi_class": "courier"},
        "now": timeutils.now(),
        **over,
    }
    return quotes.create_quote(customer_id, **values)  # type: ignore[arg-type]


def test_create_snapshots_the_address_and_lives_fifteen_minutes() -> None:
    customer = CustomerFactory.create()
    now = timeutils.now()
    row = _create(customer.pk, now=now)
    stored = DeliveryQuote.objects.get(pk=row.id)
    assert (stored.address_id, stored.address_text, stored.latitude, stored.longitude) == (
        7,
        ADDRESS.text,
        TASHKENT_PIN[0],
        TASHKENT_PIN[1],
    )
    assert (stored.region_code, stored.city_code, stored.provider) == ("10", "1001", "yandex")
    assert stored.expires_at == now + quotes.QUOTE_TTL == now + timedelta(minutes=15)
    assert row.taxi_class == "courier" and row.usable_at(now) and not row.usable_at(stored.expires_at)


def test_reuse_needs_the_same_snapshot_method_weight_and_class() -> None:
    customer = CustomerFactory.create()
    now = timeutils.now()
    row = _create(customer.pk, now=now)

    def find(**over: object) -> quotes.QuoteRow | None:
        values: dict[str, object] = {
            "address": ADDRESS,
            "method": "standard",
            "weight_g": 600,
            "taxi_class": "courier",
            "now": now,
            **over,
        }
        return quotes.reusable_quote(customer.pk, **values)  # type: ignore[arg-type]

    found = find()
    assert found is not None and found.id == row.id
    moved = quotes.AddressSnapshot(7, ADDRESS.text, Decimal("41.300000"), TASHKENT_PIN[1], "10", "1001")
    renamed = quotes.AddressSnapshot(7, "Toshkent, boshqa uy", TASHKENT_PIN[0], TASHKENT_PIN[1], "10", "1001")
    assert find(address=moved) is None and find(address=renamed) is None
    assert (
        find(method="express") is None and find(weight_g=900) is None and find(taxi_class="express") is None
    )
    assert find(now=now + timedelta(minutes=15)) is None
    assert (
        quotes.reusable_quote(
            CustomerFactory.create().pk,
            address=ADDRESS,
            method="standard",
            weight_g=600,
            taxi_class="courier",
            now=now,
        )
        is None
    )
    DeliveryQuote.objects.filter(pk=row.id).update(
        used_at=now, order=OrderRowFactory.create(customer=customer)
    )
    assert find() is None


def test_get_quote_is_scoped_to_the_customer() -> None:
    customer = CustomerFactory.create()
    row = _create(customer.pk)
    assert quotes.get_quote(row.id, customer.pk) == row
    assert quotes.get_quote(row.id, CustomerFactory.create().pk) is None


def test_lock_for_checkout_refusals() -> None:
    customer = CustomerFactory.create()
    now = timeutils.now()
    row = _create(customer.pk, now=now)
    other = quotes.AddressSnapshot(8, ADDRESS.text, ADDRESS.latitude, ADDRESS.longitude)
    cases: list[tuple[dict[str, object], str]] = [
        ({"customer_id": CustomerFactory.create().pk}, quotes.REASON_NOT_FOUND),
        ({"now": row.expires_at}, quotes.REASON_EXPIRED),
        ({"address": other}, quotes.REASON_ADDRESS_CHANGED),
    ]
    for over, reason in cases:
        kwargs: dict[str, object] = {"customer_id": customer.pk, "address": ADDRESS, "now": now, **over}
        with transaction.atomic(), pytest.raises(quotes.QuoteUnusable) as caught:
            quotes.lock_for_checkout(row.id, **kwargs)  # type: ignore[arg-type]
        assert caught.value.reason == reason
    order = OrderRowFactory.create(customer=customer)
    with transaction.atomic():
        locked = quotes.lock_for_checkout(row.id, customer_id=customer.pk, address=ADDRESS, now=now)
        assert locked.id == row.id
        quotes.mark_used(row.id, order_id=order.pk, at=now)
    stored = DeliveryQuote.objects.get(pk=row.id)
    assert stored.used_at == now and stored.order_id == order.pk
    with transaction.atomic(), pytest.raises(quotes.QuoteUnusable) as used:
        quotes.lock_for_checkout(row.id, customer_id=customer.pk, address=ADDRESS, now=now)
    assert used.value.reason == quotes.REASON_USED
    with pytest.raises(quotes.QuoteUnusable):
        quotes.mark_used(row.id, order_id=order.pk, at=now)


def test_same_place_ignores_the_bts_codes() -> None:
    other_codes = quotes.AddressSnapshot(7, ADDRESS.text, ADDRESS.latitude, ADDRESS.longitude, "11", "1101")
    assert ADDRESS.same_place(other_codes) and ADDRESS.has_pin
    assert not quotes.AddressSnapshot(None, "", None, None, "10", "1001").has_pin
