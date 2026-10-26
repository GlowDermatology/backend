"""Read side of `accounts`: the staff directory (contract §2.16 ``GET staff/``)."""

from __future__ import annotations

from typing import TypedDict

from django.db.models import F, Q, QuerySet, Value
from django.db.models.functions import Coalesce, Lower, NullIf

from glowderma.accounts.models import StaffUser


class StaffFilters(TypedDict, total=False):
    search: str
    is_active: bool


def staff_list(filters: StaffFilters) -> QuerySet[StaffUser]:
    """Staff users by display name (full name, else email; case-insensitive), ties by ``id``.

    ``search`` matches the full name or the email (case-insensitive substring); ``is_active`` filters
    when given (the API defaults it to ``true``).
    """
    rows = StaffUser.objects.all()
    if "is_active" in filters:
        rows = rows.filter(is_active=filters["is_active"])
    term = filters.get("search")
    if term:
        rows = rows.filter(Q(full_name__icontains=term) | Q(email__icontains=term))
    display = Coalesce(NullIf(F("full_name"), Value("")), F("email"))
    return rows.order_by(Lower(display), "id")
