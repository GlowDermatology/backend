"""The storefront landing page (``GET home/``, **O**): one request for the hero, the rails and the cashback
teaser. Optional auth like the catalog: a signed-in caller gets ``in_cart_quantity`` and ``Cache-Control:
private, no-cache``."""

from __future__ import annotations

from rest_framework.request import Request
from rest_framework.response import Response

from glowderma.accounts.api.customer_base import PublicAPIView
from glowderma.customers import selectors
from glowderma.customers.api.customer.schemas.home import HOME_GET
from glowderma.customers.api.customer.views.common import customer_id


class HomeView(PublicAPIView):
    public_cache_seconds = 60

    @HOME_GET
    def get(self, request: Request) -> Response:
        return Response(selectors.home_payload(self.language, customer_id(request)))


__all__ = [
    "HomeView",
]
