"""Gemini cutter: the image model repaints everything but the product in flat green; we key the green.

The repainted image is used **only as a mask**, never as pixels: the scene keeps the photo's own product.
Because the mask is traced from a redrawn image, it is trusted only if the repaint lines up with the photo:
the matte's non-green pixels are registered against the photo we sent (``core.compose.register``), and a
shift above :data:`MAX_DRIFT` of the width or any resize is refused (``scene_mask_unusable``). Hands are
painted green with the rest, so the matte says nothing about occlusion: the analyst call does.
"""

from __future__ import annotations

from io import BytesIO
from typing import Any

from PIL import Image, ImageChops

from glowderma.core import compose
from glowderma.integrations.llm.ports import ImageGenerationRequest, ImageModel, LLMBadOutput
from glowderma.integrations.secret import Secret

from .ports import CutOut, MaskUnusable, NoProduct, SegmentationRequest

MAX_DRIFT = 0.015
PROMPT = """Repaint this photo for a cut-out mask. Keep the ONE retail skincare product (bottle, tube, \
jar, box, sachet, pump) exactly as it is, in exactly the same place and size. Paint EVERYTHING else pure \
flat green #00FF00 with no shading or texture: hands, fingers, arms, sleeves, tables, desks, shelves, \
monitors, papers, other products, walls and the whole background. Where a finger covers the product, \
paint the finger green too. Return one image only, same framing and aspect ratio as the input."""

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


def _sent_photo(request: SegmentationRequest) -> Image.Image:
    with Image.open(BytesIO(request.image.data)) as source:
        source.load()
        return source.convert("RGB")


def check_alignment(matte: Image.Image, sent: Image.Image, mask_small: Image.Image) -> compose.Registration:
    """Registration of the repainted product against the photo, inside the keyed product (eroded)."""
    core = compose.erode(mask_small, max(2, round(min(sent.size) * 0.01)))
    if compose.area(core) == 0:
        core = mask_small
    return compose.register(matte.convert("RGB").resize(sent.size, Image.Resampling.BILINEAR), sent, core)


class GeminiMatteCutter:
    name = "gemini_matte"

    def __init__(self, image_model: ImageModel, *, model: str, image_size: str = "1K") -> None:
        self._model = image_model
        self._model_id = model
        self._size = image_size

    def cut(self, request: SegmentationRequest, *, api_key: Secret) -> CutOut:
        result = self._model.generate_image(
            ImageGenerationRequest(
                model=self._model_id,
                prompt=PROMPT,
                images=(request.image,),
                aspect_ratio=_closest_ratio(request.width, request.height),
                image_size=self._size,
                timeout_seconds=max(request.timeout_seconds, 60),
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
        sent = _sent_photo(request)
        mask_small = key_green(matte, sent.size)
        if mask_small.getbbox() is None or compose.area(mask_small) < 0.005 * sent.width * sent.height:
            raise NoProduct("matte: no product left after keying", **billed)
        found = check_alignment(matte, sent, mask_small)
        if max(abs(found.dx), abs(found.dy)) > MAX_DRIFT or found.scale != 1.0:
            raise MaskUnusable(f"matte moved the product (dx {found.dx:.3f}, dy {found.dy:.3f})", **billed)
        mask = compose.binary(mask_small.resize((request.width, request.height), Image.Resampling.BILINEAR))
        return CutOut(
            mask=mask,
            input_tokens=result.input_tokens,
            output_tokens=result.output_text_tokens,
            output_image_tokens=result.output_image_tokens,
        )
