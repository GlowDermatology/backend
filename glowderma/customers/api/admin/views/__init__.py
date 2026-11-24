"""Admin views of ``customers`` (admin contract §2.6), one subject per module: ``customers`` (the list (P E)
and the card), ``cashback`` (the cashback adjustment, I) and ``sessions`` (a customer's signed-in devices).
Their OpenAPI declarations live in ``glowderma.customers.api.admin.schemas``."""

from glowderma.customers.api.admin.views.cashback import (
    CashbackAdjustmentView,
)
from glowderma.customers.api.admin.views.customers import (
    CustomerViewSet,
)
from glowderma.customers.api.admin.views.sessions import (
    CustomerSessionDetailView,
    CustomerSessionsViewSet,
)

__all__ = [
    "CashbackAdjustmentView",
    "CustomerSessionDetailView",
    "CustomerSessionsViewSet",
    "CustomerViewSet",
]
