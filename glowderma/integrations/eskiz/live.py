"""Live Eskiz adapter (``ESKIZ_MODE=live``; customer contract §2.14.1, ``docs/integrations/eskiz.md``).

Protocol (Eskiz's published Postman collection "СМС шлюз от Eskiz.uz", documenter.getpostman.com/view/663428/
RzfmES4z, read 2026-10-26):

* ``POST {ESKIZ_BASE_URL}/auth/login`` (form ``email``, ``password``) → ``{"data": {"token": …}}``;
* ``PATCH {ESKIZ_BASE_URL}/auth/refresh`` (``Authorization: Bearer <old token>``) →
  ``{"data": {"token": …}}``;
* ``POST {ESKIZ_BASE_URL}/message/sms/send`` (form ``mobile_phone`` = digits without ``+``, ``message``,
  ``from`` = ``ESKIZ_SENDER``, ``callback_url`` only when ``ESKIZ_CALLBACK_URL`` is set) →
  ``{"id": "<uuid>", "message": "Waiting for SMS provider", "status": "waiting"}``.

Forms are sent as ``multipart/form-data`` like the collection's examples. The token lives only in the Django
cache (``gd:eskiz:token``, 29 days) and is fetched single-flight across processes; a 401 on send renews it
once (refresh, else a new login) and repeats the send — a 401 means the message was not accepted.

Outcomes (the ``EskizPort`` contract): the Eskiz id → accepted; HTTP 4xx (invalid number, no balance, a text
that matches no approved template, credentials Eskiz refuses) → :class:`EskizRejected`, never retried; no
answer before the request left (connect failure) → ``EskizUnavailable(sent=False)`` (the handler un-claims and
the outbox retries); a timeout, a broken answer or HTTP 5xx after sending → ``EskizUnavailable(sent=True)``
(never resent: the user may already have the code). The text is sent exactly as rendered (catalog ``sms.otp``,
which must match an Eskiz-approved template character for character).

No phone number, message text, code, password or token is ever logged or put into an exception message:
log lines carry ``sms_code_id`` and ``pii_ref(phone)`` only, and Eskiz's own messages pass through
:func:`~glowderma.integrations.httpclient.safe_text`.
"""

from __future__ import annotations

import logging

import httpx
from django.conf import settings

from glowderma.core.pii import pii_ref

from .. import httpclient
from ..tokens import SharedToken, TokenBusy
from .ports import EskizRejected, EskizUnavailable, SmsMessage

logger = logging.getLogger(__name__)
PROVIDER = "eskiz"
TOKEN_TTL_SECONDS = 29 * 24 * 3600  # Eskiz tokens are valid 30 days (customer contract §2.14.1)
PATH_LOGIN = "/auth/login"
PATH_REFRESH = "/auth/refresh"
PATH_SEND = "/message/sms/send"


def _form(fields: dict[str, str]) -> dict[str, tuple[None, str]]:
    """``multipart/form-data`` fields without files (httpx renders ``(None, value)`` as a plain field)."""
    return {name: (None, value) for name, value in fields.items()}


class LiveEskiz:
    name = "eskiz"

    def __init__(
        self,
        *,
        base_url: str,
        email: str,
        password: str,
        sender: str,
        callback_url: str = "",
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.email = email
        self.password = password
        self.sender = sender
        self.callback_url = callback_url
        self.transport = transport

    @classmethod
    def from_settings(cls) -> LiveEskiz:
        return cls(
            base_url=str(settings.ESKIZ_BASE_URL),
            email=str(settings.ESKIZ_EMAIL),
            password=str(settings.ESKIZ_PASSWORD),
            sender=str(settings.ESKIZ_SENDER),
            callback_url=str(settings.ESKIZ_CALLBACK_URL or ""),
        )

    # --- token ----------------------------------------------------------------------------------------------

    @staticmethod
    def _token_of(answer: httpclient.Answer) -> str | None:
        data = answer.json_object().get("data")
        token = data.get("token") if isinstance(data, dict) else None
        return token if answer.status == 200 and isinstance(token, str) and token else None

    def _fetch(self, http: httpx.Client, deadline: httpclient.Deadline, stale: str | None) -> str:
        if stale:
            try:
                refreshed = httpclient.send(
                    http,
                    "PATCH",
                    self.base_url + PATH_REFRESH,
                    provider=PROVIDER,
                    deadline=deadline,
                    headers={"Authorization": f"Bearer {stale}", "Accept": "application/json"},
                )
            except httpclient.TransportFailure as exc:
                raise EskizUnavailable(f"eskiz token refresh: {exc}", sent=False) from exc
            token = self._token_of(refreshed)
            if token:
                return token
            logger.info("eskiz token refresh answered HTTP %s; logging in again", refreshed.status)
        try:
            answer = httpclient.send(
                http,
                "POST",
                self.base_url + PATH_LOGIN,
                provider=PROVIDER,
                deadline=deadline,
                files=_form({"email": self.email, "password": self.password}),
                headers={"Accept": "application/json"},
            )
        except httpclient.TransportFailure as exc:
            raise EskizUnavailable(f"eskiz login: {exc}", sent=False) from exc
        token = self._token_of(answer)
        if token:
            return token
        if 400 <= answer.status < 500:
            logger.error("eskiz.auth_failed status=%s (check ESKIZ_EMAIL / ESKIZ_PASSWORD)", answer.status)
            raise EskizRejected(f"eskiz refused the login: HTTP {answer.status}")
        raise EskizUnavailable(f"eskiz login: HTTP {answer.status}", sent=False)

    # --- port -----------------------------------------------------------------------------------------------

    def _post(
        self, http: httpx.Client, deadline: httpclient.Deadline, token: str, fields: dict[str, str]
    ) -> httpclient.Answer:
        return httpclient.send(
            http,
            "POST",
            self.base_url + PATH_SEND,
            provider=PROVIDER,
            deadline=deadline,
            files=_form(fields),
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        )

    def send(self, message: SmsMessage) -> str:
        digits = message.phone.removeprefix("+")
        ref = f"sms_code_id={message.sms_code_id} phone_ref={pii_ref(message.phone)}"
        if not digits.isdigit():
            logger.warning("eskiz.send refused before sending: malformed phone %s", ref)
            raise EskizRejected("malformed phone number")
        fields = {"mobile_phone": digits, "message": message.text, "from": self.sender}
        if self.callback_url:
            fields["callback_url"] = self.callback_url
        deadline = httpclient.Deadline()
        with httpclient.new_client(self.transport) as http:
            tokens = SharedToken(
                PROVIDER,
                ttl_seconds=TOKEN_TTL_SECONDS,
                fetch=lambda stale: self._fetch(http, deadline, stale),
            )
            try:
                token = tokens.get(deadline)
                answer = self._post(http, deadline, token, fields)
                if answer.status == 401:
                    token = tokens.renew(token, deadline)
                    answer = self._post(http, deadline, token, fields)
            except TokenBusy as exc:
                raise EskizUnavailable(str(exc), sent=False) from exc
            except httpclient.TransportFailure as exc:
                logger.warning("eskiz.send no answer (%s, sent=%s) %s", exc, exc.sent, ref)
                raise EskizUnavailable(f"eskiz send: {exc}", sent=exc.sent) from exc
        body = answer.json_object()
        if answer.status >= 500:
            logger.warning("eskiz.send HTTP %s after sending %s", answer.status, ref)
            raise EskizUnavailable(f"eskiz send: HTTP {answer.status}", sent=True)
        if answer.status >= 400:
            detail = httpclient.safe_text(body.get("message")) or "refused"
            logger.warning("eskiz.send rejected HTTP %s %s", answer.status, ref)
            raise EskizRejected(f"eskiz HTTP {answer.status}: {detail}")
        message_id = body.get("id")
        if answer.status // 100 != 2 or not isinstance(message_id, str | int) or not str(message_id):
            # the message may have been accepted: never resend it
            logger.warning("eskiz.send unexpected answer HTTP %s %s", answer.status, ref)
            raise EskizUnavailable(f"eskiz send: unexpected answer HTTP {answer.status}", sent=True)
        logger.info("eskiz.send accepted %s", ref)
        return str(message_id)
