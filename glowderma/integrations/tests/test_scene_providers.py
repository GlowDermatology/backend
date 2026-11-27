"""The image-output port (Gemini image adapter over ``httpx.MockTransport``, the fake, the registry) and the
segmenters (Gemini JSON parsing of PNG and polygon masks, the green matte, the offline fake). No network."""

from __future__ import annotations

import base64
import json
from collections.abc import Iterator
from io import BytesIO
from typing import Any

import httpx
import pytest
from django.core.exceptions import ImproperlyConfigured
from PIL import Image, ImageDraw

from glowderma.core import compose
from glowderma.integrations.llm.bria_image import BriaSceneModel
from glowderma.integrations.llm.fake import FakeImageModel
from glowderma.integrations.llm.gemini_image import GeminiImageModel, _split_output
from glowderma.integrations.llm.ports import (
    GenerationResult,
    ImageGenerationRequest,
    ImageGenerationResult,
    ImagePart,
    LLMBadOutput,
    LLMNotConfigured,
    LLMRefused,
    LLMUnavailable,
)
from glowderma.integrations.llm.registry import fake_image_model, get_image_model
from glowderma.integrations.secret import Secret
from glowderma.integrations.segmentation import gemini_json
from glowderma.integrations.segmentation.bria import BriaCutter
from glowderma.integrations.segmentation.combined import CombinedSegmenter, iou
from glowderma.integrations.segmentation.fake import FakeSegmenter
from glowderma.integrations.segmentation.gemini_json import GeminiJsonSegmenter
from glowderma.integrations.segmentation.gemini_matte import GeminiMatteCutter, _closest_ratio, key_green
from glowderma.integrations.segmentation.ports import (
    CutOut,
    MaskUnusable,
    NoProduct,
    SegmentationRequest,
)
from glowderma.integrations.segmentation.registry import fake_segmenter, get_segmenter
from glowderma.integrations.tests.test_llm import Recorder

KEY = Secret("AIza-test-key-0123456789")
IMAGE_PATH = "models/gemini-nano-banana-2.1:generateContent"


def png(image: Image.Image) -> bytes:
    output = BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def canvas_bytes() -> bytes:
    canvas = Image.new("RGB", (80, 100), compose.PLACEHOLDER)
    ImageDraw.Draw(canvas).rectangle([30, 30, 49, 79], fill=(250, 20, 20))
    return png(canvas)


def image_request(**overrides: Any) -> ImageGenerationRequest:
    values: dict[str, Any] = {
        "model": "gemini-nano-banana-2.1",
        "prompt": "scene",
        "images": (ImagePart(data=canvas_bytes(), mime_type="image/png"),),
        **overrides,
    }
    return ImageGenerationRequest(**values)


def image_answer(*, parts: list[dict[str, Any]] | None = None, finish: str = "STOP") -> dict[str, Any]:
    data = base64.b64encode(png(Image.new("RGB", (8, 10), (1, 2, 3)))).decode()
    return {
        "candidates": [
            {
                "content": {
                    "role": "model",
                    "parts": parts
                    if parts is not None
                    else [
                        {"text": "thinking", "thought": True},
                        {"inlineData": {"mimeType": "image/png", "data": data}},
                    ],
                },
                "finishReason": finish,
            }
        ],
        "usageMetadata": {
            "promptTokenCount": 600,
            "candidatesTokenCount": 1180,
            "thoughtsTokenCount": 40,
            "candidatesTokensDetails": [
                {"modality": "IMAGE", "tokenCount": 1120},
                {"modality": "TEXT", "tokenCount": 60},
            ],
        },
        "modelVersion": "gemini-nano-banana-2.1-001",
    }


def adapter(recorder: Recorder) -> GeminiImageModel:
    return GeminiImageModel(httpx_client=httpx.Client(transport=httpx.MockTransport(recorder)))


# --- Gemini image adapter -----------------------------------------------------------------------------------


def test_gemini_image_sends_an_image_request_and_splits_usage() -> None:
    recorder = Recorder(httpx.Response(200, json=image_answer()))
    result = adapter(recorder).generate_image(image_request(image_size="2K"), api_key=KEY)
    assert result.mime_type == "image/png" and Image.open(BytesIO(result.image)).size == (8, 10)
    assert (result.input_tokens, result.output_text_tokens, result.output_image_tokens) == (600, 100, 1120)
    assert result.model == "gemini-nano-banana-2.1-001"
    [sent] = recorder.requests
    assert sent.url.path.endswith(IMAGE_PATH) and sent.headers["x-goog-api-key"] == KEY.reveal()
    config = json.loads(sent.content)["generationConfig"]
    assert config["responseModalities"] == ["IMAGE"]
    assert config["imageConfig"] == {"aspectRatio": "4:5", "imageSize": "2K"}
    thinking = config["thinkingConfig"]
    assert thinking.get("thinkingLevel", thinking.get("thinking_level")) == "MINIMAL"


def test_gemini_image_without_an_image_part_is_bad_output() -> None:
    recorder = Recorder(httpx.Response(200, json=image_answer(parts=[{"text": "I cannot"}])))
    with pytest.raises(LLMBadOutput) as caught:
        adapter(recorder).generate_image(image_request(), api_key=KEY)
    assert caught.value.output_tokens == 1220  # billed even though unusable


@pytest.mark.parametrize("finish", ["IMAGE_SAFETY", "PROHIBITED_CONTENT"])
def test_gemini_image_refusal(finish: str) -> None:
    recorder = Recorder(httpx.Response(200, json=image_answer(finish=finish)))
    with pytest.raises(LLMRefused):
        adapter(recorder).generate_image(image_request(), api_key=KEY)


def test_gemini_image_blocked_prompt_and_errors() -> None:
    blocked = {"promptFeedback": {"blockReason": "SAFETY"}, "candidates": []}
    with pytest.raises(LLMRefused):
        adapter(Recorder(httpx.Response(200, json=blocked))).generate_image(image_request(), api_key=KEY)
    with pytest.raises(LLMNotConfigured):
        adapter(
            Recorder(httpx.Response(404, json={"error": {"code": 404, "status": "NOT_FOUND"}}))
        ).generate_image(image_request(), api_key=KEY)
    with pytest.raises(LLMNotConfigured):
        adapter(Recorder()).generate_image(image_request(), api_key=Secret(""))
    unavailable = Recorder(httpx.Response(503, json={"error": {"code": 503, "status": "UNAVAILABLE"}}))
    with pytest.raises(LLMUnavailable):
        adapter(unavailable).generate_image(image_request(max_retries=0), api_key=KEY)


def test_usage_split_without_details_counts_everything_as_image() -> None:
    class Usage:
        candidates_token_count = 1200
        thoughts_token_count = 30
        candidates_tokens_details = None

    assert _split_output(Usage()) == (30, 1200)


# --- fake image model and registry --------------------------------------------------------------------------


@pytest.fixture
def fake_image() -> Iterator[FakeImageModel]:
    model = fake_image_model()
    model.reset()
    yield model
    model.reset()


def test_fake_image_paints_only_the_placeholder(fake_image: FakeImageModel) -> None:
    result = fake_image.generate_image(image_request(), api_key=KEY)
    image = Image.open(BytesIO(result.image)).convert("RGB")
    assert image.getpixel((40, 50)) == (250, 20, 20)  # the product, unchanged
    assert image.getpixel((2, 2)) != compose.PLACEHOLDER
    drifted = Image.open(
        BytesIO(fake_image.generate_image(image_request(prompt="[fake-drift]"), api_key=KEY).image)
    ).convert("RGB")
    assert drifted.getpixel((31, 50)) != (250, 20, 20)  # the product moved 6 px right
    assert [call.prompt for call in fake_image.calls] == ["scene", "[fake-drift]"]


@pytest.mark.parametrize(
    ("prompt", "error"),
    [
        ("[fake-no-image]", LLMBadOutput),
        ("[fake-unavailable]", LLMUnavailable),
        ("[fake-refused]", LLMRefused),
    ],
)
def test_fake_image_markers(fake_image: FakeImageModel, prompt: str, error: type[Exception]) -> None:
    with pytest.raises(error):
        fake_image.generate_image(image_request(prompt=prompt), api_key=KEY)


def test_fake_image_queue_key_and_check(fake_image: FakeImageModel) -> None:
    queued = ImageGenerationResult(b"x", "image/png", 1, 2, 3, "m")
    fake_image.push(queued)
    fake_image.push(LLMUnavailable("down"))
    assert fake_image.generate_image(image_request(), api_key=KEY) is queued
    with pytest.raises(LLMUnavailable):
        fake_image.generate_image(image_request(), api_key=KEY)
    with pytest.raises(LLMNotConfigured):
        fake_image.generate_image(image_request(), api_key=Secret("invalid-key"))
    fake_image.check(api_key=KEY, model="gemini-nano-banana-2.1")
    with pytest.raises(LLMNotConfigured):
        fake_image.check(api_key=KEY, model="unknown-model")
    assert fake_image.is_configured()


def test_image_registry_modes(settings: Any) -> None:
    settings.AI_MODE = "fake"
    assert get_image_model() is fake_image_model()
    settings.AI_MODE = "live"
    assert isinstance(get_image_model(), GeminiImageModel)
    assert isinstance(get_image_model("bria"), BriaSceneModel) and not get_image_model("bria").fills_hidden
    with pytest.raises(LLMNotConfigured):
        get_image_model("other")
    settings.AI_MODE = "other"
    with pytest.raises(ImproperlyConfigured):
        get_image_model()
    settings.AI_MODE = "fake"
    settings.DEV_FEATURES_ALLOWED = False
    settings.SETTINGS_MODULE = "config.settings.production"
    with pytest.raises(LLMNotConfigured):
        get_image_model()


# --- Gemini JSON segmenter ----------------------------------------------------------------------------------


def mask_png(size: tuple[int, int]) -> str:
    probability = Image.new("L", size, 0)
    ImageDraw.Draw(probability).ellipse([0, 0, size[0] - 1, size[1] - 1], fill=230)
    return "data:image/png;base64," + base64.b64encode(png(probability)).decode()


class ScriptedContent:
    """A content model answering one scripted JSON object."""

    name = "scripted"

    def __init__(self, data: dict[str, Any]) -> None:
        self.data = data
        self.requests: list[Any] = []

    def generate(self, request: Any, *, api_key: Secret) -> GenerationResult:
        self.requests.append(request)
        return GenerationResult(data=self.data, input_tokens=300, output_tokens=900, model="seg-1")

    def check(self, *, api_key: Secret, model: str, timeout_seconds: int = 10) -> None:
        return None

    def is_configured(self) -> bool:
        return True


def segment(data: dict[str, Any], size: tuple[int, int] = (200, 400)) -> Any:
    model = ScriptedContent(data)
    request = SegmentationRequest(
        image=ImagePart(data=b"jpeg", mime_type="image/jpeg"), width=size[0], height=size[1], model="seg"
    )
    return GeminiJsonSegmenter(model).segment(request, api_key=KEY), model


def answer(**overrides: Any) -> dict[str, Any]:
    return {
        "product": {"box_2d": [250, 250, 750, 750], "mask": mask_png((64, 64))},
        "outline": [[250, 250], [750, 250], [750, 750], [250, 750]],
        "hands": [{"box_2d": [500, 0, 600, 1000], "mask": mask_png((32, 16))}],
        "hand_present": True,
        "hand_covers_product": True,
        "covered_percent": 4.5,
        "product_cut_off": False,
        "background": "office",
        **overrides,
    }


def test_json_segmenter_reads_box_mask_hands_and_flags() -> None:
    result, model = segment(answer())
    assert result.box[0] >= 50 and result.box[2] <= 150 and result.box[1] >= 100 and result.box[3] <= 300
    assert result.hands is not None and result.outline is not None
    assert (result.hand_present, result.hand_covers_product, result.covered_percent) == (True, True, 4.5)
    assert result.background == "office" and (result.input_tokens, result.output_tokens) == (300, 900)
    [sent] = model.requests
    assert sent.max_output_tokens == gemini_json.MAX_OUTPUT_TOKENS and sent.temperature == 0.0
    assert "Hands, fingers" in sent.prompt and sent.response_schema is gemini_json.SCHEMA


def test_json_segmenter_falls_back_to_the_polygon() -> None:
    product = {
        "box_2d": [100, 100, 900, 900],
        "mask": "not base64 png",
        "polygon": [[200, 200], [800, 200], [500, 900]],
    }
    result, _ = segment(answer(product=product, hands=[], background="garden"))
    assert result.box[1] == 80 and result.background == "other"
    assert result.hands is None and result.hand_present is True  # the flag alone still counts


def test_json_segmenter_refusals() -> None:
    with pytest.raises(NoProduct) as caught:
        segment(answer(product=None))
    assert caught.value.code == "scene_no_product" and caught.value.output_tokens == 900
    with pytest.raises(LLMBadOutput):
        segment(answer(product={"box_2d": [1, 2, 3]}))
    with pytest.raises(LLMBadOutput):
        segment(answer(product={"box_2d": [100, 100, 900, 900], "mask": ""}))


def test_box_and_polygon_helpers() -> None:
    assert gemini_json.box_pixels([0, 0, 1000, 1000], 300, 200) == (0, 0, 300, 200)
    assert gemini_json.box_pixels([0, 0, 1, 1], 300, 200) is None
    assert gemini_json.box_pixels(["a", 0, 1, 1], 300, 200) is None
    assert gemini_json.polygon_mask([[0, 0], [1, 1]], 10, 10) is None
    assert gemini_json.polygon_mask([[0, 0], [1000, 0], "x"], 10, 10) is None
    assert gemini_json.region_mask("nope", 10, 10) is None
    raw = base64.b64encode(png(Image.new("L", (16, 16), 255))).decode()
    mask = gemini_json.decode_png_mask(raw, (2, 2, 6, 6), 10, 10)
    assert mask is not None and mask.getbbox() == (2, 2, 6, 6)


# --- matte and fake segmenters ------------------------------------------------------------------------------


def test_key_green_keeps_the_non_green_product() -> None:
    matte = Image.new("RGB", (100, 100), (0, 255, 0))
    ImageDraw.Draw(matte).rectangle([30, 20, 69, 89], fill=(240, 240, 240))
    mask = key_green(matte, (200, 200))
    box = mask.getbbox()
    assert box is not None and abs(box[0] - 60) <= 3 and abs(box[3] - 180) <= 3
    assert _closest_ratio(800, 1000) == "4:5" and _closest_ratio(1000, 1000) == "1:1"


def scene_photo(shift: int = 0, background: tuple[int, int, int] = (0, 255, 0)) -> Image.Image:
    photo = Image.new("RGB", (100, 100), background)
    draw = ImageDraw.Draw(photo)
    draw.rectangle([30 + shift, 20, 69 + shift, 89], fill=(240, 240, 240))
    for y in range(25, 85, 6):  # a label: something to register on
        draw.line([(34 + shift, y), (64 + shift, y)], fill=(30, 40, 120), width=2)
    return photo


def cut_request(photo: Image.Image | None = None) -> SegmentationRequest:
    sent = photo or scene_photo(background=(150, 140, 130))
    return SegmentationRequest(image=ImagePart(png(sent), "image/png"), width=200, height=200, model="m")


def test_matte_cutter_keys_an_aligned_repaint(fake_image: FakeImageModel) -> None:
    fake_image.push(ImageGenerationResult(png(scene_photo()), "image/png", 500, 10, 1120, "nb"))
    cut = GeminiMatteCutter(fake_image, model="nb").cut(cut_request(), api_key=KEY)
    box = cut.mask.getbbox()
    assert box is not None and abs(box[0] - 60) <= 4 and cut.output_image_tokens == 1120
    assert fake_image.calls[-1].aspect_ratio == "1:1" and "#00FF00" in fake_image.calls[-1].prompt


def test_matte_cutter_refuses_a_repaint_that_moved_the_product(fake_image: FakeImageModel) -> None:
    fake_image.push(ImageGenerationResult(png(scene_photo(shift=6)), "image/png", 500, 10, 1120, "nb"))
    with pytest.raises(MaskUnusable) as caught:
        GeminiMatteCutter(fake_image, model="nb").cut(cut_request(), api_key=KEY)
    assert caught.value.code == "scene_mask_unusable" and caught.value.output_tokens == 1130


def test_matte_cutter_refusals(fake_image: FakeImageModel) -> None:
    fake_image.push(
        ImageGenerationResult(png(Image.new("RGB", (50, 50), (0, 255, 0))), "image/png", 1, 1, 1, "nb")
    )
    with pytest.raises(NoProduct):
        GeminiMatteCutter(fake_image, model="nb").cut(cut_request(), api_key=KEY)
    fake_image.push(ImageGenerationResult(b"not an image", "image/png", 1, 1, 1, "nb"))
    with pytest.raises(LLMBadOutput):
        GeminiMatteCutter(fake_image, model="nb").cut(cut_request(), api_key=KEY)


def rgba_cutout() -> bytes:
    image = Image.new("RGBA", (100, 100), (0, 0, 0, 0))
    ImageDraw.Draw(image).rectangle([30, 20, 69, 89], fill=(240, 240, 240, 255))
    return png(image)


def bria(*responders: Any, prompt: str | None = None) -> tuple[BriaCutter, Recorder]:
    recorder = Recorder(*responders)
    client = httpx.Client(transport=httpx.MockTransport(recorder))
    extra: dict[str, Any] = {} if prompt is None else {"prompt": prompt}
    return BriaCutter(httpx_client=client, sleep=lambda _seconds: None, **extra), recorder


STATUS = "https://engine.prod.bria-api.com/v2/status/abc"


def test_bria_cutter_asks_for_the_product_only_and_polls() -> None:
    cutter, recorder = bria(
        httpx.Response(202, json={"request_id": "abc", "status_url": STATUS}),
        httpx.Response(200, json={"status": "IN_PROGRESS", "request_id": "abc"}),
        httpx.Response(
            200, json={"status": "COMPLETED", "result": {"image_url": "https://temp.bria.ai/r.png"}}
        ),
        httpx.Response(200, content=rgba_cutout()),
    )
    big = ImagePart(png(scene_photo(background=(150, 140, 130)).resize((400, 400))), "image/png")
    request = SegmentationRequest(
        image=ImagePart(b"small", "image/jpeg"), full_image=big, width=200, height=200, model="m"
    )
    cut = cutter.cut(request, api_key=Secret("bria-key"))
    assert cut.mask.getbbox() == (60, 40, 140, 180) and cut.calls == 1
    sent, first, second, download = recorder.requests
    assert str(sent.url) == "https://engine.prod.bria-api.com/v2/image/edit/remove_background"
    body = json.loads(sent.content)
    assert body["sync"] is False and body["model"] == "RMBG2" and "not the hand" in body["prompt"]
    assert base64.b64decode(body["image"]) == big.data  # the larger copy goes to the cutter
    assert sent.headers["api_token"] == "bria-key" and str(first.url) == STATUS  # noqa: S105
    assert first.headers["api_token"] == "bria-key"  # noqa: S105 - the status lives on Bria's host
    assert "api_token" not in download.headers  # the key never goes to the result host


def test_bria_cutter_without_a_prompt_answers_at_once() -> None:
    cutter, recorder = bria(
        httpx.Response(200, json={"result": {"image_url": "https://temp.bria.ai/r.png"}, "request_id": "1"}),
        httpx.Response(200, content=rgba_cutout()),
        prompt="",
    )
    cut = cutter.cut(cut_request(), api_key=Secret("bria-key"))
    assert cut.mask.getbbox() == (60, 40, 140, 180)
    body = json.loads(recorder.requests[0].content)
    assert body["sync"] is True and "prompt" not in body


def test_bria_cutter_polling_failures() -> None:
    accepted = httpx.Response(202, json={"request_id": "abc", "status_url": STATUS})
    cutter, _ = bria(accepted, httpx.Response(200, json={"status": "ERROR", "error": {"code": 422}}))
    with pytest.raises(LLMRefused):
        cutter.cut(cut_request(), api_key=Secret("k"))
    cutter, _ = bria(httpx.Response(202, json={"status_url": "https://evil.example/status"}))
    with pytest.raises(LLMBadOutput):
        cutter.cut(cut_request(), api_key=Secret("k"))
    ticks = iter([0.0, 0.0, 100.0, 200.0])
    recorder = Recorder(accepted, httpx.Response(200, json={"status": "IN_PROGRESS"}))
    slow = BriaCutter(
        httpx_client=httpx.Client(transport=httpx.MockTransport(recorder)),
        sleep=lambda _seconds: None,
        clock=lambda: next(ticks),
    )
    with pytest.raises(LLMUnavailable):
        slow.cut(cut_request(), api_key=Secret("k"))


@pytest.mark.parametrize(
    ("status", "error"),
    [
        (401, LLMNotConfigured),
        (403, LLMNotConfigured),
        (422, LLMRefused),
        (429, LLMUnavailable),
        (503, LLMUnavailable),
    ],
)
def test_bria_cutter_maps_errors(status: int, error: type[Exception]) -> None:
    cutter, _ = bria(httpx.Response(status, json={"error": {"code": status}}))
    with pytest.raises(error):
        cutter.cut(cut_request(), api_key=Secret("bria-key"))


def test_bria_cutter_bad_answers() -> None:
    cutter, _ = bria(httpx.Response(200, json={"result": {}}), prompt="")
    with pytest.raises(LLMBadOutput):
        cutter.cut(cut_request(), api_key=Secret("k"))
    opaque = png(Image.new("RGBA", (10, 10), (1, 2, 3, 255)))
    cutter, _ = bria(
        httpx.Response(200, json={"result": {"image_url": "https://x.example/r.png"}}),
        httpx.Response(200, content=opaque),
        prompt="",
    )
    with pytest.raises(LLMBadOutput):
        cutter.cut(cut_request(), api_key=Secret("k"))
    with pytest.raises(LLMNotConfigured):
        BriaCutter().cut(cut_request(), api_key=Secret(""))


def test_bria_key_check() -> None:
    cutter, recorder = bria(httpx.Response(404, json={}))
    cutter.check(api_key=Secret("k"))
    assert "/v2/status/" in str(recorder.requests[0].url)
    cutter, _ = bria(httpx.Response(401, json={}))
    with pytest.raises(LLMNotConfigured):
        cutter.check(api_key=Secret("bad"))


def bria_scene(*responders: Any) -> tuple[BriaSceneModel, Recorder]:
    recorder = Recorder(*responders)
    client = httpx.Client(transport=httpx.MockTransport(recorder))
    return BriaSceneModel(httpx_client=client, sleep=lambda _seconds: None), recorder


def scene_request(**overrides: Any) -> ImageGenerationRequest:
    values: dict[str, Any] = {
        "model": "nb",
        "prompt": "Gemini's layout rules",
        "images": (ImagePart(png(scene_photo()), "image/png"),),
        "cutout": ImagePart(rgba_cutout(), "image/png"),
        "scene": "A polished marble surface, soft daylight.",
        **overrides,
    }
    return ImageGenerationRequest(**values)


def test_bria_scene_builds_the_background_around_our_cut_out() -> None:
    generated = png(scene_photo(background=(10, 20, 30)))
    model, recorder = bria_scene(
        httpx.Response(202, json={"request_id": "abc", "status_url": STATUS}),
        httpx.Response(
            200, json={"status": "COMPLETED", "result": {"image_url": "https://temp.bria.ai/s.png"}}
        ),
        httpx.Response(200, content=generated),
    )
    result = model.generate_image(scene_request(), api_key=Secret("bria-key"))
    assert result.image == generated and result.mime_type == "image/png"
    assert result.billed_calls == 1 and (result.input_tokens, result.output_image_tokens) == (0, 0)
    sent, status, download = recorder.requests
    assert str(sent.url) == "https://engine.prod.bria-api.com/v2/image/edit/replace_background"
    body = json.loads(sent.content)
    assert (body["mode"], body["original_quality"], body["sync"]) == ("fibo2", True, False)
    assert base64.b64decode(body["image"]) == rgba_cutout()  # the cut-out, not the grey canvas
    assert body["prompt"] == "A polished marble surface, soft daylight."  # not Gemini's rules
    assert sent.headers["api_token"] == "bria-key" and str(status.url) == STATUS  # noqa: S105
    assert "api_token" not in download.headers


def test_bria_scene_refusals() -> None:
    model, recorder = bria_scene()
    with pytest.raises(LLMNotConfigured):
        model.generate_image(scene_request(), api_key=Secret(""))
    with pytest.raises(LLMBadOutput):
        model.generate_image(scene_request(cutout=None), api_key=Secret("k"))
    with pytest.raises(LLMBadOutput):
        model.generate_image(scene_request(scene=" "), api_key=Secret("k"))
    assert recorder.requests == []  # nothing was sent
    model, _ = bria_scene(httpx.Response(401, json={"error": {"code": 401}}))
    with pytest.raises(LLMNotConfigured):
        model.generate_image(scene_request(), api_key=Secret("bad"))
    model, _ = bria_scene(
        httpx.Response(200, json={"result": {"image_url": "https://temp.bria.ai/s.png"}}),
        httpx.Response(200, content=b"not an image"),
    )
    with pytest.raises(LLMBadOutput):
        model.generate_image(scene_request(), api_key=Secret("k"))
    checker, recorder = bria_scene(httpx.Response(404, json={}))
    checker.check(api_key=Secret("k"))
    assert "/v2/status/" in str(recorder.requests[0].url) and checker.is_configured()


class ScriptedCutter:
    name = "scripted"

    def __init__(self, mask: Image.Image) -> None:
        self.mask = mask
        self.calls = 0

    def cut(self, request: SegmentationRequest, *, api_key: Secret) -> CutOut:
        self.calls += 1
        return CutOut(mask=self.mask, input_tokens=10, output_tokens=2, output_image_tokens=100)


def test_combined_segmenter_takes_pixels_from_the_cutter() -> None:
    mask = Image.new("L", (200, 400), 0)
    ImageDraw.Draw(mask).ellipse([60, 110, 140, 290], fill=255)
    cutter = ScriptedCutter(mask)
    analyst = GeminiJsonSegmenter(
        ScriptedContent(answer(product={"box_2d": [250, 250, 750, 750]})), require_mask=False
    )
    request = SegmentationRequest(image=ImagePart(b"j", "image/jpeg"), width=200, height=400, model="m")
    result = CombinedSegmenter(analyst, cutter, cutter_key=Secret("c")).segment(request, api_key=KEY)
    assert result.exact and result.product is mask and result.cutter == "scripted"
    assert (result.cutter_input_tokens, result.output_image_tokens) == (10, 100)
    assert result.hand_covers_product and result.covered_percent == 4.5  # the analyst's flags stay


def test_the_analyst_asks_for_boxes_only() -> None:
    """The analyst never asks for masks: a lite model writing base64 masks ran out of tokens live."""
    model = ScriptedContent(answer(product={"box_2d": [250, 250, 750, 750]}))
    request = SegmentationRequest(image=ImagePart(b"j", "image/jpeg"), width=200, height=400, model="m")
    result = GeminiJsonSegmenter(model, require_mask=False).segment(request, api_key=KEY)
    sent = model.requests[0]
    assert "no masks" in sent.prompt and "mask" not in str(sent.response_schema)
    assert sent.max_output_tokens <= 1024 and result.box == (50, 100, 150, 300) and not result.exact


def test_combined_segmenter_refuses_a_cut_out_elsewhere_and_skips_cut_off_photos() -> None:
    elsewhere = Image.new("L", (200, 400), 0)
    ImageDraw.Draw(elsewhere).rectangle([0, 0, 40, 40], fill=255)
    analyst = GeminiJsonSegmenter(
        ScriptedContent(answer(product={"box_2d": [250, 250, 750, 750]})), require_mask=False
    )
    request = SegmentationRequest(image=ImagePart(b"j", "image/jpeg"), width=200, height=400, model="m")
    with pytest.raises(MaskUnusable):
        CombinedSegmenter(analyst, ScriptedCutter(elsewhere), cutter_key=Secret("c")).segment(
            request, api_key=KEY
        )
    cut_off = GeminiJsonSegmenter(ScriptedContent(answer(product_cut_off=True)), require_mask=False)
    cutter = ScriptedCutter(elsewhere)
    result = CombinedSegmenter(cut_off, cutter, cutter_key=Secret("c")).segment(request, api_key=KEY)
    assert result.cut_off and not result.exact and cutter.calls == 0
    assert iou((0, 0, 10, 10), (0, 0, 10, 10)) == 1.0 and iou((0, 0, 1, 1), (5, 5, 6, 6)) == 0.0


def test_a_placeholder_mask_keeps_only_the_box() -> None:
    tiny = "data:image/png;base64," + base64.b64encode(png(Image.new("L", (8, 8), 255))).decode()
    analyst = GeminiJsonSegmenter(
        ScriptedContent(answer(product={"box_2d": [250, 250, 750, 750], "mask": tiny})), require_mask=False
    )
    request = SegmentationRequest(image=ImagePart(b"j", "image/jpeg"), width=200, height=400, model="m")
    result = analyst.segment(request, api_key=KEY)
    assert not result.exact and result.box == (50, 100, 150, 300)
    with pytest.raises(LLMBadOutput):
        GeminiJsonSegmenter(
            ScriptedContent(answer(product={"box_2d": [250, 250, 750, 750], "mask": tiny}))
        ).segment(request, api_key=KEY)


def test_fake_segmenter_keys_the_corner_colour() -> None:
    photo = Image.new("RGB", (300, 400), (200, 200, 200))
    ImageDraw.Draw(photo).rectangle([100, 100, 199, 299], fill=(20, 40, 160))
    request = SegmentationRequest(image=ImagePart(png(photo), "image/png"), width=600, height=800, model="m")
    segmenter = FakeSegmenter()
    result = segmenter.segment(request, api_key=KEY)
    assert result.box == (200, 200, 400, 600) and segmenter.calls == [(600, 800)]
    blank = SegmentationRequest(
        image=ImagePart(png(Image.new("RGB", (50, 50))), "image/png"), width=50, height=50, model="m"
    )
    with pytest.raises(NoProduct):
        segmenter.segment(blank, api_key=KEY)
    segmenter.push(LLMUnavailable("down"))
    with pytest.raises(LLMUnavailable):
        segmenter.segment(request, api_key=KEY)
    segmenter.push(result)
    assert segmenter.segment(request, api_key=KEY) is result
    segmenter.reset()
    assert segmenter.calls == []


def test_segmenter_registry(settings: Any) -> None:
    settings.AI_MODE = "fake"
    assert get_segmenter() is fake_segmenter()
    settings.AI_MODE = "live"
    assert isinstance(get_segmenter("gemini_json"), GeminiJsonSegmenter)
    assert isinstance(get_segmenter("gemini_matte"), CombinedSegmenter)
    assert isinstance(get_segmenter("bria", cutter_key=Secret("b")), CombinedSegmenter)
    with pytest.raises(LLMNotConfigured):
        get_segmenter("bria")
    with pytest.raises(LLMNotConfigured):
        get_segmenter("other")
    settings.AI_MODE = "other"
    with pytest.raises(ImproperlyConfigured):
        get_segmenter()
    settings.AI_MODE = "fake"
    settings.DEV_FEATURES_ALLOWED = False
    settings.SETTINGS_MODULE = "config.settings.production"
    with pytest.raises(LLMNotConfigured):
        get_segmenter()
