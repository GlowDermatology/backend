"""The segmenter scenes use: an analyst call for the box, hands and flags, and a cutter for the pixels.

Current Gemini text models answer the analyst questions well (box, hand present, how much a hand hides,
cut off) but return placeholder masks, so the pixel mask comes from a cutter: Bria's background remover or
the Gemini green matte. The cutter's mask must agree with the analyst's box (intersection over union ≥
:data:`MIN_AGREEMENT`), else ``scene_mask_unusable``: a cut-out of the wrong object, or one that moved,
never reaches a scene. When the analyst already finds the product cut off, no cutter is called (the
pipeline refuses the photo anyway).
"""

from __future__ import annotations

from dataclasses import replace

from glowderma.integrations.secret import Secret

from .gemini_json import GeminiJsonSegmenter
from .ports import Cutter, MaskUnusable, Segmentation, SegmentationRequest

MIN_AGREEMENT = 0.5


def iou(a: tuple[int, int, int, int], b: tuple[int, int, int, int]) -> float:
    left, top, right, bottom = max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3])
    inter = max(0, right - left) * max(0, bottom - top)
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / union if union > 0 else 0.0


class CombinedSegmenter:
    def __init__(self, analyst: GeminiJsonSegmenter, cutter: Cutter, *, cutter_key: Secret) -> None:
        self.name = f"gemini_json+{cutter.name}"
        self._analyst = analyst
        self._cutter = cutter
        self._cutter_key = cutter_key

    def segment(self, request: SegmentationRequest, *, api_key: Secret) -> Segmentation:
        analysis = self._analyst.segment(request, api_key=api_key)
        if analysis.cut_off:
            return replace(analysis, exact=False)
        cut = self._cutter.cut(request, api_key=self._cutter_key)
        box = cut.mask.getbbox()
        if box is None or iou(box, analysis.box) < MIN_AGREEMENT:
            raise MaskUnusable(
                f"{self._cutter.name} cut-out {box} disagrees with the product box {analysis.box}",
                input_tokens=analysis.input_tokens + cut.input_tokens,
                output_tokens=analysis.output_tokens + cut.output_tokens + cut.output_image_tokens,
            )
        return replace(
            analysis,
            product=cut.mask,
            box=box,
            exact=True,
            cutter=self._cutter.name,
            cutter_calls=cut.calls,
            cutter_input_tokens=cut.input_tokens,
            cutter_text_tokens=cut.output_tokens,
            output_image_tokens=cut.output_image_tokens,
        )
