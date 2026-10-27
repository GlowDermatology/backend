"""Admin routes of ``payments`` (mounted under ``api/v1/admin/`` by ``config.urls``).

Every route name is prefixed ``admin-`` and unique across modules; the auth matrix test walks them all. The
refund operations (``payments/{id}/refunds/{refund_id}/retry|resolve/``, contract §7 item 18) write order
events, so ``commerce`` declares them.
"""

from django.urls import URLPattern, URLResolver, path

from .views import PaymentViewSet

urlpatterns: list[URLPattern | URLResolver] = [
    path("payments/", PaymentViewSet.as_view({"get": "list"}), name="admin-payments"),
]
