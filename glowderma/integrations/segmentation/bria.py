"""Bria RMBG cutter (``POST https://engine.prod.bria-api.com/v2/image/edit/remove_background``).

A dedicated background remover. The photo goes up as base64 JSON with a ``prompt`` that keeps only the
product: without it Bria keeps the whole foreground, the hand holding the product included. Prompted
requests run asynchronously (about 15 s): the answer is a ``status_url`` polled every
:data:`POLL_SECONDS` until ``COMPLETED`` (or the request's timeout). ``result.image_url`` is a PNG whose alpha
channel is the product; its alpha thresholded at 128 is the mask. The result file is served from
``temp.bria.ai`` (the worker needs outbound HTTPS to it as well). The key travels in the ``api_token``
header (never to the result host) and is never logged. Results are
kept by Bria for 3 days; we download the PNG at once and keep nothing else.

Errors become :mod:`glowderma.integrations.llm.ports` errors: 401/403 → ``ai_not_configured``; 400, 406, 413,
415, 422 → ``ai_refused``; 429, 5xx and transport failures → ``provider_unavailable``; an answer without
an image or a PNG without transparency → ``ai_bad_output``. Billed per call (``CutOut.calls``), not per token.
"""

from __future__ import annotations

import base64
import logging
import time
import uuid
from collections.abc import Callable
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
POLL_SECONDS = 1.5
PROMPT = "only the skincare product (bottle, tube, jar or box), not the hand or fingers"


def _mapped(response: httpx.Response) -> LLMError:
    code = response.status_code
    detail = f"bria {code}"
    if code in NOT_CONFIGURED:
        return LLMNotConfigured(detail)
    if code in REFUSED:
        return LLMRefused(detail)
    return LLMUnavailable(detail)


def _json(response: httpx.Response) -> dict[str, Any]:
    try:
        data = response.json()
    except ValueError:
        raise LLMBadOutput("bria: the answer is not JSON") from None
    return data if isinstance(data, dict) else {}


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
        self._client = httpx_client
        self._sleep = sleep
        self._clock = clock

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
        body: dict[str, Any] = {
            "image": base64.b64encode((request.full_image or request.image).data).decode(),
            "model": self._model,
            "sync": not self._prompt,
            "preserve_alpha": True,
        }
        if self._prompt:
            body["prompt"] = self._prompt
        headers = {"api_token": api_key.reveal(), "Accept": "application/json"}
        deadline = self._clock() + max(30.0, float(request.timeout_seconds))
        response = self._send(
            "POST", BASE_URL + REMOVE_PATH, timeout=float(request.timeout_seconds), json=body, headers=headers
        )
        if response.status_code not in (200, 202):
            error = _mapped(response)
            logger.warning("segmentation.bria_failed code=%s detail=%s", error.code, error)
            raise error
        answer = _json(response)
        if response.status_code == 202:
            answer = self._poll(answer, headers, deadline, float(request.timeout_seconds))
        url = (
            (answer.get("result") or {}).get("image_url") if isinstance(answer.get("result"), dict) else None
        )
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

    def _poll(
        self, accepted: dict[str, Any], headers: dict[str, str], deadline: float, timeout: float
    ) -> dict[str, Any]:
        """Poll an asynchronous request's ``status_url`` (on Bria's own host) until it is done."""
        status_url = accepted.get("status_url")
        if not isinstance(status_url, str) or not status_url.startswith(BASE_URL + "/status/"):
            raise LLMBadOutput("bria: no status url")
        while True:
            self._sleep(POLL_SECONDS)
            response = self._send("GET", status_url, timeout=timeout, headers=headers)
            if response.status_code != 200:
                raise _mapped(response)
            state = _json(response)
            status = state.get("status")
            if status == "COMPLETED":
                return state
            if status == "ERROR":
                raise LLMRefused("bria: the request failed")
            if status != "IN_PROGRESS":
                raise LLMUnavailable(f"bria: status {status}")
            if self._clock() > deadline:
                raise LLMUnavailable("bria: no result in time")

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
