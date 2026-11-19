"""The content-model port: the schema-shaped fake, the registry, and the Gemini adapter over the real
``google-genai`` SDK with an ``httpx.MockTransport`` (no network)."""

from __future__ import annotations

import json
import logging
import pickle
from collections.abc import Callable, Iterator
from typing import Any

import httpx
import pytest
from django.core.exceptions import ImproperlyConfigured
from google import genai
from google.genai import errors, types

from glowderma.integrations.llm import gemini
from glowderma.integrations.llm.fake import FakeContentModel, build
from glowderma.integrations.llm.gemini import GeminiContentModel, map_api_error, thinking_config
from glowderma.integrations.llm.ports import (
    GenerationRequest,
    GenerationResult,
    ImagePart,
    LLMBadOutput,
    LLMNotConfigured,
    LLMRefused,
    LLMUnavailable,
)
from glowderma.integrations.llm.registry import fake_content_model, get_content_model
from glowderma.integrations.secret import Secret

KEY = Secret("AIza-test-key-0123456789")
SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "suggestions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "key": {"type": "string", "enum": ["texts.ru.description", "seo.uz.title"]},
                    "value": {"type": "string", "maxLength": 20},
                },
                "required": ["key", "value"],
            },
        },
        "notes": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["suggestions"],
}


def request(**overrides: Any) -> GenerationRequest:
    values: dict[str, Any] = {
        "model": "gemini-2.5-flash-lite",
        "system_instruction": "rules",
        "prompt": "task <product_data>{}</product_data>",
        "response_schema": SCHEMA,
        **overrides,
    }
    return GenerationRequest(**values)


# --- Secret ---------------------------------------------------------------------------------------------


def test_secret_never_renders_its_value() -> None:
    secret = Secret("sk-very-secret")
    assert secret.reveal() == "sk-very-secret"
    for rendered in (repr(secret), str(secret), f"{secret}", f"{secret!r}", repr([secret])):
        assert "sk-very-secret" not in rendered
    with pytest.raises(TypeError):
        pickle.dumps(secret)
    with pytest.raises(TypeError):
        json.dumps({"key": secret})
    assert Secret("a") == Secret("a") and Secret("a") != Secret("b") and not Secret("")


# --- fake -----------------------------------------------------------------------------------------------


@pytest.fixture
def fake() -> Iterator[FakeContentModel]:
    model = fake_content_model()
    model.reset()
    yield model
    model.reset()


def test_fake_answers_one_item_per_enum_key(fake: FakeContentModel) -> None:
    result = fake.generate(request(), api_key=KEY)
    assert result.data == {
        "suggestions": [
            {"key": "texts.ru.description", "value": "[fake] texts.ru.desc"},
            {"key": "seo.uz.title", "value": "[fake] seo.uz.title"},
        ],
        "notes": ["[fake] notes"],
    }
    assert result.input_tokens > 0 and result.output_tokens > 0
    assert result.model == "gemini-2.5-flash-lite"
    assert fake.calls == [request()]


def test_fake_build_covers_scalars_unions_and_depth() -> None:
    schema = {
        "type": "object",
        "properties": {
            "n": {"type": "integer", "minimum": 3},
            "x": {"type": "number"},
            "b": {"type": "boolean"},
            "e": {"enum": ["a", "b"]},
            "u": {"anyOf": [{"type": "string"}, {"type": "null"}]},
            "t": {"type": ["null", "string"]},
            "o": {"type": "array", "items": {"type": "object", "properties": {"v": {"type": "string"}}}},
            "z": {},
        },
    }
    assert build(schema) == {
        "n": 3,
        "x": 0,
        "b": False,
        "e": "a",
        "u": "[fake] u",
        "t": "[fake] t",
        "o": [{"v": "[fake] o.v"}],
        "z": None,
    }
    assert build("not a schema") is None


@pytest.mark.parametrize(
    ("marker", "error"),
    [
        ("[fake-unavailable]", LLMUnavailable),
        ("[fake-refused]", LLMRefused),
        ("[fake-malformed]", LLMBadOutput),
    ],
)
def test_fake_markers_script_failures(fake: FakeContentModel, marker: str, error: type[Exception]) -> None:
    with pytest.raises(error):
        fake.generate(request(prompt=f"name {marker}"), api_key=KEY)


def test_fake_queue_and_key_and_check(fake: FakeContentModel) -> None:
    queued = GenerationResult(data={"x": 1}, input_tokens=1, output_tokens=2, model="m")
    fake.push(queued)
    fake.push(LLMUnavailable("down"))
    assert fake.generate(request(), api_key=KEY) is queued
    with pytest.raises(LLMUnavailable):
        fake.generate(request(), api_key=KEY)
    with pytest.raises(LLMNotConfigured) as caught:
        fake.generate(request(), api_key=Secret("invalid-key"))
    assert caught.value.code == "ai_not_configured"
    fake.check(api_key=KEY, model="gemini-2.5-flash-lite")
    for key, model in ((Secret("invalid"), "gemini-2.5-flash-lite"), (Secret(""), "m"), (KEY, "unknown-x")):
        with pytest.raises(LLMNotConfigured):
            fake.check(api_key=key, model=model)
    assert fake.checks == ["gemini-2.5-flash-lite", "gemini-2.5-flash-lite", "m", "unknown-x"]
    with pytest.raises(LLMBadOutput):
        fake.generate(request(response_schema={"type": "string"}), api_key=KEY)


def test_fake_keeps_no_calls_outside_dev(fake: FakeContentModel, settings: Any) -> None:
    settings.DEV_FEATURES_ALLOWED = False
    fake.generate(request(), api_key=KEY)
    fake.check(api_key=KEY, model="m")
    assert fake.calls == [] and fake.checks == []


# --- registry -------------------------------------------------------------------------------------------


def test_registry_modes(settings: Any) -> None:
    settings.AI_MODE = "fake"
    assert get_content_model() is fake_content_model()
    settings.AI_MODE = "live"
    assert isinstance(get_content_model("gemini"), GeminiContentModel)
    with pytest.raises(LLMNotConfigured):
        get_content_model("openai")
    settings.AI_MODE = "other"
    with pytest.raises(ImproperlyConfigured):
        get_content_model()


def test_registry_refuses_the_fake_under_production_settings(settings: Any) -> None:
    settings.AI_MODE = "fake"
    settings.DEV_FEATURES_ALLOWED = False
    with pytest.raises(LLMNotConfigured) as caught:
        get_content_model()
    assert caught.value.code == "ai_not_configured"


# --- Gemini ---------------------------------------------------------------------------------------------

BASE = "https://generativelanguage.googleapis.com"
GENERATE_PATH = "models/gemini-2.5-flash-lite:generateContent"


def answer(text: str | None, *, finish: str = "STOP", usage: dict[str, int] | None = None) -> dict[str, Any]:
    body: dict[str, Any] = {
        "candidates": [{"content": {"role": "model", "parts": [{"text": text}]}, "finishReason": finish}],
        "usageMetadata": usage
        if usage is not None
        else {"promptTokenCount": 120, "candidatesTokenCount": 40, "thoughtsTokenCount": 5},
        "modelVersion": "gemini-2.5-flash-lite-001",
    }
    if text is None:
        body["candidates"][0]["content"]["parts"] = []
    return body


def api_error(code: int, status: str, reason: str = "", message: str = "boom") -> httpx.Response:
    details = [{"@type": "type.googleapis.com/google.rpc.ErrorInfo", "reason": reason}] if reason else []
    return httpx.Response(
        code, json={"error": {"code": code, "message": message, "status": status, "details": details}}
    )


class Recorder:
    def __init__(self, *responders: httpx.Response | Callable[[httpx.Request], httpx.Response]) -> None:
        self.responders = list(responders)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        responder = self.responders[min(len(self.requests), len(self.responders)) - 1]
        if callable(responder):
            return responder(request)
        return httpx.Response(responder.status_code, headers=responder.headers, content=responder.content)


@pytest.fixture(autouse=True)
def fast_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gemini, "RETRY_INITIAL_DELAY_SECONDS", 0.01)
    monkeypatch.setattr(gemini, "RETRY_JITTER_SECONDS", 0.0)
    monkeypatch.setattr(gemini, "RETRY_MAX_DELAY_SECONDS", 0.02)


def adapter(recorder: Recorder) -> GeminiContentModel:
    return GeminiContentModel(httpx_client=httpx.Client(transport=httpx.MockTransport(recorder)))


def test_gemini_generate_sends_structured_request_and_reads_usage(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GOOGLE_API_KEY", "env-key-must-not-be-used")
    monkeypatch.setenv("GOOGLE_GENAI_USE_VERTEXAI", "true")
    recorder = Recorder(httpx.Response(200, json=answer('{"suggestions": [], "notes": []}')))
    image = ImagePart(data=b"\x89PNG-bytes", mime_type="image/png")
    result = adapter(recorder).generate(
        request(images=(image,), temperature=0.3, max_output_tokens=900), api_key=KEY
    )
    assert result == GenerationResult(
        data={"suggestions": [], "notes": []},
        input_tokens=120,
        output_tokens=45,
        model="gemini-2.5-flash-lite-001",
    )
    [sent] = recorder.requests
    assert str(sent.url).startswith(BASE) and sent.url.path.endswith(GENERATE_PATH)
    assert sent.headers["x-goog-api-key"] == KEY.reveal()
    assert KEY.reveal() not in str(sent.url)
    body = json.loads(sent.content)
    config = body["generationConfig"]
    assert config["responseMimeType"] == "application/json"
    assert config["responseJsonSchema"] == SCHEMA
    assert config["temperature"] == 0.3 and config["maxOutputTokens"] == 900
    # the SDK serialises this one message in snake case; the API reads both spellings
    assert config["thinkingConfig"] in ({"thinkingBudget": 0}, {"thinking_budget": 0})
    assert body["systemInstruction"]["parts"][0]["text"] == "rules"
    parts = body["contents"][0]["parts"]
    inline = parts[0].get("inlineData") or parts[0]["inline_data"]
    assert (inline.get("mimeType") or inline["mime_type"]) == "image/png"
    assert parts[1]["text"].startswith("task")


def test_gemini_retries_transient_statuses_then_succeeds() -> None:
    recorder = Recorder(api_error(503, "UNAVAILABLE"), api_error(429, "RESOURCE_EXHAUSTED"),
                        httpx.Response(200, json=answer('{"a": 1}')))  # fmt: skip
    result = adapter(recorder).generate(request(max_retries=2), api_key=KEY)
    assert result.data == {"a": 1} and len(recorder.requests) == 3


def test_gemini_gives_up_after_bounded_retries(caplog: pytest.LogCaptureFixture) -> None:
    recorder = Recorder(api_error(500, "INTERNAL", message=f"echo {KEY.reveal()}"))
    with caplog.at_level(logging.DEBUG), pytest.raises(LLMUnavailable) as caught:
        adapter(recorder).generate(request(max_retries=1), api_key=KEY)
    assert len(recorder.requests) == 2
    assert caught.value.code == "provider_unavailable"
    assert caught.value.__cause__ is None and caught.value.__suppress_context__
    assert KEY.reveal() not in str(caught.value)
    assert KEY.reveal() not in caplog.text


@pytest.mark.parametrize("level", ["DEBUG", "INFO", "WARNING"])
def test_the_sdk_retry_lines_stay_out_of_the_logs_at_every_log_level(level: str) -> None:
    # google-genai logs each retry at INFO quoting the provider's error body (which may echo the request); the
    # LOGGING config keeps its loggers at WARNING whatever LOG_LEVEL says (it only sets glowderma.*).
    from config.settings.base import logging_config

    loggers = logging_config(level)["loggers"]
    assert {loggers[name]["level"] for name in ("google_genai", "httpx2", "httpcore2")} == {"WARNING"}
    assert not logging.getLogger("google_genai._api_client").isEnabledFor(logging.INFO)


@pytest.mark.parametrize(
    ("response", "error", "code"),
    [
        (api_error(400, "INVALID_ARGUMENT", "API_KEY_INVALID", "API key not valid."), LLMNotConfigured,
         "ai_not_configured"),
        (api_error(403, "PERMISSION_DENIED"), LLMNotConfigured, "ai_not_configured"),
        (api_error(404, "NOT_FOUND"), LLMNotConfigured, "ai_not_configured"),
        (api_error(400, "INVALID_ARGUMENT"), LLMRefused, "ai_refused"),
    ],
)  # fmt: skip
def test_gemini_refusals_are_not_retried(response: httpx.Response, error: type[Exception], code: str) -> None:
    recorder = Recorder(response)
    with pytest.raises(error) as caught:
        adapter(recorder).generate(request(), api_key=KEY)
    assert len(recorder.requests) == 1
    assert getattr(caught.value, "code", None) == code


def test_gemini_transport_failure_is_unavailable() -> None:
    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    with pytest.raises(LLMUnavailable):
        adapter(Recorder(down)).generate(request(max_retries=0), api_key=KEY)


@pytest.mark.parametrize(
    ("body", "error"),
    [
        (answer("not json"), LLMBadOutput),
        (answer('["a list"]'), LLMBadOutput),
        (answer(None), LLMBadOutput),
        (answer('{"a"', finish="MAX_TOKENS"), LLMBadOutput),
        (answer("{}", finish="SAFETY"), LLMRefused),
        ({"promptFeedback": {"blockReason": "SAFETY"}}, LLMRefused),
    ],
)
def test_gemini_bad_answers(body: dict[str, Any], error: type[Exception]) -> None:
    with pytest.raises(error):
        adapter(Recorder(httpx.Response(200, json=body))).generate(request(), api_key=KEY)


@pytest.mark.parametrize(
    "body",
    [answer('{"a"', finish="MAX_TOKENS"), answer("not json"), answer("{}", finish="SAFETY")],
)
def test_gemini_unusable_answers_carry_the_billed_tokens(body: dict[str, Any]) -> None:
    with pytest.raises((LLMBadOutput, LLMRefused)) as caught:
        adapter(Recorder(httpx.Response(200, json=body))).generate(request(), api_key=KEY)
    assert (caught.value.input_tokens, caught.value.output_tokens) == (120, 45)


def test_gemini_missing_usage_counts_zero() -> None:
    body = answer('{"a": 1}', usage={})
    result = adapter(Recorder(httpx.Response(200, json=body))).generate(request(), api_key=KEY)
    assert (result.input_tokens, result.output_tokens) == (0, 0)


def test_gemini_check_reads_the_model() -> None:
    recorder = Recorder(httpx.Response(200, json={"name": "models/gemini-2.5-flash-lite"}))
    adapter(recorder).check(api_key=KEY, model="gemini-2.5-flash-lite")
    [sent] = recorder.requests
    assert sent.method == "GET" and sent.url.path.endswith("/models/gemini-2.5-flash-lite")
    with pytest.raises(LLMNotConfigured):
        adapter(Recorder(api_error(404, "NOT_FOUND"))).check(api_key=KEY, model="nope")
    with pytest.raises(LLMNotConfigured):
        adapter(Recorder()).check(api_key=Secret(""), model="m")
    assert GeminiContentModel().is_configured()


def test_gemini_thinking_per_family() -> None:
    assert thinking_config("gemini-2.5-flash-lite") == types.ThinkingConfig(thinking_budget=0)
    assert thinking_config("gemini-2.5-pro") is None
    assert thinking_config("gemini-3.1-flash-lite") == types.ThinkingConfig(
        thinking_level=types.ThinkingLevel.MINIMAL
    )
    assert thinking_config("other-model") is None


def test_map_api_error_without_details() -> None:
    error = errors.APIError(502, {"error": {"code": 502, "status": "BAD_GATEWAY"}})
    assert isinstance(map_api_error(error), LLMUnavailable)
    assert isinstance(map_api_error(errors.APIError(409, {})), LLMRefused)


def test_sdk_client_uses_the_explicit_key_not_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GOOGLE_API_KEY", "env-key")
    monkeypatch.setenv("GEMINI_API_KEY", "env-key-2")
    client = genai.Client(api_key="explicit", vertexai=False)
    assert client._api_client.api_key == "explicit"
    assert client._api_client.vertexai is False
