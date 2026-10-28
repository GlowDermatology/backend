# ruff: noqa: S105, S106 - device tokens are test values
"""The push senders over the **live** FCM adapter (customer contract §2.14.2): a dead-token answer prunes the
device, a transient failure gives the claim back for the outbox retry and never touches a device, a campaign
batch records one result per recipient — against FCM HTTP v1 answers (no network)."""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from celery.exceptions import SoftTimeLimitExceeded  # type: ignore[import-untyped]
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from django.test import override_settings

from glowderma.accounts.tests.factories import CustomerFactory, SignedInDeviceFactory
from glowderma.commerce.tests.factories import OrderFactory
from glowderma.core import outbox
from glowderma.integrations import httpclient
from glowderma.integrations.fcm.live import access_tokens
from glowderma.integrations.fcm.ports import FcmUnavailable
from glowderma.integrations.tests.live_fixtures import Script, reply
from glowderma.notifications import services
from glowderma.notifications.models import CampaignRecipient, InboxNotification
from glowderma.notifications.tests.conftest import ANCHOR
from glowderma.notifications.tests.factories import CampaignFactory

pytestmark = pytest.mark.django_db
SEND_PATH = "/v1/projects/glow-derma/messages:send"


@pytest.fixture
def live_fcm(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[Script]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()
    credentials = tmp_path / "fcm.json"
    credentials.write_text(
        json.dumps({"type": "service_account", "client_email": "fcm@glow-derma.iam", "private_key": pem})
    )
    scripted = Script().on("POST", "/token", reply("fcm", "token_ok"))
    real = httpclient.new_client
    monkeypatch.setattr(httpclient, "new_client", lambda transport=None: real(scripted.transport))
    monkeypatch.setattr(httpclient, "RETRY_BACKOFF_SECONDS", 0)
    access_tokens.clear()
    with override_settings(
        FCM_MODE="live", FCM_PROJECT_ID="glow-derma", FCM_CREDENTIALS_FILE=str(credentials)
    ):
        yield scripted
    access_tokens.clear()


def by_token(answers: dict[str, httpx.Response]) -> Callable[[httpx.Request], httpx.Response]:
    def respond(request: httpx.Request) -> httpx.Response:
        answer = answers[json.loads(request.content)["message"]["token"]]
        return httpx.Response(answer.status_code, content=answer.content, headers=answer.headers)

    return respond


def order_push(customer_id: int) -> dict[str, Any]:
    OrderFactory.create(id=10248, customer_id=customer_id, created_at=ANCHOR)
    return {"order_id": 10248, "status": "processing", "event_id": 1, "customer_id": customer_id}


def test_an_accepted_push_carries_the_inbox_id(clock: Any, live_fcm: Script) -> None:
    customer = CustomerFactory.create()
    SignedInDeviceFactory.create(customer=customer, fcm_token="live-device-1")
    live_fcm.on("POST", SEND_PATH, reply("fcm", "send_ok"))
    inbox = services.deliver_order_status(order_push(customer.pk))
    assert inbox is not None and inbox.push_state == "sent"
    message = json.loads(live_fcm.to("POST", SEND_PATH)[0].content)["message"]
    assert message["token"] == "live-device-1" and message["data"]["inbox_id"] == str(inbox.pk)
    assert message["android"]["notification"]["channel_id"] == "orders"


def test_a_dead_token_answer_prunes_the_device(clock: Any, live_fcm: Script) -> None:
    customer = CustomerFactory.create()
    device = SignedInDeviceFactory.create(customer=customer, fcm_token="live-device-gone")
    live_fcm.on("POST", SEND_PATH, reply("fcm", "error_unregistered", 404))
    inbox = services.deliver_order_status(order_push(customer.pk))
    assert inbox is not None and (inbox.push_state, inbox.push_error) == ("failed", "unregistered")
    device.refresh_from_db()
    assert (device.fcm_token, device.push_enabled) == ("", False)


@pytest.mark.parametrize(
    ("fixture", "status"),
    [
        ("error_unavailable", 503),
        ("error_quota", 429),
        ("error_internal", 500),
        ("error_permission_denied", 403),
    ],
)
def test_a_transient_failure_never_touches_the_device(
    clock: Any, live_fcm: Script, fixture: str, status: int
) -> None:
    customer = CustomerFactory.create()
    device = SignedInDeviceFactory.create(customer=customer, fcm_token="live-device-2")
    live_fcm.on("POST", SEND_PATH, reply("fcm", fixture, status))
    with pytest.raises(FcmUnavailable):
        services.deliver_order_status(order_push(customer.pk))
    assert InboxNotification.objects.get().push_state == "queued"  # given back for the outbox retry
    device.refresh_from_db()
    assert device.fcm_token == "live-device-2" and device.push_enabled is True


def test_a_campaign_batch_records_one_result_per_recipient(clock: Any, live_fcm: Script) -> None:
    alive, gone, broken = CustomerFactory.create(), CustomerFactory.create(), CustomerFactory.create()
    SignedInDeviceFactory.create(customer=alive, fcm_token="c-alive")
    dead = SignedInDeviceFactory.create(customer=gone, fcm_token="c-gone")
    kept = SignedInDeviceFactory.create(customer=broken, fcm_token="c-payload")
    live_fcm.on(
        "POST",
        SEND_PATH,
        by_token(
            {
                "c-alive": reply("fcm", "send_ok"),
                "c-gone": reply("fcm", "error_unregistered", 404),
                "c-payload": reply("fcm", "error_invalid_payload", 400),
            }
        ),
    )
    campaign = CampaignFactory.create()
    services.materialise(campaign)
    campaign.save()
    services.send_batch(campaign.pk, 0)
    errors = dict(
        CampaignRecipient.objects.filter(campaign=campaign).values_list("device__fcm_token", "error")
    )
    assert errors.get("c-alive") == "" and errors.get("c-payload") == "payload_invalid"
    dead.refresh_from_db()
    kept.refresh_from_db()
    assert dead.fcm_token == "" and kept.fcm_token == "c-payload"


def test_a_campaign_batch_gives_back_what_fcm_did_not_take(clock: Any, live_fcm: Script) -> None:
    """R4 / A-S7: one recipient's QUOTA_EXCEEDED does not fail it (nor the rest): its claim is given back,
    the cursor stays, and the outbox delivers the same batch again later for it alone."""
    first, busy, last = CustomerFactory.create(), CustomerFactory.create(), CustomerFactory.create()
    SignedInDeviceFactory.create(customer=first, fcm_token="q-first")
    SignedInDeviceFactory.create(customer=busy, fcm_token="q-busy")
    SignedInDeviceFactory.create(customer=last, fcm_token="q-last")
    answers = {
        "q-first": reply("fcm", "send_ok"),
        "q-busy": reply("fcm", "error_quota", 429),
        "q-last": reply("fcm", "send_ok"),
    }
    live_fcm.on("POST", SEND_PATH, by_token(answers))
    campaign = CampaignFactory.create()
    services.materialise(campaign)
    campaign.save()
    with pytest.raises(outbox.RetryLater) as deferred:
        services.send_batch(campaign.pk, 0)
    assert deferred.value.delay == services.NOT_SENT_RETRY
    states = dict(CampaignRecipient.objects.filter(campaign=campaign).values_list("customer_id", "state"))
    assert states == {first.pk: "sent", busy.pk: "pending", last.pk: "sent"}
    campaign.refresh_from_db()
    assert (campaign.batches_dispatched, campaign.send_cursor, campaign.state) == (0, 0, "sending")
    assert InboxNotification.objects.get(customer=busy).push_state == "queued"
    answers["q-busy"] = reply("fcm", "send_ok")
    services.send_batch(campaign.pk, 0)  # the deferred redelivery claims only the given-back recipient
    campaign.refresh_from_db()
    assert (campaign.state, campaign.sent, campaign.failed) == ("sent", 3, 0)
    sent = [json.loads(r.content)["message"]["token"] for r in live_fcm.to("POST", SEND_PATH)]
    assert sorted(sent) == ["q-busy", "q-busy", "q-first", "q-last"]


def test_a_status_push_whose_outcome_is_unknown_is_failed_never_stuck_sending(
    clock: Any, live_fcm: Script
) -> None:
    """R1 / R15: an answer lost after sending fails the row (never resent); a soft time limit while sending
    fails it ``interrupted`` and propagates — never ``sending`` forever."""
    customer = CustomerFactory.create()
    SignedInDeviceFactory.create(customer=customer, fcm_token="u-device")

    def garbled(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"Content-Encoding": "gzip"}, stream=httpx.ByteStream(b"not gzip"))

    live_fcm.on("POST", SEND_PATH, garbled)
    inbox = services.deliver_order_status(order_push(customer.pk))
    assert inbox is not None and (inbox.push_state, inbox.push_error) == ("failed", "unavailable")

    def soft_limit(request: httpx.Request) -> httpx.Response:
        raise SoftTimeLimitExceeded()

    other = CustomerFactory.create()
    SignedInDeviceFactory.create(customer=other, fcm_token="u-other")
    live_fcm.on("POST", SEND_PATH, soft_limit)
    OrderFactory.create(id=10249, customer_id=other.pk, created_at=ANCHOR)
    with pytest.raises(SoftTimeLimitExceeded):
        services.deliver_order_status(
            {"order_id": 10249, "status": "processing", "event_id": 2, "customer_id": other.pk}
        )
    row = InboxNotification.objects.filter(customer=other).get()
    assert (row.push_state, row.push_error) == ("failed", "interrupted")
