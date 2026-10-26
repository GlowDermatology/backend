"""HTTP plumbing of the live provider adapters (M3): bounded calls, a retry only when nothing left the
process, JSON answers, and log lines without secrets.

Bounds (normative for every port, see ``click.ports`` / ``bts.ports``): a call connects within
:data:`CONNECT_TIMEOUT_SECONDS` and gives up at its :class:`Deadline` (:data:`TOTAL_TIMEOUT_SECONDS` unless
the adapter passes a shorter one). httpx has no total timeout, so the body is streamed and the deadline is
checked after every chunk; the read timeout of each attempt is the time left, so a provider that trickles
bytes can stretch one call to at most twice the bound — still far below the five-minute outbox lease the
refund and shipment handlers rely on.

Retries: only failures that prove the request never left the process (connect errors, connect and pool
timeouts, proxy refusals) are retried, at most :data:`RETRY_ATTEMPTS` times and only while the deadline
allows. Everything after the request may have left (write/read errors and timeouts, a broken response, the
deadline) is a :class:`TransportFailure` with ``sent=True``: the adapter maps it to its "outcome unknown"
error and never resends on its own. HTTP statuses are never retried here; each adapter maps them.

Logs carry the provider, the method, the URL **path** (never the query string, headers or bodies) and the
outcome — no credential, token, phone number or message text ever reaches a log line from this module.
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import httpx

logger = logging.getLogger(__name__)

CONNECT_TIMEOUT_SECONDS = 3.0
TOTAL_TIMEOUT_SECONDS = 10.0
MAX_BODY_BYTES = 1 << 20  # provider answers are small JSON documents; anything larger is refused
RETRY_ATTEMPTS = 3
RETRY_BACKOFF_SECONDS = 0.2
_LONG_DIGITS = re.compile(r"\+?\d[\d ()-]{5,}\d")

# Raised before a single byte of the request was written: safe to retry, nothing reached the provider.
NOT_SENT_ERRORS: tuple[type[Exception], ...] = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.PoolTimeout,
    httpx.ProxyError,
    httpx.UnsupportedProtocol,
)


class TransportFailure(Exception):
    """The provider gave no HTTP answer. ``sent`` is false only when the request provably never left."""

    def __init__(self, detail: str, *, sent: bool) -> None:
        super().__init__(detail)
        self.sent = sent


class Deadline:
    """A monotonic point in time shared by every request of one port call."""

    def __init__(self, seconds: float = TOTAL_TIMEOUT_SECONDS) -> None:
        self.at = time.monotonic() + seconds

    def remaining(self) -> float:
        return self.at - time.monotonic()

    def expired(self) -> bool:
        return self.remaining() <= 0


@dataclass(frozen=True, slots=True)
class Answer:
    """An HTTP answer: the status and the parsed JSON body (``None`` when the body is not JSON)."""

    status: int
    data: Any

    def json_object(self) -> dict[str, Any]:
        return self.data if isinstance(self.data, dict) else {}


def new_client(transport: httpx.BaseTransport | None = None) -> httpx.Client:
    """One client per port call (or per push batch); redirects are never followed (a provider answer that
    redirects is mapped by the adapter, so a credential is never replayed to another host)."""
    return httpx.Client(
        transport=transport,
        timeout=httpx.Timeout(TOTAL_TIMEOUT_SECONDS, connect=CONNECT_TIMEOUT_SECONDS),
        follow_redirects=False,
    )


def path_of(url: str) -> str:
    """The URL path only (no host credentials, no query string) for log lines."""
    return urlsplit(url).path or "/"


def _parse(content: bytes) -> Any:
    if not content:
        return None
    try:
        return json.loads(content)
    except ValueError, RecursionError:
        return None


class _DeadlineExceeded(Exception):
    pass


class _BodyTooLarge(Exception):
    pass


def _attempt(
    http: httpx.Client, method: str, url: str, deadline: Deadline, timeout: httpx.Timeout, **kwargs: Any
) -> Answer:
    with http.stream(method, url, timeout=timeout, **kwargs) as response:
        size = 0
        chunks: list[bytes] = []
        for chunk in response.iter_bytes():
            size += len(chunk)
            if size > MAX_BODY_BYTES:
                raise _BodyTooLarge
            chunks.append(chunk)
            if deadline.expired():
                raise _DeadlineExceeded
        return Answer(status=response.status_code, data=_parse(b"".join(chunks)))


def send(
    http: httpx.Client,
    method: str,
    url: str,
    *,
    provider: str,
    deadline: Deadline,
    retry_unsent: bool = True,
    **kwargs: Any,
) -> Answer:
    """Send one request within ``deadline`` (see the module docstring for retries and failures)."""
    attempts = RETRY_ATTEMPTS if retry_unsent else 1
    path = path_of(url)
    for attempt in range(1, attempts + 1):
        remaining = deadline.remaining()
        if remaining <= 0:
            raise TransportFailure("deadline reached before the request was sent", sent=False)
        timeout = httpx.Timeout(remaining, connect=min(CONNECT_TIMEOUT_SECONDS, remaining))
        started = time.monotonic()
        try:
            answer = _attempt(http, method, url, deadline, timeout, **kwargs)
        except NOT_SENT_ERRORS as exc:
            backoff = RETRY_BACKOFF_SECONDS * 2 ** (attempt - 1)
            if attempt < attempts and deadline.remaining() > backoff + CONNECT_TIMEOUT_SECONDS / 10:
                logger.info("%s %s %s not sent (%s); retrying", provider, method, path, type(exc).__name__)
                time.sleep(backoff)
                continue
            logger.warning("%s %s %s unreachable: %s", provider, method, path, type(exc).__name__)
            raise TransportFailure(type(exc).__name__, sent=False) from exc
        except _DeadlineExceeded as exc:
            logger.warning("%s %s %s: deadline exceeded while reading the answer", provider, method, path)
            raise TransportFailure("deadline exceeded", sent=True) from exc
        except _BodyTooLarge as exc:
            logger.warning("%s %s %s: answer larger than %s bytes", provider, method, path, MAX_BODY_BYTES)
            raise TransportFailure("answer too large", sent=True) from exc
        except httpx.TransportError as exc:
            logger.warning("%s %s %s: no answer (%s)", provider, method, path, type(exc).__name__)
            raise TransportFailure(type(exc).__name__, sent=True) from exc
        elapsed_ms = int((time.monotonic() - started) * 1000)
        logger.info("%s %s %s -> %s in %s ms", provider, method, path, answer.status, elapsed_ms)
        return answer
    raise AssertionError("unreachable")  # pragma: no cover - the loop returns or raises


def safe_text(value: object, limit: int = 200) -> str:
    """A provider's free-text message for our logs and stored errors: digit runs of seven characters or more
    (phone numbers, card fragments; spaces, dashes and brackets inside the run included) become ``***``,
    whitespace is collapsed and the text is truncated to ``limit``."""
    text = value if isinstance(value, str) else ("" if value is None else str(value))
    return " ".join(_LONG_DIGITS.sub("***", text).split())[:limit]
