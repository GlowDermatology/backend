"""HTTP plumbing of the live Billz adapter: configuration, auth, the shared rate limiter, retries, errors.

Source of truth: the BILLZ 2.0 public API ``openapi.yaml`` (docs.billz.io); docs/integrations/billz.md maps
every call. Nothing here was verified against a live Billz account.

**Auth** (``post_v1_auth_login``, ``post_v2_auth_refresh``): ``POST /v1/auth/login {"secret_token"}`` →
``access_token`` (``expires_in`` seconds, 15 days for a key login) + a one-use ``refresh_token`` (30 days);
``POST /v2/auth/refresh {"refresh_token"}`` with the ``platform-id`` header (the only call that needs it) →
a new pair (rotation: a refresh token is exchanged once; presenting a spent one revokes the whole session
family). The pair lives **only** in the Django cache (Redis DB 0, :data:`TOKENS_KEY`), never in the
database or a log line. Renewal is single-flight across processes: the winner of ``cache.add(LOCK_KEY)``
(short TTL) refreshes — or logs in when there is no usable refresh token or Billz refuses it — and stores the
new pair; the others re-read the cache until the pair changes. A 401 on a normal call renews once and
repeats the call once; a second 401 is :class:`BillzUnavailable`, never a loop.

**Rate limit**: Billz answers 429 above ~2 requests/second per IP, counted over the whole integration, so
every worker shares one limiter in the cache: time is cut into slots of ``1 / rate`` seconds and a request
may start only at the beginning of a slot it won with ``cache.add`` (atomic ``SET NX`` on Redis), so two
requests never start closer than one slot apart, whichever process sends them (hosts need NTP-synced
clocks). A 429 pauses every worker (``Retry-After`` when Billz sends one, exponential backoff with jitter
otherwise), at most :data:`MAX_THROTTLED` times per call.

**Retries**: transport errors and 5xx are retried (bounded, exponential backoff with jitter) only for calls
that are safe to repeat — reads and absolute upserts — or when the request certainly never reached Billz
(connect errors). A write whose outcome is unknown (read timeout, 5xx, 408 of an async method) raises
:class:`BillzUnavailable` instead; the sale push re-reads the sale before repeating anything.

**Errors**: classic ``{"code", "message", "error", "data"}`` bodies, ``{"error": {"code", "message"}}`` and
the async envelope ``{"status_code", "error", "data": {"error_code", …}}`` are all read; the business code
(``data.error_code``) is mapped by :func:`classify` into retryable (:class:`BillzUnavailable`) or permanent
(:class:`BillzRejected`). Messages carry the method and the path template, never a query string (a phone
number), a body, a token or the key.
"""

from __future__ import annotations

import logging
import random
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
from typing import Any

import httpx
from django.conf import settings
from django.core.cache import cache as default_cache
from django.core.cache.backends.base import BaseCache

from .ports import BillzError, BillzRejected, BillzUnavailable

logger = logging.getLogger(__name__)

TOKENS_KEY = "billz:auth:tokens"  # noqa: S105 - a cache key name, not a secret
LOCK_KEY = "billz:auth:lock"
RATE_PREFIX = "billz:rate"
# Renewal: the lock outlives one login (connect + read timeouts); waiters give up after LOCK_WAIT_SECONDS.
LOCK_TTL_SECONDS = 90
LOCK_WAIT_SECONDS = 45.0
LOCK_POLL_SECONDS = 0.2
# Renew an access token this long before Billz says it expires.
ACCESS_MARGIN_SECONDS = 300
# A key login's refresh token lives 30 days (docs); used when a refresh answer has no refresh_expires_in.
DEFAULT_REFRESH_TTL_SECONDS = 30 * 86_400
# Retries of idempotent calls on transport errors / 5xx (attempts in total) and of 429 answers.
MAX_ATTEMPTS = 4
MAX_THROTTLED = 5
BACKOFF_BASE_SECONDS = 0.5
BACKOFF_CAP_SECONDS = 8.0
RETRY_AFTER_CAP_SECONDS = 60.0
# The limiter refuses to queue a request further than this into the future (BillzUnavailable instead).
RATE_MAX_WAIT_SECONDS = 60.0
RESPONSE_CHANNEL_HEADER = "Billz-Response-Channel"

# Business codes of the sale methods (llms-full "Коды ответов и ошибки", "Интернет-заказ" step 7). Retryable:
# the next attempt can succeed on its own (an async recalculation still running, a bill that changed under
# us, items an async add has not applied yet). Everything else — and every unknown code — is permanent:
# staff must act (open the shift, fix the product, check the sale) and the outbox message goes dead at once.
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
    rate_limit_per_second: float = 2.0
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
            base_url=str(settings.BILLZ_BASE_URL).rstrip("/"),
            secret_key=str(settings.BILLZ_SECRET_KEY),
            platform_id=str(settings.BILLZ_PLATFORM_ID),
            shop_id=str(settings.BILLZ_SHOP_ID),
            cashbox_id=str(settings.BILLZ_CASHBOX_ID),
            payment_type_id=str(settings.BILLZ_PAYMENT_TYPE_ID),
            rate_limit_per_second=float(settings.BILLZ_RATE_LIMIT_PER_SECOND),
            connect_timeout=float(settings.BILLZ_CONNECT_TIMEOUT_SECONDS),
            read_timeout=float(settings.BILLZ_READ_TIMEOUT_SECONDS),
            page_size=int(settings.BILLZ_PAGE_SIZE),
            full_sync_min_ratio=float(settings.BILLZ_FULL_SYNC_MIN_RATIO),
            volume_field=str(settings.BILLZ_VOLUME_FIELD),
        )


class BillzAuthError(BillzUnavailable):
    """Billz refused the integration key (login 4xx): an operator must check ``BILLZ_SECRET_KEY``."""


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
    """``(business code, string code, message)`` of any of the three Billz error bodies."""
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
    return business, string_code, message[:300]


def classify(label: str, http_status: int, body: Mapping[str, Any]) -> BillzError:
    """The typed error of a failed call (``label`` = ``"POST /v2/order-payment"``, no ids, no query)."""
    business, string_code, message = error_details(body)
    if business is not None:
        reason = ERROR_REASONS.get(business, message or "unknown Billz error")
        text = f"{label}: {reason}"
        if business in RETRYABLE_ERROR_CODES:
            return BillzUnavailable(f"{text} (Billz error {business})")
        return BillzRejected(text, error_code=business, http_status=http_status)
    if http_status in (408, 429) or http_status >= 500 or string_code in RETRYABLE_STRING_CODES:
        return BillzUnavailable(f"{label}: HTTP {http_status} {string_code}".rstrip())
    if http_status == 403:
        detail = string_code or message or "forbidden"
        return BillzRejected(
            f"{label}: the integration key may not call this route ({detail})", http_status=http_status
        )
    if http_status == 404:
        return BillzRejected(f"{label}: not found", http_status=http_status)
    detail = message or string_code or "refused"
    return BillzRejected(f"{label}: {detail}", http_status=http_status)


class RateLimiter:
    """At most ``rate`` request starts per second across every process sharing ``cache`` (module doc)."""

    def __init__(
        self,
        rate: float,
        *,
        cache: BaseCache | None = None,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
        prefix: str = RATE_PREFIX,
        max_wait: float = RATE_MAX_WAIT_SECONDS,
    ) -> None:
        if rate <= 0:
            raise ValueError("rate must be positive")
        self.interval = 1.0 / rate
        self.cache = cache if cache is not None else default_cache
        self.clock = clock
        self.sleep = sleep
        self.prefix = prefix
        self.max_wait = max_wait

    @property
    def _pause_key(self) -> str:
        return f"{self.prefix}:pause"

    def acquire(self) -> float:
        """Wait for a slot of our own and return the instant the request may start."""
        deadline = self.clock() + self.max_wait
        while True:
            now = self.clock()
            paused_until = self.cache.get(self._pause_key)
            if isinstance(paused_until, (int, float)) and paused_until > now:
                if paused_until > deadline:
                    raise BillzUnavailable("Billz rate limit: paused longer than the limiter waits")
                self.sleep(paused_until - now)
                continue
            first = int(now / self.interval) + 1  # the next slot boundary: never mid-slot
            last = int(deadline / self.interval)
            ttl = int(self.max_wait) + 5
            for slot in range(first, last + 1):
                if self.cache.add(f"{self.prefix}:{slot}", 1, timeout=ttl):
                    start = slot * self.interval
                    self.sleep(max(0.0, start - self.clock()))
                    return start
            raise BillzUnavailable("Billz rate limit: every slot of the next minute is taken")

    def pause(self, seconds: float) -> None:
        """Hold every worker back for ``seconds`` (a 429 answer)."""
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
        cache: BaseCache | None = None,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
        jitter: Callable[[], float] = random.random,
    ) -> None:
        self.config = config
        self.transport = transport
        self.cache = cache if cache is not None else default_cache
        self.clock = clock
        self.sleep = sleep
        self.jitter = jitter
        self.limiter = RateLimiter(config.rate_limit_per_second, cache=self.cache, clock=clock, sleep=sleep)

    # --- transport ------------------------------------------------------------------------------------------

    def _timeout(self) -> httpx.Timeout:
        return httpx.Timeout(
            connect=self.config.connect_timeout,
            read=self.config.read_timeout,
            write=self.config.read_timeout,
            pool=self.config.connect_timeout,
        )

    def _backoff(self, attempt: int) -> float:
        """Exponential backoff with full jitter in its upper half: base·2^(n-1) · [0.5, 1)."""
        ceiling = min(BACKOFF_CAP_SECONDS, BACKOFF_BASE_SECONDS * 2 ** (attempt - 1))
        return float(ceiling * (0.5 + self.jitter() / 2))

    def _send(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None,
        json: Any,
        headers: Mapping[str, str],
    ) -> httpx.Response:
        self.limiter.acquire()
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
    ) -> httpx.Response:
        """Send with the retries of the module doc; returns any response that is not 429/5xx-retried."""
        attempt = 0
        throttled = 0
        while True:
            try:
                response = self._send(method, path, params=params, json=json, headers=headers or {})
            except httpx.TransportError as exc:
                never_sent = isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout))
                attempt += 1
                if (idempotent or never_sent) and attempt < MAX_ATTEMPTS:
                    self.sleep(self._backoff(attempt))
                    continue
                raise BillzUnavailable(f"{label}: {type(exc).__name__}") from exc
            if response.status_code == 429:
                throttled += 1
                if throttled > MAX_THROTTLED:
                    raise BillzUnavailable(f"{label}: rate limited by Billz (HTTP 429)")
                delay = retry_after_seconds(response, now=self.clock())
                self.limiter.pause(delay if delay is not None else self._backoff(throttled))
                continue
            if response.status_code >= 500 and idempotent:
                attempt += 1
                if attempt < MAX_ATTEMPTS:
                    self.sleep(self._backoff(attempt))
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

    def _store(self, data: Mapping[str, Any], *, previous_refresh: str = "") -> dict[str, Any]:
        access = data.get("access_token")
        if not isinstance(access, str) or not access:
            raise BillzUnavailable("Billz auth: the answer carries no access token")
        now = self.clock()
        expires_in = _int(data.get("expires_in")) or 0
        refresh = data.get("refresh_token")
        refresh_in = _int(data.get("refresh_expires_in")) or DEFAULT_REFRESH_TTL_SECONDS
        access_expires_at = now + expires_in
        refresh_expires_at = now + refresh_in if isinstance(refresh, str) and refresh else 0.0
        tokens: dict[str, Any] = {
            "access": access,
            "access_expires_at": access_expires_at,
            "refresh": refresh if isinstance(refresh, str) else "",
            "refresh_expires_at": refresh_expires_at,
        }
        ttl = max(int(max(access_expires_at, refresh_expires_at) - now), 60)
        self.cache.set(TOKENS_KEY, tokens, timeout=ttl)
        return tokens

    def _login(self) -> dict[str, Any]:
        if not self.config.secret_key:
            raise BillzAuthError("Billz auth: BILLZ_SECRET_KEY is not set")
        label = "POST /v1/auth/login"
        response = self._exchange(
            "POST", "/v1/auth/login", label, json={"secret_token": self.config.secret_key}, idempotent=True
        )
        body = _body(response)
        if response.status_code != 200 or not isinstance(body.get("data"), Mapping):
            if 400 <= response.status_code < 500:
                raise BillzAuthError(
                    f"{label}: Billz refused the integration key (HTTP {response.status_code})"
                )
            raise BillzUnavailable(f"{label}: HTTP {response.status_code}")
        logger.info("Billz login succeeded")
        return self._store(body["data"])

    def _refresh(self, refresh_token: str) -> dict[str, Any] | None:
        """The rotated pair, or ``None`` when Billz refused the refresh token (expired, reused, revoked)."""
        label = "POST /v2/auth/refresh"
        # Billz hands the same new pair to every refresh with one token within 5 s, so a refresh whose answer
        # was lost is repeated quickly; after that a refused one falls back to a login.
        response = self._exchange(
            "POST",
            "/v2/auth/refresh",
            label,
            json={"refresh_token": refresh_token},
            headers={"platform-id": self.config.platform_id},
            idempotent=True,
        )
        body = _body(response)
        if response.status_code == 200 and isinstance(body.get("data"), Mapping):
            logger.info("Billz token refreshed")
            return self._store(body["data"])
        if response.status_code in (400, 401, 403):
            _, string_code, _ = error_details(body)
            logger.warning("Billz refused the refresh token (%s); logging in again", string_code or "refused")
            return None
        raise BillzUnavailable(f"{label}: HTTP {response.status_code}")

    def renew(self, *, stale: str | None = None) -> str:
        """A fresh access token, renewed by one process at a time (refresh, else login)."""
        deadline = self.clock() + LOCK_WAIT_SECONDS
        owner = uuid.uuid4().hex
        while True:
            access = self._valid_access(self._stored(), stale=stale)
            if access is not None:
                return access
            if self.cache.add(LOCK_KEY, owner, timeout=LOCK_TTL_SECONDS):
                try:
                    current = self._stored()  # renewed between our read and the lock
                    access = self._valid_access(current, stale=stale)
                    if access is not None:
                        return access
                    tokens = None
                    if current is not None and current.get("refresh"):
                        if float(current.get("refresh_expires_at", 0)) > self.clock():
                            tokens = self._refresh(str(current["refresh"]))
                    if tokens is None:
                        tokens = self._login()
                    return str(tokens["access"])
                finally:
                    if self.cache.get(LOCK_KEY) == owner:
                        self.cache.delete(LOCK_KEY)
            if self.clock() >= deadline:
                raise BillzUnavailable("Billz auth: another worker is renewing the token for too long")
            self.sleep(LOCK_POLL_SECONDS)

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
    ) -> dict[str, Any]:
        """An authenticated call; ``wait_result`` sends ``Billz-Response-Channel: HTTP`` (async methods).

        Returns the parsed body of a success (for an async method: the envelope, whose ``id`` / ``data``
        carry the result). Raises :class:`BillzUnavailable` / :class:`BillzRejected` (module doc).
        """
        headers = {RESPONSE_CHANNEL_HEADER: "HTTP"} if wait_result else {}
        token = self.access_token()
        renewed = False
        while True:
            response = self._exchange(
                method,
                path,
                label,
                params=params,
                json=json,
                headers={**headers, "Authorization": f"Bearer {token}"},
                idempotent=idempotent,
            )
            if response.status_code == 401:
                if renewed:
                    raise BillzUnavailable(f"{label}: Billz refused a freshly renewed token (HTTP 401)")
                renewed = True
                token = self.renew(stale=token)
                continue
            body = _body(response)
            status = response.status_code
            if wait_result and isinstance(body.get("status_code"), int):
                status = max(status, int(body["status_code"]))
            if status >= 300:
                raise classify(label, status, body)
            if wait_result and set(body) <= {"message"}:
                # the event was published but its result did not come back on the HTTP channel
                raise BillzUnavailable(f"{label}: Billz answered without the result (correlation id only)")
            if wait_result:
                _, string_code, _ = error_details(body)
                if string_code:
                    raise classify(label, 500 if string_code == "TIMEOUT" else 400, body)
            return body
