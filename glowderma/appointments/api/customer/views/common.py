"""What the customer ``appointments`` views share: the signed-in customer."""

from __future__ import annotations

from typing import cast

from rest_framework.request import Request

from glowderma.accounts.models import Customer


def signed_in_customer(request: Request) -> Customer:
    return cast(Customer, request.user)


__all__ = [
    "signed_in_customer",
]
