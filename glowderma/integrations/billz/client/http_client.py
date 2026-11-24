"""The Billz HTTP client: auth with single-flight renewal, the limiter, retries with backoff, deadlines and
the circuit breaker."""

from __future__ import annotations

import logging
import random
import time
import uuid
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from functools import partial
from typing import Any

import httpx
from django.core.cache import cache as default_cache
from django.core.cache.backends.base import BaseCache

from glowderma.core import alerts
from glowderma.integrations.billz.client.config import BATCH, BillzConfig, CallProfile
from glowderma.integrations.billz.client.error_mapping import (
    BillzAuthError,
    classify,
    error_details,
    int_or_none,
    json_body,
)
from glowderma.integrations.billz.client.rate_limiter import RateLimiter, retry_after_seconds
from glowderma.integrations.billz.client.shared_cache import SafeCache
from glowderma.integrations.billz.ports import (
    BillzError,
    BillzMisconfigured,
    BillzOutOfTime,
    BillzUnavailable,
)

logger = logging.getLogger("glowderma.integrations.billz.client")


TOKENS_KEY = "billz:auth:tokens"  # noqa: S105 - a cache key name, not a secret


LOCK_KEY = "billz:auth:lock"


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


# The circuit opens after this many failures in a row and stays open this long.
CIRCUIT_FAILURES = 5


# A timeout that ends this close to the active deadline was capped by it (the budget ran out, not Billz).
DEADLINE_SLACK_SECONDS = 0.2


CIRCUIT_OPEN_SECONDS = 90


RESPONSE_CHANNEL_HEADER = "Billz-Response-Channel"


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

    def _deadline_reached(self) -> bool:
        """The active deadline has (all but) passed: a timeout capped by it is the budget, not Billz."""
        remaining = self._remaining()
        return remaining is not None and remaining < DEADLINE_SLACK_SECONDS

    def _check_deadline(self, label: str) -> None:
        remaining = self._remaining()
        if remaining is not None and remaining <= 0:
            raise BillzOutOfTime(f"{label}: the Billz deadline passed before the request", not_applied=True)

    def _nap(self, seconds: float, label: str) -> None:
        """Sleep, unless that would cross the deadline (then :class:`BillzUnavailable`, nothing sent)."""
        remaining = self._remaining()
        if remaining is not None and seconds >= remaining:
            raise BillzOutOfTime(f"{label}: the Billz deadline leaves no time to wait", not_applied=True)
        self.sleep(seconds)

    def _retry_nap(self, seconds: float, label: str, failure: str) -> None:
        """The backoff before a retry of a failed request: no time left for it is Billz failing (the failure
        it follows), not the budget running out (:class:`BillzOutOfTime` is for a healthy Billz)."""
        try:
            self._nap(seconds, label)
        except BillzOutOfTime as exc:
            raise BillzUnavailable(f"{label}: {failure}; no time left to retry", not_applied=True) from exc

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
        """Count a failure towards the circuit breaker. Best effort: it runs after a request went out, so a
        cache error here is logged and swallowed — it must never decide the request's outcome (NEW-1)."""
        try:
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
                alerts.alert(
                    "billz.circuit_open", {"failures": failures, "open_seconds": CIRCUIT_OPEN_SECONDS}
                )
        except BillzUnavailable:
            logger.warning("billz.circuit_bookkeeping_skipped %s: the shared cache is unavailable", label)

    def _record_answer(self, label: str) -> None:
        """An answer resets the failure count (an open circuit still runs its time). Best effort, like
        :meth:`_record_failure`."""
        try:
            self.cache.delete(CIRCUIT_FAILURES_KEY)
        except BillzUnavailable:
            logger.warning("billz.circuit_bookkeeping_skipped %s: the shared cache is unavailable", label)

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
            request = http.build_request(
                method, f"{self.config.base_url}{path}", params=params, json=json, headers=dict(headers)
            )
            response = http.send(request, stream=True)
            try:
                return self._read_within_deadline(request, response)
            finally:
                response.close()

    def _read_within_deadline(self, request: httpx.Request, response: httpx.Response) -> httpx.Response:
        """Read the body, giving up once the deadline passed while it still drips in (review NEW-9).

        The read timeout bounds each wait for the next chunk only; an answer sent a few bytes at a time could
        otherwise hold a push far past its deadline (and the outbox lease). Past the deadline the read is
        abandoned as a :class:`httpx.ReadTimeout` — an unknown outcome, like any other read timeout.
        """
        body = bytearray()
        for chunk in response.iter_bytes():  # decoded (a corrupt gzip body raises httpx.DecodingError)
            body.extend(chunk)
            remaining = self._remaining()
            if remaining is not None and remaining < 0:
                raise httpx.ReadTimeout(
                    "the answer was still arriving at the Billz deadline", request=request
                )
        headers = [
            (name, value)
            for name, value in response.headers.multi_items()
            if name.lower() not in ("content-encoding", "content-length", "transfer-encoding")
        ]
        return httpx.Response(
            response.status_code,
            headers=headers,
            content=bytes(body),
            request=request,
            extensions=response.extensions,
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
        """Send with the retries of the module doc; returns any response that is not 429/5xx-retried.

        ``not_applied`` (review NEW-1): once a request of this exchange may have reached Billz (a transport
        error other than a connect error, an unreadable answer, a 5xx/408 answer), no error raised by the
        exchange afterwards — a retry that cannot start, a cache failure, the deadline — says "not applied":
        the caller must treat the outcome as unknown (a payment keeps its attempt stamp).
        """
        attempts_allowed = max_attempts if max_attempts is not None else self.profile.max_attempts
        attempt = 0
        throttled = 0
        maybe_applied = False
        try:
            while True:
                try:
                    response = self._send(
                        method, path, label, params=params, json=json, headers=headers or {}
                    )
                except httpx.TransportError as exc:
                    never_sent = isinstance(
                        exc, (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)
                    )
                    maybe_applied = maybe_applied or not never_sent
                    self._record_failure(label)
                    if isinstance(exc, httpx.TimeoutException) and self._deadline_reached():
                        # the timeout was capped by the deadline: our budget ran out (no time to retry)
                        out_of_time = BillzOutOfTime(
                            f"{label}: {type(exc).__name__} at the Billz deadline", not_applied=never_sent
                        )
                        out_of_time.cut_short = True
                        raise out_of_time from exc
                    attempt += 1
                    if (idempotent or never_sent) and attempt < attempts_allowed:
                        self._retry_nap(self._backoff(attempt), label, type(exc).__name__)
                        continue
                    raise BillzUnavailable(f"{label}: {type(exc).__name__}", not_applied=never_sent) from exc
                except httpx.HTTPError as exc:
                    # the answer arrived but could not be read (a corrupt gzip body …): unknown outcome
                    maybe_applied = True
                    self._record_failure(label)
                    raise BillzUnavailable(f"{label}: unreadable answer ({type(exc).__name__})") from exc
                # anything but a 429 may have been applied (a 5xx retried below, a 4xx returned)
                maybe_applied = maybe_applied or response.status_code != 429
                outcome = self._answered(response, label, attempt, attempts_allowed, throttled, idempotent)
                if outcome is None:
                    return response
                attempt, throttled = outcome
        except BillzError as exc:
            if maybe_applied:
                exc.not_applied = False
            raise

    def _answered(
        self,
        response: httpx.Response,
        label: str,
        attempt: int,
        attempts_allowed: int,
        throttled: int,
        idempotent: bool,
    ) -> tuple[int, int] | None:
        """Book an answer; ``None`` → return it, else ``(attempt, throttled)`` of the retry to send."""
        if response.status_code == 429:
            self._record_answer(label)
            throttled += 1
            if throttled > self.profile.max_throttled:
                raise BillzOutOfTime(
                    f"{label}: rate limited by Billz (HTTP 429)", http_status=429, not_applied=True
                )
            delay = retry_after_seconds(response, now=self.clock())
            self.limiter.pause(delay if delay is not None else self._backoff(throttled))
            return attempt, throttled
        if response.status_code >= 500 or response.status_code == 408:
            self._record_failure(label)
        else:
            self._record_answer(label)
        if response.status_code >= 500 and idempotent:
            attempt += 1
            if attempt < attempts_allowed:
                self._retry_nap(self._backoff(attempt), label, f"HTTP {response.status_code}")
                return attempt, throttled
        return None

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
        seconds = int_or_none(value)
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
        body = json_body(response)
        if response.status_code != 200 or not isinstance(body.get("data"), Mapping):
            if 300 <= response.status_code < 400:
                raise self._failure(label, response.status_code, body, False)
            if 400 <= response.status_code < 500:
                logger.error("billz.configuration_error %s: Billz refused the integration key", label)
                alerts.alert(
                    "billz.configuration_error", {"case": "key_refused", "http_status": response.status_code}
                )
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
        body = json_body(response)
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
            body = json_body(response)
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
            alerts.alert(
                "billz.configuration_error",
                {"case": "refused", "http_status": status, "error_code": error.error_code},
            )
        return error


__all__ = [
    "ACCESS_MARGIN_SECONDS",
    "BACKOFF_BASE_SECONDS",
    "BACKOFF_CAP_SECONDS",
    "CIRCUIT_FAILURES",
    "CIRCUIT_FAILURES_KEY",
    "CIRCUIT_OPEN_KEY",
    "CIRCUIT_OPEN_SECONDS",
    "DEADLINE_SLACK_SECONDS",
    "DEFAULT_ACCESS_TTL_SECONDS",
    "DEFAULT_REFRESH_TTL_SECONDS",
    "LOCK_KEY",
    "LOCK_POLL_SECONDS",
    "LOCK_TTL_SECONDS",
    "MAX_TTL_SECONDS",
    "MIN_ACCESS_TTL_SECONDS",
    "RENEW_BOUND_SECONDS",
    "RESPONSE_CHANNEL_HEADER",
    "TOKENS_KEY",
    "BillzClient",
]
