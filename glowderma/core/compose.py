"""Product scene compositing: masks, layout, paste-back and checks (Pillow only, no Django).

The rule this module enforces: **the product in a scene is the photo's own pixels**. A generative model only
ever paints the background; :func:`composite` puts the original cut-out back on top of whatever it returned,
and every pixel of the visible product deeper than the edge band (``core``) is copied from the photo
unchanged (one uniform resize, never a re-render).

Coordinates: boxes are ``(x0, y0, x1, y1)`` in pixels, ``x1``/``y1`` exclusive. Masks are mode ``L`` images
holding 0 or 255 (binary) unless named ``alpha``.

* Masks: :func:`binary`, :func:`erode`, :func:`dilate` (exact square windows through ``BoxBlur``),
  :func:`refine` (the paste alpha: feathered **inward only**, since outside a hand-held product there are
  fingers and office, never more product), :func:`edge` (the outline drawn for review).
* Occlusion: :func:`filled_hull` (row fill ∩ column fill of the visible mask), :func:`occlusion`.
* Layout: the 4:5 canvas, the product standing on a fixed baseline inside a centred safe zone so the square
  window (the middle ``W×W``) holds the whole product too (:func:`plan_layout`, :func:`square_box`).
* Paste-back: :func:`place`, :func:`composite`, :func:`interior_difference` (must be 0).
* Checks: :func:`register` (did the model move or resize the product?), :func:`interior_change`.
* Studio backgrounds: :func:`studio_background`, :func:`contact_shadow`.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from io import BytesIO
from typing import cast

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageStat

type Box = tuple[int, int, int, int]
type RGB = tuple[int, int, int]

PLACEHOLDER: RGB = (128, 128, 128)
RATIO = 1.25  # height / width of the master (4:5)
# The safe zone: the product box fits inside this share of the canvas, centred horizontally, its base on the
# baseline. 0.82 H leaves 0.08 H of the square window (0.1 H .. 0.9 H) under the product for the shadow.
SAFE_WIDTH = 0.64
SAFE_HEIGHT = 0.60
BASELINE = 0.82
MAX_UPSCALE = 1.5
MIN_CANVAS_WIDTH = 928
MAX_CANVAS_WIDTH = 1920


# --- masks --------------------------------------------------------------------------------------------------


def binary(mask: Image.Image, threshold: int = 128) -> Image.Image:
    return mask.convert("L").point(lambda v: 255 if v >= threshold else 0)


def erode(mask: Image.Image, radius: int) -> Image.Image:
    """Keep a pixel only when its whole ``(2r+1)²`` window is set (exact: ``BoxBlur`` is the window mean)."""
    if radius <= 0:
        return mask.copy()
    return mask.filter(ImageFilter.BoxBlur(radius)).point(lambda v: 255 if v >= 255 else 0)


def dilate(mask: Image.Image, radius: int) -> Image.Image:
    if radius <= 0:
        return mask.copy()
    return mask.filter(ImageFilter.BoxBlur(radius)).point(lambda v: 255 if v > 0 else 0)


def area(mask: Image.Image) -> int:
    return int(mask.histogram()[255])


def fill_holes(mask: Image.Image, keep_out: Image.Image | None = None) -> Image.Image:
    """``mask`` with every enclosed hole filled (a gap not connected to the photo's border).

    A hole inside a product is part of the product (anti-aliased label letters, a highlight the segmenter
    dropped): left open, the scene would show through the label. ``keep_out`` (a hand mask) is never filled.
    """
    mask = binary(mask)
    padded = Image.new("L", (mask.width + 2, mask.height + 2), 0)
    padded.paste(mask, (1, 1))
    ImageDraw.floodfill(padded, (0, 0), 128)  # the background reachable from the border
    outside = padded.crop((1, 1, mask.width + 1, mask.height + 1)).point(lambda v: 255 if v == 128 else 0)
    filled = ImageChops.invert(outside)
    if keep_out is not None:
        filled = ImageChops.lighter(ImageChops.subtract(filled, binary(keep_out)), mask)
    return filled


def edge_radius(box: Box) -> int:
    """The feather band of a product box: 0.4 % of its longer side, at least 2 px."""
    return max(2, round(max(box[2] - box[0], box[3] - box[1]) * 0.004))


@dataclass(frozen=True, slots=True)
class Refined:
    alpha: Image.Image  # the paste alpha (0 outside the visible mask, 255 in the core)
    core: Image.Image  # binary: pixels pasted fully opaque, original by construction
    radius: int


def refine(mask: Image.Image, radius: int) -> Refined:
    """The paste alpha of a binary visible-product mask.

    ``core = erode(mask, radius)`` is fully opaque; the band between the core and the mask boundary ramps
    down **inward only** (the blur of the eroded mask, clipped to the mask), so nothing outside the visible
    product (a finger, the office) is ever pasted.
    """
    mask = binary(mask)
    core = erode(mask, radius)
    ramp = core.filter(ImageFilter.GaussianBlur(radius))
    alpha = ImageChops.lighter(ImageChops.multiply(ramp, mask), core)
    return Refined(alpha=alpha, core=core, radius=radius)


def edge(mask: Image.Image, width: int = 2) -> Image.Image:
    """The outline of a binary mask, ``width`` px each side (review overlays)."""
    return ImageChops.subtract(dilate(mask, width), erode(mask, width))


def outline_overlay(photo: Image.Image, mask: Image.Image, colour: RGB = (255, 0, 160)) -> Image.Image:
    """The photo at full resolution with the mask's outline drawn on it: what a reviewer checks (a cap or a
    pump outside the line, or a finger inside it, breaks the rule the paste-back cannot catch)."""
    width = max(2, round(max(photo.size) / 600))
    line = edge(binary(mask), width)
    base = photo.convert("RGB")
    return Image.composite(Image.new("RGB", base.size, colour), base, line)


def tint(
    image: Image.Image, region: Image.Image, colour: RGB = (255, 0, 160), strength: float = 0.55
) -> Image.Image:
    """``image`` with ``region`` (binary) washed in ``colour``: the "generated inside the product"
    highlight."""
    base = image.convert("RGB")
    wash = Image.blend(base, Image.new("RGB", base.size, colour), strength)
    return Image.composite(wash, base, binary(region))


# --- occlusion ----------------------------------------------------------------------------------------------


def _convex(points: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Andrew's monotone chain: the convex hull of ``points``, counter-clockwise."""
    points = sorted(set(points))
    if len(points) <= 2:
        return points

    def cross(o: tuple[int, int], a: tuple[int, int], b: tuple[int, int]) -> int:
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])

    lower: list[tuple[int, int]] = []
    for point in points:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], point) <= 0:
            lower.pop()
        lower.append(point)
    upper: list[tuple[int, int]] = []
    for point in reversed(points):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], point) <= 0:
            upper.pop()
        upper.append(point)
    return lower[:-1] + upper[:-1]


def filled_hull(mask: Image.Image, max_side: int = 512) -> Image.Image:
    """The convex hull of a binary mask, filled, at the mask's size.

    A finger across a bottle splits the visible mask; the hull closes the gap, so the hull minus the visible
    mask is the hidden part of the product (retail packaging is close to convex; a concave pack overstates
    the hidden share, which errs towards refusing).
    """
    small = binary(mask)
    if max(small.size) > max_side:
        small = binary(mask.resize(_fit((small.width, small.height), max_side), Image.Resampling.BILINEAR))
    width, height = small.size
    points: list[tuple[int, int]] = []
    for y in range(height):
        span = small.crop((0, y, width, y + 1)).getbbox()
        if span:
            points.extend(((span[0], y), (span[2], y), (span[0], y + 1), (span[2], y + 1)))
    hull = Image.new("L", small.size, 0)
    corners = _convex(points)
    if len(corners) >= 3:
        ImageDraw.Draw(hull).polygon(corners, fill=255)
    hull = ImageChops.lighter(hull, small)
    return binary(hull.resize(mask.size, Image.Resampling.NEAREST)) if hull.size != mask.size else hull


@dataclass(frozen=True, slots=True)
class Occlusion:
    hull: Image.Image  # the product's full extent as far as we can tell (binary)
    hidden: Image.Image  # hull pixels covered by a hand (binary)
    percent: float  # hidden / hull, 0-100


def occlusion(
    visible: Image.Image, hands: Image.Image | None, outline: Image.Image | None = None
) -> Occlusion:
    """How much of the product a hand hides: ``(hull ∪ outline) ∩ hands − visible`` over the hull."""
    visible = binary(visible)
    hull = filled_hull(visible)
    if outline is not None:
        hull = ImageChops.lighter(hull, binary(outline))
    if hands is None:
        hidden = Image.new("L", visible.size, 0)
    else:
        hidden = ImageChops.subtract(ImageChops.multiply(hull, binary(hands)), visible)
    total = area(hull)
    percent = round(100.0 * area(hidden) / total, 2) if total else 0.0
    return Occlusion(hull=hull, hidden=hidden, percent=percent)


# --- layout -------------------------------------------------------------------------------------------------


def _fit(size: tuple[int, int], side: int) -> tuple[int, int]:
    width, height = size
    scale = side / max(width, height)
    return max(1, round(width * scale)), max(1, round(height * scale))


@dataclass(frozen=True, slots=True)
class Layout:
    width: int  # the 4:5 master
    height: int
    scale: float  # source px → master px
    box: Box  # the product box in the master

    @property
    def baseline(self) -> int:
        return self.box[3]


def canvas_width(box: Box) -> int:
    """The master's width: the product at its own resolution inside the safe zone, bounded to
    ``[MIN_CANVAS_WIDTH, MAX_CANVAS_WIDTH]``."""
    box_width, box_height = box[2] - box[0], box[3] - box[1]
    native = max(box_width / SAFE_WIDTH, box_height / (SAFE_HEIGHT * RATIO))
    return int(min(MAX_CANVAS_WIDTH, max(MIN_CANVAS_WIDTH, round(native))))


def plan_layout(box: Box, width: int | None = None, *, max_upscale: float = MAX_UPSCALE) -> Layout:
    """Where the product box goes on a ``width × 1.25 width`` master (``width`` from :func:`canvas_width`).

    The product fills the safe zone, except that a small photo is never enlarged more than ``max_upscale``
    (it then stands smaller, still centred on the baseline).
    """
    width = width or canvas_width(box)
    height = round(width * RATIO)
    box_width, box_height = box[2] - box[0], box[3] - box[1]
    scale = min(SAFE_WIDTH * width / box_width, SAFE_HEIGHT * height / box_height, max_upscale)
    placed_width, placed_height = max(1, round(box_width * scale)), max(1, round(box_height * scale))
    x0 = (width - placed_width) // 2
    y1 = round(BASELINE * height)
    return Layout(
        width=width, height=height, scale=scale, box=(x0, y1 - placed_height, x0 + placed_width, y1)
    )


def square_box(layout: Layout) -> Box:
    """The square window of a master: its middle ``W×W`` (the safe zone keeps the product inside it)."""
    top = (layout.height - layout.width) // 2
    return (0, top, layout.width, top + layout.width)


@dataclass(frozen=True, slots=True)
class Placed:
    product: Image.Image  # RGB, master size: the photo's pixels at the product's place (junk elsewhere)
    alpha: Image.Image  # L, master size: the paste alpha
    core: Image.Image  # binary, master size: pixels that must equal ``product`` in the final image
    visible: Image.Image  # binary, master size: the visible product


def place(photo: Image.Image, mask: Image.Image, box: Box, layout: Layout) -> Placed:
    """The photo's product moved onto the master: one crop and one uniform LANCZOS resize per layer."""
    size = (layout.box[2] - layout.box[0], layout.box[3] - layout.box[1])
    rgb = photo.convert("RGB").crop(box).resize(size, Image.Resampling.LANCZOS)
    visible_small = binary(binary(mask).crop(box).resize(size, Image.Resampling.BILINEAR))
    refined = refine(visible_small, edge_radius(layout.box))

    def on_master(layer: Image.Image, mode: str) -> Image.Image:
        blank = Image.new(mode, (layout.width, layout.height), 0 if mode == "L" else PLACEHOLDER)
        blank.paste(layer, layout.box[:2])
        return blank

    return Placed(
        product=on_master(rgb, "RGB"),
        alpha=on_master(refined.alpha, "L"),
        core=on_master(refined.core, "L"),
        visible=on_master(visible_small, "L"),
    )


def canvas(placed: Placed, background: RGB = PLACEHOLDER) -> Image.Image:
    """What the image model receives: the product on a flat placeholder."""
    return Image.composite(placed.product, Image.new("RGB", placed.product.size, background), placed.alpha)


def composite(scene: Image.Image, placed: Placed) -> Image.Image:
    """The final image: ``scene`` (resized to the master) with the original product pasted back on top."""
    if scene.size != placed.product.size:
        scene = scene.convert("RGB").resize(placed.product.size, Image.Resampling.LANCZOS)
    return Image.composite(placed.product, scene.convert("RGB"), placed.alpha)


def interior_difference(final: Image.Image, placed: Placed) -> int:
    """The largest channel difference between ``final`` and the photo's product inside the core: 0 when
    the rule holds (a lossy encode of ``final`` makes it non-zero, so check before encoding)."""
    diff = ImageChops.difference(final.convert("RGB"), placed.product)
    black = Image.new("RGB", diff.size, (0, 0, 0))
    inside = Image.composite(diff, black, placed.core)
    extrema = cast("tuple[tuple[int, int], ...]", inside.getextrema())
    return max(high for _low, high in extrema)


def interior_diff_image(final: Image.Image, placed: Placed) -> Image.Image:
    """The core's difference image, amplified ×16 (must be black)."""
    diff = ImageChops.difference(final.convert("RGB"), placed.product).point(lambda v: min(255, v * 16))
    return Image.composite(diff, Image.new("RGB", diff.size, (0, 0, 0)), placed.core)


def generated_inside(placed: Placed, hull_on_master: Image.Image) -> tuple[Image.Image, float]:
    """The hand-hidden pixels (``hull_on_master``: :func:`occlusion`'s ``hidden`` on the master) that the
    paste-back does not cover, so the model filled them, and their share of the whole product in percent."""
    pasted = binary(placed.alpha, 250)
    generated = ImageChops.subtract(binary(hull_on_master), pasted)
    generated = erode(generated, 1)  # the feather band itself is not "generated"
    total = area(ImageChops.lighter(placed.visible, binary(hull_on_master)))
    return generated, (round(100.0 * area(generated) / total, 2) if total else 0.0)


def hull_on_master(hull: Image.Image, box: Box, layout: Layout) -> Image.Image:
    size = (layout.box[2] - layout.box[0], layout.box[3] - layout.box[1])
    layer = binary(binary(hull).crop(box).resize(size, Image.Resampling.BILINEAR))
    blank = Image.new("L", (layout.width, layout.height), 0)
    blank.paste(layer, layout.box[:2])
    return blank


# --- checks -------------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Registration:
    dx: float  # share of the master's width the model shifted the product by (best match)
    dy: float
    scale: float  # how much the model resized the product (1.0 = unchanged)
    error: float  # mean absolute grey difference inside the core at the best match (0-255)
    aligned_error: float  # the same at zero offset and scale 1.0


def _grey(image: Image.Image, size: tuple[int, int]) -> Image.Image:
    return image.convert("L").resize(size, Image.Resampling.BILINEAR)


def _mean_inside(diff: Image.Image, mask: Image.Image) -> float:
    count = area(mask)
    if not count:
        return 0.0
    return float(ImageStat.Stat(diff, mask=mask).sum[0]) / count


def register(
    generated: Image.Image, sent: Image.Image, core: Image.Image, *, width: int = 232
) -> Registration:
    """Where the product is in the model's image, compared with where we put it.

    Grey copies ``width`` px wide; the generated image is shifted by ``-8..8`` px and scaled by 0.96, 1.00
    and 1.04 about the centre, scoring the mean absolute difference against the sent canvas inside the core.
    """
    height = round(width * sent.height / sent.width)
    size = (width, height)
    target = _grey(sent, size)
    mask = binary(core.resize(size, Image.Resampling.BILINEAR), 250)
    source = _grey(generated, size)
    best: tuple[float, int, int, float] | None = None
    aligned = 0.0
    for scale in (0.96, 1.0, 1.04):
        if scale == 1.0:
            scaled = source
        else:
            scaled_size = (round(width * scale), round(height * scale))
            big = source.resize(scaled_size, Image.Resampling.BILINEAR)
            left, top = (scaled_size[0] - width) // 2, (scaled_size[1] - height) // 2
            if scale > 1:
                scaled = big.crop((left, top, left + width, top + height))
            else:
                scaled = Image.new("L", size, 128)
                scaled.paste(big, (-left, -top))
        for dy in range(-8, 9):
            for dx in range(-8, 9):
                shifted = ImageChops.offset(scaled, -dx, -dy)
                error = _mean_inside(ImageChops.difference(shifted, target), mask)
                if scale == 1.0 and dx == 0 and dy == 0:
                    aligned = error
                if best is None or error < best[0] - 1e-9:
                    best = (error, dx, dy, scale)
    error, dx, dy, scale = best if best is not None else (aligned, 0, 0, 1.0)
    scale = round(1 / scale, 2)  # the correction that matched → the size change the model made
    return Registration(
        dx=dx / width, dy=dy / width, scale=scale, error=round(error, 2), aligned_error=round(aligned, 2)
    )


def interior_change(generated: Image.Image, sent: Image.Image, core: Image.Image) -> float:
    """Mean absolute grey difference inside the core between the model's image and what we sent (0-255):
    a restyled, relabelled or recoloured product scores high even when it did not move."""
    size = sent.size
    return round(
        _mean_inside(ImageChops.difference(_grey(generated, size), _grey(sent, size)), binary(core, 250)), 2
    )


# --- studio backgrounds -------------------------------------------------------------------------------------


def hex_colour(value: str) -> RGB:
    text = value.strip().lstrip("#")
    if len(text) != 6:
        raise ValueError(f"not a #rrggbb colour: {value!r}")
    return (int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16))


def studio_background(size: tuple[int, int], params: dict[str, object]) -> Image.Image:
    """A solid, linear (top → bottom) or radial (centre → corners) background.

    ``params``: ``{"fill": "solid"|"linear"|"radial", "colours": ["#rrggbb", ...]}`` (one colour for solid,
    two for the gradients), optional ``"floor": "#rrggbb"`` and ``"floor_at": 0.78`` (share of the height
    where a floor colour starts, blended over 6 % of the height).
    """
    width, height = size
    colours = [hex_colour(str(value)) for value in list(params.get("colours") or ["#f4efe8"])]  # type: ignore[call-overload]
    fill = str(params.get("fill") or "solid")
    if fill == "solid" or len(colours) < 2:
        image = Image.new("RGB", size, colours[0])
    else:
        start, end = colours[0], colours[1]
        if fill == "linear":
            ramp = Image.linear_gradient("L").resize((width, height), Image.Resampling.BILINEAR)
        else:
            ramp = Image.radial_gradient("L").resize((width, height), Image.Resampling.BILINEAR)
            ramp = ramp.point(lambda v: min(255, round(v * 1.4)))
        image = Image.composite(Image.new("RGB", size, end), Image.new("RGB", size, start), ramp)
    floor = params.get("floor")
    if floor:
        at = float(params.get("floor_at") or 0.78)  # type: ignore[arg-type]
        band = max(1, round(0.06 * height))
        top = round(at * height)
        mask = Image.new("L", size, 0)
        draw = ImageDraw.Draw(mask)
        for row in range(band):
            draw.line([(0, top + row), (width, top + row)], fill=round(255 * (row + 1) / band))
        draw.rectangle([(0, top + band), (width, height)], fill=255)
        image = Image.composite(Image.new("RGB", size, hex_colour(str(floor))), image, mask)
    return image


def contact_shadow(placed: Placed, layout: Layout, params: dict[str, object]) -> Image.Image:
    """A soft shadow under the product's base (mode ``L``, the shadow's opacity).

    The visible mask squashed into a thin ellipse-like band at the baseline, blurred; ``params``:
    ``shadow_opacity`` (0-1, default 0.35), ``shadow_softness`` (share of the product width, default 0.08).
    """
    opacity = float(params.get("shadow_opacity", 0.35))  # type: ignore[arg-type]
    softness = float(params.get("shadow_softness", 0.08))  # type: ignore[arg-type]
    x0, _y0, x1, base = layout.box
    product_width = x1 - x0
    band_height = max(4, round(product_width * 0.12))
    shadow = Image.new("L", (layout.width, layout.height), 0)
    draw = ImageDraw.Draw(shadow)
    inset = round(product_width * 0.04)
    draw.ellipse(
        [(x0 + inset, base - band_height // 2), (x1 - inset, base + band_height // 2)],
        fill=round(255 * max(0.0, min(1.0, opacity))),
    )
    return shadow.filter(ImageFilter.GaussianBlur(max(2, round(product_width * softness))))


def studio_scene(placed: Placed, layout: Layout, params: dict[str, object]) -> Image.Image:
    """A studio background with its contact shadow (no product: :func:`composite` adds it)."""
    background = studio_background((layout.width, layout.height), params)
    shadow_colour = hex_colour(str(params.get("shadow_colour") or "#2a2420"))
    return Image.composite(
        Image.new("RGB", background.size, shadow_colour), background, contact_shadow(placed, layout, params)
    )


# --- tilt ---------------------------------------------------------------------------------------------------


def tilt_degrees(mask: Image.Image, side: int = 128) -> float:
    """The principal-axis angle of a binary mask from vertical, in degrees (positive = leaning right).

    Second-order central moments of the mask downscaled to ``side`` px; 0 for an empty or round mask.
    """
    small = binary(mask.resize(_fit(mask.size, side), Image.Resampling.BILINEAR))
    width = small.width
    data = small.tobytes()
    count = sx = sy = 0.0
    sxx = syy = sxy = 0.0
    for index, value in enumerate(data):
        if value:
            x, y = index % width, index // width
            count += 1
            sx += x
            sy += y
            sxx += x * x
            syy += y * y
            sxy += x * y
    if count < 10:
        return 0.0
    mx, my = sx / count, sy / count
    mu20, mu02, mu11 = sxx / count - mx * mx, syy / count - my * my, sxy / count - mx * my
    if abs(mu20 - mu02) < 1e-6 and abs(mu11) < 1e-6:
        return 0.0
    theta = 0.5 * math.atan2(2 * mu11, mu20 - mu02)  # angle of the major axis from the x axis
    from_vertical = math.degrees(theta) - 90 if theta > 0 else math.degrees(theta) + 90
    return round(from_vertical, 1)


def rotate_upright(photo: Image.Image, mask: Image.Image, degrees: float) -> tuple[Image.Image, Image.Image]:
    """Photo and mask rotated by ``degrees`` (expanding the frame; the mask with nearest neighbours)."""
    return (
        photo.convert("RGB").rotate(
            degrees, resample=Image.Resampling.BICUBIC, expand=True, fillcolor=PLACEHOLDER
        ),
        binary(mask).rotate(degrees, resample=Image.Resampling.NEAREST, expand=True, fillcolor=0),
    )


# --- encoding -----------------------------------------------------------------------------------------------


def encode(image: Image.Image, fmt: str = "WEBP", quality: int = 92) -> bytes:
    output = BytesIO()
    if fmt == "PNG":
        image.save(output, format="PNG", optimize=True)
    else:
        image.convert("RGB").save(output, format=fmt, quality=quality, method=6 if fmt == "WEBP" else 0)
    return output.getvalue()


def decode(content: bytes) -> Image.Image:
    with Image.open(BytesIO(content)) as source:
        source.load()
        return source.copy()
