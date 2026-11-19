"""Deterministic content model (``AI_MODE=fake``): no network, schema-shaped answers, scripted failures.

The answer is built from ``request.response_schema`` alone, so it follows whatever schema the assistant
sends: an object gets every property (required or not), a string ``enum`` its first value, a free string the
text ``"[fake] <path>"`` (``<path>`` = the dotted property path, cut to ``maxLength``), a number its
``minimum`` (else 0), a boolean ``false``. An array whose item object has a property with a string ``enum``
gets **one item per enum value** (so ``{suggestions: [{key: enum[...], value}]}`` answers every requested
key, the free strings naming the key); any other array gets one item.

Markers in ``request.prompt`` script an outcome: ``[fake-unavailable]`` → :class:`LLMUnavailable`,
``[fake-refused]`` → :class:`LLMRefused`, ``[fake-malformed]`` → :class:`LLMBadOutput`. :meth:`push` queues
a ``GenerationResult`` (or an exception) that the next :meth:`generate` returns (raises) instead — for
tests. ``check`` refuses a key starting with ``invalid`` (``ai_not_configured``) and a model id starting
with ``unknown``.

``calls`` keeps every request (never the key) only while ``settings.DEV_FEATURES_ALLOWED``; the registry
never hands this adapter out under the production settings (``ai_not_configured``).
"""

from __future__ import annotations

import json
from collections import deque
from typing import Any

from glowderma.core import integration_modes
from glowderma.integrations.secret import Secret

from .ports import (
    GenerationRequest,
    GenerationResult,
    LLMBadOutput,
    LLMNotConfigured,
    LLMRefused,
    LLMUnavailable,
)

UNAVAILABLE_MARKER = "[fake-unavailable]"
REFUSED_MARKER = "[fake-refused]"
MALFORMED_MARKER = "[fake-malformed]"
INVALID_KEY_PREFIX = "invalid"
UNKNOWN_MODEL_PREFIX = "unknown"
MAX_DEPTH = 8


def _enum_property(item_schema: dict[str, Any]) -> tuple[str, list[Any]] | None:
    for name, prop in (item_schema.get("properties") or {}).items():
        values = prop.get("enum") if isinstance(prop, dict) else None
        if isinstance(values, list) and values and all(isinstance(v, str) for v in values):
            return name, values
    return None


def _text(path: str, schema: dict[str, Any]) -> str:
    text = f"[fake] {path}".strip()
    limit = schema.get("maxLength")
    return text[: int(limit)] if isinstance(limit, int) and limit > 0 else text


def build(schema: Any, path: str = "", depth: int = 0) -> Any:
    """A value that follows ``schema`` (the subset structured-output schemas use)."""
    if not isinstance(schema, dict) or depth > MAX_DEPTH:
        return None
    if "enum" in schema and schema["enum"]:
        return schema["enum"][0]
    for union in ("anyOf", "oneOf"):
        if schema.get(union):
            return build(schema[union][0], path, depth + 1)
    kind = schema.get("type")
    if isinstance(kind, list):
        kind = next((k for k in kind if k != "null"), None)
    if kind == "object" or "properties" in schema:
        return {
            name: build(prop, f"{path}.{name}" if path else name, depth + 1)
            for name, prop in (schema.get("properties") or {}).items()
        }
    if kind == "array":
        items = schema.get("items") or {}
        keyed = _enum_property(items) if isinstance(items, dict) else None
        if keyed is None:
            return [build(items, path, depth + 1)]
        name, values = keyed
        properties = items.get("properties") or {}
        return [
            {
                prop_name: value if prop_name == name else build(prop, str(value), depth + 1)
                for prop_name, prop in properties.items()
            }
            for value in values
        ]
    if kind == "string":
        return _text(path, schema)
    if kind in ("integer", "number"):
        return schema.get("minimum", 0)
    if kind == "boolean":
        return False
    return None


class FakeContentModel:
    name = "fake"

    def __init__(self) -> None:
        self.calls: list[GenerationRequest] = []
        self.checks: list[str] = []  # model ids checked
        self._queue: deque[GenerationResult | Exception] = deque()

    def is_configured(self) -> bool:
        return True

    def push(self, outcome: GenerationResult | Exception) -> None:
        self._queue.append(outcome)

    def reset(self) -> None:
        self.calls.clear()
        self.checks.clear()
        self._queue.clear()

    def generate(self, request: GenerationRequest, *, api_key: Secret) -> GenerationResult:
        if integration_modes.dev_features_allowed():
            self.calls.append(request)
        if self._queue:
            outcome = self._queue.popleft()
            if isinstance(outcome, Exception):
                raise outcome
            return outcome
        if api_key.reveal().startswith(INVALID_KEY_PREFIX):
            raise LLMNotConfigured("fake: key rejected")
        if UNAVAILABLE_MARKER in request.prompt:
            raise LLMUnavailable("fake: provider down")
        if REFUSED_MARKER in request.prompt:
            raise LLMRefused("fake: request refused")
        if MALFORMED_MARKER in request.prompt:
            raise LLMBadOutput("fake: answer is not JSON")
        data = build(request.response_schema)
        if not isinstance(data, dict):
            raise LLMBadOutput("fake: schema is not an object")
        answer = json.dumps(data, ensure_ascii=False)
        prompt_size = len(request.system_instruction) + len(request.prompt)
        return GenerationResult(
            data=data,
            input_tokens=prompt_size // 4 + 258 * len(request.images),
            output_tokens=len(answer) // 4,
            model=request.model,
        )

    def check(self, *, api_key: Secret, model: str, timeout_seconds: int = 10) -> None:
        if integration_modes.dev_features_allowed():
            self.checks.append(model)
        if api_key.reveal().startswith(INVALID_KEY_PREFIX) or not api_key:
            raise LLMNotConfigured("fake: key rejected")
        if model.startswith(UNKNOWN_MODEL_PREFIX):
            raise LLMNotConfigured("fake: unknown model")
