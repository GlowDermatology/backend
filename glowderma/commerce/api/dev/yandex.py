"""``POST admin/dev/yandex/{order_id}/advance/`` (Yandex Delivery plan §3.11; local/test only).

Mounted with the admin routes only while ``DEV_FEATURES_ALLOWED`` and ``YANDEX_DELIVERY_MODE=fake``
(:func:`enabled`, checked again per request), so the admin panel and the e2e suites can drive a fake courier:
the fake moves the order's current claim to ``status`` (``integrations.yandex_delivery.fake.advance``) and the
claim is re-read and applied exactly as the poll would (``commerce.services.apply_yandex_claim``,
``source="dev"``). Staff only (an admin view); kept out of the OpenAPI document like the other dev routes.
"""

from __future__ import annotations

from typing import Any, cast

from django.conf import settings
from drf_spectacular.utils import extend_schema
from rest_framework import serializers
from rest_framework.exceptions import NotFound
from rest_framework.request import Request
from rest_framework.response import Response

from glowderma.accounts.api.base import AdminAPIView
from glowderma.commerce import selectors, services, yandex_flow
from glowderma.commerce.api.admin.serializers import OrderDetailSerializer
from glowderma.core.api.exceptions import Conflict
from glowderma.delivery import selectors as delivery_selectors
from glowderma.delivery import yandex as delivery_yandex
from glowderma.integrations.yandex_delivery import fake as fake_yandex
from glowderma.integrations.yandex_delivery.registry import get_yandex_delivery, yandex_delivery_mode

MESSAGE_NO_CLAIM = "Buyurtmada Yandex kuryer buyurtmasi yo‘q."


def enabled() -> bool:
    """Local/test settings with the fake Yandex adapter only."""
    return bool(getattr(settings, "DEV_FEATURES_ALLOWED", False)) and yandex_delivery_mode() == "fake"


class AdvanceSerializer(serializers.Serializer[dict[str, Any]]):
    status = serializers.ChoiceField(choices=list(delivery_yandex.STATUSES))


class YandexAdvanceView(AdminAPIView):
    @extend_schema(exclude=True)
    def post(self, request: Request, pk: int) -> Response:
        if not enabled():
            raise NotFound
        body = AdvanceSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        claim = delivery_selectors.current_claim(pk)
        if claim is None or not claim.claim_id:
            raise Conflict(MESSAGE_NO_CLAIM)
        try:
            fake_yandex.advance(claim.claim_id, cast(str, body.validated_data["status"]))
        except KeyError as exc:  # the fake forgot it (a restarted process)
            raise Conflict(MESSAGE_NO_CLAIM) from exc
        port = get_yandex_delivery(yandex_flow.scenario_of(pk))
        result = services.apply_yandex_claim(pk, port.claim(claim.claim_id), source="dev")
        if result.needs_tracking_link and result.claim_pk is not None:
            yandex_flow.store_tracking_link(port, pk, result.claim_pk, claim.claim_id)
        detail = selectors.order_detail(pk)
        if detail is None:
            raise NotFound
        return Response(OrderDetailSerializer(detail).data)
