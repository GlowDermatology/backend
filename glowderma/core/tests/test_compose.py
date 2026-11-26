"""Scene compositing (``core.compose``): the product's own pixels always survive the paste-back, the checks
catch a model that moved it, and occlusion measures what a hand hides."""

from __future__ import annotations

from PIL import Image, ImageChops, ImageDraw

from glowderma.core import compose

BACKGROUND = (205, 200, 190)


def striped_product(
    size: tuple[int, int] = (1200, 1600), box: compose.Box = (450, 400, 750, 1300)
) -> Image.Image:
    """A photo whose product carries a fine, high-contrast pattern (the label a model would garble)."""
    photo = Image.new("RGB", size, BACKGROUND)
    draw = ImageDraw.Draw(photo)
    for y in range(box[1], box[3]):
        for x in range(box[0], box[2], 3):
            draw.point((x, y), fill=((x * 7) % 256, (y * 5) % 256, (x + y) % 256))
    return photo


def rect_mask(size: tuple[int, int], box: compose.Box) -> Image.Image:
    mask = Image.new("L", size, 0)
    ImageDraw.Draw(mask).rectangle([box[0], box[1], box[2] - 1, box[3] - 1], fill=255)
    return mask


def placed_product() -> tuple[compose.Layout, compose.Placed]:
    box = (450, 400, 750, 1300)
    photo = striped_product(box=box)
    layout = compose.plan_layout(box)
    return layout, compose.place(photo, rect_mask(photo.size, box), box, layout)


def test_erode_and_dilate_are_exact_square_windows() -> None:
    mask = rect_mask((100, 100), (20, 20, 60, 60))
    assert compose.erode(mask, 3).getbbox() == (23, 23, 57, 57)
    assert compose.dilate(mask, 3).getbbox() == (17, 17, 63, 63)
    assert compose.erode(mask, 0).getbbox() == mask.getbbox()


def test_refine_never_reaches_outside_the_mask_and_is_opaque_in_the_core() -> None:
    mask = rect_mask((200, 200), (50, 50, 150, 150))
    refined = compose.refine(mask, 4)
    outside = ImageChops.multiply(refined.alpha, ImageChops.invert(mask))
    assert outside.getbbox() is None
    inside_core = ImageChops.multiply(ImageChops.invert(refined.alpha), refined.core)
    assert inside_core.getbbox() is None
    assert refined.core.getbbox() == (54, 54, 146, 146)


def test_fill_holes_closes_label_gaps_but_not_hands() -> None:
    mask = rect_mask((100, 100), (20, 20, 80, 80))
    ImageDraw.Draw(mask).rectangle([40, 40, 44, 44], fill=0)  # a letter the segmenter dropped
    ImageDraw.Draw(mask).rectangle([60, 60, 64, 64], fill=0)  # a fingertip
    hand = rect_mask((100, 100), (60, 60, 65, 65))
    filled = compose.fill_holes(mask, hand)
    assert filled.getpixel((42, 42)) == 255
    assert filled.getpixel((62, 62)) == 0
    assert filled.getpixel((5, 5)) == 0


def test_layout_stands_the_product_on_the_baseline_inside_the_square() -> None:
    layout = compose.plan_layout((0, 0, 300, 900))
    assert layout.height == round(layout.width * 1.25)
    x0, y0, x1, y1 = layout.box
    assert y1 == round(compose.BASELINE * layout.height)
    assert abs((x0 + x1) / 2 - layout.width / 2) <= 1
    square = compose.square_box(layout)
    assert square[2] - square[0] == square[3] - square[1] == layout.width
    assert square[1] <= y0 and y1 <= square[3]


def test_a_small_photo_is_not_enlarged_beyond_the_cap() -> None:
    layout = compose.plan_layout((0, 0, 100, 200))
    assert layout.width == compose.MIN_CANVAS_WIDTH
    assert layout.scale == compose.MAX_UPSCALE


def test_a_large_product_caps_the_canvas() -> None:
    assert compose.canvas_width((0, 0, 3000, 3000)) == compose.MAX_CANVAS_WIDTH


def test_the_paste_back_keeps_every_core_pixel_whatever_the_scene() -> None:
    _layout, placed = placed_product()
    for scene in (
        Image.new("RGB", placed.product.size, (0, 255, 0)),
        Image.effect_noise(placed.product.size, 80).convert("RGB"),
        Image.new("RGB", (400, 500), (10, 10, 10)),  # a model answering at another size
    ):
        final = compose.composite(scene, placed)
        assert compose.interior_difference(final, placed) == 0
        assert compose.interior_diff_image(final, placed).getbbox() is None


def test_a_scene_painted_over_the_product_is_caught() -> None:
    _layout, placed = placed_product()
    tampered = compose.composite(Image.new("RGB", placed.product.size), placed)
    ImageDraw.Draw(tampered).rectangle(placed.core.getbbox(), fill=(0, 0, 0))  # type: ignore[arg-type]
    assert compose.interior_difference(tampered, placed) > 0


def test_registration_finds_a_shift_and_a_resize() -> None:
    _layout, placed = placed_product()
    sent = compose.canvas(placed)
    still = compose.register(sent, sent, placed.core)
    assert (still.dx, still.dy, still.scale, still.error) == (0.0, 0.0, 1.0, 0.0)
    shifted = Image.new("RGB", sent.size, compose.PLACEHOLDER)
    shifted.paste(sent, (round(0.03 * sent.width), 0))
    moved = compose.register(shifted, sent, placed.core)
    assert moved.dx > 0.02 and moved.aligned_error > moved.error
    width, height = sent.size
    big = sent.resize((round(width * 1.04), round(height * 1.04)))
    left, top = (big.width - width) // 2, (big.height - height) // 2
    resized = compose.register(big.crop((left, top, left + width, top + height)), sent, placed.core)
    assert resized.scale == 1.04


def test_interior_change_scores_a_restyled_product() -> None:
    _layout, placed = placed_product()
    sent = compose.canvas(placed)
    assert compose.interior_change(sent, sent, placed.core) == 0.0
    restyled = Image.composite(Image.new("RGB", sent.size, (250, 250, 250)), sent, placed.core)
    assert compose.interior_change(restyled, sent, placed.core) > 30


def test_occlusion_measures_a_finger_across_the_product() -> None:
    size = (400, 400)
    product = rect_mask(size, (150, 50, 250, 350))
    finger = rect_mask(size, (100, 190, 300, 210))
    visible = ImageChops.subtract(product, finger)
    measured = compose.occlusion(visible, finger)
    assert 6.0 <= measured.percent <= 7.5  # 20 of 300 rows
    assert compose.occlusion(product, None).percent == 0.0
    assert compose.occlusion(product, finger).percent < 0.3  # touching: only hull rounding, below the floor


def test_generated_inside_marks_only_the_hidden_part() -> None:
    size = (1200, 1600)
    box = (450, 400, 750, 1300)
    photo = striped_product(size, box)
    finger = rect_mask(size, (400, 800, 800, 850))
    visible = ImageChops.subtract(rect_mask(size, box), finger)
    hidden = compose.occlusion(visible, finger).hidden
    layout = compose.plan_layout(box)
    placed = compose.place(photo, visible, box, layout)
    inside, percent = compose.generated_inside(placed, compose.hull_on_master(hidden, box, layout))
    assert inside.getbbox() is not None and 3.0 <= percent <= 6.0
    nothing, zero = compose.generated_inside(placed, Image.new("L", placed.alpha.size, 0))
    assert nothing.getbbox() is None and zero == 0.0


def test_tilt_is_measured_and_undone() -> None:
    upright = rect_mask((400, 400), (170, 50, 230, 350))
    leaning = upright.rotate(-15)  # clockwise: the top leans right
    angle = compose.tilt_degrees(leaning)
    assert 13 <= angle <= 17
    assert abs(compose.tilt_degrees(upright)) < 1
    assert compose.tilt_degrees(Image.new("L", (50, 50), 0)) == 0.0
    photo, mask = compose.rotate_upright(Image.new("RGB", leaning.size), leaning, angle)
    assert abs(compose.tilt_degrees(mask)) < 2 and photo.size == mask.size


def test_studio_scene_backgrounds_and_shadow() -> None:
    layout, placed = placed_product()
    all_params: tuple[dict[str, object], ...] = (
        {"fill": "solid", "colours": ["#ffffff"]},
        {"fill": "linear", "colours": ["#f7f2ea", "#e9dfd1"], "floor": "#e3d7c6", "floor_at": 0.8},
        {
            "fill": "radial",
            "colours": ["#3b3532", "#1c1816"],
            "shadow_opacity": 0.6,
            "shadow_colour": "#000000",
        },
    )
    for params in all_params:
        scene = compose.studio_scene(placed, layout, params)
        assert scene.size == (layout.width, layout.height)
        final = compose.composite(scene, placed)
        assert compose.interior_difference(final, placed) == 0
    shadow = compose.contact_shadow(placed, layout, {})
    bbox = shadow.getbbox()
    assert bbox is not None and bbox[1] < layout.baseline < bbox[3]


def test_overlays_and_colours() -> None:
    photo = striped_product()
    mask = rect_mask(photo.size, (450, 400, 750, 1300))
    overlay = compose.outline_overlay(photo, mask)
    assert overlay.size == photo.size and overlay.getpixel((450, 800)) == (255, 0, 160)
    assert compose.tint(photo, mask).getpixel((10, 10)) == BACKGROUND
    assert compose.hex_colour("#0a0B0c") == (10, 11, 12)
    try:
        compose.hex_colour("red")
    except ValueError:
        pass
    else:  # pragma: no cover
        raise AssertionError("a named colour must be refused")


def test_encode_and_decode_round_trip() -> None:
    image = Image.new("RGB", (20, 30), (1, 2, 3))
    assert compose.decode(compose.encode(image, "PNG")).getpixel((0, 0)) == (1, 2, 3)
    assert compose.decode(compose.encode(image)).size == (20, 30)


def test_a_jar_with_a_finger_notch_is_not_tilted() -> None:
    jar = rect_mask((400, 400), (120, 100, 300, 320))  # slightly taller than wide
    ImageDraw.Draw(jar).rectangle([120, 180, 160, 220], fill=0)  # a cut-away finger
    assert compose.tilt_degrees(jar) == 0.0
    assert compose.tilt_degrees(compose.filled_hull(jar)) == 0.0


def test_a_flat_pack_on_a_flat_scene_is_not_called_drift() -> None:
    size = (928, 1160)
    sent = Image.new("RGB", size, compose.PLACEHOLDER)
    ImageDraw.Draw(sent).rectangle([330, 300, 600, 950], fill=(246, 246, 246))
    core = rect_mask(size, (340, 310, 590, 940))
    scene = Image.new("RGB", size, (244, 243, 240))
    ImageDraw.Draw(scene).rectangle([330, 300, 600, 950], fill=(246, 246, 246))
    answer = compose.decode(compose.encode(scene.resize((1856, 2320)), "JPEG", 85))
    found = compose.register(answer.resize(size), sent, core)
    assert (found.dx, found.dy, found.scale) == (0.0, 0.0, 1.0)
