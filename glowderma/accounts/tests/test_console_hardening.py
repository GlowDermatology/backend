"""Security hardening of the ops console (glowderma.accounts): secrets in outbox payloads, the 2FA reset and
enrolment window, the per-IP budgets of the sign-in steps."""

from __future__ import annotations

from typing import Any

import pytest
from django.test import Client

from glowderma.accounts import services as account_services
from glowderma.accounts import topics
from glowderma.accounts.tests.factories import CustomerFactory, CustomerSocialIdentityFactory
from glowderma.core import outbox
from glowderma.core.jsonmask import MASK
from glowderma.core.models import OutboxMessage

pytestmark = pytest.mark.django_db


# --- finding 2: outbox payloads with credentials -----------------------------------------------------------

# One payload shape per registered outbox topic, as its producer writes it. A value ending in ``-SECRET`` is a
# credential and must never reach the change page; ``order_id``-like ids must stay readable.
TOPIC_PAYLOADS: dict[str, dict[str, Any]] = {
    "alert.send": {
        "event": "outbox.dead",
        "at": "2026-10-31T10:00:00+05:00",
        "fields": {"n": 1},
        "host": "w1",
    },
    "apple.revoke": {
        "identity_id": 11,
        "customer_id": 12,
        "subject": "apple-sub",
        "client_id": "uz.glowderma.app",
        "sealed_token": "APPLE-SEALED-SECRET",
    },
    "billz.push_sale": {"purchase_id": 3, "order_id": 21, "billz_order_uuid": "8f0e…"},
    "billz.stock_reread": {"order_id": 22, "product_ids": [1, 2]},
    "bts.create_shipment": {"order_id": 23, "manual": False, "request_no": 1, "actor_id": None},
    "campaign.send_batch": {"campaign_id": 4, "batch_no": 0},
    "catalog.stock_synced": {"job_id": 5},
    "email.notice": {"to": "sealed-address", "lang": "uz", "kind": "password_changed"},
    "email.send": {"sms_code_id": 6, "lang": "uz", "purpose": "signup"},
    "job.queued": {"job_id": 7, "kind": "billz_import"},
    "push.account_security": {
        "customer_id": 13,
        "event": "password_reset",
        "event_id": "e" * 32,
        "provider": "",
        "farewell_tokens": ["FCM-ONE-SECRET", "FCM-TWO-SECRET"],
    },
    "push.appointment": {
        "appointment_id": 8,
        "action": "confirmed",
        "event_id": 9,
        "customer_id": 14,
        "reschedule_no": None,
    },
    "push.order_status": {"order_id": 24, "status": "processing", "event_id": 10, "customer_id": 15},
    "push.session_replaced": {"session_id": "s-1", "sid": "s-1", "fcm_token": "FCM-REPLACED-SECRET"},
    "refund.submit": {"refund_id": 1, "payment_id": 2, "order_id": 25, "provider_txn_id": "t-1", "amount": 9},
    "sms.send": {"sms_code_id": 16, "lang": "uz", "platform": "ios"},
}


def change_page(client: Client, message: OutboxMessage) -> str:
    answer = client.get(f"/django-admin/core/outboxmessage/{message.pk}/change/")
    assert answer.status_code == 200
    return answer.content.decode()


def test_the_sample_table_covers_every_registered_topic() -> None:
    assert set(TOPIC_PAYLOADS) == set(outbox._handlers), "add the new topic's payload shape above"


@pytest.mark.parametrize("topic", sorted(TOPIC_PAYLOADS))
def test_no_outbox_payload_shows_a_credential(ops_client: Client, topic: str) -> None:
    payload = TOPIC_PAYLOADS[topic]
    message = OutboxMessage.objects.create(topic=topic, payload=payload, dedupe_key=f"{topic}:probe")
    content = change_page(ops_client, message)
    assert "-SECRET" not in content
    secrets = [key for key, value in payload.items() if "SECRET" in str(value)]
    if secrets:
        assert MASK in content
    for key, value in payload.items():
        if key.endswith("_id") and isinstance(value, int):
            assert f"&quot;{key}&quot;: {value}" in content or f'"{key}": {value}' in content, key


def test_the_real_payload_builders_are_masked_on_the_change_page(ops_client: Client) -> None:
    customer = CustomerFactory.create()
    identity = CustomerSocialIdentityFactory.create(
        customer=customer,
        provider="apple",
        provider_refresh_token="SEALED-REFRESH-MARKER",  # noqa: S106
    )
    assert account_services.enqueue_apple_revoke(identity)
    assert account_services.enqueue_apple_token_revoke(
        "SEALED-ORPHAN-MARKER", client_id="uz.glowderma.app", customer_id=customer.pk, subject="other-sub"
    )
    payload = topics.account_security_payload(customer.pk, "password_reset", farewell_tokens=["FCM-MARKER"])
    outbox.enqueue(
        topics.PUSH_ACCOUNT_SECURITY,
        payload,
        dedupe_key=topics.account_security_key(customer.pk, "password_reset", payload["event_id"]),
    )
    messages = list(OutboxMessage.objects.order_by("pk"))
    assert [m.topic for m in messages] == ["apple.revoke", "apple.revoke", "push.account_security"]
    for message in messages:
        content = change_page(ops_client, message)
        assert "MARKER" not in content and MASK in content
        assert f"{customer.pk}" in content  # the customer id stays readable
