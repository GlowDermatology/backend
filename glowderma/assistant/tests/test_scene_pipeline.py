"""The product scene pipeline with fakes: the rule (product pixels untouched), the occlusion policy (the
owner's 5 % threshold), drift retries, the QA rejection, and ``manage.py scene_spike`` (no network)."""

from __future__ import annotations

import json
from decimal import Decimal
from io import BytesIO
from pathlib import Path
from typing import Any

import pytest
from django.core.management import CommandError, call_command
from PIL import Image, ImageChops, ImageDraw

from glowderma.assistant.management.commands import scene_spike
from glowderma.assistant.scenes import pipeline, prompts
from glowderma.assistant.scenes.templates import BY_KEY, STARTER
from glowderma.core import compose
from glowderma.integrations.llm.fake import FakeContentModel, FakeImageModel
from glowderma.integrations.llm.gemini import GeminiContentModel
from glowderma.integrations.llm.gemini_image import GeminiImageModel
from glowderma.integrations.llm.ports import (
    GenerationResult,
    ImageGenerationRequest,
    ImagePart,
    LLMUnavailable,
)
from glowderma.integrations.secret import Secret
from glowderma.integrations.segmentation.combined import CombinedSegmenter
from glowderma.integrations.segmentation.fake import FakeSegmenter
from glowderma.integrations.segmentation.gemini_json import GeminiJsonSegmenter
from glowderma.integrations.segmentation.ports import Segmentation

SIZE = (900, 1200)
BOX = (330, 300, 570, 1000)
STUDIO = BY_KEY["studio-ivory"]
MARBLE = BY_KEY["marble"]


def photo() -> Image.Image:
    image = Image.new("RGB", SIZE, (200, 196, 188))
    draw = ImageDraw.Draw(image)
    draw.rectangle([BOX[0], BOX[1], BOX[2] - 1, BOX[3] - 1], fill=(245, 245, 248))
    for y in range(BOX[1] + 200, BOX[1] + 500, 12):
        draw.line([(BOX[0] + 30, y), (BOX[2] - 30, y)], fill=(20, 30, 90), width=3)  # the "label"
    return image


def rect(box: compose.Box) -> Image.Image:
    mask = Image.new("L", SIZE, 0)
    ImageDraw.Draw(mask).rectangle([box[0], box[1], box[2] - 1, box[3] - 1], fill=255)
    return mask


def segmentation(hands: Image.Image | None = None, **overrides: Any) -> Segmentation:
    product = rect(BOX) if hands is None else ImageChops.subtract(rect(BOX), hands)
    values: dict[str, Any] = {
        "product": product,
        "box": product.getbbox(),
        "hands": hands,
        "hand_present": hands is not None,
        "input_tokens": 300,
        "output_tokens": 800,
        "model": "seg",
        **overrides,
    }
    return Segmentation(**values)


class QA(FakeContentModel):
    def __init__(self, **answers: bool) -> None:
        super().__init__()
        self.answers = answers

    def generate(self, request: Any, *, api_key: Secret) -> GenerationResult:
        data = {name: self.answers.get(name, False) for name in prompts.QA_SCHEMA["properties"]}
        return GenerationResult(data=data, input_tokens=400, output_tokens=30, model="qa")


def providers(segment: Segmentation | Exception | None = None, qa: QA | None = None) -> pipeline.Providers:
    segmenter = FakeSegmenter()
    segmenter.push(segment if segment is not None else segmentation())
    return pipeline.Providers(
        segmenter=segmenter, image_model=FakeImageModel(), content_model=qa or QA(), api_key=Secret("k")
    )


def run(
    template: Any, used: pipeline.Providers | None = None, **config: Any
) -> tuple[Any, Any, pipeline.Usage]:
    usage = pipeline.Usage()
    analysis, result = pipeline.run(
        photo(), template, used or providers(), pipeline.Config(straighten=False, **config), usage
    )
    return analysis, result, usage


def product_kept(result: pipeline.Result) -> bool:
    """The label stripes are in the final image, unchanged (the fake segmentation is exact)."""
    return result.interior_difference == 0


def test_studio_scene_keeps_the_product_and_costs_only_segmentation() -> None:
    analysis, result, usage = run(STUDIO)
    assert product_kept(result) and result.kind == "studio"
    width, height = result.final.size
    assert height == round(width * 1.25)
    square = result.square
    assert square[2] - square[0] == width
    assert usage.image_calls == 0 and usage.text_calls == 1
    assert analysis.occluded_pct == 0 and analysis.warnings == []


def test_ai_scene_keeps_the_product_and_records_every_call() -> None:
    used = providers()
    _analysis, result, usage = run(MARBLE, used)
    assert product_kept(result) and result.kind == "ai_scene"
    assert len(result.attempts) == 1 and result.attempts[0].qa == dict.fromkeys(
        prompts.QA_SCHEMA["properties"], False
    )
    assert result.canvas is not None and result.generated is not None
    assert (usage.image_calls, usage.text_calls, usage.image_output) == (1, 2, 1120)
    [call] = used.image_model.calls  # type: ignore[attr-defined]
    assert (
        call.aspect_ratio == "4:5" and "THE PRODUCT IS LOCKED" in call.prompt and MARBLE.prompt in call.prompt
    )
    assert "No hands, fingers" in call.prompt and prompts.DEFAULT_STYLE in call.prompt
    assert usage.cost(pipeline.Prices()) > 0


def test_a_model_that_moves_the_product_is_retried_then_refused() -> None:
    used = providers()
    model: FakeImageModel = used.image_model  # type: ignore[assignment]
    drifted = FakeImageModel().generate_image(
        ImageGenerationRequest(
            model="m",
            prompt="[fake-drift]",
            images=(ImagePart(compose.encode(blank_canvas(), "PNG"), "image/png"),),
        ),
        api_key=Secret("k"),
    )
    model.push(drifted)
    _analysis, result, _usage = run(MARBLE, used)
    assert [a.error for a in result.attempts] == [pipeline.PRODUCT_DRIFT, ""]
    assert prompts.STRICTER in model.calls[-1].prompt and prompts.STRICTER not in model.calls[0].prompt
    with pytest.raises(pipeline.SceneError) as caught:
        run(BY_KEY["silk"].__class__("x", "ai_scene", {}, prompt="[fake-drift] silk"))
    assert caught.value.code == pipeline.PRODUCT_DRIFT and len(caught.value.attempts) == 2  # type: ignore[attr-defined]


def blank_canvas() -> Image.Image:
    """A canvas the size the pipeline sends for :data:`BOX` (the drifted answer is resized anyway)."""
    layout = compose.plan_layout(BOX)
    return Image.new("RGB", (layout.width, layout.height), compose.PLACEHOLDER)


def test_a_scene_with_a_hand_or_an_office_is_rejected_by_qa() -> None:
    with pytest.raises(pipeline.SceneError) as caught:
        run(MARBLE, providers(qa=QA(hands_or_people=True)))
    assert caught.value.code == pipeline.RESULT_REJECTED
    _analysis, result, _usage = run(MARBLE, providers(qa=QA(product_floating=True)))
    assert "product_floating" in result.warnings
    _analysis, result, usage = run(MARBLE, providers(qa=QA(office_remnants=True)), qa_check=False)
    assert result.attempts[0].qa is None and usage.text_calls == 1


def test_a_hand_holding_the_product_is_cut_away() -> None:
    touching = rect((BOX[2], 600, BOX[2] + 120, 700))  # beside the product, hides nothing
    analysis, result, _usage = run(STUDIO, providers(segmentation(touching)))
    assert analysis.occluded_pct <= pipeline.OCCLUSION_FLOOR
    assert "hand_removed" in result.warnings and product_kept(result)


def finger(height: int) -> Image.Image:
    return rect((BOX[0] - 60, 650, BOX[2] + 60, 650 + height))


def test_a_small_hidden_part_is_filled_by_ai_and_shown_to_staff() -> None:
    analysis, result, _usage = run(MARBLE, providers(segmentation(finger(21))))
    assert 2.0 < analysis.occluded_pct <= 5.0
    assert "product_partly_generated" in result.warnings and result.generated_inside is not None
    assert 2.0 < result.generated_inside_pct <= 5.0
    assert product_kept(result)
    with pytest.raises(pipeline.SceneError) as caught:
        run(STUDIO, providers(segmentation(finger(21))))
    assert caught.value.code == pipeline.OCCLUSION_NEEDS_AI


def test_the_occlusion_rule_reaches_the_prompt() -> None:
    used = providers(segmentation(finger(21)))
    run(MARBLE, used)
    assert prompts.OCCLUSION_RULE in used.image_model.calls[0].prompt  # type: ignore[attr-defined]


def test_a_hand_over_too_much_of_the_product_is_refused() -> None:
    with pytest.raises(pipeline.SceneError) as caught:
        run(MARBLE, providers(segmentation(finger(84))), max_occluded_pct=5.0)
    assert caught.value.code == pipeline.HAND_COVERS_PRODUCT
    # the owner's limit (15 %) lets a typical grip through, highlighted for staff
    _analysis, result, _usage = run(MARBLE, providers(segmentation(finger(84))))
    assert 5.0 < result.generated_inside_pct <= 15.0
    with pytest.raises(pipeline.SceneError) as caught:
        run(MARBLE, providers(segmentation(finger(140))))
    assert caught.value.code == pipeline.HAND_COVERS_PRODUCT


def test_a_measured_mask_wins_over_the_models_guess() -> None:
    # live, the analyst answered "18 % hidden" for every hand-held photo: with a pixel mask we measure
    guess = segmentation(None, hand_present=True, hand_covers_product=True, covered_percent=19.0)
    analysis, result, _usage = run(MARBLE, providers(guess))
    assert analysis.occluded_pct < 0.3 and result.generated_inside_pct == 0.0
    assert "hand_removed" in result.warnings


def test_a_box_only_answer_keeps_the_models_estimate() -> None:
    box_only = segmentation(None, hand_covers_product=True, covered_percent=19.0, exact=False)
    analysis = pipeline.analyse(
        photo(), providers(box_only), pipeline.Config(), pipeline.Usage(), for_scene=False
    )
    assert analysis.occluded_pct == 19.0


def test_photos_without_a_usable_product_are_refused() -> None:
    tiny = rect((400, 500, 430, 530))
    with pytest.raises(pipeline.SceneError) as caught:
        run(STUDIO, providers(Segmentation(product=tiny, box=(400, 500, 430, 530))))
    assert caught.value.code == pipeline.NO_PRODUCT
    everything = rect((20, 20, SIZE[0] - 20, SIZE[1] - 20))
    with pytest.raises(pipeline.SceneError) as caught:
        run(STUDIO, providers(Segmentation(product=everything, box=(20, 20, SIZE[0] - 20, SIZE[1] - 20))))
    assert caught.value.code == pipeline.MASK_UNUSABLE


def test_a_product_cut_off_by_the_frame_is_refused_before_any_image_call() -> None:
    small = (0, 300, 140, 600)
    used = providers(Segmentation(product=rect(small), box=small))
    with pytest.raises(pipeline.SceneError) as caught:
        run(MARBLE, used)
    assert caught.value.code == pipeline.PRODUCT_CUT_OFF
    assert used.image_model.calls == []  # type: ignore[attr-defined]
    flagged = providers(segmentation(cut_off=True))
    with pytest.raises(pipeline.SceneError) as caught:
        run(STUDIO, flagged)
    assert caught.value.code == pipeline.PRODUCT_CUT_OFF


def test_low_resolution_warning() -> None:
    small = (300, 300, 440, 600)
    analysis, _result, _usage = run(STUDIO, providers(Segmentation(product=rect(small), box=small)))
    assert analysis.warnings == ["low_resolution"]


def test_provider_errors_propagate_with_their_code() -> None:
    with pytest.raises(LLMUnavailable):
        run(STUDIO, providers(LLMUnavailable("down")))
    used = providers()
    used.image_model.push(LLMUnavailable("down"))  # type: ignore[attr-defined]
    with pytest.raises(LLMUnavailable):
        run(MARBLE, used)


def test_a_tilted_product_is_straightened() -> None:
    tilted = photo().rotate(10, fillcolor=(200, 196, 188))
    mask = rect(BOX).rotate(10)
    used = providers(Segmentation(product=mask, box=mask.getbbox()))  # type: ignore[arg-type]
    usage = pipeline.Usage()
    _analysis, result = pipeline.run(tilted, STUDIO, used, pipeline.Config(), usage)
    assert "straightened" in result.warnings and product_kept(result)


def test_usage_cost() -> None:
    usage = pipeline.Usage()
    usage.add_text(1_000_000, 0)
    usage.add_image(0, 0, 1120)
    assert usage.cost(pipeline.Prices()) == Decimal("0.1") + Decimal("0.0336")


def test_starter_templates_render() -> None:
    assert len(STARTER) == 8 and {t.kind for t in STARTER} == {"studio", "ai_scene"}
    for template in STARTER:
        assert (template.prompt != "") == (template.kind == "ai_scene")
        assert set(template.name) == {"uz", "ru", "en"}


def test_load_photo_applies_exif_orientation() -> None:
    image = Image.new("RGB", (40, 20))
    exif = image.getexif()
    exif[0x0112] = 6  # rotate 90° clockwise to display
    output = BytesIO()
    image.save(output, format="JPEG", exif=exif)
    assert pipeline.load_photo(output.getvalue()).size == (20, 40)


# --- manage.py scene_spike ----------------------------------------------------------------------------------


@pytest.fixture
def folder(tmp_path: Path) -> Path:
    source = tmp_path / "photos"
    source.mkdir()
    photo().save(source / "cream.jpg", quality=92)
    Image.new("RGB", (300, 300), (90, 90, 90)).save(source / "empty.png")  # nothing differs: no product
    (source / "broken.webp").write_bytes(b"not an image")
    (source / "notes.txt").write_text("skipped")
    return source


def test_scene_spike_writes_a_report(folder: Path, tmp_path: Path) -> None:
    out = tmp_path / "report"
    call_command(
        "scene_spike", "--input", str(folder), "--out", str(out), "--fake", "--templates", "studio-white,silk"
    )
    report = json.loads((out / "report.json").read_text())
    summary = report["summary"]
    assert summary["photos"] == 3 and summary["rule_held"] is True and summary["ai_accepted"] == 1
    assert summary["photo_errors"] == {
        "empty.png": "scene_no_product",
        "broken.webp": "unreadable (UnidentifiedImageError)",
    }
    cream = next(entry for entry in report["photos"] if entry["photo"] == "cream.jpg")
    assert [row["template"] for row in cream["results"]] == ["studio-white", "silk"]
    for name in (
        "outline.jpg",
        "mask.png",
        "studio-white.webp",
        "studio-white_1x1.webp",
        "silk_canvas.png",
        "silk_model.webp",
    ):
        assert (out / "cream" / name).is_file()
    html = (out / "report.html").read_text()
    assert "cream.jpg" in html and "scene_no_product" in html


def test_scene_spike_refusals(folder: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(CommandError, match="not a folder"):
        call_command("scene_spike", "--input", str(tmp_path / "missing"), "--out", str(tmp_path / "o"))
    with pytest.raises(CommandError, match="unknown templates"):
        call_command(
            "scene_spike", "--input", str(folder), "--out", str(tmp_path / "o"), "--templates", "nope"
        )
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    with pytest.raises(CommandError, match="GEMINI_API_KEY is empty"):
        call_command("scene_spike", "--input", str(folder), "--out", str(tmp_path / "o"))


def test_scene_spike_keys_and_live_providers(monkeypatch: pytest.MonkeyPatch) -> None:
    assert scene_spike._key("proxy", "X").reveal() == scene_spike.PROXY_PLACEHOLDER
    monkeypatch.setenv("SPIKE_KEY", "abc")
    assert scene_spike._key("env", "SPIKE_KEY").reveal() == "abc"
    live = scene_spike.build_providers(fake=False, segmenter="json", api_key=Secret("k"))
    assert isinstance(live.segmenter, GeminiJsonSegmenter) and isinstance(live.image_model, GeminiImageModel)
    assert isinstance(live.content_model, GeminiContentModel)
    matte = scene_spike.build_providers(fake=False, segmenter="matte", api_key=Secret("k"))
    assert isinstance(matte.segmenter, CombinedSegmenter)
    bria = scene_spike.build_providers(
        fake=False, segmenter="bria", api_key=Secret("k"), bria_key=Secret("b")
    )
    assert isinstance(bria.segmenter, CombinedSegmenter)


@pytest.mark.django_db
def test_scene_spike_vault_key_needs_an_active_version() -> None:
    with pytest.raises(CommandError, match="no active gemini key"):
        scene_spike._key("vault", "X")


def test_a_reported_cover_without_a_hand_mask_is_marked_generated() -> None:
    notch = rect(BOX)
    ImageDraw.Draw(notch).rectangle([BOX[0], 600, BOX[0] + 40, 640], fill=0)  # where the finger was
    seg = Segmentation(
        product=notch, box=BOX, hand_present=True, hand_covers_product=True, covered_percent=3.0
    )
    _analysis, result, _usage = run(MARBLE, providers(seg))
    assert result.generated_inside_pct > 0 and "product_partly_generated" in result.warnings
    with pytest.raises(pipeline.SceneError) as caught:
        run(
            STUDIO,
            providers(Segmentation(product=notch, box=BOX, hand_present=True, hand_covers_product=True)),
        )
    assert caught.value.code == pipeline.OCCLUSION_NEEDS_AI


def test_a_held_product_with_a_narrow_neck_is_not_counted_as_hidden() -> None:
    """A hand holds the bottle but no finger lies over it: the neck's dip in the hull hides nothing."""
    neck = rect(BOX)
    draw = ImageDraw.Draw(neck)
    draw.rectangle([BOX[0], BOX[1], BOX[0] + 60, BOX[1] + 120], fill=0)
    draw.rectangle([BOX[2] - 60, BOX[1], BOX[2], BOX[1] + 120], fill=0)
    held = Segmentation(product=neck, box=BOX, hand_present=True, hand_covers_product=False)
    _analysis, result, _usage = run(STUDIO, providers(held))
    assert result.generated_inside_pct == 0 and product_kept(result)


def test_a_thumb_bite_counts_even_when_the_analyst_misses_it() -> None:
    """Live (BB cream): the analyst said no finger covers the tube; the silhouette's bite says otherwise."""
    bite = rect(BOX)
    ImageDraw.Draw(bite).ellipse([BOX[2] - 25, 500, BOX[2] + 25, 560], fill=0)
    held = Segmentation(product=bite, box=BOX, hand_present=True, hand_covers_product=False)
    with pytest.raises(pipeline.SceneError) as caught:
        run(STUDIO, providers(held))
    assert caught.value.code == pipeline.OCCLUSION_NEEDS_AI
    _analysis, result, _usage = run(MARBLE, providers(held))
    assert result.generated_inside_pct > 0 and product_kept(result)


def test_summary_counts_a_broken_rule() -> None:
    entries = [
        {"photo": "a.jpg", "results": [{"kind": "studio", "error": pipeline.RULE_BROKEN, "cost_usd": "0"}]}
    ]
    assert scene_spike.summarise(entries)["rule_held"] is False
