"""Banner images: a wide scene for the home screen's banner, with the shop's products in it or a scene alone.

Shape. The app shows a banner at 2.15:1 and the website at 2:1, both filling the frame (``cover``). The
finished image (the *master*) is exactly :data:`MASTER_RATIO` (2.15:1), so the app shows all of it and the
website crops 3.5 % from each side. The image models only make standard ratios, so every image is made at
21:9 (:data:`GEN_RATIO`, wider) and the middle 2.15:1 is kept: a uniform crop, never a stretch.

Where things go. The title is drawn over the bottom-left of the banner (a dark gradient, the title in up to
three lines), so the scene keeps that corner calm and the products stand on the right, all of them inside
the website's 2:1 window (:func:`web_window`) so no screen cuts a product.

The rule (as for product scenes, :mod:`.pipeline`): the model never redraws a product. Each product is cut out
of its own photo, placed on our 21:9 canvas (no two overlap), the model builds the scene around them, and the
products' own pixels are pasted back (``interior_difference == 0``); a model that moved or restyled a product
is caught by registration and retried once.

No database: :mod:`.services` drives it.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from PIL import Image

from glowderma.core import compose
from glowderma.integrations.llm.ports import (
    GenerationRequest,
    ImageGenerationRequest,
    ImagePart,
    LLMBadOutput,
    LLMError,
)

from . import pipeline, prompts
from .templates import Template

MASTER_RATIO = 2.15  # the app's banner (mobile: banner_carousel.dart)
WEB_RATIO = 2.0  # the website's banner (``--aspect-banner``)
GEN_RATIO = "21:9"  # what the image models make; the master is its middle
GEN_HEIGHT = 1000  # the canvas sent with products: 2333 × 1000
MAX_PRODUCTS = 3

# The products' area, as shares of the website's 2:1 window (the part every screen shows): the right part,
# the title's corner (bottom-left) kept free. Tuned on the real overlays (app: a gradient from 30 % of the
# height and the title 30 px above the bottom; web: the title band at the bottom with the arrow at its right).
PRODUCTS_LEFT = 0.55
PRODUCTS_RIGHT = 0.94
PRODUCTS_TOP = 0.10
BASELINE = 0.76  # where the products stand, share of the height
HERO_HEIGHT = 0.62  # the main product's height, share of the height
SIDE_HEIGHT = 0.82  # the other products, relative to the main one
GAP = 0.035  # between two products, share of the window's width
MAX_UPSCALE = 1.5  # a small photo is never enlarged more than this (it then stands smaller)
REGISTER_WIDTH = 464  # the grey copies registration compares (twice a product scene's: the canvas is wide)

# Stable error codes (SceneJob.error_code).
PRODUCT_PHOTO_MISSING = "banner_product_no_photo"
PRODUCT_PHOTO_UNUSABLE = "banner_product_photo_unusable"


@dataclass(frozen=True, slots=True)
class Cutout:
    """One product for the banner: its photo and the visible product's mask (photo size)."""

    product_id: int
    photo: Image.Image = field(repr=False)
    mask: Image.Image = field(repr=False)
    box: compose.Box


@dataclass(slots=True)
class Result:
    final: Image.Image = field(repr=False)  # the 2.15:1 master
    product_boxes: list[compose.Box] = field(default_factory=list)  # in the master
    canvas: Image.Image | None = field(default=None, repr=False)
    generated: Image.Image | None = field(default=None, repr=False)
    attempts: list[pipeline.Attempt] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    seconds: float = 0.0


def canvas_size(height: int = GEN_HEIGHT) -> tuple[int, int]:
    return round(height * 21 / 9), height


def master_window(size: tuple[int, int]) -> compose.Box:
    """The middle 2.15:1 of a 21:9 image (the whole height)."""
    width, height = size
    master_width = min(width, round(height * MASTER_RATIO))
    left = (width - master_width) // 2
    return (left, 0, left + master_width, height)


def web_window(size: tuple[int, int]) -> compose.Box:
    """The middle 2:1 of an image of any wider ratio: what the website shows of it."""
    width, height = size
    web_width = min(width, round(height * WEB_RATIO))
    left = (width - web_width) // 2
    return (left, 0, left + web_width, height)


def _scaled(box: compose.Box, height: float) -> tuple[float, float]:
    box_width, box_height = box[2] - box[0], box[3] - box[1]
    scale = min(height / box_height, MAX_UPSCALE)
    return box_width * scale, box_height * scale


def plan(boxes: list[compose.Box], size: tuple[int, int]) -> list[compose.Box]:
    """Where each product box goes on a canvas of ``size``: side by side on one baseline, never overlapping,
    inside the products' area of the website's window. The first product is the main one: the tallest, in
    the middle of three, on the left of two."""
    if not boxes:
        return []
    width, height = size
    web = web_window(size)
    web_width = web[2] - web[0]
    area_left = web[0] + PRODUCTS_LEFT * web_width
    area_right = web[0] + PRODUCTS_RIGHT * web_width
    area_width = area_right - area_left
    hero = HERO_HEIGHT * height
    sizes = [_scaled(box, hero if index == 0 else hero * SIDE_HEIGHT) for index, box in enumerate(boxes)]
    order = list(range(len(boxes)))
    if len(boxes) == 3:
        order = [1, 0, 2]
    gap = GAP * web_width
    total = sum(sizes[index][0] for index in order) + gap * (len(order) - 1)
    max_height = (BASELINE - PRODUCTS_TOP) * height
    fit = min(1.0, area_width / total, max_height / max(h for _, h in sizes))
    baseline = round(BASELINE * height)
    x = area_left + (area_width - total * fit) / 2
    placed: dict[int, compose.Box] = {}
    for index in order:
        box_width, box_height = sizes[index][0] * fit, sizes[index][1] * fit
        x0 = round(x)
        x1 = x0 + max(1, round(box_width))
        placed[index] = (x0, baseline - max(1, round(box_height)), x1, baseline)
        x += box_width + gap * fit
    return [placed[index] for index in range(len(boxes))]


def _merge(layers: list[compose.Placed], size: tuple[int, int]) -> compose.Placed:
    """One :class:`compose.Placed` for several products that do not overlap."""
    product = Image.new("RGB", size, compose.PLACEHOLDER)
    alpha = Image.new("L", size, 0)
    core = Image.new("L", size, 0)
    visible = Image.new("L", size, 0)
    for layer in layers:
        product.paste(layer.product, (0, 0), layer.alpha)
        alpha = Image.composite(layer.alpha, alpha, layer.alpha)
        core = Image.composite(layer.core, core, layer.core)
        visible = Image.composite(layer.visible, visible, layer.visible)
    return compose.Placed(product=product, alpha=alpha, core=core, visible=visible)


def place_products(cutouts: list[Cutout], size: tuple[int, int]) -> tuple[compose.Placed, list[compose.Box]]:
    targets = plan([cutout.box for cutout in cutouts], size)
    width, height = size
    layers = []
    for cutout, target in zip(cutouts, targets, strict=True):
        layout = compose.Layout(
            width=width,
            height=height,
            scale=(target[3] - target[1]) / (cutout.box[3] - cutout.box[1]),
            box=target,
        )
        layers.append(compose.place(cutout.photo, cutout.mask, cutout.box, layout))
    return _merge(layers, size), targets


def cutout_of(product_id: int, analysis: pipeline.Analysis, config: pipeline.Config) -> Cutout:
    """A product photo's analysis as a banner cut-out: whole, nothing hidden by a hand, upright."""
    if analysis.occluded_pct > pipeline.OCCLUSION_FLOOR:
        raise pipeline.SceneError(PRODUCT_PHOTO_UNUSABLE, f"product {product_id}: a hand hides part of it")
    photo, mask = analysis.photo, analysis.mask
    tilt = compose.tilt_degrees(analysis.occlusion.hull)
    if config.straighten and 3.0 < abs(tilt) < 25.0:
        photo, mask = compose.rotate_upright(photo, mask, tilt)
    box = mask.getbbox() or analysis.box
    return Cutout(product_id=product_id, photo=photo, mask=mask, box=box)


def _crop(image: Image.Image) -> Image.Image:
    return image.crop(master_window(image.size))


def _in_master(box: compose.Box, size: tuple[int, int]) -> compose.Box:
    left = master_window(size)[0]
    return (box[0] - left, box[1], box[2] - left, box[3])


def qa(
    final: Image.Image,
    providers: pipeline.Providers,
    config: pipeline.Config,
    usage: pipeline.Usage,
    *,
    with_products: bool,
) -> dict[str, bool]:
    answer = providers.content_model.generate(
        GenerationRequest(
            model=config.qa_model,
            system_instruction="You are a strict quality checker of images for a skincare shop's website.",
            prompt=prompts.banner_qa_prompt(with_products=with_products),
            response_schema=prompts.BANNER_QA_SCHEMA,
            images=(pipeline.image_part(final, 1024),),
            temperature=0.0,
            max_output_tokens=256,
            timeout_seconds=config.qa_timeout,
            max_retries=config.max_retries,
        ),
        api_key=providers.api_key,
    )
    usage.add_text(answer.input_tokens, answer.output_tokens)
    return {name: bool(answer.data.get(name)) for name in prompts.BANNER_QA_SCHEMA["properties"]}


def _generate(
    request: ImageGenerationRequest, providers: pipeline.Providers, usage: pipeline.Usage
) -> Image.Image:
    image_key = providers.image_key if providers.image_key is not None else providers.api_key
    try:
        answer = providers.image_model.generate_image(request, api_key=image_key)
    except LLMError as exc:
        usage.add_image(exc.input_tokens, 0, exc.output_tokens)
        raise
    usage.add_image(answer.input_tokens, answer.output_text_tokens, answer.output_image_tokens)
    usage.bria_scene_calls += answer.billed_calls
    try:
        return compose.decode(answer.image).convert("RGB")
    except Exception:  # noqa: BLE001 - provider bytes that do not decode
        raise LLMBadOutput("the banner image does not decode") from None


def _fail(code: str, attempts: list[pipeline.Attempt], usage: pipeline.Usage) -> pipeline.SceneError:
    error = pipeline.SceneError(code, f"{len(attempts)} attempts rejected", usage=usage)
    error.attempts = attempts  # type: ignore[attr-defined]
    return error


def with_products(
    cutouts: list[Cutout],
    template: Template,
    providers: pipeline.Providers,
    config: pipeline.Config,
    usage: pipeline.Usage,
) -> Result:
    """The products on the 21:9 canvas, the scene built around them, their pixels pasted back, the middle
    2.15:1 kept."""
    if not 1 <= len(cutouts) <= MAX_PRODUCTS:
        raise ValueError(f"a banner shows 1-{MAX_PRODUCTS} products")
    started = time.monotonic()
    size = canvas_size()
    placed, targets = place_products(cutouts, size)
    sent = compose.canvas(placed)
    reference = pipeline.reference_part(template)
    cutout_part: ImagePart | None = None
    scene = ""
    if not providers.image_model.fills_hidden:  # Bria: the products on transparency and a plain scene
        layer = sent.copy()
        layer.putalpha(placed.alpha)
        cutout_part = ImagePart(data=compose.encode(layer, "PNG"), mime_type="image/png")
        scene = prompts.bria_banner(
            template.scene or pipeline.FALLBACK_SCENE, config.style, with_products=True
        )
    attempts: list[pipeline.Attempt] = []
    last_error = pipeline.PRODUCT_DRIFT
    for number in range(1, max(1, config.max_attempts) + 1):
        began = time.monotonic()
        attempt = pipeline.Attempt(number=number, seconds=0.0)
        attempts.append(attempt)
        try:
            generated = _generate(
                ImageGenerationRequest(
                    model=config.image_model,
                    prompt=prompts.banner_products_prompt(
                        targets,
                        size,
                        len(cutouts),
                        template.scene or pipeline.FALLBACK_SCENE,
                        config.style,
                        stricter=number > 1,
                        reference=reference is not None,
                    ),
                    images=(
                        ImagePart(data=compose.encode(sent, "PNG"), mime_type="image/png"),
                        *((reference,) if reference is not None else ()),
                    ),
                    aspect_ratio=GEN_RATIO,
                    image_size=config.image_size,
                    thinking_level=config.thinking_level,
                    timeout_seconds=config.image_timeout,
                    max_retries=config.max_retries,
                    cutout=cutout_part,
                    scene=scene,
                ),
                providers,
                usage,
            )
        except LLMError as exc:
            exc.usage = usage  # type: ignore[attr-defined]
            raise
        generated = generated.resize(size, Image.Resampling.LANCZOS)
        attempt.registration = compose.register(generated, sent, placed.core, width=REGISTER_WIDTH)
        attempt.interior_change = compose.interior_change(generated, sent, placed.core)
        drift = max(abs(attempt.registration.dx), abs(attempt.registration.dy))
        if (
            drift > config.max_drift
            or attempt.registration.scale != 1.0
            or attempt.interior_change > config.max_interior_change
        ):
            attempt.error, last_error = pipeline.PRODUCT_DRIFT, pipeline.PRODUCT_DRIFT
            attempt.seconds = round(time.monotonic() - began, 2)
            continue
        whole = compose.composite(generated, placed)
        difference = compose.interior_difference(whole, placed)
        if difference != 0:
            raise pipeline.SceneError(pipeline.RULE_BROKEN, f"core differs by {difference}")
        final = _crop(whole)
        if config.qa_check:
            attempt.qa = qa(final, providers, config, usage, with_products=True)
            if any(attempt.qa.get(name) for name in prompts.BANNER_QA_FAILS):
                attempt.error, last_error = pipeline.RESULT_REJECTED, pipeline.RESULT_REJECTED
                attempt.seconds = round(time.monotonic() - began, 2)
                continue
        attempt.seconds = round(time.monotonic() - began, 2)
        result = Result(
            final=final,
            product_boxes=[_in_master(box, size) for box in targets],
            canvas=sent,
            generated=generated,
            attempts=attempts,
        )
        result.seconds = round(time.monotonic() - started, 2)
        return result
    raise _fail(last_error, attempts, usage)


def scene_only(
    template: Template, providers: pipeline.Providers, config: pipeline.Config, usage: pipeline.Usage
) -> Result:
    """A scene alone (no product), made at 21:9 and cropped to the middle 2.15:1, at the model's own size."""
    started = time.monotonic()
    reference = pipeline.reference_part(template)
    scene = ""
    if not providers.image_model.fills_hidden:
        scene = prompts.bria_banner(
            template.scene or pipeline.FALLBACK_SCENE, config.style, with_products=False
        )
    attempts: list[pipeline.Attempt] = []
    for number in range(1, max(1, config.max_attempts) + 1):
        began = time.monotonic()
        attempt = pipeline.Attempt(number=number, seconds=0.0)
        attempts.append(attempt)
        try:
            generated = _generate(
                ImageGenerationRequest(
                    model=config.image_model,
                    prompt=prompts.banner_scene_prompt(
                        template.scene or pipeline.FALLBACK_SCENE,
                        config.style,
                        reference=reference is not None,
                    ),
                    images=(reference,) if reference is not None else (),
                    aspect_ratio=GEN_RATIO,
                    image_size=config.image_size,
                    thinking_level=config.thinking_level,
                    timeout_seconds=config.image_timeout,
                    max_retries=config.max_retries,
                    scene=scene,
                ),
                providers,
                usage,
            )
        except LLMError as exc:
            exc.usage = usage  # type: ignore[attr-defined]
            raise
        final = _crop(generated)
        if config.qa_check:
            attempt.qa = qa(final, providers, config, usage, with_products=False)
            if any(attempt.qa.get(name) for name in prompts.BANNER_QA_FAILS):
                attempt.error = pipeline.RESULT_REJECTED
                attempt.seconds = round(time.monotonic() - began, 2)
                continue
        attempt.seconds = round(time.monotonic() - began, 2)
        result = Result(final=final, generated=generated, attempts=attempts)
        result.seconds = round(time.monotonic() - started, 2)
        return result
    raise _fail(pipeline.RESULT_REJECTED, attempts, usage)
