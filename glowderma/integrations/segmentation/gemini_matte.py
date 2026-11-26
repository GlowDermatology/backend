"""Fallback segmenter: the image model repaints everything but the product in flat green; we key the green.

The repainted image is used **only as a mask**, never as pixels: the scene keeps the photo's own product.
It costs a full image generation and says nothing about hands (they are painted green with the rest), so the
server cannot measure occlusion from it: ``hands`` is ``None`` and the job relies on the visible mask alone.
"""

from __future__ import annotations

from typing import Any

from PIL import Image, ImageChops

from glowderma.core import compose
from glowderma.integrations.llm.ports import ImageGenerationRequest, ImageModel, LLMBadOutput
from glowderma.integrations.secret import Secret

from .ports import NoProduct, Segmentation, SegmentationRequest

PROMPT = """Repaint this photo for a cut-out mask. Keep the ONE retail skincare product (bottle, tube, \
jar, box, sachet, pump) exactly as it is, in exactly the same place and size. Paint EVERYTHING else pure \
flat green \
#00FF00 with no shading or texture: hands, fingers, arms, sleeves, tables, desks, shelves, \
monitors, papers, other products, walls and the whole background. Where a finger covers the product, \
paint the finger green too. Return one image only, same framing and aspect ratio as the input."""


def key_green(image: Image.Image, size: tuple[int, int]) -> Image.Image:
    """The non-green pixels of a matte render, cleaned and resized to ``size`` (binary)."""
    red, green, blue = image.convert("RGB").split()
    greenish = ImageChops.multiply(
        green.point(lambda v: 255 if v >= 150 else 0),
        ImageChops.multiply(
            red.point(lambda v: 255 if v <= 120 else 0), blue.point(lambda v: 255 if v <= 120 else 0)
        ),
    )
    product = ImageChops.invert(greenish)
    product = compose.dilate(compose.erode(product, 2), 2)  # drop speckles at the key edge
    return compose.binary(product.resize(size, Image.Resampling.BILINEAR))


class GeminiMatteSegmenter:
    name = "gemini_matte"

    def __init__(self, image_model: ImageModel, *, image_size: str = "1K") -> None:
        self._model = image_model
        self._size = image_size

    def segment(self, request: SegmentationRequest, *, api_key: Secret) -> Segmentation:
        ratio = _closest_ratio(request.width, request.height)
        result = self._model.generate_image(
            ImageGenerationRequest(
                model=request.model,
                prompt=PROMPT,
                images=(request.image,),
                aspect_ratio=ratio,
                image_size=self._size,
                timeout_seconds=request.timeout_seconds,
                max_retries=request.max_retries,
            ),
            api_key=api_key,
        )
        billed: dict[str, Any] = {
            "input_tokens": result.input_tokens,
            "output_tokens": result.output_text_tokens + result.output_image_tokens,
        }
        try:
            matte = compose.decode(result.image)
        except Exception:  # noqa: BLE001 - provider bytes that do not decode
            raise LLMBadOutput("matte: the image does not decode", **billed) from None
        mask = key_green(matte, (request.width, request.height))
        box = mask.getbbox()
        if box is None or compose.area(mask) < 0.005 * request.width * request.height:
            raise NoProduct("matte: no product left after keying", **billed)
        return Segmentation(
            product=mask,
            box=box,
            input_tokens=result.input_tokens,
            output_tokens=result.output_text_tokens,
            output_image_tokens=result.output_image_tokens,
            model=result.model,
        )


RATIOS = {
    "1:1": 1.0,
    "2:3": 2 / 3,
    "3:2": 1.5,
    "3:4": 0.75,
    "4:3": 4 / 3,
    "4:5": 0.8,
    "5:4": 1.25,
    "9:16": 9 / 16,
    "16:9": 16 / 9,
}


def _closest_ratio(width: int, height: int) -> str:
    """The supported aspect ratio nearest the photo's (the matte must keep the framing to line up)."""
    actual = width / height
    return min(RATIOS, key=lambda name: abs(RATIOS[name] - actual))
