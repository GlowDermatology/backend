"""Live DevSMS adapter (``DEVSMS_MODE=live``; customer contract §2.14.1, ``docs/integrations/devsms.md``).

Protocol (DevSMS API v1, https://devsms.uz/docs/api?lang=en and its OpenAPI document
https://devsms.uz/docs/api/v1.json, read 2026-11-24):

* ``POST {DEVSMS_BASE_URL}/sms`` with ``Authorization: Bearer <DEVSMS_API_TOKEN>`` and a JSON body
  ``{"phone": "998901234567", "otp": {"template_type": 1-4, "service_name": "GlowDerma", "code": "482913"}}``:
  a **universal OTP** — one of DevSMS's four pre-approved templates (1 confirm an action, 2 password reset,
  3 sign-up, 4 sign-in), the text fixed by DevSMS, ``service_name`` 2–50 characters under DevSMS's automatic
  moderation, the code 4–8 digits. No ``message`` and no ``from``: the shared default sender (4546) is used,
  no Alpha Sender ID is registered.
* ``Idempotency-Key: <uuid>``: the key of the code (``OtpSms.idempotency_key``, one per ``SmsCode`` row). A
  repeat with the same key and body sends nothing and charges nothing: DevSMS returns the first request's
  answer (header ``Idempotent-Replayed: true``). Keys never expire and are shared by the whole account.
* ``200 {"success": true, "data": {"id": …, "status": "sent", …}}`` → accepted.

Outcomes (the ``SmsPort`` contract). Because every request carries the code's idempotency key, a request
that may or may not have been processed is safe to repeat:

* no answer (connect failure, timeout, a broken answer), a redirect, 5xx, 429 ``rate_limited``, 409
  ``idempotency_request_in_progress``, or an authentication refusal (401, and every 403 but the two
  definitive ones below: ``forbidden`` / ``account_blocked`` / ``token_locked_by_plan``, an access-denied page
  from an intermediary, …: configuration, ERROR ``devsms.auth_failed``) →
  ``SmsUnavailable(sent=False)``: the handler un-claims and the outbox repeats the request (same key);
* 502 ``provider_outcome_unknown`` → ``SmsUnavailable(sent=True)`` with DevSMS's ``meta.sms_id``: DevSMS
  charged and cannot say whether the operator delivered; a repeat only replays that answer, so nothing is
  resent (``GET /sms/{id}`` tells the outcome later);
* 402 ``insufficient_balance``, 403 ``moderation_rejected`` (the service name was refused: nothing sent or
  charged; DevSMS suspends universal OTP for 24 h after 20 refusals in a row) and 403 ``otp_suspended``
  (ERROR lines), 422 ``validation_failed`` / ``provider_rejected`` / ``unsupported_country`` /
  ``international_disabled`` / ``idempotency_key_reused``, 400 and every other 4xx → :class:`SmsRejected`
  with the reason, never retried.

No phone number, code, token or provider text is ever logged or put into an exception message: log lines carry
``sms_code_id``, ``pii_ref(phone)``, the HTTP status and DevSMS's ``error_code`` only.
"""

from __future__ import annotations

import logging
import re
from typing import Any

import httpx
from django.conf import settings

from glowderma.core.pii import pii_ref
from glowderma.integrations import httpclient
from glowderma.integrations.devsms.ports import OtpPurpose, OtpSms, SmsDelivery, SmsRejected, SmsUnavailable

logger = logging.getLogger(__name__)
PROVIDER = "devsms"
PATH_SEND = "/sms"
DEFAULT_TIMEOUT_SECONDS = 10

# The universal OTP template of each purpose (DevSMS ``otp.template_type``).
TEMPLATE_TYPES: dict[OtpPurpose, int] = {
    OtpPurpose.ACTION_CONFIRMATION: 1,
    OtpPurpose.PASSWORD_RESET: 2,
    OtpPurpose.REGISTRATION: 3,
    OtpPurpose.LOGIN: 4,
}
# DevSMS's documented bounds; the settings check the service name at start too.
CODE_PATTERN = re.compile(r"[0-9]{4,8}")
UZ_PHONE_PATTERN = re.compile(r"998[0-9]{9}")
TIMESTAMP_PATTERN = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}[ T][0-9]{2}:[0-9]{2}(:[0-9]{2})?([+-][0-9]{2}:[0-9]{2})?"
)

# The only 403s that are definitive refusals of this message (a repeat would only add to DevSMS's count of
# refusals). Every other 403 (``forbidden``, ``account_blocked``, ``token_locked_by_plan``, an intermediary's
# access-denied page) means the request was not processed: repeatable (AGENTS.md, live adapters).
DEFINITIVE_403_CODES = frozenset({"moderation_rejected", "otp_suspended"})
REPEAT_LATER_ERROR_CODES = frozenset({"idempotency_request_in_progress", "rate_limited"})
PHONE_ERROR_CODES = frozenset({"unsupported_country", "international_disabled"})


def template_type(purpose: OtpPurpose) -> int:
    """The DevSMS template of ``purpose``; :class:`SmsRejected` for a purpose with no universal template."""
    try:
        return TEMPLATE_TYPES[OtpPurpose(purpose)]
    except KeyError, ValueError:
        raise SmsRejected(
            f"no universal OTP template for {purpose!r}", reason="unsupported_purpose"
        ) from None


def provider_phone(phone: str) -> str | None:
    """The 12-digit ``998XXXXXXXXX`` DevSMS expects for an Uzbek E.164 number, else ``None``."""
    digits = phone.strip().removeprefix("+")
    return digits if UZ_PHONE_PATTERN.fullmatch(digits) else None


def valid_service_name(value: str) -> bool:
    """DevSMS's rule for ``service_name``: 2–50 characters, letters, digits, spaces, dots and hyphens."""
    return (
        2 <= len(value) <= 50
        and value == value.strip()
        and all(char.isalnum() or char in " .-" for char in value)
    )


def _error_code(body: dict[str, Any]) -> str:
    code = body.get("error_code")
    return code if isinstance(code, str) and re.fullmatch(r"[a-z_]{1,64}", code) else ""


def _meta(body: dict[str, Any]) -> dict[str, Any]:
    meta = body.get("meta")
    return meta if isinstance(meta, dict) else {}


class LiveDevsms:
    name = "devsms"

    def __init__(
        self,
        *,
        base_url: str,
        token: str,
        service_name: str,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.service_name = service_name
        self.timeout_seconds = float(timeout_seconds)
        self.transport = transport

    @classmethod
    def from_settings(cls) -> LiveDevsms:
        return cls(
            base_url=str(settings.DEVSMS_BASE_URL),
            token=str(settings.DEVSMS_API_TOKEN),
            service_name=str(settings.DEVSMS_SERVICE_NAME),
            timeout_seconds=float(settings.DEVSMS_TIMEOUT_SECONDS),
        )

    def is_configured(self) -> bool:
        """The readiness probe (``health/?ready=1``; settings only, nothing is sent): the API token is set and
        the service name follows DevSMS's rule."""
        return bool(self.token.strip()) and valid_service_name(self.service_name)

    def send_otp(self, message: OtpSms) -> SmsDelivery:
        ref = f"sms_code_id={message.sms_code_id} phone_ref={pii_ref(message.phone)}"
        template = template_type(message.purpose)
        phone = provider_phone(message.phone)
        if phone is None:
            logger.warning("devsms.send refused before sending: not an Uzbek mobile number %s", ref)
            raise SmsRejected("not an Uzbek mobile number", reason="invalid_phone")
        if not CODE_PATTERN.fullmatch(message.code):
            logger.error("devsms.send refused before sending: the code is not 4-8 digits %s", ref)
            raise SmsRejected("the code is not 4-8 digits", reason="invalid_code")
        body = {
            "phone": phone,
            "otp": {"template_type": template, "service_name": self.service_name, "code": message.code},
        }
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/json",
            "Idempotency-Key": message.idempotency_key,
        }
        try:
            with httpclient.new_client(self.transport) as http:
                answer = httpclient.send(
                    http,
                    "POST",
                    self.base_url + PATH_SEND,
                    provider=PROVIDER,
                    deadline=httpclient.Deadline(self.timeout_seconds),
                    json=body,
                    headers=headers,
                )
        except httpclient.TransportFailure as exc:
            # Whether or not the request left, a repeat with the same Idempotency-Key is safe.
            logger.warning("devsms.send no answer (%s, left=%s) %s; repeatable", exc, exc.sent, ref)
            raise SmsUnavailable(f"devsms send: {exc}", sent=False) from exc
        return self._outcome(answer, template, ref)

    def _outcome(self, answer: httpclient.Answer, template: int, ref: str) -> SmsDelivery:
        body = answer.json_object()
        code = _error_code(body)
        status = answer.status
        if answer.ok:
            data = body.get("data")
            message_id = data.get("id") if isinstance(data, dict) else None
            if body.get("success") is True and isinstance(message_id, int | str) and str(message_id):
                state = data.get("status") if isinstance(data, dict) else None
                logger.info("devsms.send accepted template_type=%s %s", template, ref)
                return SmsDelivery(message_id=str(message_id), status=state if isinstance(state, str) else "")
            logger.warning("devsms.send unexpected answer HTTP %s %s; repeatable", status, ref)
            raise SmsUnavailable(f"devsms send: unexpected answer HTTP {status}", sent=False)
        if status == 502 and code == "provider_outcome_unknown":
            sms_id = _meta(body).get("sms_id")
            logger.warning("devsms.send outcome unknown (charged) %s; not resent", ref)
            raise SmsUnavailable(
                "devsms send: provider outcome unknown",
                sent=True,
                message_id=str(sms_id) if isinstance(sms_id, int | str) else "",
            )
        if status == 401 or (status == 403 and code not in DEFINITIVE_403_CODES):
            logger.error("devsms.auth_failed status=%s error_code=%s (check DEVSMS_API_TOKEN)", status, code)
            raise SmsUnavailable(f"devsms send: HTTP {status} {code}".strip(), sent=False)
        if (
            code in REPEAT_LATER_ERROR_CODES
            or status in (408, 425, 429)
            or 300 <= status < 400
            or status >= 500
        ):
            logger.warning(
                "devsms.send not processed HTTP %s error_code=%s %s; repeatable", status, code, ref
            )
            raise SmsUnavailable(f"devsms send: HTTP {status} {code}".strip(), sent=False)
        raise self._rejection(status, code, body, ref)

    @staticmethod
    def _rejection(status: int, code: str, body: dict[str, Any], ref: str) -> SmsRejected:
        meta = _meta(body)
        if status == 402 or code == "insufficient_balance":
            logger.error("devsms.insufficient_balance %s (top up the DevSMS balance)", ref)
            return SmsRejected("devsms: insufficient balance", reason="insufficient_balance")
        if code == "moderation_rejected":
            remaining = meta.get("remaining_attempts")
            logger.error(
                "devsms.service_name_rejected remaining_attempts=%s %s (check DEVSMS_SERVICE_NAME)",
                remaining if isinstance(remaining, int) else "?",
                ref,
            )
            return SmsRejected(
                "devsms: the service name was rejected by moderation", reason="moderation_rejected"
            )
        if code == "otp_suspended":
            until = meta.get("blocked_until")
            logger.error(
                "devsms.otp_suspended blocked_until=%s %s",
                until if isinstance(until, str) and TIMESTAMP_PATTERN.fullmatch(until) else "?",
                ref,
            )
            return SmsRejected("devsms: universal OTP is suspended", reason="otp_suspended")
        if code in PHONE_ERROR_CODES:
            logger.warning("devsms.send rejected the number HTTP %s error_code=%s %s", status, code, ref)
            return SmsRejected(f"devsms: {code}", reason="invalid_phone")
        if code == "idempotency_key_reused":
            logger.error("devsms.idempotency_key_reused %s", ref)
        else:
            logger.warning("devsms.send rejected HTTP %s error_code=%s %s", status, code, ref)
        return SmsRejected(f"devsms HTTP {status} {code}".strip(), reason=code or f"http_{status}")


__all__ = [
    "CODE_PATTERN",
    "DEFAULT_TIMEOUT_SECONDS",
    "PATH_SEND",
    "PROVIDER",
    "TEMPLATE_TYPES",
    "LiveDevsms",
    "provider_phone",
    "template_type",
    "valid_service_name",
]
