"""The delivery destination of a quote or a checkout: a saved address or the submitted one."""

from __future__ import annotations

from glowderma.accounts.models import Address, Customer
from glowderma.commerce import quoting
from glowderma.commerce.api.customer.views.common import field_refusal
from glowderma.core import integration_modes
from glowderma.core.api.exceptions import (
    DeliveryUnavailable,
    ProviderUnavailable,
)
from glowderma.delivery import selectors as delivery_selectors
from glowderma.integrations.bts.ports import BtsUnavailable


def delivery_address(
    customer: Customer, address_id: int | None, region_code: str | None, city_code: str | None
) -> tuple[int | None, str, str]:
    """Rows 3 of checkout / the address checks of ``delivery/calculate/``: delivery offered, an own address or
    a region/city pair, known to BTS's reference list. Returns ``(address_id, region_code, city_code)``."""
    if not integration_modes.delivery_available():
        raise DeliveryUnavailable(key="error.delivery_not_offered")
    if address_id is not None and (region_code or city_code):  # exactly one of the two (§2.6)
        raise field_refusal("address_id", "required", message_key="field.address_required")
    if address_id is not None:
        address = Address.objects.filter(pk=address_id, customer_id=customer.pk).first()
        if address is None:
            raise field_refusal("address_id", "does_not_exist", message_key="field.address_not_found")
        region, city = address.region_code, address.city_code
    elif region_code and city_code:
        region, city = region_code, city_code
    else:
        raise field_refusal("address_id", "required", message_key="field.address_required")
    try:
        known = delivery_selectors.region_known(region, city)
    except BtsUnavailable as exc:
        raise ProviderUnavailable() from exc
    if not known:
        field = "address_id" if address_id is not None else "region_code"
        raise field_refusal(field, "address_incomplete", message_key="field.address_incomplete")
    return address_id, region, city


def address_snapshot(address: Address) -> quoting.AddressSnapshot:
    return quoting.AddressSnapshot(
        address_id=address.pk,
        text=address.text,
        latitude=address.latitude,
        longitude=address.longitude,
        region_code=address.region_code,
        city_code=address.city_code,
    )


__all__ = [
    "address_snapshot",
    "delivery_address",
]
