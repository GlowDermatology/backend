"""Admin serializers of `customers` (contract §2.6; display rules §5.3), one subject per module: ``listing``
(the list query), ``card`` (the customer card), ``cashback`` (the adjustment) and ``sessions``.

Every serializer reads the frozen rows of ``loyalty.selectors`` / ``customers.selectors`` (attributes named as
the contract keys); none touches another module's ORM row except the annotated ``Customer`` of the list.
``CustomerListItem`` is declared once, by ``notifications`` (the segment members list returns the same shape),
and the list view renders with it over rows annotated by ``loyalty.selectors.list_annotations()``.
"""

from glowderma.customers.api.admin.serializers.card import (
    CustomerCardSerializer,
    CustomerDeviceRowSerializer,
    CustomerDeviceSerializer,
    CustomerGroupSerializer,
    CustomerTierSerializer,
    CustomerTotalsSerializer,
    LedgerEntrySerializer,
    NextTierSerializer,
    OnlineOrdersSerializer,
    PurchaseSerializer,
)
from glowderma.customers.api.admin.serializers.cashback import (
    REASON_MAX_LENGTH,
    REASON_MIN_LENGTH,
    CashbackAdjustmentDirectionSerializer,
    CashbackAdjustmentResultSerializer,
    CashbackAdjustmentSerializer,
)
from glowderma.customers.api.admin.serializers.listing import (
    CustomerListQuerySerializer,
)
from glowderma.customers.api.admin.serializers.sessions import (
    AdminCustomerSessionSerializer,
)

__all__ = [
    "AdminCustomerSessionSerializer",
    "REASON_MAX_LENGTH",
    "REASON_MIN_LENGTH",
    "CashbackAdjustmentDirectionSerializer",
    "CashbackAdjustmentResultSerializer",
    "CashbackAdjustmentSerializer",
    "CustomerCardSerializer",
    "CustomerDeviceRowSerializer",
    "CustomerDeviceSerializer",
    "CustomerGroupSerializer",
    "CustomerListQuerySerializer",
    "CustomerTierSerializer",
    "CustomerTotalsSerializer",
    "LedgerEntrySerializer",
    "NextTierSerializer",
    "OnlineOrdersSerializer",
    "PurchaseSerializer",
]
