"""Live Click adapter (``CLICK_MODE=live``): the outbound Merchant API — payment status check and reversal by
Click's payment id (``click_paydoc_id``) — and the pay page URL (customer contract §2.7.2, §2.13.0;
``docs/integrations/click.md``). The SHOP API callbacks (Prepare/Complete) are the webhook views; nothing here
is called by them.

Requests go to ``{CLICK_MERCHANT_API_URL}/payment/status/{service_id}/{payment_id}`` (GET) and
``…/payment/reversal/{service_id}/{payment_id}`` (DELETE) with the signed ``Auth`` header of
:func:`.protocol.merchant_auth_header`; each call is bounded (3 s connect, 10 s in total) and only a request
that never left is retried (:mod:`glowderma.integrations.httpclient`).

Failure mapping (the refund handler asks :meth:`LiveClick.payment_status` before any reversal and again after
a refusal or an unknown outcome, so it only needs these to be truthful). Any 2xx is an answer (Click documents
200 and 201):

* the request provably never left, or Click answered that it did **not** process it (a redirect — never
  followed —, 408 / 425 / 429, 401 / 403) → ``ClickNotProcessed`` for both calls (a reversal's claim is
  released and the outbox retries);
* no usable answer after the request may have left (timeout, broken or undecodable body) or HTTP 5xx →
  ``ClickUnavailable`` (for a reversal: outcome unknown);
* another HTTP 4xx (bad request, unknown payment) → status: ``ClickStatusUnknown``; reversal:
  ``ClickRefundRejected`` with Click's ``error_code`` when the body carries one, else ``http_<status>``;
* 2xx that is not a JSON object with an integer ``error_code`` → status: ``ClickStatusUnknown``; reversal:
  ``ClickUnavailable`` (outcome unknown);
* 2xx with ``error_code`` ≠ 0 → status: ``ClickStatusUnknown``; reversal:
  ``ClickRefundRejected(str(code), error_note)``;
* 2xx with ``error_code`` 0 → status: ``paid`` (``payment_status`` 2) / ``reversed`` (``-99``, verify M3)
  (:func:`.protocol.merchant_payment_state`), any other ``payment_status`` ``ClickStatusUnknown``; reversal:
  ``RefundReceipt(payment_id)``;
* an answer of any other shape the adapter cannot read → ``ClickStatusUnknown`` / ``ClickUnavailable``, never
  an untyped exception.

A 401/403 (a wrong ``CLICK_MERCHANT_USER_ID`` / secret, or a clock far off) is logged at ERROR
``click.auth_failed``. Nothing secret is logged: not the ``Auth`` header, not the secret, not a body.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import httpx
from django.conf import settings

from .. import httpclient
from . import protocol
from .ports import (
    ClickNotProcessed,
    ClickRefundRejected,
    ClickStatusUnknown,
    ClickUnavailable,
    PaymentState,
    RefundReceipt,
)

logger = logging.getLogger(__name__)
PROVIDER = "click"
INVALID_PAYMENT_ID = "invalid_payment_id"


class LiveClick:
    name = "click"

    def __init__(
        self,
        *,
        api_url: str,
        service_id: str,
        merchant_id: str,
        merchant_user_id: str,
        secret_key: str,
        pay_url: str,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.api_url = api_url.rstrip("/")
        self.service_id = service_id
        self.merchant_id = merchant_id
        self.merchant_user_id = merchant_user_id
        self.secret_key = secret_key
        self.pay_base = pay_url
        self.transport = transport

    @classmethod
    def from_settings(cls) -> LiveClick:
        return cls(
            api_url=str(settings.CLICK_MERCHANT_API_URL),
            service_id=str(settings.CLICK_SERVICE_ID),
            merchant_id=str(settings.CLICK_MERCHANT_ID),
            merchant_user_id=str(settings.CLICK_MERCHANT_USER_ID),
            secret_key=str(settings.CLICK_SECRET_KEY),
            pay_url=str(settings.CLICK_PAY_URL),
        )

    def is_configured(self) -> bool:
        """The readiness probe (``health/?ready=1``; settings only, nothing is sent): the three numeric Click
        ids are real (not the ``00000`` default) and the secret key is set."""
        ids = (self.service_id, self.merchant_id, self.merchant_user_id)
        real_ids = all(value.strip().isdigit() and value.strip().strip("0") for value in ids)
        return bool(real_ids and self.secret_key.strip())

    # --- helpers --------------------------------------------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        return {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Auth": protocol.merchant_auth_header(self.merchant_user_id, self.secret_key, int(time.time())),
        }

    def _url(self, template: str, payment_ref: str) -> str:
        return f"{self.api_url}/" + template.format(service_id=self.service_id, payment_id=payment_ref)

    def _call(self, method: str, template: str, payment_ref: str, *, retry_unsent: bool) -> httpclient.Answer:
        deadline = httpclient.Deadline()
        with httpclient.new_client(self.transport) as http:
            answer = httpclient.send(
                http,
                method,
                self._url(template, payment_ref),
                provider=PROVIDER,
                deadline=deadline,
                retry_unsent=retry_unsent,
                headers=self._headers(),
            )
        if answer.status in (401, 403):
            logger.error(
                "click.auth_failed status=%s (check CLICK_MERCHANT_USER_ID and the secret)", answer.status
            )
        return answer

    @staticmethod
    def _error_code(body: dict[str, Any]) -> int | None:
        return httpclient.int_code(body.get("error_code"))

    def _answer(self, method: str, template: str, payment_ref: str, what: str) -> httpclient.Answer:
        """The HTTP answer; no answer → ``ClickNotProcessed`` (never sent) / ``ClickUnavailable`` (may have
        left), an answer that says "not processed" → ``ClickNotProcessed``, 5xx → ``ClickUnavailable``."""
        try:
            answer = self._call(method, template, payment_ref, retry_unsent=True)
        except httpclient.TransportFailure as exc:
            if not exc.sent:
                raise ClickNotProcessed(f"{what}: {exc}") from exc
            raise ClickUnavailable(f"{what}: {exc}") from exc
        if answer.not_processed:
            raise ClickNotProcessed(f"{what}: HTTP {answer.status} (not processed)")
        if answer.status >= 500:
            raise ClickUnavailable(f"{what}: HTTP {answer.status}")
        return answer

    # --- port -----------------------------------------------------------------------------------------------

    def payment_status(self, payment_ref: str) -> PaymentState:
        if not payment_ref.isascii() or not payment_ref.isdigit():
            raise ClickStatusUnknown("not a Click payment id")
        what = "Click status check"
        answer = self._answer("GET", protocol.MERCHANT_PATH_STATUS, payment_ref, what)
        if not answer.ok:
            raise ClickStatusUnknown(f"{what}: HTTP {answer.status}")
        body = answer.json_object()
        code = self._error_code(body)
        if code is None:
            raise ClickStatusUnknown(f"{what}: answer without error_code")
        if code != protocol.MERCHANT_API_OK:
            note = httpclient.safe_text(body.get("error_note"), 120)
            raise ClickStatusUnknown(f"{what}: error_code {code} {note}".strip())
        raw = body.get("payment_status")
        state = protocol.merchant_payment_state(raw)
        if state is None:
            shown = httpclient.safe_text(raw if isinstance(raw, int | str) else type(raw).__name__, 32)
            logger.warning("click.status_unknown payment_status=%s", shown)
            raise ClickStatusUnknown(f"{what}: payment_status {shown}")
        return state

    def refund(self, payment_ref: str, amount: int) -> RefundReceipt:
        """Reverse the whole payment (Click's ``payment/reversal`` has no amount; ``amount`` is the refund's,
        always the full payment amount)."""
        if not payment_ref.isascii() or not payment_ref.isdigit():
            raise ClickRefundRejected(INVALID_PAYMENT_ID, "not a Click payment id")
        what = "Click reversal"
        answer = self._answer("DELETE", protocol.MERCHANT_PATH_REVERSAL, payment_ref, what)
        body = answer.json_object()
        code = self._error_code(body)
        if not answer.ok:
            # a 4xx Click explains in its own body keeps Click's code (≤ 19 characters, fits error_code)
            note = httpclient.safe_text(body.get("error_note"), 120)
            error_code = str(code) if code is not None else f"http_{answer.status}"
            detail = f"HTTP {answer.status} error_code {code} {note}" if code is not None else ""
            detail = detail.rstrip() or f"HTTP {answer.status}"
            raise ClickRefundRejected(error_code, f"Click refused the reversal: {detail}")
        if code is None:
            raise ClickUnavailable(f"{what}: answer without error_code")
        if code != protocol.MERCHANT_API_OK:
            note = httpclient.safe_text(body.get("error_note"), 120) or "refused"
            raise ClickRefundRejected(str(code), f"Click refused the reversal: {note}")
        payment_id = body.get("payment_id")
        reference = httpclient.int_code(payment_id)
        return RefundReceipt(provider_refund_id=str(reference) if reference is not None else payment_ref)

    def pay_url(self, account_ref: str, amount: int, return_url: str) -> str:
        return protocol.pay_url(
            self.pay_base,
            service_id=self.service_id,
            merchant_id=self.merchant_id,
            amount=amount,
            account_ref=account_ref,
            return_url=return_url,
        )
