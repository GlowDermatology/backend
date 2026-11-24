"""Live Uzum Checkout adapter (``UZUM_MODE=live``; docs/integrations/uzum.md).

Every call is a JSON POST to ``{UZUM_CHECKOUT_BASE_URL}{path}`` with ``X-Terminal-Id`` and ``X-API-Key``
(register adds ``Content-Language``, refund ``X-Operation-Id``). Answers are ``{errorCode, message,
result}`` and only HTTP 200 is documented, so ``errorCode`` is always read. Failure mapping:

* the request provably never left, or Uzum says it did not process it (a redirect — never followed —,
  401/403/408/425/429) → :class:`UzumNotProcessed`;
* no usable answer after the request may have left (timeout, broken body), HTTP 5xx, or a 2xx without an
  integer ``errorCode`` → :class:`UzumUnavailable` (outcome unknown);
* another 4xx → :class:`UzumRejected` with the body's ``errorCode`` when it has one, else ``http_<status>``;
* ``errorCode`` ≠ 0 → :class:`UzumRejected` (``str(errorCode)``, Uzum's message, digits masked);
* ``errorCode`` 0 with a result the adapter cannot read → :class:`UzumUnavailable`.

Nothing secret is logged: not the API key, not a body (``httpclient`` logs the path and the status only).
"""

from __future__ import annotations

import logging
from typing import Any

import httpx
from django.conf import settings

from .. import httpclient
from . import protocol
from .ports import (
    REGISTER_TIMEOUT_SECONDS,
    WORKER_TIMEOUT_SECONDS,
    OrderStatus,
    RefundAccepted,
    Registration,
    UzumNotProcessed,
    UzumRejected,
    UzumUnavailable,
)

logger = logging.getLogger(__name__)
PROVIDER = "uzum"


class LiveUzum:
    name = "uzum"

    def __init__(
        self,
        *,
        base_url: str,
        terminal_id: str,
        api_key: str,
        redirect_hosts: list[str] | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.terminal_id = terminal_id
        self.api_key = api_key
        self.redirect_hosts = [host.lower() for host in (redirect_hosts or [])]
        self.transport = transport

    @classmethod
    def from_settings(cls) -> LiveUzum:
        return cls(
            base_url=str(settings.UZUM_CHECKOUT_BASE_URL),
            terminal_id=str(settings.UZUM_TERMINAL_ID),
            api_key=str(settings.UZUM_API_KEY),
            redirect_hosts=list(getattr(settings, "UZUM_REDIRECT_HOSTS", []) or []),
        )

    def is_configured(self) -> bool:
        return bool(
            self.base_url.startswith("https://") and self.terminal_id.strip() and self.api_key.strip()
        )

    # --- helpers --------------------------------------------------------------------------------------------

    def _headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        return {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "X-Terminal-Id": self.terminal_id,
            "X-API-Key": self.api_key,
            **(extra or {}),
        }

    def _post(
        self,
        path: str,
        body: dict[str, Any],
        *,
        what: str,
        seconds: float,
        headers: dict[str, str] | None = None,
    ) -> Any:
        """The ``result`` of an ``errorCode`` 0 answer (see the module docstring for every other outcome)."""
        deadline = httpclient.Deadline(seconds)
        try:
            with httpclient.new_client(self.transport) as http:
                answer = httpclient.send(
                    http,
                    "POST",
                    f"{self.base_url}{path}",
                    provider=PROVIDER,
                    deadline=deadline,
                    json=body,
                    headers=self._headers(headers),
                )
        except httpclient.TransportFailure as exc:
            if not exc.sent:
                raise UzumNotProcessed(f"{what}: {exc}") from exc
            raise UzumUnavailable(f"{what}: {exc}") from exc
        if answer.status in (401, 403):
            logger.error(
                "uzum.auth_failed status=%s (check UZUM_TERMINAL_ID and UZUM_API_KEY)", answer.status
            )
        if answer.not_processed:
            raise UzumNotProcessed(f"{what}: HTTP {answer.status} (not processed)")
        if answer.status >= 500:
            raise UzumUnavailable(f"{what}: HTTP {answer.status}")
        data = answer.json_object()
        code = httpclient.int_code(data.get("errorCode"))
        message = httpclient.safe_text(data.get("message"), 160)
        if not answer.ok:
            raise UzumRejected(str(code) if code is not None else f"http_{answer.status}", message)
        if code is None:
            raise UzumUnavailable(f"{what}: answer without errorCode")
        if code != protocol.OK:
            raise UzumRejected(str(code), message)
        return data.get("result")

    # --- port -----------------------------------------------------------------------------------------------

    def register(
        self,
        *,
        order_number: str,
        amount: int,
        client_id: str,
        language: str,
        success_url: str,
        failure_url: str,
        session_timeout: int,
    ) -> Registration:
        body = {
            "amount": amount,
            "clientId": client_id,
            "currency": protocol.CURRENCY_UZS,
            "orderNumber": order_number,
            "viewType": protocol.VIEW_TYPE_REDIRECT,
            "sessionTimeoutSecs": session_timeout,
            "successUrl": success_url,
            "failureUrl": failure_url,
            "paymentParams": {"payType": protocol.PAY_TYPE_ONE_STEP, "operationType": "PAYMENT"},
        }
        result = self._post(
            protocol.PATH_REGISTER,
            body,
            what="Uzum register",
            seconds=REGISTER_TIMEOUT_SECONDS,
            headers={"Content-Language": protocol.content_language(language)},
        )
        result = result if isinstance(result, dict) else {}
        order_id = result.get("orderId")
        url = result.get("paymentRedirectUrl")
        if not protocol.is_uuid(order_id):
            raise UzumUnavailable("Uzum register: answer without an orderId")
        if not protocol.redirect_url_ok(url, self.redirect_hosts):
            # registered at Uzum, but we will not send a payer there: the order stays unpaid at Uzum
            logger.error("uzum.redirect_refused (not https or not an allowed host)")
            raise UzumRejected("redirect_refused", "Uzum returned a payment page we do not trust")
        return Registration(order_id=str(order_id).lower(), redirect_url=str(url))

    def order_status(self, order_id: str) -> OrderStatus:
        if not protocol.is_uuid(order_id):
            raise UzumRejected("invalid_order_id", "not an Uzum order id")
        result = self._post(
            protocol.PATH_ORDER_STATUS,
            {"orderId": order_id},
            what="Uzum status",
            seconds=WORKER_TIMEOUT_SECONDS,
        )
        status = protocol.parse_order_status(result)
        if status is None:
            raise UzumUnavailable("Uzum status: answer we cannot read")
        if status.order_id != order_id.lower():
            raise UzumUnavailable("Uzum status: answer for another order")
        return status

    def refund(self, order_id: str, amount: int, operation_id: str) -> RefundAccepted:
        if not protocol.is_uuid(order_id):
            raise UzumRejected("invalid_order_id", "not an Uzum order id")
        result = self._post(
            protocol.PATH_REFUND,
            {"orderId": order_id, "amount": amount},
            what="Uzum refund",
            seconds=WORKER_TIMEOUT_SECONDS,
            headers={"X-Operation-Id": operation_id},
        )
        result = result if isinstance(result, dict) else {}
        accepted = result.get("operationId")
        return RefundAccepted(operation_id=str(accepted).lower() if protocol.is_uuid(accepted) else "")
