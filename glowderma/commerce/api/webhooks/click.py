"""Click SHOP API callbacks (customer contract §2.13.1): ``webhooks/click/prepare/`` and ``…/complete/``.

Order of work (§2.13.0): the protocol checks that need no database (parameters → content type → IP →
signature →
action → service id → account reference), the advisory lock of the Click transaction, the inbox duplicate
check,
``commerce.services.lock_order`` before any state read, the state transition through a commerce service that
names the payment, and the ``WebhookInbox`` row last, all in one transaction. An unexpected exception rolls
everything back and answers HTTP 500 (Click retries). A Click port that is fake under the production settings
starts no transaction and honours no capture: every Prepare answers ``-1``; a Complete is parsed and
signature-checked, and only one naming a payment attached while Click was live runs the Complete steps, with
the
capture always refused (``record_late_capture(case="fake_mode")``, a manual refund).
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from django.conf import settings
from django.db import connection, transaction
from django.http import QueryDict
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema
from rest_framework.request import Request
from rest_framework.response import Response

from glowderma.accounts.api.customer_base import WebhookAPIView
from glowderma.commerce import payment_ref, selectors, services, state
from glowderma.commerce.api.webhooks import inbox
from glowderma.core import enums, integration_modes, timeutils
from glowderma.core.api.exceptions import AlreadyPaid, InvalidTransition, PaymentInProgress
from glowderma.integrations.click import protocol
from glowderma.payments import selectors as payment_selectors
from glowderma.payments import services as payment_services

logger = logging.getLogger(__name__)
PROVIDER = "click"
FORM_CONTENT_TYPE = "application/x-www-form-urlencoded"
FAKE_MODE = "fake_mode"
BAD_SIGNATURE = "bad_signature"
_PREPARED = (enums.PaymentStatus.PENDING, enums.PaymentStatus.PAID, enums.PaymentStatus.REFUNDED)


def advisory_lock(key: str) -> None:
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", [key])


def _parse(request: Request) -> tuple[dict[str, str] | None, str]:
    """``(params, raw_body)``: form fields as raw strings; JSON only with ``CLICK_ACCEPT_JSON`` (strings
    required). ``None`` for any other content type or an unparsable body."""
    raw = request.body.decode("utf-8", errors="replace")
    content_type = (request.content_type or "").split(";")[0].strip().lower()
    if content_type == FORM_CONTENT_TYPE:
        query = QueryDict(raw, encoding="utf-8")
        return {key: query.get(key, "") for key in query}, raw
    if protocol.CLICK_ACCEPT_JSON and content_type == "application/json":
        try:
            data = json.loads(raw, parse_int=str, parse_float=str)
        except ValueError:
            return None, raw
        if isinstance(data, dict) and all(isinstance(v, str) for v in data.values()):
            return dict(data), raw
    return None, raw


class _ClickCallback(WebhookAPIView):
    complete: bool = False

    def _answer(self, params: dict[str, str], error: int, payment_id: int | None = None) -> dict[str, Any]:
        return protocol.response(params, error, complete=self.complete, payment_id=payment_id)

    @property
    def _event_prefix(self) -> str:
        return "complete" if self.complete else "prepare"

    def _rejected(self, request: Request, params: Any, error: int, code: str, started: float) -> Response:
        body = self._answer(params if isinstance(params, dict) else {}, error)
        inbox.store_rejected(
            PROVIDER, request, params, status=200, body=body, error_code=code, started=started
        )
        return Response(body)

    def post(self, request: Request) -> Response:
        started = time.monotonic()
        try:
            return self._handle(request, started)
        except Exception:
            logger.exception("click.%s failed; everything rolled back", self._event_prefix)
            return Response({"error": protocol.UPDATE_FAILED, "error_note": "Internal error"}, status=500)

    def _handle(self, request: Request, started: float) -> Response:
        fake = integration_modes.callbacks_refused(PROVIDER)
        if fake and not self.complete:  # nothing read before this check (§2.13.0)
            return self._rejected(request, None, protocol.SIGN_FAILED, FAKE_MODE, started)
        params, raw = _parse(request)
        if params is None:
            return self._rejected(
                request, raw[: inbox.REJECTED_PAYLOAD_MAX], protocol.BAD_REQUEST, "bad_request", started
            )
        if protocol.missing_or_malformed(params, complete=self.complete):
            return self._rejected(request, params, protocol.BAD_REQUEST, "bad_request", started)
        if not inbox.ip_allowed(request, settings.CLICK_ALLOWED_IPS):
            return self._rejected(request, params, protocol.BAD_REQUEST, "ip_not_allowed", started)
        if not protocol.sign_ok(params, str(settings.CLICK_SECRET_KEY), complete=self.complete):
            return self._rejected(
                request, params, protocol.SIGN_FAILED, FAKE_MODE if fake else BAD_SIGNATURE, started
            )
        # Past the signature the request is authenticated: these refusals are stored uncapped, with the raw
        # body, as their own ``rejected:{uuid4}`` rows — never under the delivery's key, where their foreign
        # action / service id / reference would make the correct delivery a fingerprint conflict (-8).
        expected_action = protocol.ACTION_COMPLETE if self.complete else protocol.ACTION_PREPARE
        if params["action"] != expected_action:
            return self._refused(request, params, protocol.ACTION_NOT_FOUND, raw, started, fake=fake)
        if params["service_id"] != str(settings.CLICK_SERVICE_ID):
            return self._refused(request, params, protocol.BAD_REQUEST, raw, started, fake=fake)
        order_id = payment_ref.verify(params["merchant_trans_id"])
        if order_id is None:
            return self._refused(request, params, protocol.USER_NOT_FOUND, raw, started, fake=fake)
        with transaction.atomic():
            advisory_lock(f"click:{params['click_trans_id']}")
            if self.complete:
                return self._complete(request, params, order_id, raw, started, fake=fake)
            return self._prepare(request, params, order_id, raw, started)

    # --- shared pieces ---

    def _store(
        self,
        request: Request,
        params: dict[str, str],
        error: int,
        raw: str,
        started: float,
        *,
        payment_id: int | None = None,
        order_id: int | None = None,
    ) -> Response:
        body = self._answer(params, error, payment_id)
        entity = (
            f"payment:{payment_id}" if payment_id is not None else (f"order:{order_id}" if order_id else "")
        )
        inbox.store_delivery(
            PROVIDER,
            f"{self._event_prefix}:{params['click_trans_id']}",
            request,
            params,
            status=200,
            body=body,
            outcome=inbox.OUTCOME_ACCEPTED if error == protocol.OK else inbox.OUTCOME_REJECTED,
            error_code=None if error == protocol.OK else str(error),
            entity_ref=entity,
            raw_body=raw,
            started=started,
        )
        return Response(body)

    def _refused(
        self, request: Request, params: dict[str, str], error: int, raw: str, started: float, *, fake: bool
    ) -> Response:
        """An authenticated protocol refusal (action, service id, account reference); with a fake Click under
        the production settings it names no attached payment: ``-1`` and a bounded row (§2.13.0)."""
        if fake:
            return self._rejected(request, params, protocol.SIGN_FAILED, FAKE_MODE, started)
        body = self._answer(params, error)
        inbox.store_conflict(
            PROVIDER,
            request,
            params,
            status=200,
            body=body,
            error_code=str(error),
            started=started,
            raw_body=raw,
        )
        return Response(body)

    def _conflicting(self, params: dict[str, str]) -> bool:
        row = inbox.exists(PROVIDER, f"{self._event_prefix}:{params['click_trans_id']}")
        if row is None or not isinstance(row.payload, dict):
            return False
        return inbox.semantic_fingerprint(row.payload) != inbox.semantic_fingerprint(params)

    def _conflict(self, request: Request, params: dict[str, str], started: float) -> Response:
        body = self._answer(params, protocol.BAD_REQUEST)
        inbox.store_conflict(
            PROVIDER,
            request,
            params,
            status=200,
            body=body,
            error_code="fingerprint_conflict",
            started=started,
        )
        return Response(body)

    # --- Prepare (action 0) ---

    def _prepare(
        self, request: Request, params: dict[str, str], order_id: int, raw: str, started: float
    ) -> Response:
        if self._conflicting(params):
            return self._conflict(request, params, started)
        txn = params["click_trans_id"]
        bound = payment_selectors.payment_by_txn(PROVIDER, txn)
        if bound is not None:
            # A transaction we already bound is answered from state, never re-attached: re-running step 8
            # would supersede the order's live attempt for a transaction that can no longer be attached.
            if bound.order_id != order_id:
                return self._store(request, params, protocol.BAD_REQUEST, raw, started, order_id=order_id)
            if bound.status in _PREPARED:  # a plain or re-signed retry (refunded: captured, then returned)
                return self._store(request, params, protocol.OK, raw, started, payment_id=bound.id)
            # failed (by Click, superseded, expired or cancelled): a repeated attempt of a cancelled payment
            return self._store(
                request, params, protocol.TRANSACTION_CANCELLED, raw, started, payment_id=bound.id
            )
        try:
            order = services.lock_order(order_id)
        except services.OrderNotFound:
            return self._store(request, params, protocol.USER_NOT_FOUND, raw, started)
        now = timeutils.now()
        if order.status in state.PAID_STATUSES or selectors.honoured_paid(order.pk):
            return self._store(request, params, protocol.ALREADY_PAID, raw, started, order_id=order.pk)
        if not selectors.is_payable(order, None, now, attaching=True):
            return self._store(
                request, params, protocol.TRANSACTION_CANCELLED, raw, started, order_id=order.pk
            )
        if any(
            p.provider == enums.PaymentProvider.PAYME and selectors.is_open(p) for p in order.payments.all()
        ):
            return self._store(
                request, params, protocol.TRANSACTION_CANCELLED, raw, started, order_id=order.pk
            )
        if int(params["error"]) < 0:
            return self._store(
                request, params, protocol.TRANSACTION_CANCELLED, raw, started, order_id=order.pk
            )
        if protocol.parse_amount(params["amount"]) != order.total:
            return self._store(request, params, protocol.BAD_AMOUNT, raw, started, order_id=order.pk)
        try:
            # One savepoint: a refusal rolls back the supersede and the new row (§2.13.0, nothing
            # half-applied), and only the inbox row of the -8 answer is written.
            with transaction.atomic():
                payment = services.ensure_payable_payment(order.pk, PROVIDER, superseding=txn, at=now)
                payment_services.attach_transaction(
                    payment.id,
                    txn,
                    {
                        "merchant_trans_id": params["merchant_trans_id"],
                        "merchant_prepare_id": payment.id,
                        "click_trans_id": txn,
                        "click_paydoc_id": params["click_paydoc_id"],
                        "service_id": params["service_id"],
                        "prepare_at": now.isoformat(),
                        "sign_time": params["sign_time"],
                    },
                )
        except payment_services.TransactionTaken, PaymentInProgress, AlreadyPaid, InvalidTransition:
            return self._store(request, params, protocol.BAD_REQUEST, raw, started, order_id=order.pk)
        return self._store(request, params, protocol.OK, raw, started, payment_id=payment.id)

    # --- Complete (action 1) ---

    def _complete(
        self, request: Request, params: dict[str, str], order_id: int, raw: str, started: float, *, fake: bool
    ) -> Response:
        txn = params["click_trans_id"]
        payment_id = int(params["merchant_prepare_id"])
        bound = payment_selectors.get_payment(payment_id)
        named = (
            bound is not None
            and bound.order_id == order_id
            and bound.provider == PROVIDER
            and bound.transaction_id == txn
        )
        if fake and not (named and bound is not None and bound.provider_state.get("prepare_at")):
            # only a Complete of a transaction attached while Click was live gets past a fake Click (§2.13.0)
            return self._rejected(request, params, protocol.SIGN_FAILED, FAKE_MODE, started)
        if self._conflicting(params):
            return self._conflict(request, params, started)
        if (
            named
            and bound is not None
            and bound.status in (enums.PaymentStatus.PAID, enums.PaymentStatus.REFUNDED)
        ):
            return self._store(request, params, protocol.OK, raw, started, payment_id=bound.id)
        try:
            order = services.lock_order(order_id)
        except services.OrderNotFound:
            return self._store(request, params, protocol.USER_NOT_FOUND, raw, started)
        payment = payment_selectors.get_payment(payment_id)  # re-read under the Order lock
        if (
            payment is None
            or payment.order_id != order.pk
            or payment.provider != PROVIDER
            or payment.transaction_id != txn
        ):
            return self._store(
                request, params, protocol.TRANSACTION_NOT_FOUND, raw, started, order_id=order.pk
            )
        if payment.status in (enums.PaymentStatus.PAID, enums.PaymentStatus.REFUNDED):
            return self._store(request, params, protocol.OK, raw, started, payment_id=payment.id)
        now = timeutils.now()
        error = int(params["error"])
        if error < 0:
            if payment.status == enums.PaymentStatus.PENDING:
                services.fail_payment_effects(order.pk, payment.id, str(error), provider_txn_id=txn)
            return self._store(
                request, params, protocol.TRANSACTION_CANCELLED, raw, started, payment_id=payment.id
            )
        if protocol.parse_amount(params["amount"]) != payment.amount:
            return self._store(request, params, protocol.BAD_AMOUNT, raw, started, payment_id=payment.id)
        if payment.status == enums.PaymentStatus.FAILED and _failed_by_click(payment.error_code):
            return self._store(
                request, params, protocol.TRANSACTION_CANCELLED, raw, started, payment_id=payment.id
            )
        completion = {
            "complete_at": now.isoformat(),
            "error": params["error"],
            "error_note": params.get("error_note", ""),
        }
        case = services.late_capture_case(order, payment, now)
        if case is None and not fake:
            services.mark_paid(order.pk, payment.id, PROVIDER, txn, raw=completion)
        else:
            services.record_late_capture(
                order.pk, payment.id, PROVIDER, txn, case=case or FAKE_MODE, raw=completion
            )
        return self._store(request, params, protocol.OK, raw, started, payment_id=payment.id)


def _failed_by_click(error_code: str) -> bool:
    """A payment Click failed earlier carries Click's numeric error code (``"-5017"``)."""
    try:
        int(error_code)
    except ValueError:
        return False
    return True


SCHEMA = {
    "request": {FORM_CONTENT_TYPE: OpenApiTypes.OBJECT},
    "responses": {200: OpenApiTypes.OBJECT, 500: OpenApiTypes.OBJECT},
    "auth": [],
}


class ClickPrepareView(_ClickCallback):
    complete = False

    @extend_schema(
        operation_id="webhook_click_prepare",
        description="Click SHOP API Prepare (action 0); provider-native body (customer contract §2.13.1).",
        **SCHEMA,  # type: ignore[arg-type]
    )
    def post(self, request: Request) -> Response:
        return super().post(request)


class ClickCompleteView(_ClickCallback):
    complete = True

    @extend_schema(
        operation_id="webhook_click_complete",
        description="Click SHOP API Complete (action 1); provider-native body (customer contract §2.13.1).",
        **SCHEMA,  # type: ignore[arg-type]
    )
    def post(self, request: Request) -> Response:
        return super().post(request)
