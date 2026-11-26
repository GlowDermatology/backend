"""``get_segmenter(name, …)``: the segmenter a scene uses.

``AI_MODE=fake`` → the process-wide :class:`.fake.FakeSegmenter` (refused under the production settings,
like the content model). ``live``:

* ``gemini_json`` — one Gemini call returns box, hands and pixel masks (models that still segment);
* ``gemini_matte`` — the Gemini analyst (box, hands, flags) + the Gemini green-matte cutter for the pixels;
* ``bria`` — the Gemini analyst + Bria's background remover for the pixels (``cutter_key`` = Bria's key).
"""

from __future__ import annotations

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

from glowderma.core import integration_modes
from glowderma.integrations.llm.gemini import GeminiContentModel
from glowderma.integrations.llm.gemini_image import GeminiImageModel
from glowderma.integrations.llm.ports import LLMNotConfigured
from glowderma.integrations.llm.registry import PROVIDER
from glowderma.integrations.secret import Secret

from .bria import BriaCutter
from .combined import CombinedSegmenter
from .fake import FakeSegmenter
from .gemini_json import GeminiJsonSegmenter
from .gemini_matte import GeminiMatteCutter
from .ports import Segmenter

NAMES = ("gemini_json", "gemini_matte", "bria")

_fake = FakeSegmenter()


def get_segmenter(
    name: str = "gemini_matte",
    *,
    image_model: str = "gemini-nano-banana-2.1",
    image_size: str = "1K",
    cutter_key: Secret | None = None,
    bria_model: str = "RMBG2",
) -> Segmenter:
    mode = settings.AI_MODE
    if mode == "fake":
        if integration_modes.fake_in_production(PROVIDER):
            raise LLMNotConfigured("fake segmenter refused under the production settings")
        return _fake
    if mode != "live":
        raise ImproperlyConfigured(f"Unknown AI_MODE {mode!r}")
    if name == "gemini_json":
        return GeminiJsonSegmenter(GeminiContentModel())
    analyst = GeminiJsonSegmenter(GeminiContentModel(), require_mask=False)
    if name == "gemini_matte":
        cutter = GeminiMatteCutter(GeminiImageModel(), model=image_model, image_size=image_size)
        return CombinedSegmenter(analyst, cutter, cutter_key=cutter_key or Secret(""))
    if name == "bria":
        if cutter_key is None or not cutter_key:
            raise LLMNotConfigured("bria: no key")
        return CombinedSegmenter(analyst, BriaCutter(model=bria_model), cutter_key=cutter_key)
    raise LLMNotConfigured(f"no live segmenter {name!r}")


def fake_segmenter() -> FakeSegmenter:
    return _fake
