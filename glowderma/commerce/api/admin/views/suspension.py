"""The dispatch suspension (Yandex Delivery plan §3.9)."""

from __future__ import annotations

from rest_framework.request import Request
from rest_framework.response import Response

from glowderma.accounts.api.base import AdminAPIView
from glowderma.commerce.api.admin.schemas.suspension import DELIVERY_SUSPENSION_GET, DELIVERY_SUSPENSION_POST
from glowderma.commerce.api.admin.serializers import (
    SuspensionBodySerializer,
    SuspensionSerializer,
)
from glowderma.core.idempotency import idempotent
from glowderma.delivery import selectors as delivery_selectors
from glowderma.delivery import services as delivery_services


class DeliverySuspensionView(AdminAPIView):
    @DELIVERY_SUSPENSION_GET
    def get(self, request: Request) -> Response:
        current = delivery_services.suspension_of(delivery_selectors.settings_row())
        return Response(SuspensionSerializer(current).data)

    @DELIVERY_SUSPENSION_POST
    @idempotent()
    def post(self, request: Request) -> Response:
        body = SuspensionBodySerializer(data=request.data)
        body.is_valid(raise_exception=True)
        current = delivery_services.set_suspension(
            bool(body.validated_data["suspended"]),
            body.validated_data["until"],
            body.validated_data["note"],
            actor=request.user,
            request=request,
        )
        return Response(SuspensionSerializer(current).data)


__all__ = [
    "DeliverySuspensionView",
]
