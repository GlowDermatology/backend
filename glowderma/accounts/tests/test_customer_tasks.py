"""``accounts.tasks``: ``push.session_replaced`` and ``accounts.cleanup_auth`` (plan §1 P0, §8)."""

# ruff: noqa: S105, S106 - FCM tokens and hashes in fixtures are test data, not secrets

import logging
from datetime import timedelta
from typing import Any

import pytest

from glowderma.accounts import services, tasks, topics
from glowderma.accounts.models import AuthSession, Device, SmsCode
from glowderma.accounts.tests.factories import (
    CustomerFactory,
    CustomerSessionFactory,
    DeviceFactory,
    SmsCodeFactory,
    StaffUserFactory,
    start_test_session,
)
from glowderma.core import outbox, timeutils
from glowderma.core.models import OutboxMessage
from glowderma.integrations.fcm.registry import get_fcm_port

pytestmark = pytest.mark.django_db


def device_pk(session: AuthSession) -> int:
    if session.device_id is None:  # pragma: no cover - customer sessions always have a device
        raise AssertionError("customer session without a device")
    return session.device_id


@pytest.fixture
def fcm() -> Any:
    port: Any = get_fcm_port()
    port.sent.clear()
    yield port
    port.sent.clear()


def replaced_message(token: str) -> OutboxMessage:
    return OutboxMessage.objects.create(
        topic=topics.PUSH_SESSION_REPLACED,
        payload={"session_id": "s-1", "sid": "s-1", "fcm_token": token},
        dedupe_key=topics.session_replaced_key("s-1"),
    )


def test_session_replaced_sends_a_data_only_push_and_blanks_the_token(fcm: Any) -> None:
    message = replaced_message("tok-old")
    outbox.deliver(message)
    [push] = fcm.sent
    assert push.token == "tok-old" and push.data_only
    assert push.data == {"type": "session_replaced", "sid": "s-1"}
    assert (push.title, push.body, push.apns_push_type, push.apns_priority) == ("", "", "background", 5)
    message.refresh_from_db()
    assert message.payload == {"session_id": "s-1", "sid": "s-1", "fcm_token": ""}
    outbox.deliver(message)  # a redelivery sends nothing
    assert len(fcm.sent) == 1


def test_an_unregistered_token_prunes_the_devices_holding_it(fcm: Any) -> None:
    device = DeviceFactory.create(fcm_token="tok-x-bad", push_enabled=True, is_active=False)
    message = replaced_message("tok-x-bad")
    tasks.push_session_replaced(message)
    device.refresh_from_db()
    assert (device.fcm_token, device.push_enabled) == ("", False) and device.fcm_invalidated_at is not None
    message.refresh_from_db()
    assert message.payload["fcm_token"] == ""


def test_a_payload_error_never_touches_a_device(fcm: Any, caplog: pytest.LogCaptureFixture) -> None:
    device = DeviceFactory.create(fcm_token="tok-x-payload", push_enabled=True)
    with caplog.at_level(logging.ERROR, logger="glowderma.accounts.tasks"):
        tasks.push_session_replaced(replaced_message("tok-x-payload"))
    device.refresh_from_db()
    assert device.fcm_token == "tok-x-payload" and device.push_enabled
    assert "push.payload_invalid topic=push.session_replaced" in caplog.text


def test_a_transport_failure_propagates_for_the_outbox_retry(fcm: Any) -> None:
    from glowderma.integrations.fcm.ports import FcmUnavailable

    message = replaced_message("tok-x-down")
    with pytest.raises(FcmUnavailable):
        tasks.push_session_replaced(message)
    message.refresh_from_db()
    assert message.payload["fcm_token"] == "tok-x-down"  # kept for the retry


def test_sign_in_on_a_new_device_delivers_the_farewell_push(
    fcm: Any, django_capture_on_commit_callbacks: Any
) -> None:
    customer = CustomerFactory.create()
    first = start_test_session(customer, fid="fid-first-001")
    Device.objects.filter(pk=device_pk(first.session)).update(fcm_token="tok-first", push_enabled=True)
    with django_capture_on_commit_callbacks(execute=True):
        services.start_customer_session(
            customer.pk, fid="fid-second-02", platform="ios", app_version="1.0.0", issue_device_key=True
        )
    assert [push.data for push in fcm.sent] == [{"type": "session_replaced", "sid": str(first.session.pk)}]
    assert OutboxMessage.objects.get(topic=topics.PUSH_SESSION_REPLACED).status == "done"


def test_cleanup_auth_purges_only_the_right_rows() -> None:
    now = timeutils.now()
    old_code = SmsCodeFactory.create(created_at=now - timedelta(hours=25))
    fresh_code = SmsCodeFactory.create(created_at=now - timedelta(hours=23))
    stale = {
        status: OutboxMessage.objects.create(
            topic=topics.SMS_SEND, payload={}, dedupe_key=f"sms.send:{status}", status=status
        )
        for status in ("done", "dead", "pending", "failed")
    }
    other_done = OutboxMessage.objects.create(
        topic="push.order_status", payload={}, dedupe_key="x", status="done"
    )
    OutboxMessage.objects.filter(pk__in=[m.pk for m in [*stale.values(), other_done]]).update(
        updated_at=now - timedelta(hours=25)
    )
    fresh_done = OutboxMessage.objects.create(
        topic=topics.SMS_SEND, payload={}, dedupe_key="sms.send:new", status="done"
    )
    customer = CustomerFactory.create()
    revoked_old = CustomerSessionFactory.create(
        customer=customer, revoked_at=now - timedelta(days=91), revoke_reason="logout"
    )
    revoked_recent = CustomerSessionFactory.create(customer=customer, revoked_at=now - timedelta(days=89))
    expired_old = CustomerSessionFactory.create(
        customer=customer, absolute_expires_at=now - timedelta(days=91)
    )
    open_session = CustomerSessionFactory.create(customer=customer)
    staff_session = AuthSession.objects.create(
        audience="staff",
        staff=StaffUserFactory.create(),
        device_label="x",
        refresh_hash="e" * 64,
        absolute_expires_at=now - timedelta(days=200),
        revoked_at=now - timedelta(days=200),
    )
    result = tasks.cleanup_auth()
    assert result == {"sms_codes": 1, "outbox_messages": 2, "sessions": 2}
    assert (
        not SmsCode.objects.filter(pk=old_code.pk).exists()
        and SmsCode.objects.filter(pk=fresh_code.pk).exists()
    )
    remaining = set(OutboxMessage.objects.values_list("dedupe_key", flat=True))
    assert remaining == {"sms.send:pending", "sms.send:failed", "x", fresh_done.dedupe_key}
    sessions = set(AuthSession.objects.values_list("pk", flat=True))
    assert {revoked_recent.pk, open_session.pk, staff_session.pk} <= sessions
    assert not {revoked_old.pk, expired_old.pk} & sessions
