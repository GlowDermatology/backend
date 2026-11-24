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

**Circuit breaker** (review B6): :data:`CIRCUIT_FAILURES` transport errors, timeouts or 5xx/408 answers in a
row (across every worker) open the circuit for :data:`CIRCUIT_OPEN_SECONDS`: calls then fail fast with
:class:`BillzUnavailable` instead of holding a worker for minutes (ERROR ``billz.circuit_open``). Any other
answer resets the count of failures in a row; an open circuit always runs its full time (nothing is sent
while it is open, so nothing can close it early). The bookkeeping is best effort: a cache error there is
logged, never the outcome of the request it follows.

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

The package: ``config`` (the settings and call profiles), ``error_mapping`` (Billz answers → errors),
``shared_cache`` (:class:`SafeCache`), ``rate_limiter`` and ``http_client`` (:class:`BillzClient`: auth,
retries, deadlines and the circuit breaker).
"""

from glowderma.integrations.billz.client.config import (
    BATCH,
    INTERACTIVE,
    BillzConfig,
    CallProfile,
)
from glowderma.integrations.billz.client.error_mapping import (
    CODE_LIMIT,
    CONFIGURATION_ERROR_CODES,
    ERROR_REASONS,
    MESSAGE_LIMIT,
    RETRYABLE_ERROR_CODES,
    RETRYABLE_STRING_CODES,
    SHIFT_CLOSED_CODES,
    BillzAuthError,
    classify,
    error_details,
)
from glowderma.integrations.billz.client.http_client import (
    ACCESS_MARGIN_SECONDS,
    BACKOFF_BASE_SECONDS,
    BACKOFF_CAP_SECONDS,
    CIRCUIT_FAILURES,
    CIRCUIT_FAILURES_KEY,
    CIRCUIT_OPEN_KEY,
    CIRCUIT_OPEN_SECONDS,
    DEADLINE_SLACK_SECONDS,
    DEFAULT_ACCESS_TTL_SECONDS,
    DEFAULT_REFRESH_TTL_SECONDS,
    LOCK_KEY,
    LOCK_POLL_SECONDS,
    LOCK_TTL_SECONDS,
    MAX_TTL_SECONDS,
    MIN_ACCESS_TTL_SECONDS,
    RENEW_BOUND_SECONDS,
    RESPONSE_CHANNEL_HEADER,
    TOKENS_KEY,
    BillzClient,
)
from glowderma.integrations.billz.client.rate_limiter import (
    RATE_PREFIX,
    RETRY_AFTER_CAP_SECONDS,
    RateLimiter,
    retry_after_seconds,
)
from glowderma.integrations.billz.client.shared_cache import (
    CACHE_BACKEND_ERRORS,
    SafeCache,
)

__all__ = [
    "ACCESS_MARGIN_SECONDS",
    "BACKOFF_BASE_SECONDS",
    "BACKOFF_CAP_SECONDS",
    "BATCH",
    "CACHE_BACKEND_ERRORS",
    "CIRCUIT_FAILURES",
    "CIRCUIT_FAILURES_KEY",
    "CIRCUIT_OPEN_KEY",
    "CIRCUIT_OPEN_SECONDS",
    "CODE_LIMIT",
    "CONFIGURATION_ERROR_CODES",
    "DEADLINE_SLACK_SECONDS",
    "DEFAULT_ACCESS_TTL_SECONDS",
    "DEFAULT_REFRESH_TTL_SECONDS",
    "ERROR_REASONS",
    "INTERACTIVE",
    "LOCK_KEY",
    "LOCK_POLL_SECONDS",
    "LOCK_TTL_SECONDS",
    "MAX_TTL_SECONDS",
    "MESSAGE_LIMIT",
    "MIN_ACCESS_TTL_SECONDS",
    "RATE_PREFIX",
    "RENEW_BOUND_SECONDS",
    "RESPONSE_CHANNEL_HEADER",
    "RETRYABLE_ERROR_CODES",
    "RETRYABLE_STRING_CODES",
    "RETRY_AFTER_CAP_SECONDS",
    "SHIFT_CLOSED_CODES",
    "TOKENS_KEY",
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
