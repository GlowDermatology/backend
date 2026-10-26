"""``POST dev/payments/{order_id}/simulate/`` (customer contract §2.15; local/test settings only).

Mounted by ``config.urls`` only with ``DEV_FEATURES_ALLOWED``, ``FAKE_PAYMENTS_ENABLED`` and fake
Click/Payme. It
builds a correctly signed Click Prepare + Complete (or Payme ``CheckPerformTransaction`` +
``CreateTransaction`` +
``PerformTransaction`` / ``CancelTransaction``) and runs it through the **real webhook views** in-process, so
the
mobile team can finish a purchase without provider accounts."""

from __future__ import annotations

import json
from typing import Any, cast
from urllib.parse import urlencode

from django.test import RequestFactory
from drf_spectacular.utils import extend_schema
from rest_framework import serializers
from rest_framework.exceptions import NotFound
from rest_framework.request import Request
from rest_framework.response import Response

from glowderma.accounts.api.customer_base import CustomerAPIView
from glowderma.accounts.models import Customer
from glowderma.commerce import payment_ref, selectors, services
from glowderma.commerce.api.webhooks.click import ClickCompleteView, ClickPrepareView
from glowderma.commerce.api.webhooks.payme import PaymeView, now_ms
from glowderma.commerce.models import Order
from glowderma.core import enums
from glowderma.core.api.schema import CUSTOMER_ERRORS, DEVICE_HEADERS
from glowderma.integrations.click import fake as fake_click
from glowderma.integrations.click import sandbox as click_sandbox
from glowderma.integrations.payme import fake as fake_payme
from glowderma.integrations.payme import sandbox as payme_sandbox

OUTCOMES = ("success", "fail")
CLICK_DECLINED = -5017
PAYME_REASON_EXECUTION_ERROR = 3


class SimulateSerializer(serializers.Serializer[dict[str, Any]]):
    outcome = serializers.ChoiceField(choices=list(OUTCOMES))
    provider = serializers.ChoiceField(
        choices=enums.PaymentProvider.values, required=False, allow_null=True, default=None
    )


class SimulateStepSerializer(serializers.Serializer[dict[str, Any]]):
    call = serializers.CharField()
    response = serializers.JSONField()


class SimulateResultSerializer(serializers.Serializer[dict[str, Any]]):
    order_id = serializers.IntegerField()
    provider = serializers.ChoiceField(choices=enums.PaymentProvider.values)
    steps = SimulateStepSerializer(many=True)


def _digits(value: str) -> str:
    return "".join(ch for ch in value if ch.isdigit()) or "1"


def _click(order: Order, success: bool) -> list[dict[str, Any]]:
    scenario = services.fake_scenario(order, "click")
    txn = _digits(fake_click.transaction_id(order.pk, scenario))
    paydoc = _digits(fake_click.paydoc_id(order.pk, scenario))
    reference = payment_ref.build(order.pk)
    factory = RequestFactory()
    steps: list[dict[str, Any]] = []
    prepare = click_sandbox.prepare_params(
        click_trans_id=txn, merchant_trans_id=reference, amount=order.total, click_paydoc_id=paydoc
    )
    answer = _call(
        ClickPrepareView,
        factory.post(
            "/api/v1/webhooks/click/prepare/",
            urlencode(prepare),
            content_type="application/x-www-form-urlencoded",
        ),
    )
    steps.append({"call": "prepare", "response": answer})
    if answer.get("error") != 0:
        return steps
    complete = click_sandbox.complete_params(
        click_trans_id=txn,
        merchant_trans_id=reference,
        merchant_prepare_id=answer["merchant_prepare_id"],
        amount=order.total,
        click_paydoc_id=paydoc,
        error=0 if success else CLICK_DECLINED,
    )
    answer = _call(
        ClickCompleteView,
        factory.post(
            "/api/v1/webhooks/click/complete/",
            urlencode(complete),
            content_type="application/x-www-form-urlencoded",
        ),
    )
    steps.append({"call": "complete", "response": answer})
    return steps


def _payme(order: Order, success: bool) -> list[dict[str, Any]]:
    scenario = services.fake_scenario(order, "payme")
    txn = fake_payme.transaction_id(order.pk, scenario)
    reference = payment_ref.build(order.pk)
    tiyin = order.total * 100
    factory = RequestFactory()
    auth = payme_sandbox.auth_header()
    steps: list[dict[str, Any]] = []
    calls = [
        ("check_perform", payme_sandbox.check_perform(reference, tiyin)),
        ("create", payme_sandbox.create(txn, reference, tiyin, now_ms())),
        ("perform", payme_sandbox.perform(txn))
        if success
        else ("cancel", payme_sandbox.cancel(txn, PAYME_REASON_EXECUTION_ERROR)),
    ]
    for name, body in calls:
        request = factory.post(
            "/api/v1/webhooks/payme/",
            json.dumps(body),
            content_type="application/json",
            HTTP_AUTHORIZATION=auth,
        )
        answer = _call(PaymeView, request)
        steps.append({"call": name, "response": answer})
        if "error" in answer:
            break
    return steps


def _call(view: Any, request: Any) -> dict[str, Any]:
    response = view.as_view()(request)
    return cast(dict[str, Any], response.data)


class SimulatePaymentView(CustomerAPIView):
    @extend_schema(
        operation_id="dev_payments_simulate",
        parameters=DEVICE_HEADERS,
        request=SimulateSerializer,
        responses={200: SimulateResultSerializer, **CUSTOMER_ERRORS},
        description="Local/test settings only: a signed provider callback sequence through the real webhook "
        "views.",
    )
    def post(self, request: Request, order_id: int) -> Response:
        customer = cast(Customer, request.user)
        order = selectors.customer_order(order_id, customer.pk)
        if order is None:
            raise NotFound
        body = SimulateSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        provider = body.validated_data["provider"] or order.payment_provider
        success = body.validated_data["outcome"] == "success"
        steps = _click(order, success) if provider == enums.PaymentProvider.CLICK else _payme(order, success)
        return Response(
            SimulateResultSerializer({"order_id": order.pk, "provider": provider, "steps": steps}).data
        )
