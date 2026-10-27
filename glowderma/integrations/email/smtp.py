"""SMTP e-mail adapter (``EMAIL_MODE=smtp``): any relay, chosen through the ``EMAIL_HOST``, ``EMAIL_PORT``,
``EMAIL_HOST_USER``, ``EMAIL_HOST_PASSWORD``, ``EMAIL_USE_TLS``, ``EMAIL_USE_SSL``, ``EMAIL_TIMEOUT`` and
``EMAIL_FROM`` variables (settings ``SMTP_*`` and ``EMAIL_FROM``).

One ``smtplib`` conversation per message (STARTTLS or implicit TLS with the system's default certificate
checks, a login when an account is set), a plain-text body with an HTML alternative, a ``Message-ID`` of the
sender's domain and ``Auto-Submitted: auto-generated``. ``django.core.mail`` is not used: Django 6.1
deprecates its ``EMAIL_*`` settings and its connection helper. Failures map to the port's outcomes
(``integrations.email.ports``):

* the relay refused the recipient or the sender, or answered a permanent 5xx to the data → ``EmailRejected``;
* no connection (DNS, refused, TLS handshake), a refused ``HELO``/``STARTTLS`` or login → ``EmailUnavailable``
  with ``sent=False`` (nothing was accepted: the outbox retries);
* anything else once the message was being handed over (a timeout, a dropped connection, a 4xx to the data)
  → ``EmailUnavailable`` with ``sent=True`` (unknown: never resent).

Nothing personal or secret is logged: the message reference, the exception class and the SMTP code at most.
"""

from __future__ import annotations

import logging
import smtplib
import socket
import ssl
from email.message import EmailMessage as MimeMessage
from email.utils import formatdate, make_msgid, parseaddr

from django.conf import settings

from .ports import EmailMessage, EmailRejected, EmailUnavailable

logger = logging.getLogger(__name__)
# Raised before the relay accepted anything (connection, greeting, TLS, login).
NOT_SENT_ERRORS: tuple[type[BaseException], ...] = (
    smtplib.SMTPConnectError,
    smtplib.SMTPHeloError,
    smtplib.SMTPAuthenticationError,
    smtplib.SMTPNotSupportedError,
    ConnectionRefusedError,
    socket.gaierror,
    ssl.SSLError,
)


def smtp_class(use_ssl: bool) -> type[smtplib.SMTP]:
    """``smtplib.SMTP_SSL`` for implicit TLS, else ``smtplib.SMTP`` (tests replace this function)."""
    return smtplib.SMTP_SSL if use_ssl else smtplib.SMTP


class SmtpEmail:
    name = "smtp"

    def __init__(
        self,
        *,
        host: str,
        port: int,
        username: str,
        password: str,
        use_tls: bool,
        use_ssl: bool,
        sender: str,
        timeout: int,
    ) -> None:
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.use_tls = use_tls
        self.use_ssl = use_ssl
        self.sender = sender
        self.timeout = timeout

    @classmethod
    def from_settings(cls) -> SmtpEmail:
        return cls(
            host=str(settings.SMTP_HOST),
            port=int(settings.SMTP_PORT),
            username=str(settings.SMTP_USER),
            password=str(settings.SMTP_PASSWORD),
            use_tls=bool(settings.SMTP_USE_TLS),
            use_ssl=bool(settings.SMTP_USE_SSL),
            sender=str(settings.EMAIL_FROM),
            timeout=int(settings.SMTP_TIMEOUT),
        )

    @property
    def sender_address(self) -> str:
        return parseaddr(self.sender)[1]

    def is_configured(self) -> bool:
        return bool(self.host) and "@" in self.sender_address

    def build(self, message: EmailMessage) -> MimeMessage:
        mime = MimeMessage()
        mime["Subject"] = message.subject
        mime["From"] = self.sender
        mime["To"] = message.to
        mime["Date"] = formatdate(localtime=False)
        mime["Message-ID"] = make_msgid(domain=self.sender_address.rpartition("@")[2] or None)
        mime["Auto-Submitted"] = "auto-generated"
        mime.set_content(message.text)
        mime.add_alternative(message.html, subtype="html")
        return mime

    def send(self, message: EmailMessage) -> str:
        mime = self.build(message)
        try:
            self._deliver(mime)
        except (smtplib.SMTPRecipientsRefused, smtplib.SMTPSenderRefused) as exc:
            logger.warning("email.send refused %s (%s)", message.reference, type(exc).__name__)
            raise EmailRejected(type(exc).__name__) from None
        except smtplib.SMTPDataError as exc:
            if 500 <= int(exc.smtp_code) < 600:
                logger.warning("email.send refused %s (data %s)", message.reference, exc.smtp_code)
                raise EmailRejected(f"data refused {exc.smtp_code}") from None
            logger.warning("email.send outcome unknown %s (data %s)", message.reference, exc.smtp_code)
            raise EmailUnavailable(f"data {exc.smtp_code}", sent=True) from None
        except NOT_SENT_ERRORS as exc:
            logger.warning("email.send not sent %s (%s)", message.reference, type(exc).__name__)
            raise EmailUnavailable(type(exc).__name__, sent=False) from None
        except (smtplib.SMTPException, OSError) as exc:
            logger.warning("email.send outcome unknown %s (%s)", message.reference, type(exc).__name__)
            raise EmailUnavailable(type(exc).__name__, sent=True) from None
        return str(mime["Message-ID"]).strip("<>")

    def _deliver(self, mime: MimeMessage) -> None:
        context = ssl.create_default_context()
        kwargs: dict[str, object] = {"timeout": self.timeout}
        if self.use_ssl:
            kwargs["context"] = context
        with smtp_class(self.use_ssl)(self.host, self.port, **kwargs) as connection:  # type: ignore[arg-type]
            if self.use_tls:
                connection.starttls(context=context)
            if self.username:
                connection.login(self.username, self.password)
            connection.send_message(mime, from_addr=self.sender_address, to_addrs=[str(mime["To"])])
