"""Gemini image generation (Nano Banana) through ``google-genai`` (``AI_MODE=live``, provider ``gemini``).

The transport, retries and error mapping are :class:`~.gemini.GeminiContentModel`'s. The request asks for
``response_modalities=["IMAGE"]`` with an ``ImageConfig`` (aspect ratio, size) and the cheapest thinking
level; the answer's first non-thought ``inline_data`` image part is returned. Hard rules go in the prompt
text: a system instruction is not documented for image models.

Usage: ``candidates_tokens_details`` splits the output by modality (``IMAGE`` is billed at the image rate,
the rest, thinking included, at the text rate); when the split is missing every output token counts as
image output (the dearer rate, so a budget is never under-charged).
"""

from __future__ import annotations

from typing import Any

from google.genai import types

from glowderma.integrations.secret import Secret

from .gemini import REFUSED_FINISH_REASONS, GeminiContentModel, _count
from .ports import ImageGenerationRequest, ImageGenerationResult, LLMBadOutput, LLMNotConfigured, LLMRefused

THINKING_LEVELS = {
    "minimal": types.ThinkingLevel.MINIMAL,
    "low": types.ThinkingLevel.LOW,
    "medium": types.ThinkingLevel.MEDIUM,
    "high": types.ThinkingLevel.HIGH,
}


def _split_output(usage: Any) -> tuple[int, int]:
    """``(text_and_thinking, image)`` output tokens of a response's usage metadata."""
    total = _count(getattr(usage, "candidates_token_count", 0))
    thoughts = _count(getattr(usage, "thoughts_token_count", 0))
    details = list(getattr(usage, "candidates_tokens_details", None) or [])
    if not details:
        return thoughts, total
    image = 0
    for detail in details:
        modality = str(getattr(detail, "modality", "") or "").rsplit(".", 1)[-1]
        if modality == "IMAGE":
            image += _count(getattr(detail, "token_count", 0))
    return thoughts + max(0, total - image), image


class GeminiImageModel(GeminiContentModel):
    name = "gemini"

    def generate_image(self, request: ImageGenerationRequest, *, api_key: Secret) -> ImageGenerationResult:
        if not api_key:
            raise LLMNotConfigured("gemini: empty key")
        level = THINKING_LEVELS.get(request.thinking_level)
        config = types.GenerateContentConfig(
            response_modalities=["IMAGE"],
            image_config=types.ImageConfig(aspect_ratio=request.aspect_ratio, image_size=request.image_size),
            thinking_config=types.ThinkingConfig(thinking_level=level) if level is not None else None,
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
        return self._image_result(request, response)

    def _image_result(self, request: ImageGenerationRequest, response: Any) -> ImageGenerationResult:
        usage = getattr(response, "usage_metadata", None)
        input_tokens = _count(getattr(usage, "prompt_token_count", 0))
        text_tokens, image_tokens = _split_output(usage)
        billed: dict[str, Any] = {"input_tokens": input_tokens, "output_tokens": text_tokens + image_tokens}
        feedback = getattr(response, "prompt_feedback", None)
        if feedback is not None and getattr(feedback, "block_reason", None):
            raise LLMRefused(f"gemini prompt blocked {feedback.block_reason}", **billed)
        candidates = list(getattr(response, "candidates", None) or [])
        finish = str(getattr(candidates[0], "finish_reason", "") or "") if candidates else ""
        finish = finish.rsplit(".", 1)[-1]
        if finish in REFUSED_FINISH_REASONS:
            raise LLMRefused(f"gemini finish {finish}", **billed)
        content = getattr(candidates[0], "content", None) if candidates else None
        for part in list(getattr(content, "parts", None) or []):
            blob = getattr(part, "inline_data", None)
            if getattr(part, "thought", False) or blob is None or not getattr(blob, "data", None):
                continue
            mime = str(getattr(blob, "mime_type", "") or "")
            if not mime.startswith("image/"):
                continue
            return ImageGenerationResult(
                image=bytes(blob.data),
                mime_type=mime,
                input_tokens=input_tokens,
                output_text_tokens=text_tokens,
                output_image_tokens=image_tokens,
                model=str(getattr(response, "model_version", None) or request.model),
            )
        raise LLMBadOutput(f"gemini answered without an image (finish {finish or 'none'})", **billed)
