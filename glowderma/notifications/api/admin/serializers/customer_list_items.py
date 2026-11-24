"""``CustomerListItem``: one customer row of the admin customers list and of a segment's members."""

from __future__ import annotations

from rest_framework import serializers

from glowderma.accounts.models import Customer
from glowderma.core import enums
from glowderma.core.api.fields import (
    PercentField,
)


class CustomerListItemSerializer(serializers.Serializer[Customer]):
    """Contract §2.6 ``CustomerListItem``: the one renderer of ``GET customers/`` and of segment members.

    Rows carry the ``loyalty.selectors.list_annotations()`` values (``lifetime_total``, ``balance``,
    ``percent``, ``online_orders_count``).
    """

    id = serializers.IntegerField()
    name = serializers.CharField(allow_blank=True)
    phone = serializers.CharField(allow_null=True, help_text="Null for an account without a phone yet.")
    email = serializers.CharField(allow_null=True, help_text="Verified e-mail address | null.")
    status = serializers.ChoiceField(choices=enums.CustomerStatus.values)
    registered_at = serializers.DateTimeField()
    lifetime_total = serializers.IntegerField()
    percent = PercentField(allow_null=True)
    balance = serializers.IntegerField()
    online_orders_count = serializers.IntegerField()


__all__ = [
    "CustomerListItemSerializer",
]
