"""HTTP plumbing shared by the live OAuth adapters: one timeout, retries on transport errors only."""

import logging
from typing import Any

import httpx
from tenacity import Retrying, retry_if_exception_type, stop_after_attempt, wait_exponential

logger = logging.getLogger(__name__)
TIMEOUT_SECONDS = 10.0
# Two retries after the first attempt, exponential backoff starting at RETRY_BACKOFF_SECONDS.
RETRY_ATTEMPTS = 3
RETRY_BACKOFF_SECONDS = 0.25


def client(transport: httpx.BaseTransport | None = None) -> httpx.Client:
    return httpx.Client(timeout=TIMEOUT_SECONDS, transport=transport)


def request(http: httpx.Client, method: str, url: str, **kwargs: Any) -> httpx.Response:
    """Send the request, retrying only on transport errors (connect, read, timeout); never on a status."""
    retrying = Retrying(
        retry=retry_if_exception_type(httpx.TransportError),
        stop=stop_after_attempt(RETRY_ATTEMPTS),
        wait=wait_exponential(multiplier=RETRY_BACKOFF_SECONDS),
        reraise=True,
    )
    for attempt in retrying:
        with attempt:
            return http.request(method, url, **kwargs)
    raise AssertionError("unreachable")  # pragma: no cover - Retrying either returns or re-raises


def json_body(response: httpx.Response) -> dict[str, Any]:
    try:
        body = response.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}
