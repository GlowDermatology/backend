"""The ``me/`` payload and the customer's addresses (customer contract §2.2)."""

from __future__ import annotations

from typing import Any

from glowderma.accounts.models import Address, Customer, CustomerSocialIdentity, Device
from glowderma.core import timeutils
from glowderma.customers.selectors import common
from glowderma.site_config import selectors as site_config_selectors


def address_payload(address: Address) -> dict[str, Any]:
    return {
        "id": address.pk,
        "label": address.label,
        "text": address.text,
        "region_code": address.region_code,
        "city_code": address.city_code,
        "comment": address.comment,
        "latitude": float(address.latitude) if address.latitude is not None else None,
        "longitude": float(address.longitude) if address.longitude is not None else None,
        "is_default": address.is_default,
        "created_at": timeutils.isoformat(address.created_at),
        "updated_at": timeutils.isoformat(address.updated_at),
    }


def customer_addresses(customer_id: int) -> list[Address]:
    """Default first, then ``updated_at`` desc (customer contract §2.2.7)."""
    return list(Address.objects.filter(customer_id=customer_id).order_by("-is_default", "-updated_at", "-id"))


def me_payload(customer: Customer, *, fid: str | None) -> dict[str, Any]:
    """``Me`` (customer contract §2.2.1); ``push_enabled`` of the current device (``fid``)."""
    terms = site_config_selectors.current_terms()
    current = terms.version if terms.published else ""
    providers = sorted(
        CustomerSocialIdentity.objects.filter(customer_id=customer.pk).values_list("provider", flat=True)
    )
    device = Device.objects.filter(customer_id=customer.pk, fid=fid).first() if fid else None
    default = Address.objects.filter(customer_id=customer.pk, is_default=True).first()
    return {
        "id": customer.pk,
        "phone": customer.phone or None,
        "email": customer.email or None,
        "name": customer.name,
        "language": customer.language,
        **common.media_urls("image", customer.image),
        "registered_at": timeutils.isoformat(customer.history_from or customer.registered_at),
        "has_password": bool(customer.password),
        "linked_providers": providers,
        "push_enabled": bool(device and device.push_enabled),
        "consent": {
            "terms_version": customer.terms_version or None,
            "accepted_at": timeutils.isoformat(customer.consent_at) if customer.consent_at else None,
            "current_version": current or None,
            "update_required": bool(current) and customer.terms_version != current,
        },
        "default_address": address_payload(default) if default is not None else None,
    }


__all__ = [
    "address_payload",
    "customer_addresses",
    "me_payload",
]
