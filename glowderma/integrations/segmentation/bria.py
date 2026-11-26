"""Bria RMBG cutter (``POST https://engine.prod.bria-api.com/v2/image/edit/remove_background``).

A dedicated background remover: the photo goes up as base64 JSON (``sync: true``), the answer's
``result.image_url`` is a PNG whose alpha channel is the product (fingers and the office removed); its alpha
thresholded at 128 is the mask. The key travels in the ``api_token`` header and is never logged. Results are
kept by Bria for 3 days; we download the PNG at once and keep nothing else.

Errors become :mod:`glowderma.integrations.llm.ports` errors: 401/403 → ``ai_not_configured``; 400, 406, 413,
415, 422 → ``ai_refused``; 429, 5xx and transport failures → ``provider_unavailable``; an answer without
an image or a PNG without transparency → ``ai_bad_output``. Billed per call (``CutOut.calls``), not per token.
"""

from __future__ import annotations

import base64
import logging
import uuid
from io import BytesIO
from typing import Any

import httpx
from PIL import Image

from glowderma.core import compose
from glowderma.integrations.llm.ports import (
    LLMBadOutput,
    LLMError,
    LLMNotConfigured,
    LLMRefused,
    LLMUnavailable,
)
from glowderma.integrations.secret import Secret

from .ports import CutOut, NoProduct, SegmentationRequest

logger = logging.getLogger(__name__)

BASE_URL = "https://engine.prod.bria-api.com/v2"
REMOVE_PATH = "/image/edit/remove_background"
NOT_CONFIGURED = frozenset({401, 403})
REFUSED = frozenset({400, 404, 406, 413, 415, 422, 460})
MAX_DOWNLOAD_BYTES = 40 * 1024 * 1024


def _mapped(response: httpx.Response) -> LLMError:
    code = response.status_code
    detail = f"bria {code}"
    if code in NOT_CONFIGURED:
        return LLMNotConfigured(detail)
    if code in REFUSED:
        return LLMRefused(detail)
    return LLMUnavailable(detail)


class BriaCutter:
    name = "bria"

    def __init__(self, *, model: str = "RMBG2", httpx_client: httpx.Client | None = None) -> None:
        self._model = model
        self._client = httpx_client

    def _http(self, timeout: float) -> httpx.Client:
        return self._client or httpx.Client(timeout=timeout)

    def _send(self, method: str, url: str, *, timeout: float, **kwargs: Any) -> httpx.Response:
        client = self._http(timeout)
        try:
            return client.request(method, url, timeout=timeout, **kwargs)
        except (httpx.HTTPError, OSError) as exc:
            logger.warning("segmentation.bria_failed transport %s", type(exc).__name__)
            raise LLMUnavailable(f"bria transport {type(exc).__name__}") from None
        finally:
            if self._client is None:
                client.close()

    def cut(self, request: SegmentationRequest, *, api_key: Secret) -> CutOut:
        if not api_key:
            raise LLMNotConfigured("bria: empty key")
        body = {
            "image": base64.b64encode(request.image.data).decode(),
            "model": self._model,
            "sync": True,
            "preserve_alpha": True,
        }
        response = self._send(
            "POST",
            BASE_URL + REMOVE_PATH,
            timeout=float(request.timeout_seconds),
            json=body,
            headers={"api_token": api_key.reveal(), "Accept": "application/json"},
        )
        if response.status_code != 200:
            error = _mapped(response)
            logger.warning("segmentation.bria_failed code=%s detail=%s", error.code, error)
            raise error
        try:
            url = response.json()["result"]["image_url"]
        except ValueError, KeyError, TypeError:
            raise LLMBadOutput("bria: no result image") from None
        if not isinstance(url, str) or not url.startswith("https://"):
            raise LLMBadOutput("bria: no result image")
        image = self._send("GET", url, timeout=float(request.timeout_seconds))
        if image.status_code != 200 or len(image.content) > MAX_DOWNLOAD_BYTES:
            raise LLMUnavailable(f"bria result download {image.status_code}")
        try:
            with Image.open(BytesIO(image.content)) as source:
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
        """A key check that processes nothing: the status of an unknown request is 404 for a valid key,
        401/403 for a bad one."""
        if not api_key:
            raise LLMNotConfigured("bria: empty key")
        response = self._send(
            "GET",
            f"{BASE_URL}/status/{uuid.uuid4().hex}",
            timeout=float(timeout_seconds),
            headers={"api_token": api_key.reveal()},
        )
        if response.status_code in NOT_CONFIGURED:
            raise LLMNotConfigured(f"bria {response.status_code}")
        if response.status_code >= 500 or response.status_code == 429:
            raise LLMUnavailable(f"bria {response.status_code}")
