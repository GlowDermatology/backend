"""The segmenter port: one staff photo in, the visible product's mask and the hands around it out.

Masks are mode ``L`` binary images at the photo's size. ``outline`` is the product's full outline as the model
sees it (including parts a finger hides), ``hands`` every hand or finger region touching the product's box;
both may be ``None``. ``covered_percent`` is the model's own estimate of the hidden share (a second opinion:
the server measures it from the masks, ``core.compose.occlusion``).

Errors are :mod:`glowderma.integrations.llm.ports` errors (``provider_unavailable``, ``ai_not_configured``,
``ai_refused``, ``ai_bad_output``); :class:`NoProduct` (``scene_no_product``) when the photo shows none.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from PIL import Image

from glowderma.integrations.llm.ports import ImagePart, LLMError
from glowderma.integrations.secret import Secret

CODE_NO_PRODUCT = "scene_no_product"
BACKGROUNDS = ("plain", "office", "home", "shelf", "outdoor", "other")


class NoProduct(LLMError):
    code = CODE_NO_PRODUCT


@dataclass(frozen=True, slots=True)
class SegmentationRequest:
    image: ImagePart  # the photo as sent (at most 1024 px on the long side)
    width: int  # the photo's full size: masks come back at this size
    height: int
    model: str
    timeout_seconds: int = 45
    max_retries: int = 1


@dataclass(frozen=True, slots=True)
class Segmentation:
    product: Image.Image = field(repr=False)  # visible product, binary L at (width, height)
    box: tuple[int, int, int, int]  # of ``product``, pixels, exclusive
    outline: Image.Image | None = field(default=None, repr=False)
    hands: Image.Image | None = field(default=None, repr=False)
    hand_present: bool = False
    hand_covers_product: bool = False
    covered_percent: float = 0.0
    cut_off: bool = False
    background: str = "other"
    input_tokens: int = 0
    output_tokens: int = 0  # text rate
    output_image_tokens: int = 0  # image rate (the matte strategy)
    model: str = ""


class Segmenter(Protocol):
    name: str

    def segment(self, request: SegmentationRequest, *, api_key: Secret) -> Segmentation:
        """Raises an :class:`~glowderma.integrations.llm.ports.LLMError` subclass."""
        ...
