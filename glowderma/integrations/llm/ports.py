"""The content-model port of the AI product content assistant.

One structured call per generation: a system instruction (the fixed app rules plus admin style guidance),
one user prompt (task + product data), optional images, and a JSON Schema the answer must follow. The
adapter returns the parsed JSON object and the token usage; the caller validates the content.

Outcomes the assistant tells apart (each error carries a stable ``code`` for ``GenerationJob.error_code``;
its message is ours, never the provider's text, and never contains the key):

* :class:`LLMUnavailable` (``provider_unavailable``): timeout, transport failure, 429 or 5xx after the
  adapter's bounded retries — transient, the admin may try again;
* :class:`LLMRefused` with ``code`` ``ai_not_configured`` (key rejected, permission denied, unknown model,
  or a fake adapter under the production settings) or ``ai_refused`` (the request was refused: safety block,
  invalid argument) — never retried;
* :class:`LLMBadOutput` (``ai_bad_output``): the answer is not a JSON object (truncated, not JSON, empty).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from glowderma.integrations.secret import Secret

CODE_UNAVAILABLE = "provider_unavailable"
CODE_NOT_CONFIGURED = "ai_not_configured"
CODE_REFUSED = "ai_refused"
CODE_BAD_OUTPUT = "ai_bad_output"


@dataclass(frozen=True, slots=True)
class ImagePart:
    data: bytes = field(repr=False)
    mime_type: str  # "image/webp" | "image/jpeg" | "image/png"


@dataclass(frozen=True, slots=True)
class GenerationRequest:
    model: str  # provider model id, e.g. "gemini-2.5-flash-lite"
    system_instruction: str
    prompt: str
    response_schema: dict[str, Any]  # JSON Schema of the answer object
    images: tuple[ImagePart, ...] = ()
    temperature: float = 0.4
    max_output_tokens: int = 4096
    timeout_seconds: int = 20  # per HTTP attempt
    max_retries: int = 2  # extra attempts on 429 / 5xx / transport errors (0 = one attempt)


@dataclass(frozen=True, slots=True)
class GenerationResult:
    data: dict[str, Any]
    input_tokens: int
    output_tokens: int  # thinking tokens included (billed as output)
    model: str  # the model version that answered (falls back to the requested id)


class LLMError(Exception):
    """``input_tokens`` / ``output_tokens``: what the provider billed for an answer it did send but we cannot
    use (refused, truncated, not JSON), so the job still records the real cost; 0 when no answer arrived."""

    code = CODE_UNAVAILABLE

    def __init__(
        self, detail: str = "", *, code: str | None = None, input_tokens: int = 0, output_tokens: int = 0
    ) -> None:
        super().__init__(detail or self.__class__.__name__)
        if code is not None:
            self.code = code
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens


class LLMUnavailable(LLMError):
    code = CODE_UNAVAILABLE


class LLMRefused(LLMError):
    code = CODE_REFUSED


class LLMNotConfigured(LLMRefused):
    code = CODE_NOT_CONFIGURED


class LLMBadOutput(LLMError):
    code = CODE_BAD_OUTPUT


class ContentModel(Protocol):
    name: str

    def generate(self, request: GenerationRequest, *, api_key: Secret) -> GenerationResult:
        """One structured generation; raises an :class:`LLMError` subclass."""
        ...

    def check(self, *, api_key: Secret, model: str, timeout_seconds: int = 10) -> None:
        """A cheap credential + model check (no tokens generated); raises an :class:`LLMError` subclass."""
        ...

    def is_configured(self) -> bool: ...
