"""The e-mail port (hybrid identity): the fake's outcomes and the SMTP adapter's mapping of relay answers."""

from __future__ import annotations

import logging
import smtplib
import socket
from collections.abc import Callable
from typing import Any

import pytest
from django.core.exceptions import ImproperlyConfigured
from django.test import override_settings

from glowderma.integrations.email import registry
from glowderma.integrations.email import smtp as smtp_module
from glowderma.integrations.email.fake import FakeEmail
from glowderma.integrations.email.ports import EmailMessage, EmailRejected, EmailUnavailable
from glowderma.integrations.email.smtp import SmtpEmail

MESSAGE = EmailMessage(
    to="malika@example.uz", subject="Kod", text="Kod: 123456", html="<p>123456</p>", reference="sms_code_id=7"
)


def test_fake_keeps_messages_under_dev_settings_and_maps_the_reserved_domains() -> None:
    fake = FakeEmail()
    assert fake.send(MESSAGE) == "fake-sms_code_id=7"
    assert fake.sent == [MESSAGE] and fake.is_configured()
    with pytest.raises(EmailRejected):
        fake.send(EmailMessage("x@rejected.test", "s", "t", "h", "r"))
    with pytest.raises(EmailUnavailable) as down:
        fake.send(EmailMessage("x@down.test", "s", "t", "h", "r"))
    assert down.value.sent is False


def test_fake_under_production_settings_keeps_nothing_and_logs_no_address(
    caplog: pytest.LogCaptureFixture,
) -> None:
    fake = FakeEmail()
    with override_settings(DEV_FEATURES_ALLOWED=False), caplog.at_level(logging.INFO):
        fake.send(MESSAGE)
    assert fake.sent == []
    assert "sms_code_id=7" in caplog.text and "malika" not in caplog.text and "123456" not in caplog.text


def test_registry_selects_the_adapter_and_the_fixed_code() -> None:
    assert registry.get_email_port() is registry.fake_email()
    assert registry.new_code() == "123456"  # ESKIZ_FAKE_CODE of the local/test settings
    with override_settings(EMAIL_MODE="smtp", SMTP_HOST="smtp.example.uz"):
        port = registry.get_email_port()
        assert isinstance(port, SmtpEmail) and port.is_configured()
        code = registry.new_code()
        assert len(code) == 6 and code.isdigit()
    with override_settings(EMAIL_MODE="smtp", SMTP_HOST=""):
        assert not SmtpEmail.from_settings().is_configured()
    with override_settings(EMAIL_MODE="pigeon"), pytest.raises(ImproperlyConfigured):
        registry.get_email_port()


class RecordingSMTP:
    """Stands in for ``smtplib.SMTP``: records the conversation, or raises ``failure`` at ``stage``."""

    instances: list[RecordingSMTP] = []
    failure: BaseException | None = None
    stage = "send"

    def __init__(self, host: str, port: int, **kwargs: Any) -> None:
        self.host, self.port, self.kwargs = host, port, kwargs
        self.calls: list[str] = []
        self.sent: list[tuple[str, list[str], Any]] = []
        RecordingSMTP.instances.append(self)
        self._maybe_fail("connect")

    def __enter__(self) -> RecordingSMTP:
        return self

    def __exit__(self, *exc: object) -> None:
        self.calls.append("quit")

    def _maybe_fail(self, stage: str) -> None:
        if self.stage == stage and self.failure is not None:
            raise self.failure

    def starttls(self, **kwargs: Any) -> None:
        self.calls.append("starttls")

    def login(self, user: str, password: str) -> None:
        self.calls.append(f"login:{user}")
        self._maybe_fail("login")

    def send_message(self, message: Any, from_addr: str, to_addrs: list[str]) -> dict[str, Any]:
        self._maybe_fail("send")
        self.sent.append((from_addr, to_addrs, message))
        return {}


@pytest.fixture
def smtp(monkeypatch: pytest.MonkeyPatch) -> type[RecordingSMTP]:
    RecordingSMTP.instances = []
    RecordingSMTP.failure = None
    RecordingSMTP.stage = "send"
    monkeypatch.setattr(smtp_module, "smtp_class", lambda use_ssl: RecordingSMTP)
    return RecordingSMTP


def adapter() -> SmtpEmail:
    return SmtpEmail(
        host="smtp.example.uz",
        port=587,
        username="relay-user",
        password="relay-password",  # noqa: S106 - a test value
        use_tls=True,
        use_ssl=False,
        sender="Glow Derma <no-reply@glowderma.uz>",
        timeout=10,
    )


def test_smtp_sends_text_and_html_over_starttls(smtp: type[RecordingSMTP]) -> None:
    message_id = adapter().send(MESSAGE)
    [connection] = smtp.instances
    assert (connection.host, connection.port, connection.kwargs["timeout"]) == ("smtp.example.uz", 587, 10)
    assert "starttls" in connection.calls and "login:relay-user" in connection.calls
    assert connection.calls[-1] == "quit"
    [(sender, recipients, mime)] = connection.sent
    assert sender == "no-reply@glowderma.uz" and recipients == ["malika@example.uz"]
    text = mime.as_string()
    assert (
        "multipart/alternative" in text and "text/html" in text and "Auto-Submitted: auto-generated" in text
    )
    assert message_id.endswith("@glowderma.uz") and f"<{message_id}>" in text


def test_smtp_implicit_tls_skips_starttls_and_an_open_relay_skips_login(
    smtp: type[RecordingSMTP], monkeypatch: pytest.MonkeyPatch
) -> None:
    chosen: list[bool] = []

    def choose(use_ssl: bool) -> type[RecordingSMTP]:
        chosen.append(use_ssl)
        return RecordingSMTP

    monkeypatch.setattr(smtp_module, "smtp_class", choose)
    relay = SmtpEmail(
        host="smtp.example.uz",
        port=465,
        username="",
        password="",
        use_tls=False,
        use_ssl=True,
        sender="no-reply@glowderma.uz",
        timeout=5,
    )
    relay.send(MESSAGE)
    [connection] = smtp.instances
    assert chosen == [True] and "context" in connection.kwargs
    assert connection.calls == ["quit"]


def test_smtp_class_follows_the_tls_mode() -> None:
    assert smtp_module.smtp_class(True) is smtplib.SMTP_SSL
    assert smtp_module.smtp_class(False) is smtplib.SMTP


@pytest.mark.parametrize(
    ("stage", "failure", "outcome"),
    [
        ("send", smtplib.SMTPRecipientsRefused({"malika@example.uz": (550, b"no")}), "rejected"),
        ("send", smtplib.SMTPSenderRefused(550, b"no", "no-reply@glowderma.uz"), "rejected"),
        ("send", smtplib.SMTPDataError(554, b"spam"), "rejected"),
        ("send", smtplib.SMTPDataError(451, b"later"), "unknown"),
        ("send", smtplib.SMTPServerDisconnected("gone"), "unknown"),
        ("send", TimeoutError("slow"), "unknown"),
        ("login", smtplib.SMTPAuthenticationError(535, b"bad"), "not_sent"),
        ("connect", ConnectionRefusedError("refused"), "not_sent"),
        ("connect", socket.gaierror("dns"), "not_sent"),
        ("connect", smtplib.SMTPConnectError(421, b"busy"), "not_sent"),
    ],
)
def test_smtp_failures_map_to_the_port_outcomes(
    smtp: type[RecordingSMTP],
    stage: str,
    failure: BaseException,
    outcome: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    smtp.stage, smtp.failure = stage, failure
    send: Callable[[], str] = lambda: adapter().send(MESSAGE)  # noqa: E731
    with caplog.at_level(logging.WARNING):
        if outcome == "rejected":
            with pytest.raises(EmailRejected):
                send()
        else:
            with pytest.raises(EmailUnavailable) as caught:
                send()
            assert caught.value.sent is (outcome == "unknown")
    assert "sms_code_id=7" in caplog.text
    assert "malika" not in caplog.text and "123456" not in caplog.text and "relay-password" not in caplog.text
