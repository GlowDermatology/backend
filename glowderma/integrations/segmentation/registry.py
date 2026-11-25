"""``get_segmenter(name)``: the segmenter a scene uses.

``AI_MODE=fake`` → the process-wide :class:`.fake.FakeSegmenter` (refused under the production settings,
like the content model); ``live`` → ``gemini_json`` (default) or ``gemini_matte`` over the live Gemini
adapters. A dedicated background remover (e.g. Bria) becomes one more entry in :data:`LIVE_SEGMENTERS`.
"""

from __future__ import annotations

from collections.abc import Callable

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

from glowderma.core import integration_modes
from glowderma.integrations.llm.gemini import GeminiContentModel
from glowderma.integrations.llm.gemini_image import GeminiImageModel
from glowderma.integrations.llm.ports import LLMNotConfigured
from glowderma.integrations.llm.registry import PROVIDER

from .fake import FakeSegmenter
from .gemini_json import GeminiJsonSegmenter
from .gemini_matte import GeminiMatteSegmenter
from .ports import Segmenter

LIVE_SEGMENTERS: dict[str, Callable[[], Segmenter]] = {
    "gemini_json": lambda: GeminiJsonSegmenter(GeminiContentModel()),
    "gemini_matte": lambda: GeminiMatteSegmenter(GeminiImageModel()),
}

_fake = FakeSegmenter()


def get_segmenter(name: str = "gemini_json") -> Segmenter:
    mode = settings.AI_MODE
    if mode == "fake":
        if integration_modes.fake_in_production(PROVIDER):
            raise LLMNotConfigured("fake segmenter refused under the production settings")
        return _fake
    if mode == "live":
        factory = LIVE_SEGMENTERS.get(name)
        if factory is None:
            raise LLMNotConfigured(f"no live segmenter {name!r}")
        return factory()
    raise ImproperlyConfigured(f"Unknown AI_MODE {mode!r}")


def fake_segmenter() -> FakeSegmenter:
    return _fake
