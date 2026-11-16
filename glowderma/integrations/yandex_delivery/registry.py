"""``get_yandex_delivery(scenario)``: the adapter of ``settings.YANDEX_DELIVERY_MODE`` (default
``INTEGRATIONS_MODE``).

``fake`` returns :class:`.fake.FakeYandexDelivery` over the scenario (``Order.metadata["fake"]["yandex"]`` or
the ``X-GD-Fake-Scenario`` header); ``live`` returns :class:`.live.LiveYandexDelivery` built from the
``YANDEX_DELIVERY_*`` settings (the scenario is ignored).
"""

from collections.abc import Mapping
from typing import Any

from django.conf import settings

from .fake import FakeYandexDelivery
from .live import LiveYandexDelivery
from .ports import YandexDeliveryPort


def yandex_delivery_mode() -> str:
    return str(getattr(settings, "YANDEX_DELIVERY_MODE", None) or settings.INTEGRATIONS_MODE)


def get_yandex_delivery(scenario: Mapping[str, Any] | None = None) -> YandexDeliveryPort:
    mode = yandex_delivery_mode()
    if mode == "fake":
        return FakeYandexDelivery(scenario)
    if mode == "live":
        return LiveYandexDelivery.from_settings()
    raise ValueError(f"Unknown YANDEX_DELIVERY_MODE {mode!r}")
