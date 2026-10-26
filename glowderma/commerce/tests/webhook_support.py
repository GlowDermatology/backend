"""Helpers of the offline Click / Payme sandbox suites (plan §5) and the BTS webhook tests: signed requests
through the real webhook views, a clock pinned relative to the real "now" (access tokens stay valid)."""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import urlencode

from rest_framework.test import APIClient

from glowderma.commerce import payment_ref
from glowderma.commerce.models import Order
from glowderma.core import timeutils
from glowderma.core.seeding import pinned_clock
from glowderma.integrations.click import sandbox as click_sandbox
from glowderma.integrations.models import WebhookInbox
from glowderma.integrations.payme import sandbox as payme_sandbox
from glowderma.payments.models import Payment

CLICK_PREPARE = "/api/v1/webhooks/click/prepare/"
CLICK_COMPLETE = "/api/v1/webhooks/click/complete/"
PAYME_URL = "/api/v1/webhooks/payme/"
BTS_URL = "/api/v1/webhooks/bts/"
FORM = "application/x-www-form-urlencoded"
TXN = 2875510123


def real_now() -> datetime:
    return timeutils.now().replace(microsecond=0)


@contextmanager
def at(instant: datetime) -> Iterator[None]:
    with pinned_clock(instant):
        yield


def ref(order: Order) -> str:
    return payment_ref.build(order.pk)


def prepare_params(order: Order, txn: int = TXN, **overrides: Any) -> dict[str, str]:
    values: dict[str, Any] = {
        "click_trans_id": txn,
        "merchant_trans_id": ref(order),
        "amount": order.total,
        "click_paydoc_id": txn + 1_000_000,
    }
    values.update(overrides)
    return click_sandbox.prepare_params(**values)


def complete_params(order: Order, payment_id: int, txn: int = TXN, **overrides: Any) -> dict[str, str]:
    values: dict[str, Any] = {
        "click_trans_id": txn,
        "merchant_trans_id": ref(order),
        "merchant_prepare_id": payment_id,
        "amount": order.total,
        "click_paydoc_id": txn + 1_000_000,
    }
    values.update(overrides)
    return click_sandbox.complete_params(**values)


def click(url: str, params: dict[str, str], *, content_type: str = FORM, **extra: Any) -> dict[str, Any]:
    body = urlencode(params) if content_type == FORM else json.dumps(params)
    response = APIClient().generic("POST", url, body, content_type=content_type, **extra)
    assert response.status_code == 200, response.content
    return dict(response.json())


def prepare(order: Order, txn: int = TXN, **overrides: Any) -> dict[str, Any]:
    return click(CLICK_PREPARE, prepare_params(order, txn, **overrides))


def complete(order: Order, payment_id: int, txn: int = TXN, **overrides: Any) -> dict[str, Any]:
    return click(CLICK_COMPLETE, complete_params(order, payment_id, txn, **overrides))


def payme(body: dict[str, Any] | str, *, key: str | None = None, auth: str | None = None) -> dict[str, Any]:
    raw = body if isinstance(body, str) else json.dumps(body)
    header = auth if auth is not None else payme_sandbox.auth_header(key)
    response = APIClient().generic("POST", PAYME_URL, raw, content_type="application/json", HTTP_AUTHORIZATION=header)
    assert response.status_code == 200, response.content
    return dict(response.json())


def tiyin(order: Order) -> int:
    return order.total * 100


def ms(instant: datetime) -> int:
    return int(instant.timestamp() * 1000)


def rows(provider: str) -> list[WebhookInbox]:
    return list(WebhookInbox.objects.filter(provider=provider).order_by("id"))


def keys(provider: str) -> list[str]:
    return [row.event_key for row in rows(provider)]


def payments(order: Order) -> list[Payment]:
    return list(Payment.objects.filter(order=order).order_by("id"))


def later(instant: datetime, **delta: float) -> datetime:
    return instant + timedelta(**delta)
