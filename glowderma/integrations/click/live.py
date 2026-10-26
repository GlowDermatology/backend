"""Live Click adapter (``CLICK_MODE=live``): the outbound Merchant API — payment status check and reversal by
Click's payment id (``click_paydoc_id``) — and the pay page URL (customer contract §2.7.2, §2.13.0;
``docs/integrations/click.md``). The SHOP API callbacks (Prepare/Complete) are the webhook views; nothing here
is called by them.

Requests go to ``{CLICK_MERCHANT_API_URL}/payment/status/{service_id}/{payment_id}`` (GET) and
``…/payment/reversal/{service_id}/{payment_id}`` (DELETE) with the signed ``Auth`` header of
:func:`.protocol.merchant_auth_header`; each call is bounded (3 s connect, 10 s in total) and only a request
that never left is retried (:mod:`glowderma.integrations.httpclient`).

Failure mapping (the refund handler asks :meth:`LiveClick.payment_status` before any reversal and again after
a refusal or an unknown outcome, so it only needs these to be truthful):

* no answer (connect failure, timeout, broken body) or HTTP 5xx → ``ClickUnavailable`` for both calls (for a
  reversal: outcome unknown);
* HTTP 4xx (auth, unknown payment, bad request) → status: ``ClickStatusUnknown``; reversal:
  ``ClickRefundRejected("http_<status>")``;
* HTTP 200 that is not a JSON object with ``error_code`` → status: ``ClickStatusUnknown``; reversal:
  ``ClickUnavailable`` (outcome unknown);
* HTTP 200 with ``error_code`` ≠ 0 → status: ``ClickStatusUnknown``; reversal:
  ``ClickRefundRejected(str(code), error_note)``;
* HTTP 200 with ``error_code`` 0 → status: ``paid`` / ``reversed`` (:func:`.protocol.merchant_payment_state`),
  any other ``payment_status`` ``ClickStatusUnknown``; reversal: ``RefundReceipt(payment_id)``.

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
        code = body.get("error_code")
        if isinstance(code, bool):
            return None
        if isinstance(code, int):
            return code
        if isinstance(code, str) and code.lstrip("-").isdigit():
            return int(code)
        return None

    # --- port -----------------------------------------------------------------------------------------------

    def payment_status(self, payment_ref: str) -> PaymentState:
        if not payment_ref.isdigit():
            raise ClickStatusUnknown("not a Click payment id")
        try:
            answer = self._call("GET", protocol.MERCHANT_PATH_STATUS, payment_ref, retry_unsent=True)
        except httpclient.TransportFailure as exc:
            raise ClickUnavailable(f"Click status check: {exc}") from exc
        if answer.status >= 500:
            raise ClickUnavailable(f"Click status check: HTTP {answer.status}")
        if answer.status != 200:
            raise ClickStatusUnknown(f"Click status check: HTTP {answer.status}")
        body = answer.json_object()
        code = self._error_code(body)
        if code is None:
            raise ClickStatusUnknown("Click status check: answer without error_code")
        if code != protocol.MERCHANT_API_OK:
            note = httpclient.safe_text(body.get("error_note"))
            raise ClickStatusUnknown(f"Click status check: error_code {code} {note}".strip())
        state = protocol.merchant_payment_state(body.get("payment_status"))
        if state is None:
            logger.warning("click.status_unknown payment_status=%r", body.get("payment_status"))
            raise ClickStatusUnknown(f"Click status check: payment_status {body.get('payment_status')!r}")
        return state

    def refund(self, payment_ref: str, amount: int) -> RefundReceipt:
        """Reverse the whole payment (Click's ``payment/reversal`` has no amount; ``amount`` is the refund's,
        always the full payment amount)."""
        if not payment_ref.isdigit():
            raise ClickRefundRejected(INVALID_PAYMENT_ID, "not a Click payment id")
        try:
            answer = self._call("DELETE", protocol.MERCHANT_PATH_REVERSAL, payment_ref, retry_unsent=True)
        except httpclient.TransportFailure as exc:
            raise ClickUnavailable(f"Click reversal: {exc}") from exc
        if answer.status >= 500:
            raise ClickUnavailable(f"Click reversal: HTTP {answer.status}")
        if answer.status != 200:
            raise ClickRefundRejected(
                f"http_{answer.status}", f"Click refused the reversal: HTTP {answer.status}"
            )
        body = answer.json_object()
        code = self._error_code(body)
        if code is None:
            raise ClickUnavailable("Click reversal: answer without error_code")
        if code != protocol.MERCHANT_API_OK:
            note = httpclient.safe_text(body.get("error_note")) or "refused"
            raise ClickRefundRejected(str(code), f"Click refused the reversal: {note}")
        payment_id = body.get("payment_id")
        reference = str(payment_id) if isinstance(payment_id, int | str) and str(payment_id) else payment_ref
        return RefundReceipt(provider_refund_id=reference[:64])

    def pay_url(self, account_ref: str, amount: int, return_url: str) -> str:
        return protocol.pay_url(
            self.pay_base,
            service_id=self.service_id,
            merchant_id=self.merchant_id,
            amount=amount,
            account_ref=account_ref,
            return_url=return_url,
        )
