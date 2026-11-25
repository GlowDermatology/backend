"""Default segmenter: one structured Gemini call (the content-model port) returning boxes and masks.

The answer follows :data:`SCHEMA`. Gemini's segmentation output gives ``box_2d`` as ``[y0, x0, y1, x1]`` on a
0-1000 scale and the mask either as a base64 PNG probability map **of the box** (optionally prefixed
``data:image/png;base64,``) or as a polygon of ``[x, y]`` points on the 0-1000 scale of the whole photo; both
are accepted (the documentation shows each). A mask that does not decode falls back to the polygon, then to
nothing (``ai_bad_output``).

The prompt is code, not an editable row: the parser depends on it.
"""

from __future__ import annotations

import base64
import binascii
from io import BytesIO
from typing import Any

from PIL import Image, ImageChops, ImageDraw

from glowderma.core import compose
from glowderma.integrations.llm.ports import ContentModel, GenerationRequest, LLMBadOutput, LLMError
from glowderma.integrations.secret import Secret

from .ports import BACKGROUNDS, NoProduct, Segmentation, SegmentationRequest

PROMPT = """The photo was taken by shop staff and shows ONE retail skincare product (bottle, tube, jar, box, \
sachet, pump). It may be held in a hand, and the background may be an office, a desk, a shelf or a \
table.
Answer with JSON only, following the schema.
1. "product": "box_2d" [y0, x0, y1, x1] on a 0-1000 scale and "mask" (a base64 PNG segmentation mask \
of the box) of the PRODUCT ONLY: packaging, cap, pump, label, and only the parts of it that are \
actually visible. Hands, fingers, nails, arms, sleeves, jewellery, tables, desks, shelves, monitors, \
keyboards, papers, other products and the whole background are NOT the product: exclude them even \
where they touch or cover it. If there are several products take the largest one nearest the centre. \
If there is no product, set "product" to null.
2. "outline": the complete outline of that product as if nothing covered it (include parts hidden by \
fingers), a polygon of [x, y] points on a 0-1000 scale of the whole photo.
3. "hands": "box_2d" and "mask" of every hand, finger or arm region touching or overlapping the \
product's box; [] if none.
4. "hand_present": any hand, finger or arm visible anywhere in the photo.
5. "hand_covers_product": a finger or hand hides part of the product's surface (not merely touching \
its edge); "covered_percent": the hidden share of the product's outline, 0-100.
6. "product_cut_off": the photo's edge cuts the product.
7. "background": one of plain, office, home, shelf, outdoor, other."""

_REGION: dict[str, Any] = {
    "type": "object",
    "properties": {
        "box_2d": {"type": "array", "items": {"type": "integer"}, "minItems": 4, "maxItems": 4},
        "mask": {"type": "string"},
    },
    "required": ["box_2d"],
}
_POLYGON: dict[str, Any] = {
    "type": "array",
    "items": {"type": "array", "items": {"type": "integer"}, "minItems": 2, "maxItems": 2},
}
SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "product": {
            "anyOf": [
                {**_REGION, "properties": {**_REGION["properties"], "polygon": _POLYGON}},
                {"type": "null"},
            ]
        },
        "outline": _POLYGON,
        "hands": {"type": "array", "items": _REGION},
        "hand_present": {"type": "boolean"},
        "hand_covers_product": {"type": "boolean"},
        "covered_percent": {"type": "number", "minimum": 0, "maximum": 100},
        "product_cut_off": {"type": "boolean"},
        "background": {"type": "string", "enum": list(BACKGROUNDS)},
    },
    "required": [
        "product",
        "hand_present",
        "hand_covers_product",
        "covered_percent",
        "product_cut_off",
        "background",
    ],
}
MAX_OUTPUT_TOKENS = 24_576  # a base64 PNG mask is long: the default 4096 would truncate it


def box_pixels(box_2d: Any, width: int, height: int) -> tuple[int, int, int, int] | None:
    """``[y0, x0, y1, x1]`` (0-1000) → pixel ``(x0, y0, x1, y1)``; ``None`` when malformed or empty."""
    if (
        not isinstance(box_2d, list)
        or len(box_2d) != 4
        or not all(isinstance(v, int | float) for v in box_2d)
    ):
        return None
    y0, x0, y1, x1 = (min(1000.0, max(0.0, float(v))) for v in box_2d)
    box = (
        round(x0 * width / 1000),
        round(y0 * height / 1000),
        round(x1 * width / 1000),
        round(y1 * height / 1000),
    )
    return box if box[2] - box[0] >= 2 and box[3] - box[1] >= 2 else None


def decode_png_mask(text: Any, box: tuple[int, int, int, int], width: int, height: int) -> Image.Image | None:
    """A base64 PNG probability map of ``box`` → a binary mask of the whole photo (threshold 127)."""
    if not isinstance(text, str) or not text.strip():
        return None
    payload = text.split(",", 1)[1] if text.startswith("data:") else text
    try:
        raw = base64.b64decode(payload, validate=False)
        with Image.open(BytesIO(raw)) as source:
            source.load()
            probability = source.convert("L")
    except binascii.Error, ValueError, OSError:
        return None
    size = (box[2] - box[0], box[3] - box[1])
    probability = probability.resize(size, Image.Resampling.BICUBIC)
    mask = Image.new("L", (width, height), 0)
    mask.paste(compose.binary(probability, 128), box[:2])
    return mask


def polygon_mask(points: Any, width: int, height: int) -> Image.Image | None:
    """``[[x, y], ...]`` (0-1000 of the whole photo) → a filled binary mask; ``None`` when unusable."""
    if not isinstance(points, list) or len(points) < 3:
        return None
    corners: list[tuple[float, float]] = []
    for point in points:
        if (
            not isinstance(point, list)
            or len(point) != 2
            or not all(isinstance(v, int | float) for v in point)
        ):
            return None
        corners.append((float(point[0]) * width / 1000, float(point[1]) * height / 1000))
    mask = Image.new("L", (width, height), 0)
    ImageDraw.Draw(mask).polygon(corners, fill=255)
    return mask if mask.getbbox() else None


def region_mask(region: Any, width: int, height: int) -> Image.Image | None:
    if not isinstance(region, dict):
        return None
    box = box_pixels(region.get("box_2d"), width, height)
    if box is None:
        return None
    mask = decode_png_mask(region.get("mask"), box, width, height)
    if mask is None or not mask.getbbox():
        mask = polygon_mask(region.get("polygon"), width, height)
    return mask


def _union(masks: list[Image.Image], size: tuple[int, int]) -> Image.Image | None:
    if not masks:
        return None
    union = Image.new("L", size, 0)
    for mask in masks:
        union = ImageChops.lighter(union, mask)
    return union


def parse(data: dict[str, Any], width: int, height: int) -> dict[str, Any]:
    """The structured answer → :class:`.ports.Segmentation` fields (no token counts)."""
    product = data.get("product")
    if product is None:
        raise NoProduct("segmentation: no product in the photo")
    mask = region_mask(product, width, height)
    if mask is None:
        raise LLMBadOutput("segmentation: the product mask does not decode")
    box = mask.getbbox()
    if box is None:
        raise NoProduct("segmentation: empty product mask")
    hands = [
        m for m in (region_mask(r, width, height) for r in list(data.get("hands") or [])) if m is not None
    ]
    covered = data.get("covered_percent")
    background = data.get("background")
    return {
        "product": mask,
        "box": box,
        "outline": polygon_mask(data.get("outline"), width, height),
        "hands": _union(hands, (width, height)),
        "hand_present": bool(data.get("hand_present")) or bool(hands),
        "hand_covers_product": bool(data.get("hand_covers_product")),
        "covered_percent": float(covered) if isinstance(covered, int | float) else 0.0,
        "cut_off": bool(data.get("product_cut_off")),
        "background": background if background in BACKGROUNDS else "other",
    }


class GeminiJsonSegmenter:
    name = "gemini_json"

    def __init__(self, content_model: ContentModel) -> None:
        self._model = content_model

    def segment(self, request: SegmentationRequest, *, api_key: Secret) -> Segmentation:
        result = self._model.generate(
            GenerationRequest(
                model=request.model,
                system_instruction="You are a precise product segmentation tool for an online shop.",
                prompt=PROMPT,
                response_schema=SCHEMA,
                images=(request.image,),
                temperature=0.0,
                max_output_tokens=MAX_OUTPUT_TOKENS,
                timeout_seconds=request.timeout_seconds,
                max_retries=request.max_retries,
            ),
            api_key=api_key,
        )
        try:
            fields = parse(result.data, request.width, request.height)
        except LLMError as exc:
            exc.input_tokens, exc.output_tokens = result.input_tokens, result.output_tokens
            raise
        return Segmentation(
            **fields, input_tokens=result.input_tokens, output_tokens=result.output_tokens, model=result.model
        )
