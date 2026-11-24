"""``GET settings/public/`` (customer contract §2.4, **O**, cached 60 s)."""

from __future__ import annotations

from typing import Any

from rest_framework.request import Request
from rest_framework.response import Response

from glowderma.accounts.api.customer_base import PublicAPIView
from glowderma.core import integration_modes
from glowderma.site_config import selectors
from glowderma.site_config.api.customer.schemas import PUBLIC_SETTINGS_GET


def public_delivery() -> dict[str, Any]:
    """``delivery`` of ``settings/public/`` (plan §3.4): from ``core.integration_modes`` only (``site_config``
    sits below ``delivery``). Whether a Yandex method is switched on, the warehouse and the pilot list are
    decided per customer by ``delivery/calculate/``."""
    providers = integration_modes.delivery_providers()
    return {"providers": providers, "default_provider": providers[0] if providers else None}


class PublicSettingsView(PublicAPIView):
    public_cache_seconds = 60

    @PUBLIC_SETTINGS_GET
    def get(self, request: Request) -> Response:
        return Response({**selectors.public_settings(self.language), "delivery": public_delivery()})
