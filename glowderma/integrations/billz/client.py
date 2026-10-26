"""HTTP plumbing of the live Billz adapter: configuration, auth, the shared rate limiter, retries, errors.

Source of truth: the BILLZ 2.0 public API ``openapi.yaml`` (docs.billz.io); docs/integrations/billz.md maps
every call. Nothing here was verified against a live Billz account.

**Shared cache**: tokens, the limiter, the circuit breaker and the adapter's locks live in the Django cache
(Redis DB 0). Every cache operation goes through :class:`SafeCache`, so Redis being down is
:class:`BillzUnavailable` ("shared cache unavailable", nothing sent to Billz) — an admin lookup answers 503,
never 500.

**Auth** (``post_v1_auth_login``, ``post_v2_auth_refresh``): ``POST /v1/auth/login {"secret_token"}`` →
``access_token`` (``expires_in`` seconds, 15 days for a key login) + a one-use ``refresh_token`` (30 days);
``POST /v2/auth/refresh {"refresh_token"}`` → a new pair (rotation: presenting a spent refresh token revokes
the whole session family). Both send the ``platform-id`` header (``openapi.yaml`` declares it on both). The
pair lives **only** in the cache (:data:`TOKENS_KEY`), never in the database or a log line. Renewal is
single-flight across processes: the winner of ``cache.add(LOCK_KEY)`` (TTL :data:`LOCK_TTL_SECONDS`) renews
within :data:`RENEW_BOUND_SECONDS` (at most half the TTL, so the lock cannot expire under it). Before it
presents the refresh token it **removes it from the cache**, so no other worker can ever present the same
token again (Billz would revoke the family); a refresh that fails or times out falls back to a login, never to
a second refresh. The new pair is stored only while the renewer still owns the lock. The others re-read the
cache until the pair changes. A 401 on a normal call renews once and repeats the call once; a second 401 is
:class:`BillzUnavailable`, never a loop. A login answer without ``expires_in`` lives
:data:`DEFAULT_ACCESS_TTL_SECONDS` (clamped like every lifetime).

**Rate limit**: Billz answers 429 above ~2 requests/second per IP, counted over the whole integration, so
every worker shares one limiter in the cache: time is cut into slots of ``1 / rate`` seconds and a request
may start only at the beginning of a slot it won with ``cache.add`` (atomic ``SET NX`` on Redis), so two
requests never start closer than one slot apart, whichever process sends them (hosts need NTP-synced
clocks). A 429 pauses every worker (``Retry-After`` when Billz sends one, exponential backoff with jitter
otherwise).

**Profiles and deadlines** (review B5): a :class:`CallProfile` bounds each call — the batch profile (sync,
sale push) waits up to a minute for a limiter slot and retries 4 times; the :data:`INTERACTIVE` profile of
the admin lookups (which run inside the idempotency transaction of ``POST products/``) waits 3 s for a slot,
retries once, reads for at most 10 s, fails fast while a 429 pause is in force and gives up after 25 s in
total. :meth:`BillzClient.deadline` sets an absolute deadline (the sale push: 240 s, below the outbox lease)
that every wait, sleep and timeout honours.

**Circuit breaker** (review B6): :data:`CIRCUIT_FAILURES` transport errors, timeouts or 5xx answers in a row
(across every worker) open the circuit for :data:`CIRCUIT_OPEN_SECONDS`: calls then fail fast with
:class:`BillzUnavailable` instead of holding a worker for minutes (ERROR ``billz.circuit_open``). Any other
answer closes it again.

**Retries**: transport errors and 5xx are retried (bounded, exponential backoff with jitter) only for calls
that are safe to repeat — reads and absolute upserts — or when the request certainly never reached Billz
(connect errors). A write whose outcome is unknown (read timeout, 5xx, 408 of an async method) raises
:class:`BillzUnavailable` instead; the sale push re-reads the sale before repeating anything. (AGENTS.md asks
adapters to retry transport errors only; Billz is the documented exception: its 429 is the documented
rate-limit signal and its 5xx are retried for repeatable calls only.) Any other httpx error (a body that does
not decode) is :class:`BillzUnavailable`, never an untyped exception.

**Errors**: classic ``{"code", "message", "error", "data"}`` bodies, ``{"error": {"code", "message"}}`` and
the async envelope ``{"status_code", "error", "data": {"error_code", …}}`` are all read; :func:`classify`
maps them: retryable business codes → :class:`BillzUnavailable`; the closed shift → :class:`BillzShiftClosed`
(deferred, decision D2); configuration-type failures (3xx, 403, 404 of a route, 20009, 20085) →
:class:`BillzMisconfigured` (retryable, ERROR ``billz.configuration_error``, decision D3); everything else →
:class:`BillzRejected`. Messages carry the method and the path template, never a query string (a phone
number), a body, a token or the key; Billz's own free text is scrubbed (control characters dropped, digit runs
masked) before it is kept anywhere.
"""

from __future__ import annotations

import logging
import random
import time
import uuid
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
from functools import partial
from typing import Any

import httpx
from django.conf import settings
from django.core.cache import cache as default_cache
from django.core.cache.backends.base import BaseCache

from glowderma.core.pii import clean_text, scrub_free_text

from .ports import (
    BillzError,
    BillzMisconfigured,
    BillzRejected,
    BillzShiftClosed,
    BillzUnavailable,
)

logger = logging.getLogger(__name__)

TOKENS_KEY = "billz:auth:tokens"  # noqa: S105 - a cache key name, not a secret
LOCK_KEY = "billz:auth:lock"
RATE_PREFIX = "billz:rate"
CIRCUIT_FAILURES_KEY = "billz:circuit:failures"
CIRCUIT_OPEN_KEY = "billz:circuit:open_until"
# Renewal: one renewal (a refresh, else a login) takes at most RENEW_BOUND_SECONDS; the lock lives twice that.
LOCK_TTL_SECONDS = 90
RENEW_BOUND_SECONDS = 40.0
LOCK_POLL_SECONDS = 0.2
# Renew an access token this long before Billz says it expires.
ACCESS_MARGIN_SECONDS = 300
# Token lifetimes: an answer without expires_in, and the bounds every lifetime is clamped to.
DEFAULT_ACCESS_TTL_SECONDS = 3_600
MIN_ACCESS_TTL_SECONDS = 2 * ACCESS_MARGIN_SECONDS
MAX_TTL_SECONDS = 30 * 86_400
# A key login's refresh token lives 30 days (docs); used when a refresh answer has no refresh_expires_in.
DEFAULT_REFRESH_TTL_SECONDS = 30 * 86_400
BACKOFF_BASE_SECONDS = 0.5
BACKOFF_CAP_SECONDS = 8.0
RETRY_AFTER_CAP_SECONDS = 60.0
# The circuit opens after this many failures in a row and stays open this long.
CIRCUIT_FAILURES = 5
CIRCUIT_OPEN_SECONDS = 90
RESPONSE_CHANNEL_HEADER = "Billz-Response-Channel"
# Billz's own free text kept in an error (after scrubbing).
MESSAGE_LIMIT = 300
CODE_LIMIT = 64


@dataclass(frozen=True, slots=True)
class CallProfile:
    """How long one call may wait and how often it is retried (module doc, review B5)."""

    name: str
    max_attempts: int  # transport errors / 5xx of repeatable calls, attempts in total
    max_throttled: int  # 429 answers tolerated per call (0: fail at the first)
    limiter_wait: float  # how far ahead the limiter may queue a request
    read_timeout_cap: float | None  # caps BILLZ_READ_TIMEOUT_SECONDS
    renew_wait: float  # how long to wait for another worker's token renewal
    wait_on_pause: bool  # False: a 429 pause in force fails fast instead of sleeping
    call_budget: float | None  # seconds one call may take in total (None: the active deadline only)


BATCH = CallProfile(
    name="batch",
    max_attempts=4,
    max_throttled=5,
    limiter_wait=60.0,
    read_timeout_cap=None,
    renew_wait=45.0,
    wait_on_pause=True,
    call_budget=None,
)
INTERACTIVE = CallProfile(
    name="interactive",
    max_attempts=2,
    max_throttled=0,
    limiter_wait=3.0,
    read_timeout_cap=10.0,
    renew_wait=5.0,
    wait_on_pause=False,
    call_budget=25.0,
)

# Business codes of the sale methods (llms-full "Коды ответов и ошибки", "Интернет-заказ" step 7). Retryable:
# the next attempt can succeed on its own (an async recalculation still running, a bill that changed under
# us, items an async add has not applied yet). The closed shift waits for the next shift (decision D2); a
# configuration-type code waits for staff to fix a setting (decision D3). Everything else — and every unknown
# code — is permanent: staff must act (fix the product, check the sale) and the outbox message goes dead.
ERROR_REASONS: dict[int, str] = {
    20009: "the cash box belongs to another shop than the sale",
    20010: "no open cash box in the sale's shop (shift closed)",
    20011: "the cash box has not enough money",
    20015: "the sale has no items",
    20016: "not enough stock in the sale's shop",
    20017: "the sale was deleted in Billz",
    20020: "the payments do not match the bill total",
    20033: "no open shift",
    20036: "product not found in Billz",
    20037: "the product is archived in Billz",
    20047: "not enough customer cashback balance",
    20063: "the sale was not sent to the fiscal operator (OFD)",
    20082: "payment refused: the cash box is closed",
    20084: "the bill is not calculated yet",
    20085: "the seller has no access to the shop",
}
RETRYABLE_ERROR_CODES = frozenset({20015, 20020, 20084})
SHIFT_CLOSED_CODES = frozenset({20010, 20033, 20082})
CONFIGURATION_ERROR_CODES = frozenset({20009, 20085})
RETRYABLE_STRING_CODES = frozenset({"TIMEOUT", "TOO_MANY_REQUESTS", "rate_limited", "server_error"})


@dataclass(frozen=True, slots=True)
class BillzConfig:
    """The live client's settings (``BILLZ_*``); the key is kept out of ``repr``."""

    base_url: str
    secret_key: str = field(repr=False)
    platform_id: str
    shop_id: str
    cashbox_id: str
    payment_type_id: str
    rate_limit_per_second: float = 1.5
    connect_timeout: float = 5.0
    read_timeout: float = 30.0
    page_size: int = 300
    full_sync_min_ratio: float = 0.8
    volume_field: str = ""

    @property
    def configured(self) -> bool:
        """Everything a sync and a sale push need is set (production refuses to start otherwise)."""
        return all((self.secret_key, self.shop_id, self.cashbox_id, self.payment_type_id))

    @classmethod
    def from_settings(cls) -> BillzConfig:
        return cls(
            base_url=str(settings.BILLZ_BASE_URL).strip().rstrip("/"),
            secret_key=str(settings.BILLZ_SECRET_KEY).strip(),
            platform_id=str(settings.BILLZ_PLATFORM_ID).strip(),
            shop_id=str(settings.BILLZ_SHOP_ID).strip(),
            cashbox_id=str(settings.BILLZ_CASHBOX_ID).strip(),
            payment_type_id=str(settings.BILLZ_PAYMENT_TYPE_ID).strip(),
            rate_limit_per_second=float(settings.BILLZ_RATE_LIMIT_PER_SECOND),
            connect_timeout=float(settings.BILLZ_CONNECT_TIMEOUT_SECONDS),
            read_timeout=float(settings.BILLZ_READ_TIMEOUT_SECONDS),
            page_size=int(settings.BILLZ_PAGE_SIZE),
            full_sync_min_ratio=float(settings.BILLZ_FULL_SYNC_MIN_RATIO),
            volume_field=str(settings.BILLZ_VOLUME_FIELD).strip(),
        )


class BillzAuthError(BillzMisconfigured):
    """Billz refused the integration key (login 4xx): an operator must check ``BILLZ_SECRET_KEY``."""

    code = "billz_auth_failed"


class SafeCache:
    """The cache as the adapter uses it: any backend failure (Redis down) is :class:`BillzUnavailable`.

    Only the operations the adapter needs; ``timeout`` is always passed explicitly (Django reads ``None`` as
    "never expires").
    """

    def __init__(self, cache: BaseCache | SafeCache) -> None:
        self.backend: BaseCache = cache.backend if isinstance(cache, SafeCache) else cache

    def _run(self, operation: str, call: Callable[[], Any]) -> Any:
        try:
            return call()
        except Exception as exc:
            logger.error("billz.cache_unavailable operation=%s error=%s", operation, type(exc).__name__)
            raise BillzUnavailable("Billz: shared cache unavailable", not_applied=True) from exc

    def get(self, key: str, default: Any = None) -> Any:
        return self._run("get", lambda: self.backend.get(key, default))

    def set(self, key: str, value: Any, *, timeout: float) -> None:
        self._run("set", lambda: self.backend.set(key, value, timeout=timeout))

    def add(self, key: str, value: Any, *, timeout: float) -> bool:
        return bool(self._run("add", lambda: self.backend.add(key, value, timeout=timeout)))

    def delete(self, key: str) -> None:
        self._run("delete", lambda: self.backend.delete(key))

    def set_many(self, data: Mapping[str, Any], *, timeout: float) -> None:
        self._run("set_many", lambda: self.backend.set_many(dict(data), timeout=timeout))

    def touch(self, key: str, *, timeout: float) -> bool:
        return bool(self._run("touch", lambda: self.backend.touch(key, timeout=timeout)))

    def incr(self, key: str, delta: int = 1) -> int:
        return int(self._run("incr", lambda: self.backend.incr(key, delta)))


def _body(response: httpx.Response) -> dict[str, Any]:
    try:
        body = response.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


def _int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def error_details(body: Mapping[str, Any]) -> tuple[int | None, str, str]:
    """``(business code, string code, message)`` of any of the three Billz error bodies; the texts are
    scrubbed (review B13: Billz may echo a phone number, a NUL byte would break the dead-letter write)."""
    data = body.get("data")
    business = _int(data.get("error_code")) if isinstance(data, Mapping) else None
    error = body.get("error")
    string_code = ""
    message = ""
    if isinstance(error, Mapping):
        string_code = str(error.get("code") or "")
        message = str(error.get("message") or "")
    elif isinstance(error, str):
        string_code = error
    if isinstance(data, Mapping) and data.get("message"):
        message = str(data["message"])
    elif not message and isinstance(body.get("message"), str):
        message = str(body["message"])
    return business, clean_text(string_code, CODE_LIMIT), scrub_free_text(message, MESSAGE_LIMIT)


def classify(label: str, http_status: int, body: Mapping[str, Any], *, resource: bool = False) -> BillzError:
    """The typed error of a failed call (``label`` = ``"POST /v2/order-payment"``, no ids, no query).

    ``resource``: the call reads one resource by id (``GET /v2/order/{id}``), so a 404 means that resource is
    gone (permanent); on every other route a 404 means the route itself is wrong (configuration, D3).
    """
    business, string_code, message = error_details(body)
    if business is not None:
        reason = ERROR_REASONS.get(business, message or "unknown Billz error")
        text = f"{label}: {reason} (Billz error {business})"
        if business in RETRYABLE_ERROR_CODES:
            return BillzUnavailable(text, http_status=http_status, error_code=business, not_applied=True)
        if business in SHIFT_CLOSED_CODES:
            return BillzShiftClosed(text, http_status=http_status, error_code=business, not_applied=True)
        if business in CONFIGURATION_ERROR_CODES:
            return BillzMisconfigured(text, http_status=http_status, error_code=business, not_applied=True)
        return BillzRejected(f"{label}: {reason}", error_code=business, http_status=http_status)
    if http_status in (408, 429) or http_status >= 500 or string_code in RETRYABLE_STRING_CODES:
        # 429 / TOO_MANY_REQUESTS: Billz refused before doing anything; 408 / 5xx / TIMEOUT: unknown outcome
        refused = http_status == 429 or string_code in ("TOO_MANY_REQUESTS", "rate_limited")
        return BillzUnavailable(
            f"{label}: HTTP {http_status} {string_code}".rstrip(),
            http_status=http_status,
            not_applied=refused,
        )
    if 300 <= http_status < 400:
        return BillzMisconfigured(
            f"{label}: Billz answered a redirect (HTTP {http_status}); check BILLZ_BASE_URL",
            http_status=http_status,
            not_applied=True,
        )
    if http_status == 403:
        detail = string_code or message or "forbidden"
        return BillzMisconfigured(
            f"{label}: the integration key may not call this route ({detail}) [HTTP 403]",
            http_status=http_status,
            not_applied=True,
        )
    if http_status == 404:
        if resource:
            return BillzRejected(f"{label}: not found", http_status=http_status)
        detail = message or string_code or "not found"
        return BillzMisconfigured(
            f"{label}: {detail} [HTTP 404]; check BILLZ_BASE_URL and the routes granted to the key",
            http_status=http_status,
            not_applied=True,
        )
    detail = message or string_code or "refused"
    return BillzRejected(f"{label}: {detail}", http_status=http_status)


class RateLimiter:
    """At most ``rate`` request starts per second across every process sharing ``cache`` (module doc)."""

    def __init__(
        self,
        rate: float,
        *,
        cache: BaseCache | SafeCache | None = None,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
        prefix: str = RATE_PREFIX,
        max_wait: float = BATCH.limiter_wait,
    ) -> None:
        if rate <= 0:
            raise ValueError("rate must be positive")
        self.interval = 1.0 / rate
        self.cache = SafeCache(cache if cache is not None else default_cache)
        self.clock = clock
        self.sleep = sleep
        self.prefix = prefix
        self.max_wait = max_wait

    @property
    def _pause_key(self) -> str:
        return f"{self.prefix}:pause"

    def acquire(self, *, max_wait: float | None = None, wait_on_pause: bool = True) -> float:
        """Wait for a slot of our own and return the instant the request may start.

        Never waits longer than ``max_wait`` (default: the limiter's own); ``wait_on_pause=False`` fails at
        once while a 429 pause is in force. Either refusal is :class:`BillzUnavailable` (nothing was sent).
        """
        wait = self.max_wait if max_wait is None else max_wait
        if wait <= 0:
            raise BillzUnavailable("Billz rate limit: no time left to wait for a slot", not_applied=True)
        deadline = self.clock() + wait
        while True:
            now = self.clock()
            paused_until = self.cache.get(self._pause_key)
            if isinstance(paused_until, (int, float)) and paused_until > now:
                if not wait_on_pause or paused_until > deadline:
                    raise BillzUnavailable(
                        "Billz rate limit: paused longer than the limiter waits", not_applied=True
                    )
                self.sleep(paused_until - now)
                continue
            first = int(now / self.interval) + 1  # the next slot boundary: never mid-slot
            last = int(deadline / self.interval)
            ttl = int(wait) + 5
            for slot in range(first, last + 1):
                if self.cache.add(f"{self.prefix}:{slot}", 1, timeout=ttl):
                    start = slot * self.interval
                    self.sleep(max(0.0, start - self.clock()))
                    return start
            raise BillzUnavailable(
                "Billz rate limit: every slot the limiter may wait for is taken", not_applied=True
            )

    def pause(self, seconds: float) -> None:
        """Hold every worker back for ``seconds`` (a 429 answer). Not atomic: two 429s at once may keep the
        shorter pause, which only means the next request meets another 429 and pauses again."""
        until = self.clock() + seconds
        current = self.cache.get(self._pause_key)
        if isinstance(current, (int, float)) and current >= until:
            return
        self.cache.set(self._pause_key, until, timeout=int(seconds) + 2)


def retry_after_seconds(response: httpx.Response, *, now: float) -> float | None:
    """``Retry-After`` as seconds (delta or HTTP date), capped; ``None`` when absent or unreadable."""
    value = response.headers.get("Retry-After", "").strip()
    if not value:
        return None
    try:
        seconds = float(value)
    except ValueError:
        try:
            seconds = parsedate_to_datetime(value).timestamp() - now
        except TypeError, ValueError:
            return None
    if seconds != seconds:  # NaN
        return None
    return min(max(seconds, 0.0), RETRY_AFTER_CAP_SECONDS)


class BillzClient:
    """One call = limiter slot → request → typed outcome; tokens renewed single-flight (module doc)."""

    def __init__(
        self,
        config: BillzConfig,
        *,
        transport: httpx.BaseTransport | None = None,
        cache: BaseCache | SafeCache | None = None,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
        jitter: Callable[[], float] = random.random,
        profile: CallProfile = BATCH,
    ) -> None:
        self.config = config
        self.transport = transport
        self.cache = SafeCache(cache if cache is not None else default_cache)
        self.clock = clock
        self.sleep = sleep
        self.jitter = jitter
        self.profile = profile
        self.limiter = RateLimiter(config.rate_limit_per_second, cache=self.cache, clock=clock, sleep=sleep)
        self._deadline: float | None = None  # absolute (``clock`` time); see :meth:`deadline`

    def with_profile(self, profile: CallProfile) -> BillzClient:
        """A client sharing this one's configuration, transport, cache and clock under another profile."""
        return BillzClient(
            self.config,
            transport=self.transport,
            cache=self.cache,
            clock=self.clock,
            sleep=self.sleep,
            jitter=self.jitter,
            profile=profile,
        )

    # --- deadlines ------------------------------------------------------------------------------------------

    @contextmanager
    def deadline(self, seconds: float) -> Iterator[None]:
        """Every call, wait and sleep inside the block ends within ``seconds`` (or the enclosing deadline)."""
        previous = self._deadline
        candidate = self.clock() + seconds
        self._deadline = candidate if previous is None else min(previous, candidate)
        try:
            yield
        finally:
            self._deadline = previous

    def _remaining(self) -> float | None:
        return None if self._deadline is None else self._deadline - self.clock()

    def _check_deadline(self, label: str) -> None:
        remaining = self._remaining()
        if remaining is not None and remaining <= 0:
            raise BillzUnavailable(f"{label}: the Billz deadline passed before the request", not_applied=True)

    def _nap(self, seconds: float, label: str) -> None:
        """Sleep, unless that would cross the deadline (then :class:`BillzUnavailable`, nothing sent)."""
        remaining = self._remaining()
        if remaining is not None and seconds >= remaining:
            raise BillzUnavailable(f"{label}: the Billz deadline leaves no time to wait", not_applied=True)
        self.sleep(seconds)

    # --- transport ------------------------------------------------------------------------------------------

    def _timeout(self) -> httpx.Timeout:
        read = self.config.read_timeout
        if self.profile.read_timeout_cap is not None:
            read = min(read, self.profile.read_timeout_cap)
        connect = self.config.connect_timeout
        remaining = self._remaining()
        if remaining is not None:
            read = max(0.1, min(read, remaining))
            connect = max(0.1, min(connect, remaining))
        return httpx.Timeout(connect=connect, read=read, write=read, pool=connect)

    def _backoff(self, attempt: int) -> float:
        """Exponential backoff with full jitter in its upper half: base·2^(n-1) · [0.5, 1)."""
        ceiling = min(BACKOFF_CAP_SECONDS, BACKOFF_BASE_SECONDS * 2 ** (attempt - 1))
        return float(ceiling * (0.5 + self.jitter() / 2))

    def _check_circuit(self, label: str) -> None:
        until = self.cache.get(CIRCUIT_OPEN_KEY)
        if isinstance(until, (int, float)) and until > self.clock():
            raise BillzUnavailable(
                f"{label}: Billz is failing; calls are paused for up to {CIRCUIT_OPEN_SECONDS} s",
                not_applied=True,
            )

    def _record_failure(self, label: str) -> None:
        self.cache.add(CIRCUIT_FAILURES_KEY, 0, timeout=CIRCUIT_OPEN_SECONDS * 2)
        failures = self.cache.incr(CIRCUIT_FAILURES_KEY)
        if failures >= CIRCUIT_FAILURES:
            self.cache.set(
                CIRCUIT_OPEN_KEY, self.clock() + CIRCUIT_OPEN_SECONDS, timeout=CIRCUIT_OPEN_SECONDS + 5
            )
            self.cache.delete(CIRCUIT_FAILURES_KEY)
            logger.error(
                "billz.circuit_open after %d failures in a row (last: %s); calls fail fast for %d s",
                failures,
                label,
                CIRCUIT_OPEN_SECONDS,
            )

    def _record_answer(self) -> None:
        self.cache.delete(CIRCUIT_FAILURES_KEY)

    def _send(
        self,
        method: str,
        path: str,
        label: str,
        *,
        params: Mapping[str, Any] | None,
        json: Any,
        headers: Mapping[str, str],
    ) -> httpx.Response:
        self._check_deadline(label)
        self._check_circuit(label)
        remaining = self._remaining()
        wait = self.profile.limiter_wait if remaining is None else min(self.profile.limiter_wait, remaining)
        self.limiter.acquire(max_wait=wait, wait_on_pause=self.profile.wait_on_pause)
        with httpx.Client(timeout=self._timeout(), transport=self.transport) as http:
            return http.request(
                method, f"{self.config.base_url}{path}", params=params, json=json, headers=dict(headers)
            )

    def _exchange(
        self,
        method: str,
        path: str,
        label: str,
        *,
        params: Mapping[str, Any] | None = None,
        json: Any = None,
        headers: Mapping[str, str] | None = None,
        idempotent: bool,
        max_attempts: int | None = None,
    ) -> httpx.Response:
        """Send with the retries of the module doc; returns any response that is not 429/5xx-retried."""
        attempts_allowed = max_attempts if max_attempts is not None else self.profile.max_attempts
        attempt = 0
        throttled = 0
        while True:
            try:
                response = self._send(method, path, label, params=params, json=json, headers=headers or {})
            except httpx.TransportError as exc:
                never_sent = isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout))
                self._record_failure(label)
                attempt += 1
                if (idempotent or never_sent) and attempt < attempts_allowed:
                    self._nap(self._backoff(attempt), label)
                    continue
                raise BillzUnavailable(f"{label}: {type(exc).__name__}", not_applied=never_sent) from exc
            except httpx.HTTPError as exc:
                # the answer arrived but could not be read (a corrupt gzip body …): unknown outcome
                self._record_failure(label)
                raise BillzUnavailable(f"{label}: unreadable answer ({type(exc).__name__})") from exc
            if response.status_code == 429:
                self._record_answer()
                throttled += 1
                if throttled > self.profile.max_throttled:
                    raise BillzUnavailable(
                        f"{label}: rate limited by Billz (HTTP 429)", http_status=429, not_applied=True
                    )
                delay = retry_after_seconds(response, now=self.clock())
                self.limiter.pause(delay if delay is not None else self._backoff(throttled))
                continue
            if response.status_code >= 500 or response.status_code == 408:
                self._record_failure(label)
            else:
                self._record_answer()
            if response.status_code >= 500 and idempotent:
                attempt += 1
                if attempt < attempts_allowed:
                    self._nap(self._backoff(attempt), label)
                    continue
            return response

    # --- tokens ---------------------------------------------------------------------------------------------

    def _stored(self) -> dict[str, Any] | None:
        value = self.cache.get(TOKENS_KEY)
        return value if isinstance(value, dict) and value.get("access") else None

    def _valid_access(self, tokens: Mapping[str, Any] | None, *, stale: str | None = None) -> str | None:
        """The stored access token unless it is missing, the ``stale`` one or about to expire."""
        if tokens is None:
            return None
        access = tokens.get("access")
        if not isinstance(access, str) or not access or access == stale:
            return None
        if float(tokens.get("access_expires_at", 0)) - ACCESS_MARGIN_SECONDS <= self.clock():
            return None
        return access

    @staticmethod
    def _lifetime(value: Any, default: int, minimum: int) -> int:
        seconds = _int(value)
        if seconds is None or seconds <= 0:
            seconds = default
        return max(minimum, min(seconds, MAX_TTL_SECONDS))

    def _store(self, data: Mapping[str, Any], owner: str) -> dict[str, Any]:
        access = data.get("access_token")
        if not isinstance(access, str) or not access:
            raise BillzUnavailable("Billz auth: the answer carries no access token")
        now = self.clock()
        expires_in = self._lifetime(
            data.get("expires_in"), DEFAULT_ACCESS_TTL_SECONDS, MIN_ACCESS_TTL_SECONDS
        )
        refresh = data.get("refresh_token")
        refresh_in = self._lifetime(data.get("refresh_expires_in"), DEFAULT_REFRESH_TTL_SECONDS, 60)
        access_expires_at = now + expires_in
        refresh_expires_at = now + refresh_in if isinstance(refresh, str) and refresh else 0.0
        tokens: dict[str, Any] = {
            "access": access,
            "access_expires_at": access_expires_at,
            "refresh": refresh if isinstance(refresh, str) else "",
            "refresh_expires_at": refresh_expires_at,
        }
        if self.cache.get(LOCK_KEY) == owner:
            ttl = max(int(max(access_expires_at, refresh_expires_at) - now), 60)
            self.cache.set(TOKENS_KEY, tokens, timeout=ttl)
        else:  # the lock expired under us: another worker renews now; its pair wins, ours is used once
            logger.warning("Billz token renewal outlived its lock; the new pair is not stored")
        return tokens

    def _login(self, owner: str) -> dict[str, Any]:
        if not self.config.secret_key:
            raise BillzAuthError("Billz auth: BILLZ_SECRET_KEY is not set", not_applied=True)
        label = "POST /v1/auth/login"
        response = self._exchange(
            "POST",
            "/v1/auth/login",
            label,
            json={"secret_token": self.config.secret_key},
            headers={"platform-id": self.config.platform_id},
            idempotent=True,
            max_attempts=2,
        )
        body = _body(response)
        if response.status_code != 200 or not isinstance(body.get("data"), Mapping):
            if 300 <= response.status_code < 400:
                raise self._failure(label, response.status_code, body, False)
            if 400 <= response.status_code < 500:
                logger.error("billz.configuration_error %s: Billz refused the integration key", label)
                raise BillzAuthError(
                    f"{label}: Billz refused the integration key (HTTP {response.status_code})",
                    http_status=response.status_code,
                    not_applied=True,
                )
            raise BillzUnavailable(f"{label}: HTTP {response.status_code}", http_status=response.status_code)
        logger.info("Billz login succeeded")
        return self._store(body["data"], owner)

    def _refresh(self, refresh_token: str, owner: str) -> dict[str, Any] | None:
        """The rotated pair, or ``None`` when the refresh failed in any way (then the caller logs in).

        Sent once, never repeated: the token was already removed from the cache, and a refresh whose answer
        was lost must not be presented again (Billz would revoke the whole session family).
        """
        label = "POST /v2/auth/refresh"
        try:
            response = self._exchange(
                "POST",
                "/v2/auth/refresh",
                label,
                json={"refresh_token": refresh_token},
                headers={"platform-id": self.config.platform_id},
                idempotent=False,
                max_attempts=1,
            )
        except BillzUnavailable as exc:
            if exc.not_applied:
                raise  # nothing was sent (deadline, limiter, circuit, cache): no login either
            logger.warning("Billz refresh failed (%s); logging in instead", exc)
            return None
        body = _body(response)
        if response.status_code == 200 and isinstance(body.get("data"), Mapping):
            logger.info("Billz token refreshed")
            return self._store(body["data"], owner)
        _, string_code, _ = error_details(body)
        logger.warning(
            "Billz refused the refresh token (HTTP %s %s); logging in again",
            response.status_code,
            string_code or "refused",
        )
        return None

    def _renew_locked(self, owner: str, stale: str | None) -> str:
        current = self._stored()  # renewed between our read and the lock
        access = self._valid_access(current, stale=stale)
        if access is not None:
            return access
        tokens = None
        refresh = str(current.get("refresh") or "") if current is not None else ""
        if current is not None and refresh and float(current.get("refresh_expires_at", 0)) > self.clock():
            # consume the token before presenting it: no other worker can ever present it again
            self.cache.set(
                TOKENS_KEY, {**current, "refresh": "", "refresh_expires_at": 0.0}, timeout=LOCK_TTL_SECONDS
            )
            tokens = self._refresh(refresh, owner)
        if tokens is None:
            tokens = self._login(owner)
        return str(tokens["access"])

    def renew(self, *, stale: str | None = None) -> str:
        """A fresh access token, renewed by one process at a time (refresh, else login; module doc)."""
        remaining = self._remaining()
        wait = self.profile.renew_wait if remaining is None else min(self.profile.renew_wait, remaining)
        give_up_at = self.clock() + wait
        owner = uuid.uuid4().hex
        while True:
            access = self._valid_access(self._stored(), stale=stale)
            if access is not None:
                return access
            if self.cache.add(LOCK_KEY, owner, timeout=LOCK_TTL_SECONDS):
                try:
                    # bounded well inside the lock's TTL, so the lock cannot expire while we renew
                    with self.deadline(min(RENEW_BOUND_SECONDS, LOCK_TTL_SECONDS / 2)):
                        return self._renew_locked(owner, stale)
                finally:
                    if self.cache.get(LOCK_KEY) == owner:
                        self.cache.delete(LOCK_KEY)
            if self.clock() >= give_up_at:
                raise BillzUnavailable(
                    "Billz auth: another worker is renewing the token for too long", not_applied=True
                )
            self._nap(LOCK_POLL_SECONDS, "Billz auth")

    def access_token(self) -> str:
        current = self._stored()
        access = self._valid_access(current)
        if access is not None:
            return access
        return self.renew(stale=str(current["access"]) if current else None)

    # --- calls ----------------------------------------------------------------------------------------------

    def call(
        self,
        method: str,
        path: str,
        *,
        label: str,
        params: Mapping[str, Any] | None = None,
        json: Any = None,
        idempotent: bool,
        wait_result: bool = False,
        resource: bool = False,
    ) -> dict[str, Any]:
        """An authenticated call; ``wait_result`` asks an async method for its result over HTTP (the
        ``Billz-Response-Channel: HTTP`` header **and** query parameter, both documented).

        Returns the parsed body of a success (for an async method: the envelope, whose ``data`` carries the
        result). Raises :class:`BillzUnavailable` / :class:`BillzRejected` (module doc); ``resource`` marks a
        read of one resource by id (a 404 is then permanent, :func:`classify`).
        """
        if self.profile.call_budget is None:
            return self._call(method, path, label, params, json, idempotent, wait_result, resource)
        with self.deadline(self.profile.call_budget):
            return self._call(method, path, label, params, json, idempotent, wait_result, resource)

    def _call(
        self,
        method: str,
        path: str,
        label: str,
        params: Mapping[str, Any] | None,
        json: Any,
        idempotent: bool,
        wait_result: bool,
        resource: bool,
    ) -> dict[str, Any]:
        headers = {RESPONSE_CHANNEL_HEADER: "HTTP"} if wait_result else {}
        query = {**(params or {}), RESPONSE_CHANNEL_HEADER: "HTTP"} if wait_result else params
        token = self._token(self.access_token)
        renewed = False
        while True:
            response = self._exchange(
                method,
                path,
                label,
                params=query,
                json=json,
                headers={**headers, "Authorization": f"Bearer {token}"},
                idempotent=idempotent,
            )
            if response.status_code == 401:
                if renewed:
                    raise BillzUnavailable(
                        f"{label}: Billz refused a freshly renewed token (HTTP 401)",
                        http_status=401,
                        not_applied=True,
                    )
                renewed = True
                token = self._token(partial(self.renew, stale=token))
                continue
            body = _body(response)
            status = response.status_code
            if wait_result and isinstance(body.get("status_code"), int):
                status = max(status, int(body["status_code"]))
            if status >= 300:
                raise self._failure(label, status, body, resource)
            if wait_result and set(body) <= {"message"}:
                # the event was published but its result did not come back on the HTTP channel
                raise BillzUnavailable(f"{label}: Billz answered without the result (correlation id only)")
            if wait_result:
                _, string_code, _ = error_details(body)
                if string_code:
                    raise self._failure(label, 500 if string_code == "TIMEOUT" else 400, body, resource)
            return body

    @staticmethod
    def _token(get: Callable[[], str]) -> str:
        """A token for the call; a failure to get one means the call itself was never sent."""
        try:
            return get()
        except BillzError as exc:
            exc.not_applied = True
            raise

    @staticmethod
    def _failure(label: str, status: int, body: Mapping[str, Any], resource: bool) -> BillzError:
        error = classify(label, status, body, resource=resource)
        if isinstance(error, BillzMisconfigured):
            logger.error("billz.configuration_error %s", error)
        return error


__all__ = [
    "BATCH",
    "INTERACTIVE",
    "BillzAuthError",
    "BillzClient",
    "BillzConfig",
    "CallProfile",
    "RateLimiter",
    "SafeCache",
    "classify",
    "error_details",
    "retry_after_seconds",
]
