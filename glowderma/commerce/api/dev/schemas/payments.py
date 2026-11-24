"""OpenAPI declarations for ``POST dev/payments/{order_id}/simulate/`` (customer contract §2.15; local/test
settings only).

Mounted by ``config.urls`` only with ``DEV_FEATURES_ALLOWED``, ``FAKE_PAYMENTS_ENABLED`` and fake Click/Payme.
It builds a correctly signed Click Prepare + Complete (or Payme ``CheckPerformTransaction`` +
``CreateTransaction`` + ``PerformTransaction`` / ``CancelTransaction``) and runs it through the **real webhook
views** in-process, so the mobile team can finish a purchase without provider accounts."""

from __future__ import annotations

from drf_spectacular.utils import extend_schema

from glowderma.commerce.api.dev.serializers import (
    SimulateResultSerializer,
    SimulateSerializer,
)
from glowderma.core.api.schema import CUSTOMER_ERRORS, DEVICE_HEADERS

SIMULATE_PAYMENT_POST = extend_schema(
    operation_id="dev_payments_simulate",
    parameters=DEVICE_HEADERS,
    request=SimulateSerializer,
    responses={200: SimulateResultSerializer, **CUSTOMER_ERRORS},
    description="Local/test settings only: a signed provider callback sequence through the real webhook "
    "views.",
)


__all__ = [
    "SIMULATE_PAYMENT_POST",
]
