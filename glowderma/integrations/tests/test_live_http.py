"""The shared plumbing of the live adapters: bounded calls, retries only before sending, the token cache."""

import logging
import threading
import time
from collections.abc import Iterator

import httpx
import pytest
from django.core.cache import cache

from glowderma.integrations import httpclient, tokens
from glowderma.integrations.httpclient import Deadline, TransportFailure, safe_text
from glowderma.integrations.tokens import SharedToken, TokenBusy

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


# --- the shared token ---------------------------------------------------------------------------------------


class Fetcher:
    def __init__(self, delay: float = 0.0) -> None:
        self.calls: list[str | None] = []
        self.delay = delay
        self.lock = threading.Lock()

    def __call__(self, stale: str | None) -> str:
        time.sleep(self.delay)
        with self.lock:
            self.calls.append(stale)
            return f"token-{len(self.calls)}"


def test_the_token_is_cached_and_renewed_once_after_a_refusal() -> None:
    fetch = Fetcher()
    token = SharedToken("unit", ttl_seconds=60, fetch=fetch)
    assert token.get(Deadline()) == "token-1"
    assert token.get(Deadline()) == "token-1"
    assert cache.get("gd:unit:token") == "token-1"
    assert token.renew("token-1", Deadline()) == "token-2"
    assert fetch.calls == [None, "token-1"]
    # a worker that saw the old token refused reuses the newer one another worker fetched
    assert token.renew("token-1", Deadline()) == "token-2"
    assert len(fetch.calls) == 2
    token.forget()
    assert cache.get("gd:unit:token") is None


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
    assert cache.get("gd:burst:token-lock") is None  # released


def test_a_busy_lock_until_the_deadline_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tokens, "POLL_SECONDS", 0.01)
    cache.add("gd:held:token-lock", "someone-else", 30)
    token = SharedToken("held", ttl_seconds=60, fetch=Fetcher())
    with pytest.raises(TokenBusy):
        token.get(Deadline(0.05))


def test_a_cache_outage_degrades_to_a_login_per_call(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def broken(*args: object, **kwargs: object) -> None:
        raise ConnectionError("redis down")

    for name in ("get", "set", "add", "delete"):
        monkeypatch.setattr(cache, name, broken)
    fetch = Fetcher()
    token = SharedToken("down", ttl_seconds=60, fetch=fetch)
    assert token.get(Deadline()) == "token-1"
    assert token.get(Deadline()) == "token-2"
    token.forget()
    assert "token-1" not in caplog.text and "token-2" not in caplog.text
