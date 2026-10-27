"""The live Eskiz adapter (customer contract §2.14.1; docs/integrations/eskiz.md): token login and refresh
(single flight, kept only in the cache), the send form, the ``sent`` semantics of every failure, and logs
without phone numbers, texts or secrets."""

import logging
import threading
import time
from email.parser import BytesParser
from email.policy import HTTP

import httpx
import pytest
from django.core.cache import cache
from django.test import override_settings

from glowderma.core.pii import pii_ref
from glowderma.integrations import httpclient, tokens
from glowderma.integrations.eskiz.live import LiveEskiz
from glowderma.integrations.eskiz.ports import EskizRejected, EskizUnavailable, SmsMessage
from glowderma.integrations.eskiz.registry import get_eskiz_port

from .live_fixtures import Script, connect_error, read_timeout, reply

BASE = "https://notify.eskiz.uz/api"
PASSWORD = "eskiz-password-value"  # noqa: S105 - test value
PHONE = "+998901234567"
TEXT = "Glow Derma: tasdiqlash kodingiz 481516. Kodni hech kimga bermang."
MESSAGE = SmsMessage(phone=PHONE, text=TEXT, sms_code_id=42)


TOKEN_KEY = f"gd:eskiz:token:{tokens.account_scope(BASE, 'sms@glowderma.uz')}"


@pytest.fixture(autouse=True)
def fast_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(httpclient, "RETRY_BACKOFF_SECONDS", 0)
    tokens.reset_local()


def eskiz(script: Script, callback_url: str = "") -> LiveEskiz:
    script.routes.setdefault(("POST", "/api/auth/login"), [reply("eskiz", "login_ok")])
    return LiveEskiz(
        base_url=BASE + "/",
        email="sms@glowderma.uz",
        password=PASSWORD,
        sender="GlowDerma",
        callback_url=callback_url,
        transport=script.transport,
    )


def form(request: httpx.Request) -> dict[str, str]:
    """The fields of a multipart/form-data body."""
    raw = b"Content-Type: " + request.headers["Content-Type"].encode() + b"\r\n\r\n" + request.content
    message = BytesParser(policy=HTTP).parsebytes(raw)
    return {
        str(part.get_param("name", header="content-disposition")): part.get_content().strip()
        for part in message.iter_parts()
    }


def test_a_send_logs_in_once_and_posts_the_documented_form() -> None:
    script = Script().on("POST", "/api/message/sms/send", reply("eskiz", "send_ok"))
    adapter = eskiz(script)
    assert adapter.send(MESSAGE) == "59bf10a2-aba8-4694-8fd5-0be20102a580"
    assert adapter.send(MESSAGE) == "59bf10a2-aba8-4694-8fd5-0be20102a580"
    (login,) = script.to("POST", "/api/auth/login")
    assert form(login) == {"email": "sms@glowderma.uz", "password": PASSWORD}
    first, _ = script.to("POST", "/api/message/sms/send")
    assert str(first.url) == f"{BASE}/message/sms/send"
    assert first.headers["Authorization"] == "Bearer eskiz-token-1"
    assert form(first) == {"mobile_phone": "998901234567", "message": TEXT, "from": "GlowDerma"}
    assert cache.get(TOKEN_KEY) == "eskiz-token-1"  # the token lives only in the cache


def test_the_callback_url_is_sent_only_when_configured() -> None:
    script = Script().on("POST", "/api/message/sms/send", reply("eskiz", "send_ok"))
    eskiz(script, callback_url="https://api.glowderma.uz/sms-status").send(MESSAGE)
    assert form(script.to("POST", "/api/message/sms/send")[0])["callback_url"] == (
        "https://api.glowderma.uz/sms-status"
    )


def test_a_401_refreshes_the_token_once_and_repeats_the_send() -> None:
    script = Script().on(
        "POST",
        "/api/message/sms/send",
        reply("eskiz", "unauthorized", 401),
        reply("eskiz", "send_ok"),
    )
    script.on("PATCH", "/api/auth/refresh", reply("eskiz", "refresh_ok"))
    assert eskiz(script).send(MESSAGE) == "59bf10a2-aba8-4694-8fd5-0be20102a580"
    (refresh,) = script.to("PATCH", "/api/auth/refresh")
    assert refresh.headers["Authorization"] == "Bearer eskiz-token-1"
    _, second = script.to("POST", "/api/message/sms/send")
    assert second.headers["Authorization"] == "Bearer eskiz-token-2"
    assert cache.get(TOKEN_KEY) == "eskiz-token-2"


def test_a_refused_refresh_falls_back_to_a_login() -> None:
    script = Script().on(
        "POST", "/api/message/sms/send", reply("eskiz", "unauthorized", 401), reply("eskiz", "send_ok")
    )
    script.on("PATCH", "/api/auth/refresh", reply("eskiz", "unauthorized", 401))
    script.on(
        "POST",
        "/api/auth/login",
        reply("eskiz", "login_ok"),
        httpx.Response(200, json={"data": {"token": "eskiz-token-3"}}),
    )
    eskiz(script).send(MESSAGE)
    assert len(script.to("POST", "/api/auth/login")) == 2
    assert script.to("POST", "/api/message/sms/send")[1].headers["Authorization"] == "Bearer eskiz-token-3"


def test_a_second_401_is_not_processed_never_a_loop(caplog: pytest.LogCaptureFixture) -> None:
    script = Script().on("POST", "/api/message/sms/send", reply("eskiz", "unauthorized", 401))
    script.on("PATCH", "/api/auth/refresh", reply("eskiz", "refresh_ok"))
    with pytest.raises(EskizUnavailable, match="HTTP 401") as raised:
        eskiz(script).send(MESSAGE)
    assert raised.value.sent is False  # Eskiz did not take the message: the outbox may send it again
    assert len(script.to("POST", "/api/message/sms/send")) == 2
    assert "eskiz.auth_failed" in caplog.text


@pytest.mark.parametrize(
    ("responder", "error", "sent", "posts"),
    [
        (reply("eskiz", "send_rejected", 400), EskizRejected, None, 1),
        (httpx.Response(402, json={"message": "Insufficient balance"}), EskizRejected, None, 1),
        (httpx.Response(500, json={}), EskizUnavailable, True, 1),
        (httpx.Response(200, json={"status": "waiting"}), EskizUnavailable, True, 1),
        (httpx.Response(502, json={}), EskizUnavailable, True, 1),
        (read_timeout, EskizUnavailable, True, 1),
        (connect_error, EskizUnavailable, False, httpclient.RETRY_ATTEMPTS),
        # A-S6 / R5: answers that say the message was not processed: the OTP is retried, never dropped
        (httpx.Response(429, json={"message": "Too many requests"}), EskizUnavailable, False, 1),
        (httpx.Response(408, json={}), EskizUnavailable, False, 1),
        (httpx.Response(425, json={}), EskizUnavailable, False, 1),
        (httpx.Response(403, json={}), EskizUnavailable, False, 1),
        (httpx.Response(301, headers={"Location": "https://elsewhere"}), EskizUnavailable, False, 1),
        # R1: an undecodable answer after sending is an unknown outcome (never resent), never an httpx error
        (
            lambda request: httpx.Response(
                200, headers={"Content-Encoding": "gzip"}, stream=httpx.ByteStream(b"not gzip")
            ),
            EskizUnavailable,
            True,
            1,
        ),
    ],
)
def test_send_outcomes(responder: object, error: type[Exception], sent: bool | None, posts: int) -> None:
    script = Script().on("POST", "/api/message/sms/send", responder)  # type: ignore[arg-type]
    with pytest.raises(error) as raised:
        eskiz(script).send(MESSAGE)
    if sent is not None:
        assert isinstance(raised.value, EskizUnavailable) and raised.value.sent is sent
    assert "998901234567" not in str(raised.value) and "481516" not in str(raised.value)
    # a message that may have reached Eskiz is never sent twice by the adapter
    assert len(script.to("POST", "/api/message/sms/send")) == posts


def test_login_failures(caplog: pytest.LogCaptureFixture) -> None:
    script = Script().on("POST", "/api/auth/login", reply("eskiz", "login_refused", 401))
    # R5 / R10: refused credentials are not the message's fault (retried later) and are not tried again for
    # a minute: five SMS, one login attempt
    for n in range(5):
        with pytest.raises(EskizUnavailable, match="refused") as refused:
            eskiz(script).send(SmsMessage(phone=PHONE, text=TEXT, sms_code_id=n))
        assert refused.value.sent is False
    assert len(script.to("POST", "/api/auth/login")) == 1
    assert caplog.text.count("eskiz.auth_failed") == 1
    cache.clear()
    script = Script().on("POST", "/api/auth/login", connect_error)
    with pytest.raises(EskizUnavailable) as down:
        eskiz(script).send(MESSAGE)
    assert down.value.sent is False
    script = Script().on("POST", "/api/auth/login", httpx.Response(503, json={}))
    with pytest.raises(EskizUnavailable) as down:
        eskiz(script).send(MESSAGE)
    assert down.value.sent is False and script.to("POST", "/api/message/sms/send") == []


def test_an_otp_eskiz_echoes_never_reaches_an_error() -> None:
    """R14: Eskiz's free text may echo the message: digit runs of four or more are masked (the OTP has 6)."""
    echo = httpx.Response(400, json={"message": f"Template mismatch: {TEXT}"})
    script = Script().on("POST", "/api/message/sms/send", echo)
    with pytest.raises(EskizRejected) as raised:
        eskiz(script).send(MESSAGE)
    assert "481516" not in str(raised.value) and "Template mismatch" in str(raised.value)


def test_the_token_cache_down_fails_closed() -> None:
    """R2: with the cache down ten SMS log in once (process-local single flight), not ten times."""

    class DownCache:
        def __getattr__(self, name: str) -> object:
            def fail(*args: object, **kwargs: object) -> None:
                raise ConnectionError("redis down")

            return fail

    script = Script().on("POST", "/api/message/sms/send", reply("eskiz", "send_ok"))
    adapter = eskiz(script)
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(tokens, "cache", DownCache())
        for n in range(10):
            adapter.send(SmsMessage(phone=PHONE, text=TEXT, sms_code_id=n))
    assert len(script.to("POST", "/api/auth/login")) == 1


def test_rotated_credentials_never_reuse_the_old_accounts_token() -> None:
    """A-S9: the token key is scoped by sha256(base URL | email)."""
    cache.set("gd:eskiz:token", "token-of-the-previous-account", 3600)
    script = Script().on("POST", "/api/message/sms/send", reply("eskiz", "send_ok"))
    eskiz(script).send(MESSAGE)
    (sent,) = script.to("POST", "/api/message/sms/send")
    assert sent.headers["Authorization"] == "Bearer eskiz-token-1"


def test_a_malformed_phone_is_never_sent() -> None:
    script = Script()
    with pytest.raises(EskizRejected):
        eskiz(script).send(SmsMessage(phone="+99890-123", text=TEXT, sms_code_id=1))
    assert script.requests == []


def test_concurrent_sends_share_one_login() -> None:
    def slow_login(request: httpx.Request) -> httpx.Response:
        time.sleep(0.1)
        return reply("eskiz", "login_ok")

    script = Script().on("POST", "/api/auth/login", slow_login)
    script.on("POST", "/api/message/sms/send", reply("eskiz", "send_ok"))
    adapter = eskiz(script)
    start = threading.Event()
    ids: list[str] = []

    def worker(n: int) -> None:
        start.wait(10)
        ids.append(adapter.send(SmsMessage(phone=PHONE, text=TEXT, sms_code_id=n)))

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(6)]
    for thread in threads:
        thread.start()
    start.set()
    for thread in threads:
        thread.join(10)
    assert len(ids) == 6 and len(script.to("POST", "/api/auth/login")) == 1


def test_logs_carry_the_code_id_and_a_phone_reference_only(caplog: pytest.LogCaptureFixture) -> None:
    script = Script().on("POST", "/api/message/sms/send", reply("eskiz", "send_ok"))
    with caplog.at_level(logging.DEBUG):
        eskiz(script).send(MESSAGE)
        script.on("POST", "/api/message/sms/send", reply("eskiz", "send_rejected", 400))
        with pytest.raises(EskizRejected):
            eskiz(script).send(MESSAGE)
    assert f"phone_ref={pii_ref(PHONE)}" in caplog.text and "sms_code_id=42" in caplog.text
    for secret in ("998901234567", "481516", "tasdiqlash", PASSWORD, "eskiz-token-1"):
        assert secret not in caplog.text


@override_settings(
    ESKIZ_MODE="live",
    ESKIZ_BASE_URL=BASE,
    ESKIZ_EMAIL="sms@glowderma.uz",
    ESKIZ_PASSWORD=PASSWORD,
    ESKIZ_SENDER="GlowDerma",
    ESKIZ_CALLBACK_URL="",
)
def test_the_registry_builds_the_live_adapter_from_the_settings() -> None:
    live = get_eskiz_port()
    assert isinstance(live, LiveEskiz)
    assert (live.base_url, live.email, live.sender, live.callback_url) == (
        BASE,
        "sms@glowderma.uz",
        "GlowDerma",
        "",
    )
