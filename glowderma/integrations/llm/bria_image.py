"""Bria scene generation (``POST https://engine.prod.bria-api.com/v2/image/edit/replace_background``).

``mode: "fibo2"``: Bria keeps the given foreground (the PNG's alpha: the product we cut out, already placed on
our 4:5 master), builds a new scene around it from a text prompt with its Fibo 2 model, and pastes the product
back over it. ``original_quality`` keeps the master's size, so the result lines up with our canvas; the scene
pipeline still checks the alignment and pastes the original pixels back itself. Live: about 20 s per image,
billed per image (``billed_calls``), not per token. The HTTP contract and errors are :mod:`.bria_http`'s.

A banner's scene without products uses Fibo 2's text to image (``POST /v2/image/generate``, 1 MP, ``21:9``)
with the same plain ``scene``.

Bria builds only the background: it cannot paint a part of the product a finger hid
(:attr:`BriaSceneModel.fills_hidden` is ``False``). It takes one plain English ``scene`` (about 90-110 words,
no special characters); Gemini's rule-laden ``prompt`` is not sent.
"""

from __future__ import annotations

import base64
import time
from collections.abc import Callable
from io import BytesIO
from typing import Any

import httpx
from PIL import Image

from glowderma.integrations.secret import Secret

from .bria_http import BriaHttp
from .ports import ImageGenerationRequest, ImageGenerationResult, LLMBadOutput, LLMNotConfigured

REPLACE_PATH = "/image/edit/replace_background"
GENERATE_PATH = "/image/generate"  # text to image (Fibo 2): a banner's scene without products
GENERATE_RATIOS = frozenset(
    {"1:1", "2:3", "3:2", "3:4", "4:3", "4:5", "5:4", "9:16", "16:9", "16:10", "2:1", "21:9", "3:1"}
)
MODE = "fibo2"
MIN_WAIT_SECONDS = 90.0  # polling budget: live about 20 s


class BriaSceneModel:
    name = "bria"
    fills_hidden = False

    def __init__(
        self,
        *,
        httpx_client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._http = BriaHttp(httpx_client=httpx_client, sleep=sleep, clock=clock, log_name="llm.bria_scene")

    def is_configured(self) -> bool:
        return True

    def generate_image(self, request: ImageGenerationRequest, *, api_key: Secret) -> ImageGenerationResult:
        if not api_key:
            raise LLMNotConfigured("bria: empty key")
        if not request.scene.strip():
            raise LLMBadOutput("bria: a scene needs a scene description")
        body: dict[str, Any]
        if request.cutout is not None:
            path = REPLACE_PATH
            body = {
                "image": base64.b64encode(request.cutout.data).decode(),
                "prompt": request.scene,
                "mode": MODE,
                "original_quality": True,
                "sync": False,
            }
        elif not request.images:
            # A scene alone (a banner without products): Fibo 2 text to image at 1 MP, the widest ratios
            # only at that size (Bria refuses 21:9 with 4 MP).
            if request.aspect_ratio not in GENERATE_RATIOS:
                raise LLMBadOutput(f"bria: no {request.aspect_ratio} text to image")
            path = GENERATE_PATH
            body = {
                "prompt": request.scene,
                "aspect_ratio": request.aspect_ratio,
                "resolution": "1MP",
                "sync": False,
            }
        else:
            raise LLMBadOutput("bria: a scene needs the product cut-out")
        timeout = float(request.timeout_seconds)
        content = self._http.run(
            path, body, api_key=api_key, timeout=timeout, deadline=max(MIN_WAIT_SECONDS, timeout)
        )
        try:
            with Image.open(BytesIO(content)) as image:
                image.load()
                mime = Image.MIME.get(image.format or "", "image/png")
        except Exception:  # noqa: BLE001 - provider bytes that do not decode
            raise LLMBadOutput("bria: the scene does not decode") from None
        return ImageGenerationResult(
            image=content,
            mime_type=mime,
            input_tokens=0,
            output_text_tokens=0,
            output_image_tokens=0,
            model=MODE,
            billed_calls=1,
        )

    def check(self, *, api_key: Secret, model: str = MODE, timeout_seconds: int = 10) -> None:
        """A key check that generates nothing (see :meth:`BriaHttp.check`)."""
        self._http.check(api_key=api_key, timeout_seconds=timeout_seconds)
