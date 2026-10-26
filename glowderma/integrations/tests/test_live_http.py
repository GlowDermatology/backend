"""The shared plumbing of the live adapters: bounded calls, retries only before sending, the token cache."""

import logging
import socket
import threading
import time
from collections.abc import Callable, Iterator

import httpx
import pytest
from celery.exceptions import SoftTimeLimitExceeded  # type: ignore[import-untyped]
from django.core.cache import cache

from glowderma.integrations import httpclient, tokens
from glowderma.integrations.httpclient import Deadline, TransportFailure, safe_text
from glowderma.integrations.tokens import LoginRefused, SharedToken, TokenBusy

from .live_fixtures import Script, connect_error, read_timeout

URL = "https://provider.example/api/v1/thing?secret=query-value"


@pytest.fixture(autouse=True)
def fast_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(httpclient, "RETRY_BACKOFF_SECONDS", 0)


def call(script: Script, *, retry_unsent: bool = True, seconds: float = 5.0) -> httpclient.Answer:
    with httpclient.new_client(script.transport) as http:
        return httpclient.send(
            http, "GET", URL, provider="test", deadline=Deadline(seconds), retry_unsent=retry_unsent
        )


def test_an_answer_carries_the_status_and_the_parsed_json() -> None:
    script = Script().on("GET", "/api/v1/thing", httpx.Response(201, json={"ok": True}))
    answer = call(script)
    assert (answer.status, answer.data, answer.json_object()) == (201, {"ok": True}, {"ok": True})
    script.on("GET", "/api/v1/thing", httpx.Response(200, text="<html>not json</html>"))
    assert call(script).data is None and call(script).json_object() == {}


def test_a_request_that_never_left_is_retried_then_reported_unsent() -> None:
    script = Script().on("GET", "/api/v1/thing", connect_error, httpx.Response(200, json={"n": 1}))
    assert call(script).data == {"n": 1}
    assert len(script.requests) == 2
    script = Script().on("GET", "/api/v1/thing", connect_error)
    with pytest.raises(TransportFailure) as failure:
        call(script)
    assert failure.value.sent is False
    assert len(script.requests) == httpclient.RETRY_ATTEMPTS
    script = Script().on("GET", "/api/v1/thing", connect_error)
    with pytest.raises(TransportFailure):
        call(script, retry_unsent=False)
    assert len(script.requests) == 1


def test_a_failure_after_sending_is_never_retried() -> None:
    script = Script().on("GET", "/api/v1/thing", read_timeout, httpx.Response(200, json={}))
    with pytest.raises(TransportFailure) as failure:
        call(script)
    assert failure.value.sent is True
    assert len(script.requests) == 1


def test_an_expired_deadline_sends_nothing() -> None:
    script = Script().on("GET", "/api/v1/thing", httpx.Response(200, json={}))
    with pytest.raises(TransportFailure) as failure:
        call(script, seconds=-1)
    assert failure.value.sent is False
    assert script.requests == []


class Trickle(httpx.SyncByteStream):
    """A body that arrives slowly, chunk by chunk."""

    def __iter__(self) -> Iterator[bytes]:
        for _ in range(20):
            time.sleep(0.02)
            yield b" "


def test_the_total_deadline_bounds_a_slow_answer() -> None:
    script = Script().on("GET", "/api/v1/thing", lambda request: httpx.Response(200, stream=Trickle()))
    started = time.monotonic()
    with pytest.raises(TransportFailure) as failure:
        call(script, seconds=0.1)
    assert failure.value.sent is True
    assert time.monotonic() - started < 0.3


def test_an_oversized_answer_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(httpclient, "MAX_BODY_BYTES", 10)
    script = Script().on("GET", "/api/v1/thing", httpx.Response(200, content=b"x" * 100))
    with pytest.raises(TransportFailure, match="too large") as failure:
        call(script)
    assert failure.value.sent is True


def test_log_lines_carry_the_path_only(caplog: pytest.LogCaptureFixture) -> None:
    script = Script().on("GET", "/api/v1/thing", httpx.Response(200, json={"token": "body-secret"}))
    with caplog.at_level(logging.DEBUG, logger="glowderma.integrations"):
        call(script)
    text = caplog.text
    assert "/api/v1/thing" in text
    assert "query-value" not in text and "body-secret" not in text and "provider.example" not in text


def test_safe_text_masks_phone_numbers_and_truncates() -> None:
    assert safe_text("Invalid phone number 998901234567") == "Invalid phone number ***"
    assert safe_text("call +998 90 123-45-67 now") == "call *** now"
    assert safe_text("amount 784400 so'm") == "amount 784400 so'm"
    assert safe_text(None) == "" and safe_text(-5017) == "-5017"
    assert len(safe_text("x" * 500)) == 200


def bad_gzip(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, headers={"Content-Encoding": "gzip"}, stream=httpx.ByteStream(b"not gzip"))


def test_an_undecodable_answer_is_an_unknown_outcome_never_an_untyped_error() -> None:
    """A-M1 / R1: httpx.DecodingError (a RequestError, not a TransportError) after the request left."""
    script = Script().on("GET", "/api/v1/thing", bad_gzip, httpx.Response(200, json={}))
    with pytest.raises(TransportFailure) as failure:
        call(script)
    assert failure.value.sent is True and isinstance(failure.value.__cause__, httpx.DecodingError)
    assert len(script.requests) == 1  # never repeated


@pytest.mark.parametrize(
    "error",
    [httpx.StreamConsumed(), httpx.TooManyRedirects("loop"), RuntimeError("bug"), OSError("reset")],
    ids=["stream_error", "http_error", "runtime_error", "os_error"],
)
def test_any_exception_after_the_request_may_have_left_is_sent_true(error: Exception) -> None:
    def fail(request: httpx.Request) -> httpx.Response:
        raise error

    with pytest.raises(TransportFailure) as failure:
        call(Script().on("GET", "/api/v1/thing", fail))
    assert failure.value.sent is True


def test_a_soft_time_limit_is_never_swallowed() -> None:
    def soft_limit(request: httpx.Request) -> httpx.Response:
        raise SoftTimeLimitExceeded()

    with pytest.raises(SoftTimeLimitExceeded):
        call(Script().on("GET", "/api/v1/thing", soft_limit))


def _serve_once(handler: Callable[[socket.socket], None]) -> int:
    listener = socket.socket()
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)

    def serve() -> None:
        conn, _ = listener.accept()
        try:
            conn.recv(65536)
            handler(conn)
        except OSError:
            pass
        finally:
            conn.close()
            listener.close()

    threading.Thread(target=serve, daemon=True).start()
    return int(listener.getsockname()[1])


def test_a_trickled_status_line_and_headers_are_cut_at_the_deadline() -> None:
    """R6: the watchdog bounds the header phase too (a byte every 0.3 s keeps each read alive)."""

    def trickle(conn: socket.socket) -> None:
        conn.sendall(b"HTTP/1.1 200 OK\r\n")
        for byte in b"X-Slow: " + b"a" * 40:
            conn.sendall(bytes([byte]))
            time.sleep(0.3)

    port = _serve_once(trickle)
    started = time.monotonic()
    with httpclient.new_client() as http, pytest.raises(TransportFailure) as failure:
        httpclient.send(http, "GET", f"http://127.0.0.1:{port}/x", provider="p", deadline=Deadline(1.0))
    assert time.monotonic() - started < 2.0
    assert failure.value.sent is True and str(failure.value) == "deadline exceeded"


def test_a_complete_answer_whose_last_chunk_lands_after_the_deadline_is_kept() -> None:
    """R16: the deadline is checked before the next chunk is read, and a body that is complete is used."""

    def late(request: httpx.Request) -> httpx.Response:
        def body() -> Iterator[bytes]:
            yield b'{"ok"'
            time.sleep(0.15)
            yield b": true}"

        return httpx.Response(200, headers={"Content-Length": "12"}, stream=_Iter(body()))

    answer = call(Script().on("GET", "/api/v1/thing", late), seconds=0.1)
    assert answer.data == {"ok": True}


class _Iter(httpx.SyncByteStream):
    def __init__(self, chunks: Iterator[bytes]) -> None:
        self.chunks = chunks

    def __iter__(self) -> Iterator[bytes]:
        yield from self.chunks


def test_codes_are_parsed_strictly() -> None:
    assert [httpclient.int_code(v) for v in (0, -5017, "2", "-99", "0012")] == [0, -5017, 2, -99, 12]
    for weird in ("--1", "²", "-²", "٣٦١", " 5", "5 ", "1.0", "", None, True, 1.0, 10**19, "9" * 19):
        assert httpclient.int_code(weird) is None, weird


def test_nan_and_infinity_are_not_json_numbers() -> None:
    script = Script().on("GET", "/api/v1/thing", httpx.Response(200, content=b'{"price": NaN}'))
    assert call(script).data is None


def test_answers_tell_processed_from_not_processed() -> None:
    assert httpclient.Answer(201, {}).ok and not httpclient.Answer(301, {}).ok
    for status in (301, 302, 307, 401, 403, 408, 425, 429):
        assert httpclient.Answer(status, None).not_processed, status
    for status in (200, 201, 400, 404, 409, 422, 500, 502, 503):
        assert not httpclient.Answer(status, None).not_processed, status


def test_clients_share_one_tls_context_and_keep_no_idle_connection() -> None:
    with httpclient.new_client() as first, httpclient.new_client() as second:
        assert httpclient.ssl_context() is httpclient.ssl_context()
        pool_a = first._transport._pool  # type: ignore[attr-defined]
        pool_b = second._transport._pool  # type: ignore[attr-defined]
        assert pool_a._ssl_context is pool_b._ssl_context is httpclient.ssl_context()
        assert pool_a._max_keepalive_connections == 0


# --- the shared token ---------------------------------------------------------------------------------------


class Fetcher:
    def __init__(self, delay: float = 0.0, refuse: bool = False) -> None:
        self.calls: list[str | None] = []
        self.deadlines: list[float] = []
        self.delay = delay
        self.refuse = refuse
        self.lock = threading.Lock()

    def __call__(self, stale: str | None, deadline: Deadline) -> str:
        time.sleep(self.delay)
        with self.lock:
            self.calls.append(stale)
            self.deadlines.append(deadline.remaining())
            if self.refuse:
                raise LoginRefused("refused")
            return f"token-{len(self.calls)}"


@pytest.fixture(autouse=True)
def no_local_tokens() -> Iterator[None]:
    tokens.reset_local()
    yield
    tokens.reset_local()


def token_key(name: str, *parts: str) -> str:
    return f"gd:{name}:token:{tokens.account_scope(*parts)}"


def test_the_token_is_cached_and_renewed_once_after_a_refusal() -> None:
    fetch = Fetcher()
    token = SharedToken("unit", ttl_seconds=60, fetch=fetch, scope=tokens.account_scope("https://u", "me"))
    assert token.get(Deadline()) == "token-1"
    assert token.get(Deadline()) == "token-1"
    assert cache.get(token_key("unit", "https://u", "me")) == "token-1"
    assert token.renew("token-1", Deadline()) == "token-2"
    assert fetch.calls == [None, "token-1"]
    # a worker that saw the old token refused reuses the newer one another worker fetched
    assert token.renew("token-1", Deadline()) == "token-2"
    assert len(fetch.calls) == 2
    token.forget()
    assert cache.get(token_key("unit", "https://u", "me")) is None


def test_the_token_key_is_scoped_to_the_account() -> None:
    """A-S9 / R12: rotated credentials (or another host) never reuse the previous account's token."""
    old_scope = tokens.account_scope("https://h", "old@glowderma.uz")
    old = SharedToken("scoped", ttl_seconds=60, fetch=Fetcher(), scope=old_scope)
    assert old.get(Deadline()) == "token-1"
    rotated = Fetcher()
    new_scope = tokens.account_scope("https://h", "rotated@glowderma.uz")
    new = SharedToken("scoped", ttl_seconds=60, fetch=rotated, scope=new_scope)
    assert new.get(Deadline()) == "token-1" and rotated.calls == [None]
    assert old.key != new.key and "glowderma" not in new.key


def test_concurrent_callers_log_in_once() -> None:
    fetch = Fetcher(delay=0.2)
    token = SharedToken("burst", ttl_seconds=60, fetch=fetch)
    start = threading.Event()
    seen: list[str] = []

    def worker() -> None:
        start.wait(10)
        seen.append(token.get(Deadline()))

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    start.set()
    for thread in threads:
        thread.join(10)
    assert fetch.calls == [None]
    assert seen == ["token-1"] * 8
    assert cache.get(token.lock_key) is None  # released


def test_the_lock_outlives_a_bounded_fetch() -> None:
    """R11: the lock lives ≥ 45 s and a fetch gets at most half of it, whatever the caller's deadline."""
    assert tokens.LOCK_SECONDS >= 45
    fetch = Fetcher()
    SharedToken("bounded", ttl_seconds=60, fetch=fetch).get(Deadline(600))
    assert 0 < fetch.deadlines[0] <= tokens.LOCK_SECONDS / 2


def test_the_lock_is_released_only_by_its_owner() -> None:
    token = SharedToken("owner", ttl_seconds=60, fetch=Fetcher())
    cache.set(token.lock_key, "someone-else", 30)
    token._unlock("me")
    assert cache.get(token.lock_key) == "someone-else"
    token._unlock("someone-else")
    assert cache.get(token.lock_key) is None


def test_a_busy_lock_until_the_deadline_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tokens, "POLL_SECONDS", 0.01)
    token = SharedToken("held", ttl_seconds=60, fetch=Fetcher())
    cache.add(token.lock_key, "someone-else", 30)
    with pytest.raises(TokenBusy):
        token.get(Deadline(0.05))


def test_a_refused_login_is_remembered_for_a_minute() -> None:
    """R10: credentials the provider refused are not tried again by every call (lockout risk)."""
    fetch = Fetcher(refuse=True)
    token = SharedToken("refused", ttl_seconds=60, fetch=fetch)
    for _ in range(5):
        with pytest.raises(LoginRefused):
            token.get(Deadline())
    assert len(fetch.calls) == 1
    assert cache.get(token.refused_key) == 1
    token.forget()  # credential rotation: try again at once
    with pytest.raises(LoginRefused):
        token.get(Deadline())
    assert len(fetch.calls) == 2


def _break_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(*args: object, **kwargs: object) -> None:
        raise ConnectionError("redis down")

    for name in ("get", "set", "add", "delete", "delete_many"):
        monkeypatch.setattr(cache, name, broken)


def test_a_cache_outage_fails_closed_with_one_login_per_process(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """R2: without the cache no call logs in on its own; the process-local fallback logs in once per token
    lifetime (single flight), plus at most one renewal a minute after a 401."""
    _break_cache(monkeypatch)
    fetch = Fetcher()
    token = SharedToken("down", ttl_seconds=60, fetch=fetch)
    assert [token.get(Deadline()) for _ in range(10)] == ["token-1"] * 10
    with pytest.raises(TokenBusy):
        token.renew("token-1", Deadline())  # a 401 within a minute of the login: no login, "unavailable"
    tokens._local(token.key).logged_in_at -= tokens.LOCAL_RENEW_SECONDS  # type: ignore[operator]
    assert token.renew("token-1", Deadline()) == "token-2"  # a 401 later on: one renewal
    assert token.get(Deadline()) == "token-2"
    assert fetch.calls == [None, "token-1"]
    assert "token-1" not in caplog.text and "token-2" not in caplog.text


def test_a_cache_outage_logs_in_once_across_threads(monkeypatch: pytest.MonkeyPatch) -> None:
    _break_cache(monkeypatch)
    fetch = Fetcher(delay=0.1)
    token = SharedToken("down-burst", ttl_seconds=60, fetch=fetch)
    seen: list[str] = []
    threads = [threading.Thread(target=lambda: seen.append(token.get(Deadline()))) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)
    assert seen == ["token-1"] * 6 and fetch.calls == [None]


def test_a_cache_outage_remembers_a_refused_login(monkeypatch: pytest.MonkeyPatch) -> None:
    _break_cache(monkeypatch)
    fetch = Fetcher(refuse=True)
    token = SharedToken("down-refused", ttl_seconds=60, fetch=fetch)
    for _ in range(3):
        with pytest.raises(LoginRefused):
            token.get(Deadline())
    assert len(fetch.calls) == 1
