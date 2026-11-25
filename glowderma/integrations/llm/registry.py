"""``get_content_model(provider)``: the adapter ``settings.AI_MODE`` selects.

``fake`` → the process-wide :class:`.fake.FakeContentModel`
(:func:`get_image_model`: :class:`.fake.FakeImageModel`) (every provider name); ``live`` → the provider's
adapter (:data:`LIVE_PROVIDERS`). A fake under the production settings
(``core.integration_modes.fake_in_production("ai")``) is refused with :class:`.ports.LLMNotConfigured`
(``ai_not_configured``): it would show invented texts as AI suggestions.
"""

from __future__ import annotations

from collections.abc import Callable

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

from glowderma.core import integration_modes

from .fake import FakeContentModel, FakeImageModel
from .gemini import GeminiContentModel
from .gemini_image import GeminiImageModel
from .ports import ContentModel, ImageModel, LLMNotConfigured

PROVIDER = "ai"  # the name in core.integration_modes.PROVIDER_MODE_SETTINGS
LIVE_PROVIDERS: dict[str, Callable[[], ContentModel]] = {"gemini": GeminiContentModel}
LIVE_IMAGE_PROVIDERS: dict[str, Callable[[], ImageModel]] = {"gemini": GeminiImageModel}

_fake = FakeContentModel()
_fake_image = FakeImageModel()


def get_content_model(provider: str = "gemini") -> ContentModel:
    mode = settings.AI_MODE
    if mode == "fake":
        if integration_modes.fake_in_production(PROVIDER):
            raise LLMNotConfigured("fake AI adapter refused under the production settings")
        return _fake
    if mode == "live":
        factory = LIVE_PROVIDERS.get(provider)
        if factory is None:
            raise LLMNotConfigured(f"no live adapter for provider {provider!r}")
        return factory()
    raise ImproperlyConfigured(f"Unknown AI_MODE {mode!r}")


def fake_content_model() -> FakeContentModel:
    return _fake


def get_image_model(provider: str = "gemini") -> ImageModel:
    """The image-output adapter, chosen like :func:`get_content_model`."""
    mode = settings.AI_MODE
    if mode == "fake":
        if integration_modes.fake_in_production(PROVIDER):
            raise LLMNotConfigured("fake AI adapter refused under the production settings")
        return _fake_image
    if mode == "live":
        factory = LIVE_IMAGE_PROVIDERS.get(provider)
        if factory is None:
            raise LLMNotConfigured(f"no live image adapter for provider {provider!r}")
        return factory()
    raise ImproperlyConfigured(f"Unknown AI_MODE {mode!r}")


def fake_image_model() -> FakeImageModel:
    return _fake_image
