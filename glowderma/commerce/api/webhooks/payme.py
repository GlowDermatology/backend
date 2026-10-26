"""Payme Merchant API (JSON-RPC 2.0) callbacks (customer contract §2.13.2): ``webhooks/payme/``.

Every answer is HTTP 200 with a JSON-RPC body; amounts are tiyin and exist only here; times are epoch ms. Basic
auth with ``PAYME_KEY`` first (``-32504``), then the JSON-RPC envelope, then the method — each method takes the
advisory lock of its Payme transaction and ``commerce.services.lock_order`` before reading state, and the
``WebhookInbox`` row is written last (every delivery its own row; read-only methods without the raw body). An
unexpected exception rolls everything back and answers ``-32400``. A Payme port that is fake under the production
settings answers ``-32504`` to every call before anything is read.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from typing import Any

from django.conf import settings
from django.db import transaction
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema
from rest_framework.request import Request
from rest_framework.response import Response

from glowderma.accounts.api.customer_base import WebhookAPIView
from glowderma.commerce import payment_ref, selectors, services, state
from glowderma.commerce.api.webhooks import inbox
from glowderma.commerce.api.webhooks.click import advisory_lock
from glowderma.commerce.models import Order
from glowderma.core import enums, integration_modes, timeutils
from glowderma.core.api.exceptions import AlreadyPaid, InvalidTransition, PaymentInProgress
from glowderma.integrations.payme import protocol as rpc
from glowderma.payments import selectors as payment_selectors
from glowderma.payments import services as payment_services

logger = logging.getLogger(__name__)
PROVIDER = "payme"
PAYME = enums.PaymentProvider.PAYME


class RpcError(Exception):
    def __init__(self, code: int, data: Any = None) -> None:
        super().__init__(code)
        self.code = code
        self.data = data


def now_ms() -> int:
    return int(timeutils.now().timestamp() * 1000)


def _account_field() -> str:
    return str(settings.PAYME_ACCOUNT_FIELD)


def _order_not_found() -> RpcError:
    return RpcError(rpc.ERR_ORDER_NOT_FOUND, _account_field())


def _busy() -> RpcError:
    return RpcError(rpc.ERR_ORDER_BUSY, _account_field())


def _state(payment: payment_selectors.PaymentRow) -> dict[str, Any]:
    return dict(payment.provider_state or {})


class PaymeView(WebhookAPIView):
    http_method_names = ["post", "get", "put", "patch", "delete"]

    @extend_schema(
        operation_id="webhook_payme_rpc",
        description="Payme Merchant API (JSON-RPC 2.0); HTTP 200 with a JSON-RPC body (customer contract §2.13.2).",
        request={"application/json": OpenApiTypes.OBJECT},
        responses={200: OpenApiTypes.OBJECT},
        auth=[],
    )
    def post(self, request: Request) -> Response:
        started = time.monotonic()
        if integration_modes.callbacks_refused(PROVIDER):  # nothing read before this check (§2.13.0)
            return self._rejected(request, None, None, "fake_mode", started)
        if not rpc.auth_ok(str(request.headers.get("Authorization", "")), str(settings.PAYME_KEY)):
            return self._rejected(request, *self._peek(request), "bad_auth", started)
        if not inbox.ip_allowed(request, settings.PAYME_ALLOWED_IPS):
            return self._rejected(request, *self._peek(request), "ip_not_allowed", started)
        raw = request.body.decode("utf-8", errors="replace")
        try:
            body = json.loads(raw)
        except ValueError:
            return self._answer_error(request, raw, None, rpc.ERR_PARSE, None, "-", started)
        request_id = body.get("id") if isinstance(body, dict) else None
        if (
            not isinstance(body, dict)
            or not isinstance(body.get("method"), str)
            or not isinstance(body.get("params"), dict)
            or "id" not in body
        ):
            return self._answer_error(request, raw, body, rpc.ERR_INVALID_REQUEST, request_id, "-", started)
        method = str(body["method"])
        params: dict[str, Any] = body["params"]
        if method not in rpc.METHODS:
            return self._answer_error(
                request, raw, body, rpc.ERR_METHOD_NOT_FOUND, request_id, method, started
            )
        try:
            with transaction.atomic():
                try:
                    result, entity = self._dispatch(method, params)
                    answer = rpc.rpc_result(request_id, result)
                    error_code: str | None = None
                except RpcError as exc:
                    answer = rpc.rpc_error(request_id, exc.code, exc.data)
                    error_code, entity = str(exc.code), ""
                self._store(request, raw, body, method, params, answer, error_code, entity, started)
                return Response(answer)
        except Exception:
            logger.exception("payme.%s failed; everything rolled back", method)
            return Response(rpc.rpc_error(request_id, rpc.ERR_SYSTEM))

    @extend_schema(exclude=True)
    def get(self, request: Request) -> Response:
        """Any non-POST: JSON-RPC ``-32300`` (HTTP 200, as the protocol requires)."""
        return Response(rpc.rpc_error(None, rpc.ERR_TRANSPORT))

    put = patch = delete = get

    # --- inbox -----------------------------------------------------------------------------------------------

    @staticmethod
    def _peek(request: Request) -> tuple[Any, Any]:
        """``(body, id)`` of an unauthenticated request, for the bounded row and the echoed id only."""
        try:
            body = json.loads(request.body.decode("utf-8", errors="replace"))
        except ValueError:
            return None, None
        return body, body.get("id") if isinstance(body, dict) else None

    def _rejected(
        self, request: Request, payload: Any, request_id: Any, code: str, started: float
    ) -> Response:
        answer = rpc.rpc_error(request_id, rpc.ERR_INSUFFICIENT_PRIVILEGE)
        inbox.store_rejected(
            PROVIDER, request, payload, status=200, body=answer, error_code=code, started=started
        )
        return Response(answer)

    def _answer_error(
        self, request: Request, raw: str, body: Any, code: int, request_id: Any, method: str, started: float
    ) -> Response:
        answer = rpc.rpc_error(request_id, code)
        inbox.store_delivery(
            PROVIDER,
            f"{method}:-:{uuid.uuid4().hex}",
            request,
            body if isinstance(body, (dict, list)) else None,
            status=200,
            body=answer,
            outcome=inbox.OUTCOME_REJECTED,
            error_code=str(code),
            raw_body=raw,
            started=started,
        )
        return Response(answer)

    def _store(
        self,
        request: Request,
        raw: str,
        body: dict[str, Any],
        method: str,
        params: dict[str, Any],
        answer: dict[str, Any],
        error_code: str | None,
        entity: str,
        started: float,
    ) -> None:
        txn = params.get("id")
        inbox.store_delivery(
            PROVIDER,
            f"{method}:{txn if isinstance(txn, str) and txn else '-'}:{uuid.uuid4().hex}",
            request,
            body,
            status=200,
            body=answer,
            outcome=inbox.OUTCOME_ACCEPTED if error_code is None else inbox.OUTCOME_REJECTED,
            error_code=error_code,
            entity_ref=entity,
            raw_body="" if method in rpc.READ_ONLY_METHODS else raw,
            started=started,
        )

    # --- methods ---------------------------------------------------------------------------------------------

    def _dispatch(self, method: str, params: dict[str, Any]) -> tuple[Any, str]:
        handler = {
            "CheckPerformTransaction": self._check_perform,
            "CreateTransaction": self._create,
            "PerformTransaction": self._perform,
            "CancelTransaction": self._cancel,
            "CheckTransaction": self._check,
            "GetStatement": self._statement,
        }[method]
        return handler(params)

    @staticmethod
    def _order_from_account(params: dict[str, Any]) -> int:
        account = params.get("account")
        value = account.get(_account_field()) if isinstance(account, dict) else None
        order_id = payment_ref.verify(value)
        if order_id is None:
            raise _order_not_found()
        return order_id

    @staticmethod
    def _lock(order_id: int) -> Order:
        try:
            return services.lock_order(order_id)
        except services.OrderNotFound as exc:
            raise _order_not_found() from exc

    @staticmethod
    def _refuse_unpayable(order: Order) -> None:
        if order.status in state.PAID_STATUSES or selectors.honoured_paid(order.pk):
            raise RpcError(rpc.ERR_CANNOT_PERFORM)
        if not selectors.is_payable(order, None, timeutils.now(), attaching=True):
            raise RpcError(rpc.ERR_CANNOT_PERFORM)

    @staticmethod
    def _refuse_busy(order: Order) -> None:
        now = timeutils.now()
        for payment in order.payments.all():
            if payment.provider == PAYME and selectors.is_open(payment):
                raise _busy()
            if payment.provider == enums.PaymentProvider.CLICK and selectors.in_flight(payment, now):
                raise _busy()

    @staticmethod
    def _check_amount(params: dict[str, Any], order: Order) -> None:
        amount = params.get("amount")
        if not rpc.is_int(amount) or amount <= 0 or amount != order.total * 100:
            raise RpcError(rpc.ERR_WRONG_AMOUNT)

    def _check_perform(self, params: dict[str, Any]) -> tuple[Any, str]:
        order_id = self._order_from_account(params)
        advisory_lock(f"payme:order:{order_id}")
        order = self._lock(order_id)
        self._refuse_unpayable(order)
        self._refuse_busy(order)
        self._check_amount(params, order)
        return {"allow": True}, f"order:{order.pk}"

    @staticmethod
    def _txn_id(params: dict[str, Any]) -> str:
        value = params.get("id")
        if not isinstance(value, str) or not value or len(value) > 64:
            raise RpcError(rpc.ERR_INVALID_REQUEST)
        return value

    def _payment(self, params: dict[str, Any]) -> tuple[payment_selectors.PaymentRow, Order]:
        txn = self._txn_id(params)
        advisory_lock(f"payme:{txn}")
        found = payment_selectors.payment_by_txn(PAYME, txn)
        if found is None:
            raise RpcError(rpc.ERR_TRANSACTION_NOT_FOUND)
        order = services.lock_order(found.order_id)
        payment = payment_selectors.get_payment(found.id)
        assert payment is not None  # noqa: S101 - payments are never deleted
        return payment, order

    @staticmethod
    def _timed_out(payment: payment_selectors.PaymentRow) -> bool:
        created = _state(payment).get("create_time")
        return rpc.is_int(created) and now_ms() - int(created) > rpc.TIMEOUT_MS

    @staticmethod
    def _cancel_state1(payment: payment_selectors.PaymentRow, order: Order, reason: int) -> int:
        """State 1 → −1 (``cancel_time``, ``reason``); the payment fails with ``payment_failed``."""
        cancel_time = now_ms()
        payment_services.merge_provider_state(
            payment.id, {"state": rpc.STATE_CANCELLED, "cancel_time": cancel_time, "reason": reason}
        )
        if payment.status == enums.PaymentStatus.PENDING:
            services.fail_payment_effects(
                order.pk, payment.id, str(reason), provider_txn_id=payment.transaction_id
            )
        return cancel_time

    def _create(self, params: dict[str, Any]) -> tuple[Any, str]:
        txn = self._txn_id(params)
        advisory_lock(f"payme:{txn}")
        existing = payment_selectors.payment_by_txn(PAYME, txn)
        if existing is not None:
            order = services.lock_order(existing.order_id)
            payment = payment_selectors.get_payment(existing.id)
            assert payment is not None  # noqa: S101
            stored = _state(payment)
            if stored.get("state") != rpc.STATE_CREATED:
                raise RpcError(rpc.ERR_CANNOT_PERFORM)
            if self._timed_out(payment):
                self._cancel_state1(payment, order, rpc.REASON_TIMEOUT)
                raise RpcError(rpc.ERR_CANNOT_PERFORM)
            return (
                {
                    "create_time": stored.get("create_time"),
                    "transaction": str(payment.id),
                    "state": rpc.STATE_CREATED,
                },
                f"payment:{payment.id}",
            )
        order_id = self._order_from_account(params)
        order = self._lock(order_id)
        self._refuse_unpayable(order)
        self._refuse_busy(order)
        self._check_amount(params, order)
        payme_time = params.get("time")
        if not rpc.is_int(payme_time):
            raise RpcError(rpc.ERR_INVALID_REQUEST)
        created = now_ms()
        if created - int(payme_time) > rpc.TIMEOUT_MS:
            raise RpcError(rpc.ERR_CANNOT_PERFORM)
        try:
            payment = services.ensure_payable_payment(order.pk, PAYME)
            payment_services.attach_transaction(
                payment.id,
                txn,
                {
                    "transaction": txn,
                    "time": int(payme_time),
                    "create_time": created,
                    "state": rpc.STATE_CREATED,
                    "account": params.get("account"),
                },
            )
        except (payment_services.TransactionTaken, PaymentInProgress) as exc:
            raise _busy() from exc
        except AlreadyPaid as exc:
            raise RpcError(rpc.ERR_CANNOT_PERFORM) from exc
        return {"create_time": created, "transaction": str(payment.id), "state": rpc.STATE_CREATED}, (
            f"payment:{payment.id}"
        )

    def _perform(self, params: dict[str, Any]) -> tuple[Any, str]:
        payment, order = self._payment(params)
        stored = _state(payment)
        entity = f"payment:{payment.id}"
        if stored.get("state") == rpc.STATE_PERFORMED:
            return {
                "transaction": str(payment.id),
                "perform_time": stored.get("perform_time"),
                "state": 2,
            }, entity
        if stored.get("state") != rpc.STATE_CREATED:
            raise RpcError(rpc.ERR_CANNOT_PERFORM)
        if self._timed_out(payment):
            self._cancel_state1(payment, order, rpc.REASON_TIMEOUT)
            raise RpcError(rpc.ERR_CANNOT_PERFORM)
        if not selectors.is_payable(order, payment, timeutils.now(), attaching=False):
            raise RpcError(rpc.ERR_CANNOT_PERFORM)  # no money is taken: Payme cancels the state-1 transaction
        performed = now_ms()
        try:
            services.mark_paid(
                order.pk,
                payment.id,
                PAYME,
                payment.transaction_id or "",
                raw={"state": rpc.STATE_PERFORMED, "perform_time": performed},
            )
        except InvalidTransition as exc:
            raise RpcError(rpc.ERR_CANNOT_PERFORM) from exc
        return {"transaction": str(payment.id), "perform_time": performed, "state": 2}, entity

    def _cancel(self, params: dict[str, Any]) -> tuple[Any, str]:
        payment, order = self._payment(params)
        reason = params.get("reason")
        if not rpc.is_int(reason):
            raise RpcError(rpc.ERR_INVALID_REQUEST)
        stored = _state(payment)
        entity = f"payment:{payment.id}"
        current = stored.get("state")
        if current == rpc.STATE_CREATED:
            cancel_time = self._cancel_state1(payment, order, int(reason))
            return {
                "transaction": str(payment.id),
                "cancel_time": cancel_time,
                "state": rpc.STATE_CANCELLED,
            }, entity
        if current == rpc.STATE_PERFORMED:
            refund = next(
                (
                    r
                    for r in payment_selectors.refunds_for_order(order.pk)
                    if r.payment_id == payment.id and r.state == enums.RefundState.MANUAL_REQUIRED
                ),
                None,
            )
            if refund is None:
                raise RpcError(rpc.ERR_CANNOT_CANCEL)
            cancel_time = now_ms()
            payment_services.merge_provider_state(
                payment.id,
                {
                    "state": rpc.STATE_CANCELLED_AFTER_PERFORM,
                    "cancel_time": cancel_time,
                    "reason": int(reason),
                },
            )
            services.resolve_refund(refund.id, enums.RefundState.SUCCEEDED, None)
            return {
                "transaction": str(payment.id),
                "cancel_time": cancel_time,
                "state": rpc.STATE_CANCELLED_AFTER_PERFORM,
            }, entity
        return {
            "transaction": str(payment.id),
            "cancel_time": stored.get("cancel_time", 0),
            "state": current,
        }, entity

    def _check(self, params: dict[str, Any]) -> tuple[Any, str]:
        payment, _order = self._payment(params)
        stored = _state(payment)
        return {
            "create_time": stored.get("create_time", 0),
            "perform_time": stored.get("perform_time") or 0,
            "cancel_time": stored.get("cancel_time") or 0,
            "transaction": str(payment.id),
            "state": stored.get("state"),
            "reason": stored.get("reason"),
        }, f"payment:{payment.id}"

    def _statement(self, params: dict[str, Any]) -> tuple[Any, str]:
        start, end = params.get("from"), params.get("to")
        if not rpc.is_int(start) or not rpc.is_int(end):
            raise RpcError(rpc.ERR_INVALID_REQUEST)
        transactions = []
        for payment in payment_selectors.payme_statement(int(start), int(end)):
            stored = _state(payment)
            transactions.append(
                {
                    "id": payment.transaction_id,
                    "time": stored.get("time"),
                    "amount": payment.amount * 100,
                    "account": {_account_field(): payment_ref.build(payment.order_id)},
                    "create_time": stored.get("create_time", 0),
                    "perform_time": stored.get("perform_time") or 0,
                    "cancel_time": stored.get("cancel_time") or 0,
                    "transaction": str(payment.id),
                    "state": stored.get("state"),
                    "reason": stored.get("reason"),
                    "receivers": None,
                }
            )
        return {"transactions": transactions}, ""
