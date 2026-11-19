"""Google Gemini through the official ``google-genai`` SDK (``AI_MODE=live``, provider ``gemini``).

* The client is built per call with the **explicit** key and ``vertexai=False``, so neither
  ``GOOGLE_API_KEY`` / ``GEMINI_API_KEY`` nor the Vertex switches of the environment are ever read; the SDK
  sends the key in the ``x-goog-api-key`` header (never in a URL). The key arrives as a
  :class:`~glowderma.integrations.secret.Secret` and is revealed only into the client constructor.
* ``HttpOptions(timeout=…)`` is in **milliseconds** (per attempt); ``HttpRetryOptions`` retries 408/429/5xx
  and transient transport errors with exponential backoff capped at 8 s, ``attempts = 1 + max_retries``. A
  bad key, a refusal or a bad answer is never retried here.
* Structured output: ``response_mime_type="application/json"`` + ``response_json_schema``; the text is
  parsed with :func:`json.loads` and must be an object.
* Thinking: ``gemini-2.5-*`` (not ``pro``) runs with ``thinking_budget=0``; ``gemini-3*`` with
  ``thinking_level=MINIMAL``; other ids keep the model default. Output tokens include
  ``thoughts_token_count``.
* Errors become :mod:`.ports` errors with our own short detail (status code and status name only): the SDK's
  message and response body never leave this module, and the SDK exception is not chained.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx
from google import genai
from google.genai import errors, types

from glowderma.integrations.secret import Secret

from .ports import (
    GenerationRequest,
    GenerationResult,
    LLMBadOutput,
    LLMError,
    LLMNotConfigured,
    LLMRefused,
    LLMUnavailable,
)

logger = logging.getLogger(__name__)

RETRY_STATUS_CODES = [408, 429, 500, 502, 503, 504]
RETRY_MAX_DELAY_SECONDS = 8.0
RETRY_INITIAL_DELAY_SECONDS = 1.0
RETRY_JITTER_SECONDS = 1.0
NOT_CONFIGURED_STATUSES = frozenset({401, 403, 404})
REFUSED_FINISH_REASONS = frozenset(
    {"SAFETY", "RECITATION", "BLOCKLIST", "PROHIBITED_CONTENT", "SPII", "IMAGE_SAFETY", "LANGUAGE", "OTHER"}
)


def thinking_config(model: str) -> types.ThinkingConfig | None:
    """The cheapest thinking setting per model family (``None`` = the model default)."""
    if model.startswith("gemini-2.5") and "pro" not in model:
        return types.ThinkingConfig(thinking_budget=0)
    if model.startswith("gemini-3"):
        return types.ThinkingConfig(thinking_level=types.ThinkingLevel.MINIMAL)
    return None


def _key_invalid(exc: errors.APIError) -> bool:
    """400 ``API_KEY_INVALID`` (the Gemini API's answer to a wrong key) — read from the error details only."""
    text = json.dumps(exc.details, default=str) if exc.details is not None else ""
    return "API_KEY_INVALID" in text or "API key not valid" in text


def map_api_error(exc: errors.APIError) -> LLMError:
    code = int(exc.code or 0)
    detail = f"gemini {code} {exc.status or ''}".strip()
    if code in NOT_CONFIGURED_STATUSES or (code == 400 and _key_invalid(exc)):
        return LLMNotConfigured(detail)
    if code in (408, 429) or code >= 500:
        return LLMUnavailable(detail)
    return LLMRefused(detail)


def _count(value: Any) -> int:
    return int(value) if isinstance(value, int) and value > 0 else 0


class GeminiContentModel:
    name = "gemini"

    def __init__(self, *, httpx_client: httpx.Client | None = None) -> None:
        # ``httpx_client``: tests pass a client over ``httpx.MockTransport``; production lets the SDK build
        # one.
        self._httpx_client = httpx_client

    def is_configured(self) -> bool:
        # The key is not a setting: it is read from Vault per job (``assistant.credentials``).
        return True

    def _client(self, api_key: Secret, *, timeout_seconds: int, max_retries: int) -> genai.Client:
        options = types.HttpOptions(
            timeout=int(timeout_seconds * 1000),
            retry_options=types.HttpRetryOptions(
                attempts=1 + max(0, max_retries),
                http_status_codes=RETRY_STATUS_CODES,
                initial_delay=RETRY_INITIAL_DELAY_SECONDS,
                jitter=RETRY_JITTER_SECONDS,
                max_delay=RETRY_MAX_DELAY_SECONDS,
            ),
            httpx_client=self._httpx_client,
        )
        return genai.Client(api_key=api_key.reveal(), vertexai=False, http_options=options)

    def _call(self, call: Any) -> Any:
        try:
            return call()
        except errors.APIError as exc:
            mapped = map_api_error(exc)
        except (httpx.HTTPError, OSError) as exc:
            mapped = LLMUnavailable(f"gemini transport {type(exc).__name__}")
        except Exception as exc:  # noqa: BLE001 - e.g. httpx2 errors, SDK parsing failures: never the message
            if type(exc).__name__.endswith(("Timeout", "TimeoutException", "ConnectError", "TransportError")):
                mapped = LLMUnavailable(f"gemini transport {type(exc).__name__}")
            else:
                mapped = LLMUnavailable(f"gemini failure {type(exc).__name__}")
        logger.warning("llm.gemini_failed code=%s detail=%s", mapped.code, mapped)
        raise mapped from None

    def generate(self, request: GenerationRequest, *, api_key: Secret) -> GenerationResult:
        config = types.GenerateContentConfig(
            system_instruction=request.system_instruction,
            temperature=request.temperature,
            max_output_tokens=request.max_output_tokens,
            response_mime_type="application/json",
            response_json_schema=request.response_schema,
            thinking_config=thinking_config(request.model),
            candidate_count=1,
        )
        contents: list[Any] = [
            types.Part.from_bytes(data=image.data, mime_type=image.mime_type) for image in request.images
        ]
        contents.append(request.prompt)
        with self._client(
            api_key, timeout_seconds=request.timeout_seconds, max_retries=request.max_retries
        ) as client:
            response = self._call(
                lambda: client.models.generate_content(model=request.model, contents=contents, config=config)
            )
        return self._result(request, response)

    def _result(self, request: GenerationRequest, response: Any) -> GenerationResult:
        usage = getattr(response, "usage_metadata", None)
        input_tokens = _count(getattr(usage, "prompt_token_count", 0))
        output_tokens = _count(getattr(usage, "candidates_token_count", 0)) + _count(
            getattr(usage, "thoughts_token_count", 0)
        )
        billed: dict[str, Any] = {"input_tokens": input_tokens, "output_tokens": output_tokens}
        feedback = getattr(response, "prompt_feedback", None)
        if feedback is not None and getattr(feedback, "block_reason", None):
            raise LLMRefused(f"gemini prompt blocked {feedback.block_reason}", **billed)
        candidates = list(getattr(response, "candidates", None) or [])
        finish = str(getattr(candidates[0], "finish_reason", "") or "") if candidates else ""
        finish = finish.rsplit(".", 1)[-1]  # an enum member renders as "FinishReason.STOP" in some versions
        if finish in REFUSED_FINISH_REASONS:
            raise LLMRefused(f"gemini finish {finish}", **billed)
        try:
            text = response.text
        except Exception:  # noqa: BLE001 - a malformed candidate list
            text = None
        if not text:
            raise LLMBadOutput(f"gemini empty answer (finish {finish or 'none'})", **billed)
        try:
            data = json.loads(text)
        except ValueError:
            raise LLMBadOutput(f"gemini answer is not JSON (finish {finish or 'none'})", **billed) from None
        if not isinstance(data, dict):
            raise LLMBadOutput("gemini answer is not a JSON object", **billed)
        return GenerationResult(
            data=data,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            model=str(getattr(response, "model_version", None) or request.model),
        )

    def check(self, *, api_key: Secret, model: str, timeout_seconds: int = 10) -> None:
        """``models.get``: proves the key works and the model id exists, generating nothing."""
        if not api_key:
            raise LLMNotConfigured("gemini: empty key")
        with self._client(api_key, timeout_seconds=timeout_seconds, max_retries=1) as client:
            self._call(lambda: client.models.get(model=model))
