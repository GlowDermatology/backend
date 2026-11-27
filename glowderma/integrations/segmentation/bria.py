"""Bria RMBG cutter (``POST https://engine.prod.bria-api.com/v2/image/edit/remove_background``).

A dedicated background remover. The photo goes up as base64 JSON with a ``prompt`` that keeps only the
product: without it Bria keeps the whole foreground, the hand holding the product included. Prompted
requests run asynchronously (about 15 s): the answer is a ``status_url`` polled every
:data:`POLL_SECONDS` until ``COMPLETED`` (or the request's timeout). ``result.image_url`` is a PNG whose alpha
channel is the product; its alpha thresholded at 128 is the mask. The result file is served from
``temp.bria.ai`` (the worker needs outbound HTTPS to it as well). The key travels in the ``api_token``
header (never to the result host) and is never logged. Results are
kept by Bria for 3 days; we download the PNG at once and keep nothing else.

The HTTP contract, error mapping and polling are :mod:`glowderma.integrations.llm.bria_http`'s; a PNG
without transparency is ``ai_bad_output``. Billed per call (``CutOut.calls``), not per token.
"""

from __future__ import annotations

import base64
import time
from collections.abc import Callable
from io import BytesIO
from typing import Any

import httpx
from PIL import Image

from glowderma.core import compose
from glowderma.integrations.llm.bria_http import BASE_URL, BriaHttp
from glowderma.integrations.llm.ports import LLMBadOutput
from glowderma.integrations.secret import Secret

from .ports import CutOut, NoProduct, SegmentationRequest

__all__ = ["BASE_URL", "PROMPT", "REMOVE_PATH", "BriaCutter"]

REMOVE_PATH = "/image/edit/remove_background"
PROMPT = "only the skincare product (bottle, tube, jar or box), not the hand or fingers"


class BriaCutter:
    name = "bria"

    def __init__(
        self,
        *,
        model: str = "RMBG2",
        prompt: str = PROMPT,
        httpx_client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        # a prompt cannot be combined with RMBG 3.0 (Bria answers 422)
        self._model = "RMBG2" if prompt else model
        self._prompt = prompt
        self._http = BriaHttp(
            httpx_client=httpx_client, sleep=sleep, clock=clock, log_name="segmentation.bria"
        )

    def cut(self, request: SegmentationRequest, *, api_key: Secret) -> CutOut:
        body: dict[str, Any] = {
            "image": base64.b64encode((request.full_image or request.image).data).decode(),
            "model": self._model,
            "sync": not self._prompt,
            "preserve_alpha": True,
        }
        if self._prompt:
            body["prompt"] = self._prompt
        timeout = float(request.timeout_seconds)
        content = self._http.run(
            REMOVE_PATH, body, api_key=api_key, timeout=timeout, deadline=max(30.0, timeout)
        )
        try:
            with Image.open(BytesIO(content)) as source:
                source.load()
                cut = source.convert("RGBA")
        except Exception:  # noqa: BLE001 - provider bytes that do not decode
            raise LLMBadOutput("bria: the result does not decode") from None
        alpha = cut.getchannel("A")
        if alpha.getextrema() == (255, 255):
            raise LLMBadOutput("bria: the result has no transparency")
        mask = compose.binary(alpha.resize((request.width, request.height), Image.Resampling.BILINEAR))
        if mask.getbbox() is None:
            raise NoProduct("bria: nothing kept")
        return CutOut(mask=mask)

    def check(self, *, api_key: Secret, timeout_seconds: int = 10) -> None:
        """A key check that processes nothing (see :meth:`BriaHttp.check`)."""
        self._http.check(api_key=api_key, timeout_seconds=timeout_seconds)
