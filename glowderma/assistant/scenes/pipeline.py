"""The product scene pipeline: photo → segmentation → occlusion policy → 4:5 layout → scene → paste-back
→ checks. No database: the worker (scenes release) and ``manage.py scene_spike`` both drive it.

The rule (owner's hard condition): the model never redraws the product. The pipeline sends the model a
canvas with the cut-out on a grey placeholder, then pastes the photo's own product back over whatever came
back (:func:`glowderma.core.compose.composite`) and asserts the core is untouched
(``interior_difference == 0``). A model that moved, resized or restyled the product is caught by
registration (ghost edges) and retried once with a stricter prompt; a scene that still shows a hand, a
person or an office is caught by the QA call.

Hands: a hand holding the product is background (excluded from the mask, never pasted). Where a hand hides
part of the product, those pixels do not exist in the photo; up to ``max_occluded_pct`` of the outline the
image model may fill them (AI templates only) and the result records exactly where
(``generated_inside`` mask and percent) for staff to check; above it the photo is refused.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from decimal import Decimal
from io import BytesIO

from PIL import Image, ImageOps

from glowderma.core import compose
from glowderma.integrations.llm.ports import (
    ContentModel,
    GenerationRequest,
    ImageGenerationRequest,
    ImageModel,
    ImagePart,
    LLMBadOutput,
    LLMError,
)
from glowderma.integrations.secret import Secret
from glowderma.integrations.segmentation.ports import Segmentation, SegmentationRequest, Segmenter

from . import prompts
from .templates import Template

SEND_SIDE = 1024  # long side of the photo sent for segmentation
CUT_SIDE = 2048  # long side of the copy a pixel cutter (Bria) receives: sharper edges
MIN_PRODUCT_SHARE = 0.03
MAX_PRODUCT_SHARE = 0.85
OCCLUSION_FLOOR = 0.3  # percent: below this a hand only touches the product
# A finger's bite is narrower than ~10 % of the product's long side. Live: a thumb's bite (BB cream)
# reads 0.67 % hidden at 5 %, a bottle's neck 0 % at 5 % but 0.9 % at 6 % and 5.2 % at 8 %.
NOTCH_REACH = 0.05
LOW_RESOLUTION_SIDE = 500  # product box long side, px
EDGE_MARGIN = 0.01  # a product box this close to the photo's border (share of the side) is cut off

# Stable error codes (scenes release: SceneJob.error_code; console and staff messages map them).
NO_PRODUCT = "scene_no_product"
MASK_UNUSABLE = "scene_mask_unusable"
HAND_COVERS_PRODUCT = "scene_hand_covers_product"
OCCLUSION_NEEDS_AI = "scene_occlusion_needs_ai"
OCCLUSION_NOT_FILLABLE = "scene_occlusion_not_fillable"  # the AI provider builds only the background
PRODUCT_DRIFT = "scene_product_drift"
RESULT_REJECTED = "scene_result_rejected"
PRODUCT_CUT_OFF = "scene_product_cut_off"
RULE_BROKEN = "scene_rule_broken"  # interior_difference != 0: a bug, never shown as a scene


@dataclass(slots=True)
class Usage:
    """Token counts per price bucket, summed over every provider call of one scene."""

    text_input: int = 0  # segmentation + QA (text model)
    text_output: int = 0
    image_input: int = 0  # image model
    image_text_output: int = 0  # image model's text and thinking output
    image_output: int = 0
    text_calls: int = 0
    image_calls: int = 0
    bria_calls: int = 0  # the cutter, billed per call
    bria_scene_calls: int = 0  # Bria scene images, billed per image

    def add_text(self, input_tokens: int, output_tokens: int) -> None:
        self.text_input += input_tokens
        self.text_output += output_tokens
        self.text_calls += 1

    def add_image(self, input_tokens: int, text_tokens: int, image_tokens: int) -> None:
        self.image_input += input_tokens
        self.image_text_output += text_tokens
        self.image_output += image_tokens
        self.image_calls += 1

    def cost(self, prices: Prices) -> Decimal:
        million = Decimal(1_000_000)
        total = (
            Decimal(self.text_input) * prices.text_input
            + Decimal(self.text_output) * prices.text_output
            + Decimal(self.image_input) * prices.image_input
            + Decimal(self.image_text_output) * prices.image_text_output
            + Decimal(self.image_output) * prices.image_output
        ) / million + (
            Decimal(self.bria_calls) * prices.bria_per_call
            + Decimal(self.bria_scene_calls) * prices.bria_scene_per_call
        )
        return total.quantize(Decimal("0.000001"))


@dataclass(frozen=True, slots=True)
class Prices:
    """USD per 1M tokens (``bria_per_call`` / ``bria_scene_per_call``: per call). Defaults are placeholders
    from the providers' pages at the time of writing; the console's values win in production."""

    text_input: Decimal = Decimal("0.10")
    text_output: Decimal = Decimal("0.40")
    image_input: Decimal = Decimal("1.50")
    image_text_output: Decimal = Decimal("7.50")
    image_output: Decimal = Decimal("30.00")
    bria_per_call: Decimal = Decimal("0.025")  # background removal with a prompt
    bria_scene_per_call: Decimal = Decimal("0.032")  # background generation


@dataclass(frozen=True)
class Config:
    segment_model: str = "gemini-3.5-flash-lite"  # the analyst (box, hands, flags); 2.5 is closed to new keys
    image_model: str = "gemini-nano-banana-2.1"
    qa_model: str = "gemini-3.5-flash-lite"
    image_size: str = "2K"
    thinking_level: str = "minimal"
    segment_timeout: int = 45
    image_timeout: int = 90
    qa_timeout: int = 20
    max_retries: int = 1
    max_attempts: int = 2
    max_occluded_pct: float = 15.0  # the owner's limit for AI fills (studio refuses any hidden part)
    max_drift: float = 0.01  # share of the master's width
    max_interior_change: float = 18.0  # mean grey difference inside the core (tuned by the spike)
    qa_check: bool = True
    straighten: bool = True
    style: str = prompts.DEFAULT_STYLE


@dataclass(frozen=True, slots=True)
class Providers:
    segmenter: Segmenter
    image_model: ImageModel
    content_model: ContentModel  # the QA check
    api_key: Secret  # Gemini: the analyst and the QA check
    image_key: Secret | None = None  # the image model's own key (Bria's); ``None``: ``api_key``


class SceneError(Exception):
    def __init__(self, code: str, detail: str = "", *, usage: Usage | None = None) -> None:
        super().__init__(detail or code)
        self.code = code
        self.usage = usage


@dataclass(slots=True)
class Analysis:
    photo: Image.Image = field(repr=False)  # RGB, EXIF-transposed
    segmentation: Segmentation
    mask: Image.Image = field(repr=False)  # visible product, binary
    box: compose.Box
    occlusion: compose.Occlusion = field(repr=False)
    occluded_pct: float
    warnings: list[str]
    seconds: float


@dataclass(slots=True)
class Prepared:
    layout: compose.Layout
    placed: compose.Placed = field(repr=False)
    hidden: Image.Image = field(repr=False)  # hand-hidden product pixels on the master (binary)
    tilt: float
    straightened: bool


@dataclass(slots=True)
class Attempt:
    number: int
    seconds: float
    registration: compose.Registration | None = None
    interior_change: float | None = None
    qa: dict[str, bool] | None = None
    error: str = ""


@dataclass(slots=True)
class Result:
    template: str
    kind: str
    final: Image.Image = field(repr=False)  # the 4:5 master, not yet encoded
    square: compose.Box
    product_box: compose.Box = (0, 0, 0, 0)  # where the product stands in the master
    canvas: Image.Image | None = field(default=None, repr=False)  # what the image model received
    generated: Image.Image | None = field(default=None, repr=False)  # what it returned (resized)
    generated_inside: Image.Image | None = field(default=None, repr=False)
    generated_inside_pct: float = 0.0
    interior_difference: int = 0
    attempts: list[Attempt] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    seconds: float = 0.0


def load_photo(content: bytes) -> Image.Image:
    with Image.open(BytesIO(content)) as source:
        source.load()
        image = ImageOps.exif_transpose(source) or source.copy()
    return image.convert("RGB")


def image_part(photo: Image.Image, side: int = SEND_SIDE) -> ImagePart:
    copy = photo.copy()
    if max(copy.size) > side:
        copy.thumbnail((side, side), Image.Resampling.LANCZOS)
    return ImagePart(data=compose.encode(copy, "JPEG", 90), mime_type="image/jpeg")


def analyse(
    photo: Image.Image, providers: Providers, config: Config, usage: Usage, *, for_scene: bool = True
) -> Analysis:
    """Segment the photo and measure occlusion. For a scene the product must be whole in the frame and have
    a pixel mask; ``for_scene=False`` (fitting renditions) only needs the product's box."""
    started = time.monotonic()
    try:
        segmentation = providers.segmenter.segment(
            SegmentationRequest(
                image=image_part(photo),
                full_image=image_part(photo, CUT_SIDE),
                width=photo.width,
                height=photo.height,
                model=config.segment_model,
                timeout_seconds=config.segment_timeout,
                max_retries=config.max_retries,
            ),
            api_key=providers.api_key,
        )
    except LLMError as exc:
        usage.add_text(exc.input_tokens, exc.output_tokens)
        raise
    usage.add_text(segmentation.input_tokens, segmentation.output_tokens)
    if segmentation.cutter_input_tokens or segmentation.output_image_tokens:
        usage.add_image(
            segmentation.cutter_input_tokens,
            segmentation.cutter_text_tokens,
            segmentation.output_image_tokens,
        )
    if segmentation.cutter == "bria":
        usage.bria_calls += segmentation.cutter_calls
    if for_scene and (segmentation.cut_off or touches_edge(segmentation.box, photo.size)):
        # Part of the product is outside the photo: those pixels do not exist and the rule forbids
        # inventing them, so no scene is made (staff retake the photo with the whole product in frame).
        raise SceneError(
            PRODUCT_CUT_OFF, f"product box {segmentation.box} reaches the photo's edge", usage=usage
        )
    if for_scene and not segmentation.exact:
        raise SceneError(MASK_UNUSABLE, "no pixel mask of the product", usage=usage)
    mask = compose.fill_holes(segmentation.product, segmentation.hands)
    share = compose.area(mask) / (photo.width * photo.height)
    if share < MIN_PRODUCT_SHARE:
        raise SceneError(NO_PRODUCT, f"product covers {share:.1%} of the photo", usage=usage)
    if share > MAX_PRODUCT_SHARE:
        raise SceneError(MASK_UNUSABLE, f"mask covers {share:.1%} of the photo", usage=usage)
    box = mask.getbbox()
    if box is None:
        raise SceneError(NO_PRODUCT, "empty mask", usage=usage)
    # The analyst's outline polygon is coarse (a few points live): it is not trusted for any measurement.
    occlusion = compose.occlusion(mask, segmentation.hands, None)
    if segmentation.exact and segmentation.hands is None and segmentation.hand_present:
        # A held product cut by a cutter that drops fingers with the background: what a finger hid shows
        # as a bite in the silhouette. Live, the analyst's "covers" flag changed between two runs of the
        # same photo and the convex hull counted a bottle's neck as 4.8 % hidden, so the bite is measured
        # by shape alone. The analyst's "covered_percent" (live: 15-18 % for every photo) is not used.
        side = max(box[2] - box[0], box[3] - box[1])
        occlusion = compose.notches(mask, max(2, round(NOTCH_REACH * side)), max(2, round(0.006 * side)))
    occluded = occlusion.percent
    if not segmentation.exact and segmentation.hand_covers_product:
        occluded = max(occluded, round(segmentation.covered_percent, 2))  # box only (fit): the estimate
    warnings: list[str] = []
    if segmentation.hand_present and occluded <= OCCLUSION_FLOOR:
        warnings.append("hand_removed")
    if for_scene and touches_edge(box, photo.size):
        raise SceneError(PRODUCT_CUT_OFF, f"product box {box} reaches the photo's edge", usage=usage)
    if max(box[2] - box[0], box[3] - box[1]) < LOW_RESOLUTION_SIDE:
        warnings.append("low_resolution")
    return Analysis(
        photo=photo,
        segmentation=segmentation,
        mask=mask,
        box=box,
        occlusion=occlusion,
        occluded_pct=occluded,
        warnings=warnings,
        seconds=round(time.monotonic() - started, 2),
    )


def touches_edge(box: compose.Box, size: tuple[int, int]) -> bool:
    width, height = size
    margin_x, margin_y = max(1, round(EDGE_MARGIN * width)), max(1, round(EDGE_MARGIN * height))
    return (
        box[0] <= margin_x or box[1] <= margin_y or box[2] >= width - margin_x or box[3] >= height - margin_y
    )


def check_occlusion(analysis: Analysis, kind: str, config: Config) -> bool:
    """The owner's policy; ``True`` when the model will fill a small hidden part."""
    if analysis.occluded_pct <= OCCLUSION_FLOOR:
        return False
    if kind == "studio":
        raise SceneError(OCCLUSION_NEEDS_AI, f"{analysis.occluded_pct}% hidden by a hand")
    if analysis.occluded_pct > config.max_occluded_pct:
        raise SceneError(HAND_COVERS_PRODUCT, f"{analysis.occluded_pct}% hidden by a hand")
    return True


def prepare(analysis: Analysis, config: Config) -> Prepared:
    """The product on the 4:5 master, upright when it leans 3-25° (a hand-held shot)."""
    photo, mask, hidden = analysis.photo, analysis.mask, analysis.occlusion.hidden
    tilt = compose.tilt_degrees(analysis.occlusion.hull)
    straightened = config.straighten and 3.0 < abs(tilt) < 25.0
    if straightened:
        photo, mask = compose.rotate_upright(photo, mask, tilt)
        hidden = compose.binary(hidden).rotate(
            tilt, resample=Image.Resampling.NEAREST, expand=True, fillcolor=0
        )
    box = mask.getbbox() or analysis.box
    layout = compose.plan_layout(box)
    placed = compose.place(photo, mask, box, layout)
    return Prepared(
        layout=layout,
        placed=placed,
        hidden=compose.hull_on_master(hidden, box, layout),
        tilt=tilt,
        straightened=straightened,
    )


def _finish(result: Result, prepared: Prepared) -> Result:
    result.interior_difference = compose.interior_difference(result.final, prepared.placed)
    if result.interior_difference != 0:
        raise SceneError(RULE_BROKEN, f"core differs by {result.interior_difference}")
    if prepared.straightened:
        result.warnings.append("straightened")
    return result


def studio(analysis: Analysis, template: Template, config: Config) -> Result:
    started = time.monotonic()
    check_occlusion(analysis, "studio", config)
    prepared = prepare(analysis, config)
    scene = compose.studio_scene(prepared.placed, prepared.layout, template.params)
    final = compose.composite(scene, prepared.placed)
    result = Result(
        template=template.key,
        kind="studio",
        final=final,
        square=compose.square_box(prepared.layout),
        product_box=prepared.layout.box,
        warnings=list(analysis.warnings),
    )
    result.seconds = round(time.monotonic() - started, 2)
    return _finish(result, prepared)


def _qa(final: Image.Image, providers: Providers, config: Config, usage: Usage) -> dict[str, bool]:
    answer = providers.content_model.generate(
        GenerationRequest(
            model=config.qa_model,
            system_instruction="You are a strict quality checker of product photos for an online shop.",
            prompt=prompts.QA_PROMPT,
            response_schema=prompts.QA_SCHEMA,
            images=(image_part(final, 768),),
            temperature=0.0,
            max_output_tokens=256,
            timeout_seconds=config.qa_timeout,
            max_retries=config.max_retries,
        ),
        api_key=providers.api_key,
    )
    usage.add_text(answer.input_tokens, answer.output_tokens)
    return {name: bool(answer.data.get(name)) for name in prompts.QA_SCHEMA["properties"]}


def ai_scene(
    analysis: Analysis, template: Template, providers: Providers, config: Config, usage: Usage
) -> Result:
    started = time.monotonic()
    fill = check_occlusion(analysis, "ai_scene", config)
    if fill and not providers.image_model.fills_hidden:
        raise SceneError(
            OCCLUSION_NOT_FILLABLE,
            f"{analysis.occluded_pct}% hidden; {providers.image_model.name} builds only the background",
        )
    prepared = prepare(analysis, config)
    sent = compose.canvas(prepared.placed)
    cutout_part: ImagePart | None = None
    scene = ""
    if not providers.image_model.fills_hidden:
        # A provider that keeps a given foreground and builds only the background (Bria) gets the same
        # canvas with the paste alpha (only flat placeholder can show around the product, never the photo's
        # hand or office) and a plain scene description instead of the layout rules.
        cutout = sent.copy()
        cutout.putalpha(prepared.placed.alpha)
        cutout_part = ImagePart(data=compose.encode(cutout, "PNG"), mime_type="image/png")
        scene = prompts.bria_scene(template.prompt, config.style)
    image_key = providers.image_key if providers.image_key is not None else providers.api_key
    # The model sees the canvas at its own 4:5 size; the result is resized back to the master.
    attempts: list[Attempt] = []
    last_error = PRODUCT_DRIFT
    for number in range(1, max(1, config.max_attempts) + 1):
        began = time.monotonic()
        attempt = Attempt(number=number, seconds=0.0)
        attempts.append(attempt)
        prompt = prompts.scene_prompt(
            prepared.layout, template.prompt, config.style, occluded=fill, stricter=number > 1
        )
        try:
            answer = providers.image_model.generate_image(
                ImageGenerationRequest(
                    model=config.image_model,
                    prompt=prompt,
                    images=(ImagePart(data=compose.encode(sent, "PNG"), mime_type="image/png"),),
                    aspect_ratio="4:5",
                    image_size=config.image_size,
                    thinking_level=config.thinking_level,
                    timeout_seconds=config.image_timeout,
                    max_retries=config.max_retries,
                    cutout=cutout_part,
                    scene=scene,
                ),
                api_key=image_key,
            )
        except LLMError as exc:
            usage.add_image(exc.input_tokens, 0, exc.output_tokens)
            exc.usage = usage  # type: ignore[attr-defined]
            raise
        usage.add_image(answer.input_tokens, answer.output_text_tokens, answer.output_image_tokens)
        usage.bria_scene_calls += answer.billed_calls
        try:
            generated = compose.decode(answer.image).convert("RGB")
        except Exception:  # noqa: BLE001 - provider bytes that do not decode
            raise LLMBadOutput("the scene image does not decode") from None
        generated = generated.resize(sent.size, Image.Resampling.LANCZOS)
        attempt.registration = compose.register(generated, sent, prepared.placed.core)
        attempt.interior_change = compose.interior_change(generated, sent, prepared.placed.core)
        drift = max(abs(attempt.registration.dx), abs(attempt.registration.dy))
        if drift > config.max_drift or attempt.registration.scale != 1.0:
            attempt.error, last_error = PRODUCT_DRIFT, PRODUCT_DRIFT
            attempt.seconds = round(time.monotonic() - began, 2)
            continue
        if attempt.interior_change > config.max_interior_change:
            attempt.error, last_error = PRODUCT_DRIFT, PRODUCT_DRIFT
            attempt.seconds = round(time.monotonic() - began, 2)
            continue
        final = compose.composite(generated, prepared.placed)
        if config.qa_check:
            attempt.qa = _qa(final, providers, config, usage)
            if any(attempt.qa.get(name) for name in prompts.QA_FAILS):
                attempt.error, last_error = RESULT_REJECTED, RESULT_REJECTED
                attempt.seconds = round(time.monotonic() - began, 2)
                continue
        attempt.seconds = round(time.monotonic() - began, 2)
        inside, inside_pct = compose.generated_inside(prepared.placed, prepared.hidden)
        warnings = list(analysis.warnings)
        if inside_pct > 0:
            warnings.append("product_partly_generated")
        if attempt.qa and attempt.qa.get("product_floating"):
            warnings.append("product_floating")
        result = Result(
            template=template.key,
            kind="ai_scene",
            final=final,
            square=compose.square_box(prepared.layout),
            product_box=prepared.layout.box,
            canvas=sent,
            generated=generated,
            generated_inside=inside if inside_pct > 0 else None,
            generated_inside_pct=inside_pct,
            attempts=attempts,
            warnings=warnings,
        )
        result.seconds = round(time.monotonic() - started, 2)
        return _finish(result, prepared)
    error = SceneError(last_error, f"{len(attempts)} attempts rejected", usage=usage)
    error.attempts = attempts  # type: ignore[attr-defined]
    raise error


def run(
    photo: Image.Image, template: Template, providers: Providers, config: Config, usage: Usage
) -> tuple[Analysis, Result]:
    analysis = analyse(photo, providers, config, usage)
    if template.kind == "studio":
        return analysis, studio(analysis, template, config)
    return analysis, ai_scene(analysis, template, providers, config, usage)
