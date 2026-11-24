"""The admin cashback adjustment of a customer (admin contract §2.6, I)."""

from __future__ import annotations

from typing import Any, cast

from rest_framework.exceptions import NotFound
from rest_framework.request import Request
from rest_framework.response import Response

from glowderma.accounts.api.base import AdminAPIView
from glowderma.core.api.exceptions import ContractValidationError
from glowderma.core.idempotency import idempotent
from glowderma.customers import selectors, services
from glowderma.customers.api.admin.schemas.cashback import CASHBACK_ADJUSTMENT_POST
from glowderma.customers.api.admin.serializers import (
    CashbackAdjustmentDirectionSerializer,
    CashbackAdjustmentResultSerializer,
)
from glowderma.loyalty.services import LoyaltyValidationError


class CashbackAdjustmentView(AdminAPIView):
    @CASHBACK_ADJUSTMENT_POST
    @idempotent()
    def post(self, request: Request, pk: int) -> Response:
        body: Any = request.data
        data: dict[str, Any] = body if isinstance(body, dict) else {}
        direction = CashbackAdjustmentDirectionSerializer(
            data={k: v for k, v in data.items() if k == "direction"}
        )
        if not selectors.customer_exists(int(pk)):
            raise NotFound()
        direction.is_valid(raise_exception=True)
        try:
            outcome = services.adjust_cashback(
                int(pk),
                direction=direction.validated_data["direction"],
                amount=data.get("amount"),
                reason=data.get("reason"),
                actor_id=cast(int, request.user.pk),
                request=request,
            )
        except services.CustomerNotFound as exc:
            raise NotFound() from exc
        except LoyaltyValidationError as exc:
            raise ContractValidationError(exc.errors, message=exc.message) from exc
        return Response(CashbackAdjustmentResultSerializer(outcome).data, status=201)


__all__ = [
    "CashbackAdjustmentView",
]
