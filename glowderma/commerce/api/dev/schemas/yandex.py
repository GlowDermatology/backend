"""OpenAPI declarations for ``POST admin/dev/yandex/{order_id}/advance/`` (Yandex Delivery plan §3.11;
local/test only).

Mounted with the admin routes only while ``DEV_FEATURES_ALLOWED`` and ``YANDEX_DELIVERY_MODE=fake``
(:func:`enabled`, checked again per request), so the admin panel and the e2e suites can drive a fake courier:
the fake moves the order's current claim to ``status`` (``integrations.yandex_delivery.fake.advance``) and the
claim is re-read and applied exactly as the poll would (``commerce.services.apply_yandex_claim``,
``source="dev"``). Staff only (an admin view); kept out of the OpenAPI document like the other dev routes."""

from __future__ import annotations

from drf_spectacular.utils import extend_schema

YANDEX_ADVANCE_POST = extend_schema(exclude=True)


__all__ = [
    "YANDEX_ADVANCE_POST",
]
