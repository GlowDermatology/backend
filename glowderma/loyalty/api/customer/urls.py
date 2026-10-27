"""Customer routes of `loyalty` (customer contract §2.0); mounted by ``config.urls`` at ``api/v1/``."""

from django.urls import URLPattern, URLResolver, path

from .views import CashbackLedgerViewSet, CashbackView, QrView

urlpatterns: list[URLPattern | URLResolver] = [
    path("me/cashback/", CashbackView.as_view(), name="customer-cashback"),
    path(
        "me/cashback/ledger/",
        CashbackLedgerViewSet.as_view({"get": "list"}),
        name="customer-cashback-ledger",
    ),
    path("me/qr/", QrView.as_view(), name="customer-qr"),
]
