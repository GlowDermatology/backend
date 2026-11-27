"""Bria's HTTP contract (``https://engine.prod.bria-api.com/v2``), shared by the background remover
(:mod:`glowderma.integrations.segmentation.bria`) and the scene generator (:mod:`.bria_image`).

Requests are JSON with a base64 ``image``; the key travels in the ``api_token`` header, never to the result
host and never in a log. An asynchronous request answers 202 with a ``status_url`` (on Bria's own host),
polled every :data:`POLL_SECONDS` until ``COMPLETED``; ``result.image_url`` is then served from
``temp.bria.ai`` (kept 3 days by Bria; we download it at once). Errors become :mod:`.ports` errors: 401/403 →
``ai_not_configured``; 400, 404, 406, 413, 415, 422, 460 → ``ai_refused``; 429, 5xx and transport failures →
``provider_unavailable``; an answer without an image → ``ai_bad_output``. Billed per call, not per token.
"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Callable
from typing import Any

import httpx

from glowderma.integrations.secret import Secret

from .ports import LLMBadOutput, LLMError, LLMNotConfigured, LLMRefused, LLMUnavailable

logger = logging.getLogger(__name__)

BASE_URL = "https://engine.prod.bria-api.com/v2"
NOT_CONFIGURED = frozenset({401, 403})
REFUSED = frozenset({400, 404, 406, 413, 415, 422, 460})
MAX_DOWNLOAD_BYTES = 40 * 1024 * 1024
POLL_SECONDS = 1.5


def mapped(response: httpx.Response) -> LLMError:
    code = response.status_code
    detail = f"bria {code}"
    if code in NOT_CONFIGURED:
        return LLMNotConfigured(detail)
    if code in REFUSED:
        return LLMRefused(detail)
    return LLMUnavailable(detail)


def as_json(response: httpx.Response) -> dict[str, Any]:
    try:
        data = response.json()
    except ValueError:
        raise LLMBadOutput("bria: the answer is not JSON") from None
    return data if isinstance(data, dict) else {}


class BriaHttp:
    """One Bria call: submit, wait for the result (polling when asynchronous), download the image."""

    def __init__(
        self,
        *,
        httpx_client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        log_name: str = "bria",
    ) -> None:
        self._client = httpx_client
        self._sleep = sleep
        self._clock = clock
        self._log_name = log_name

    def send(self, method: str, url: str, *, timeout: float, **kwargs: Any) -> httpx.Response:
        client = self._client or httpx.Client(timeout=timeout)
        try:
            return client.request(method, url, timeout=timeout, **kwargs)
        except (httpx.HTTPError, OSError) as exc:
            logger.warning("%s_failed transport %s", self._log_name, type(exc).__name__)
            raise LLMUnavailable(f"bria transport {type(exc).__name__}") from None
        finally:
            if self._client is None:
                client.close()

    def run(
        self, path: str, body: dict[str, Any], *, api_key: Secret, timeout: float, deadline: float
    ) -> bytes:
        """POST ``body`` to ``BASE_URL + path`` and return the result image's bytes.

        ``timeout`` bounds each HTTP request; ``deadline`` (seconds from now) bounds the polling.
        """
        if not api_key:
            raise LLMNotConfigured("bria: empty key")
        headers = {"api_token": api_key.reveal(), "Accept": "application/json"}
        until = self._clock() + deadline
        response = self.send("POST", BASE_URL + path, timeout=timeout, json=body, headers=headers)
        if response.status_code not in (200, 202):
            error = mapped(response)
            logger.warning("%s_failed code=%s detail=%s", self._log_name, error.code, error)
            raise error
        answer = as_json(response)
        if response.status_code == 202:
            answer = self.poll(answer, headers, until, timeout)
        result = answer.get("result")
        url = result.get("image_url") if isinstance(result, dict) else None
        if not isinstance(url, str) or not url.startswith("https://"):
            raise LLMBadOutput("bria: no result image")
        image = self.send("GET", url, timeout=timeout)
        if image.status_code != 200 or len(image.content) > MAX_DOWNLOAD_BYTES:
            raise LLMUnavailable(f"bria result download {image.status_code}")
        return image.content

    def poll(
        self, accepted: dict[str, Any], headers: dict[str, str], until: float, timeout: float
    ) -> dict[str, Any]:
        """Poll an asynchronous request's ``status_url`` (on Bria's own host) until it is done."""
        status_url = accepted.get("status_url")
        if not isinstance(status_url, str) or not status_url.startswith(BASE_URL + "/status/"):
            raise LLMBadOutput("bria: no status url")
        while True:
            self._sleep(POLL_SECONDS)
            response = self.send("GET", status_url, timeout=timeout, headers=headers)
            if response.status_code != 200:
                raise mapped(response)
            state = as_json(response)
            status = state.get("status")
            if status == "COMPLETED":
                return state
            if status == "ERROR":
                raise LLMRefused("bria: the request failed")
            if status != "IN_PROGRESS":
                raise LLMUnavailable(f"bria: status {status}")
            if self._clock() > until:
                raise LLMUnavailable("bria: no result in time")

    def check(self, *, api_key: Secret, timeout_seconds: int = 10) -> None:
        """A key check that processes nothing: the status of an unknown request is 404 for a valid key,
        401/403 for a bad one."""
        if not api_key:
            raise LLMNotConfigured("bria: empty key")
        response = self.send(
            "GET",
            f"{BASE_URL}/status/{uuid.uuid4().hex}",
            timeout=float(timeout_seconds),
            headers={"api_token": api_key.reveal()},
        )
        if response.status_code in NOT_CONFIGURED:
            raise LLMNotConfigured(f"bria {response.status_code}")
        if response.status_code >= 500 or response.status_code == 429:
            raise LLMUnavailable(f"bria {response.status_code}")
