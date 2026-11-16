"""``POST webhooks/yandex-delivery/`` (plan §3.7, decision 10): our HMAC ``ref`` in the query, the claim of
that order, inbox dedupe on ``(claim_id, updated_ts)``, ``yandex.sync_claim`` enqueued and no Yandex call in
the request; refused for a fake under the production posture; the ref never stored."""

# ruff: noqa: F811 - the fixtures come from yandex_dispatch_support and are requested by name

from __future__ import annotations

from typing import Any

import pytest
from django.test import override_settings
from rest_framework.test import APIClient

from glowderma.accounts.models import StaffUser
from glowderma.commerce import payment_ref, tasks, topics, yandex_ref
from glowderma.commerce.models import Order
from glowderma.commerce.tests.yandex_dispatch_support import (  # noqa: F401 - fixtures
    calls,
    dispatch,
    fresh_fake_yandex,
    no_pauses,
    open_warehouse,
    run,
    shipment_of,
    yandex_order,
)
from glowderma.core import enums
from glowderma.core.models import OutboxMessage
from glowderma.delivery.models import CourierClaim
from glowderma.integrations.models import WebhookInbox
from glowderma.integrations.yandex_delivery import fake as fake_yandex

pytestmark = pytest.mark.django_db

URL = "/api/v1/webhooks/yandex-delivery/"
TS = "2026-11-15T10:00:00.123456+00:00"


@pytest.fixture(autouse=True)
def warehouse() -> None:
    open_warehouse()


def _accepted(staff: StaffUser) -> tuple[Order, CourierClaim]:
    order = yandex_order()
    claim = dispatch(order, staff.pk)
    run(claim)
    claim.refresh_from_db()
    return order, claim


def _post(client: APIClient, query: str) -> Any:
    return client.post(f"{URL}?{query}", data=b"", content_type="application/json")


def _query(order: Order, claim_id: str, updated_ts: str = TS) -> str:
    # exactly what Yandex builds: our "?ref=…&" + "updated_ts=…&claim_id=…" by concatenation
    callback = yandex_ref.callback_url(order.pk) or f"{URL}?ref={yandex_ref.build(order.pk)}&"
    return callback.split("?", 1)[1] + f"updated_ts={updated_ts.replace('+', '%2B')}&claim_id={claim_id}"


@override_settings(YANDEX_DELIVERY_CALLBACK_URL="https://api.example.uz/api/v1/webhooks/yandex-delivery/")
def test_a_verified_callback_enqueues_a_sync_and_calls_nobody(
    anon_client: APIClient, staff: StaffUser, calls: dict[str, int]
) -> None:
    order, claim = _accepted(staff)
    before = dict(calls)
    response = _post(anon_client, _query(order, claim.claim_id))
    assert response.status_code == 200 and response.json() == {"ok": True}
    assert calls == before  # no Yandex call in the request
    message = OutboxMessage.objects.get(topic=topics.YANDEX_SYNC_CLAIM)
    assert message.payload == {"order_id": order.pk, "claim_id": claim.claim_id, "updated_ts": TS}
    row = WebhookInbox.objects.get(provider="yandex", event_key=f"{claim.claim_id}:{TS}")
    assert row.path == URL and "ref" not in row.path
    assert row.payload == {"claim_id": claim.claim_id, "updated_ts": TS}
    assert yandex_ref.build(order.pk) not in str(row.payload) + row.raw_body + str(row.headers)
    assert row.entity_ref == f"order:{order.pk}"
    # the handler then re-reads the claim and applies it
    fake_yandex.advance(claim.claim_id, "pickup_arrived")
    tasks.sync_yandex_claim(message)
    assert shipment_of(order).stage == enums.ShipmentStage.COURIER_ARRIVED


def test_a_repeated_callback_is_a_duplicate(anon_client: APIClient, staff: StaffUser) -> None:
    order, claim = _accepted(staff)
    assert _post(anon_client, _query(order, claim.claim_id)).status_code == 200
    second = _post(anon_client, _query(order, claim.claim_id))
    assert second.status_code == 200 and second.json() == {"ok": True}
    assert OutboxMessage.objects.filter(topic=topics.YANDEX_SYNC_CLAIM).count() == 1
    assert WebhookInbox.objects.filter(provider="yandex", result__outcome="duplicate").count() == 1
    # a new updated_ts is a new hint
    assert _post(anon_client, _query(order, claim.claim_id, "2026-11-15T10:01:00+00:00")).status_code == 200
    assert OutboxMessage.objects.filter(topic=topics.YANDEX_SYNC_CLAIM).count() == 2


@pytest.mark.parametrize(
    ("query", "status"),
    [
        ("", 400),
        ("updated_ts=x&claim_id=y", 400),
        ("ref=10001&claim_id=y", 400),
        ("ref=garbage-zz&claim_id=y", 403),
    ],
)
def test_a_missing_or_malformed_ref_is_refused(anon_client: APIClient, query: str, status: int) -> None:
    response = _post(anon_client, query)
    assert response.status_code == status and response.json() == {"ok": False}
    assert not OutboxMessage.objects.filter(topic=topics.YANDEX_SYNC_CLAIM).exists()


def test_a_wrong_hmac_or_a_payment_ref_is_refused(anon_client: APIClient, staff: StaffUser) -> None:
    order, claim = _accepted(staff)
    other = yandex_order()
    forged = yandex_ref.build(other.pk).replace(str(other.pk), str(order.pk), 1)
    for ref in (forged, payment_ref.build(order.pk), f"{order.pk}-{'0' * 16}"):
        response = _post(anon_client, f"ref={ref}&updated_ts={TS}&claim_id={claim.claim_id}")
        assert response.status_code == 403 and response.json() == {"ok": False}
    assert not OutboxMessage.objects.filter(topic=topics.YANDEX_SYNC_CLAIM).exists()
    rejected = WebhookInbox.objects.filter(provider="yandex", result__error_code="bad_ref")
    assert rejected.count() == 3 and all(row.event_key.startswith("rejected:") for row in rejected)


def test_a_claim_of_another_order_is_ignored(anon_client: APIClient, staff: StaffUser) -> None:
    order, claim = _accepted(staff)
    other, other_claim = _accepted(staff)
    response = _post(anon_client, _query(order, other_claim.claim_id))
    assert response.status_code == 200 and response.json() == {"ok": True, "ignored": True}
    unknown = _post(anon_client, _query(order, "not-a-claim"))
    assert unknown.status_code == 200 and unknown.json()["ignored"] is True
    assert not OutboxMessage.objects.filter(topic=topics.YANDEX_SYNC_CLAIM).exists()


def test_an_old_claim_of_the_order_is_accepted_as_a_hint(anon_client: APIClient, staff: StaffUser) -> None:
    order, first = _accepted(staff)
    fake_yandex.advance(first.claim_id, "cancelled_by_taxi")
    tasks.poll_yandex()  # stage failed, first claim ended
    second = dispatch(order, staff.pk)
    run(second)
    response = _post(anon_client, _query(order, first.claim_id))
    assert response.status_code == 200 and response.json() == {"ok": True}
    message = OutboxMessage.objects.get(topic=topics.YANDEX_SYNC_CLAIM)
    tasks.sync_yandex_claim(message)
    assert shipment_of(order).claim_id == second.pk


def test_refused_for_a_fake_under_the_production_posture(
    anon_client: APIClient, staff: StaffUser, monkeypatch: pytest.MonkeyPatch
) -> None:
    order, claim = _accepted(staff)
    from glowderma.core import integration_modes

    monkeypatch.setattr(integration_modes, "fake_in_production", lambda provider: provider == "yandex")
    response = _post(anon_client, _query(order, claim.claim_id))
    assert response.status_code == 403
    assert WebhookInbox.objects.filter(provider="yandex", result__error_code="fake_mode").count() == 1
    assert not OutboxMessage.objects.filter(topic=topics.YANDEX_SYNC_CLAIM).exists()


def test_the_ref_is_domain_separated_and_verified() -> None:
    ref = yandex_ref.build(10248)
    assert ref.startswith("10248-") and len(ref.split("-")[1]) == 16
    assert yandex_ref.verify(ref) == 10248
    assert yandex_ref.verify(payment_ref.build(10248)) is None
    assert payment_ref.verify(ref) is None
    assert yandex_ref.verify("10248") is None and yandex_ref.verify(None) is None
    assert yandex_ref.verify(ref.upper()) is None
    with override_settings(YANDEX_DELIVERY_CALLBACK_URL=""):
        assert yandex_ref.callback_url(10248) is None
    with override_settings(YANDEX_DELIVERY_CALLBACK_URL="https://api.example.uz/hook/"):
        assert yandex_ref.callback_url(10248) == f"https://api.example.uz/hook/?ref={ref}&"
