"""HTTP plumbing of the live provider adapters (M3): bounded calls, a retry only when nothing left the
process, JSON answers, and log lines without secrets.

Bounds (normative for every port, see ``click.ports`` / ``bts.ports``): a call connects within
:data:`CONNECT_TIMEOUT_SECONDS` and gives up at its :class:`Deadline` (:data:`TOTAL_TIMEOUT_SECONDS` unless
the adapter passes a shorter one). httpx has no total timeout, so every attempt runs under a **watchdog**:
when the deadline passes, the connection of the attempt is shut down (status line, headers or body — whatever
is still being read), and the body is checked against the deadline after every chunk, before the next one is
read. An answer that is complete when the deadline passes (``Content-Length`` reached) is kept.

Retries: only failures that prove the request never left the process (connect errors, connect and pool
timeouts, proxy refusals) are retried, at most :data:`RETRY_ATTEMPTS` times and only while the deadline
allows. **Everything else** that goes wrong once the request may have left — a write/read error or timeout, a
body that does not decode (``httpx.DecodingError``), a stream error, the deadline, any unexpected exception —
is a :class:`TransportFailure` with ``sent=True``: the adapter maps it to its "outcome unknown" error and
never resends on its own. A Celery soft time limit is never swallowed (it is re-raised as it is). HTTP
statuses are never retried here; each adapter maps them.

Clients keep no idle connection (each request opens its own, so the watchdog can always reach the socket of
the attempt it bounds) and share one module-level TLS context (building one costs ~20 ms of CPU).

Logs carry the provider, the method, the URL **path** (never the query string, headers or bodies) and the
outcome — no credential, token, phone number or message text ever reaches a log line from this module.
"""

from __future__ import annotations

import json
import logging
import re
import socket
import ssl
import threading
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import httpx
from celery.exceptions import SoftTimeLimitExceeded  # type: ignore[import-untyped]

logger = logging.getLogger(__name__)

CONNECT_TIMEOUT_SECONDS = 3.0
TOTAL_TIMEOUT_SECONDS = 10.0
MAX_BODY_BYTES = 1 << 20  # provider answers are small JSON documents; anything larger is refused
RETRY_ATTEMPTS = 3
RETRY_BACKOFF_SECONDS = 0.2
_LONG_DIGITS = re.compile(r"\+?\d[\d ()-]{5,}\d")
_INTEGER = re.compile(r"-?[0-9]{1,18}")

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

    @property
    def ok(self) -> bool:
        """Any 2xx status is an answer the adapter parses (Click documents 201, for instance)."""
        return 200 <= self.status < 300

    @property
    def not_processed(self) -> bool:
        """A status that says the provider did **not** process the request, so repeating it later is safe:
        a redirect (never followed), 408 / 425 / 429, or an authentication refusal (401 / 403)."""
        return 300 <= self.status < 400 or self.status in NOT_PROCESSED_STATUSES


# 401/403: our credentials or the token were refused (configuration, never the request's fault); 408/425/429:
# the provider asks us to come back later.
NOT_PROCESSED_STATUSES = frozenset({401, 403, 408, 425, 429})

_ssl_context: ssl.SSLContext | None = None
_ssl_lock = threading.Lock()


def ssl_context() -> ssl.SSLContext:
    """One verifying TLS context per process (certifi, or ``SSL_CERT_FILE`` / ``SSL_CERT_DIR``)."""
    global _ssl_context
    with _ssl_lock:
        if _ssl_context is None:
            _ssl_context = httpx.create_ssl_context()
        return _ssl_context


def new_client(transport: httpx.BaseTransport | None = None) -> httpx.Client:
    """One client per port call (or per push worker); redirects are never followed (a provider answer that
    redirects is mapped by the adapter, so a credential is never replayed to another host), no connection is
    kept idle (the watchdog of :func:`send` must reach the socket of each attempt)."""
    return httpx.Client(
        transport=transport,
        verify=ssl_context(),
        timeout=httpx.Timeout(TOTAL_TIMEOUT_SECONDS, connect=CONNECT_TIMEOUT_SECONDS),
        limits=httpx.Limits(max_keepalive_connections=0),
        follow_redirects=False,
    )


def int_code(value: object) -> int | None:
    """A provider's numeric code (an int, or a string of ASCII digits with an optional ``-``), else ``None``.

    Strict on purpose: ``"--1"``, ``"²"``, ``"٣"``, ``" 5"``, a bool or a float never read as a code."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if abs(value) < 10**18 else None
    if isinstance(value, str) and _INTEGER.fullmatch(value):
        return int(value)
    return None


def path_of(url: str) -> str:
    """The URL path only (no host credentials, no query string) for log lines."""
    return urlsplit(url).path or "/"


def _no_constant(name: str) -> Any:
    raise ValueError(f"JSON constant {name} is not a number we accept")


def _parse(content: bytes) -> Any:
    if not content:
        return None
    try:
        return json.loads(content, parse_constant=_no_constant)  # NaN / Infinity are not JSON
    except ValueError, RecursionError:
        return None


class _DeadlineExceeded(Exception):
    pass


class _BodyTooLarge(Exception):
    pass


class _Watchdog:
    """Shuts the attempt's TCP connection down when the deadline passes, whatever is being read then.

    The socket is learnt from httpcore's ``trace`` extension (``connection.connect_tcp.complete``); a client
    with a custom transport (the tests' ``MockTransport``) has none, and the per-chunk check bounds it."""

    def __init__(self, deadline: Deadline) -> None:
        self.fired = threading.Event()
        self._sockets: list[socket.socket] = []
        self._lock = threading.Lock()
        self._timer = threading.Timer(max(0.0, deadline.remaining()), self._fire)
        self._timer.daemon = True

    def trace(self, name: str, info: dict[str, Any]) -> None:
        if not name.endswith("connect_tcp.complete"):
            return
        stream = info.get("return_value")
        sock = stream.get_extra_info("socket") if stream is not None else None
        if isinstance(sock, socket.socket):
            with self._lock:
                self._sockets.append(sock)
                if self.fired.is_set():
                    _shutdown(sock)

    def _fire(self) -> None:
        with self._lock:
            self.fired.set()
            for sock in self._sockets:
                _shutdown(sock)

    def __enter__(self) -> _Watchdog:
        self._timer.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._timer.cancel()


def _shutdown(sock: socket.socket) -> None:
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass  # already closed


def _complete(response: httpx.Response) -> bool:
    """The whole body announced by ``Content-Length`` has arrived."""
    length = response.headers.get("content-length", "")
    return length.isascii() and length.isdigit() and response.num_bytes_downloaded >= int(length)


def _attempt(
    http: httpx.Client,
    method: str,
    url: str,
    deadline: Deadline,
    timeout: httpx.Timeout,
    watchdog: _Watchdog,
    **kwargs: Any,
) -> Answer:
    extensions = {**kwargs.pop("extensions", {}), "trace": watchdog.trace}
    with http.stream(method, url, timeout=timeout, extensions=extensions, **kwargs) as response:
        size = 0
        chunks: list[bytes] = []
        for chunk in response.iter_bytes():
            size += len(chunk)
            if size > MAX_BODY_BYTES:
                raise _BodyTooLarge
            chunks.append(chunk)
            if deadline.expired() and not _complete(response):
                raise _DeadlineExceeded  # checked before the next chunk is read
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
        with _Watchdog(deadline) as watchdog:
            try:
                answer = _attempt(http, method, url, deadline, timeout, watchdog, **dict(kwargs))
            except SoftTimeLimitExceeded:
                raise  # the worker's time is up: never turned into a provider outcome
            except NOT_SENT_ERRORS as exc:
                backoff = RETRY_BACKOFF_SECONDS * 2 ** (attempt - 1)
                if attempt < attempts and deadline.remaining() > backoff + CONNECT_TIMEOUT_SECONDS / 10:
                    logger.info(
                        "%s %s %s not sent (%s); retrying", provider, method, path, type(exc).__name__
                    )
                    time.sleep(backoff)
                    continue
                logger.warning("%s %s %s unreachable: %s", provider, method, path, type(exc).__name__)
                raise TransportFailure(type(exc).__name__, sent=False) from exc
            except _DeadlineExceeded as exc:
                logger.warning("%s %s %s: deadline exceeded while reading the answer", provider, method, path)
                raise TransportFailure("deadline exceeded", sent=True) from exc
            except _BodyTooLarge as exc:
                logger.warning(
                    "%s %s %s: answer larger than %s bytes", provider, method, path, MAX_BODY_BYTES
                )
                raise TransportFailure("answer too large", sent=True) from exc
            except Exception as exc:  # noqa: BLE001 - after the request may have left, every failure is "unknown"
                detail = "deadline exceeded" if watchdog.fired.is_set() else type(exc).__name__
                logger.warning("%s %s %s: no usable answer (%s)", provider, method, path, detail)
                raise TransportFailure(detail, sent=True) from exc
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
