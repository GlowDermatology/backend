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
from glowderma.integrations.segmentation.fake import FakeSegmenter
from glowderma.integrations.segmentation.gemini_json import GeminiJsonSegmenter
from glowderma.integrations.segmentation.gemini_matte import GeminiMatteSegmenter, _closest_ratio, key_green
from glowderma.integrations.segmentation.ports import NoProduct, SegmentationRequest
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
        "hands": [{"box_2d": [500, 0, 600, 1000], "mask": mask_png((32, 8))}],
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
    raw = base64.b64encode(png(Image.new("L", (4, 4), 255))).decode()
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


def test_matte_segmenter_uses_the_image_model(fake_image: FakeImageModel) -> None:
    green = Image.new("RGB", (100, 100), (0, 255, 0))
    ImageDraw.Draw(green).rectangle([30, 20, 69, 89], fill=(240, 240, 240))
    fake_image.push(ImageGenerationResult(png(green), "image/png", 500, 10, 1120, "nb"))
    request = SegmentationRequest(image=ImagePart(b"x", "image/jpeg"), width=100, height=100, model="nb")
    result = GeminiMatteSegmenter(fake_image).segment(request, api_key=KEY)
    assert result.box[0] >= 28 and result.output_image_tokens == 1120 and result.hands is None
    assert fake_image.calls[-1].aspect_ratio == "1:1" and "#00FF00" in fake_image.calls[-1].prompt
    fake_image.push(
        ImageGenerationResult(png(Image.new("RGB", (50, 50), (0, 255, 0))), "image/png", 1, 1, 1, "nb")
    )
    with pytest.raises(NoProduct):
        GeminiMatteSegmenter(fake_image).segment(request, api_key=KEY)
    fake_image.push(ImageGenerationResult(b"not an image", "image/png", 1, 1, 1, "nb"))
    with pytest.raises(LLMBadOutput):
        GeminiMatteSegmenter(fake_image).segment(request, api_key=KEY)


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
    assert isinstance(get_segmenter(), GeminiJsonSegmenter)
    assert isinstance(get_segmenter("gemini_matte"), GeminiMatteSegmenter)
    with pytest.raises(LLMNotConfigured):
        get_segmenter("bria")
    settings.AI_MODE = "other"
    with pytest.raises(ImproperlyConfigured):
        get_segmenter()
    settings.AI_MODE = "fake"
    settings.DEV_FEATURES_ALLOWED = False
    settings.SETTINGS_MODULE = "config.settings.production"
    with pytest.raises(LLMNotConfigured):
        get_segmenter()


def test_the_raw_answer_is_kept_without_mask_payloads() -> None:
    result, _ = segment(answer())
    assert result.raw["product"]["mask"].startswith("<") and result.raw["background"] == "office"
    assert result.raw["hands"][0]["mask"].endswith("chars>")
