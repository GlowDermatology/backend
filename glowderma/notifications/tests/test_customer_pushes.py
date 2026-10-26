"""The M2 push senders (customer contract §2.14.2, §4.3, §2.12; plan §4 P3): ``inbox_id`` in every payload,
channel and priority per type, customer-language order-status titles, the history horizon, the
``push.account_security`` handler with farewell notices, token pruning and ``payload_invalid``."""

import logging
from datetime import timedelta
from typing import Any

import pytest

from glowderma.accounts import topics as account_topics
from glowderma.accounts.models import Customer, Device
from glowderma.accounts.tests.factories import CustomerFactory, DeviceFactory
from glowderma.appointments.tests.factories import AppointmentFactory
from glowderma.commerce.tests.factories import OrderFactory
from glowderma.core import enums
from glowderma.core.models import OutboxMessage
from glowderma.core.tasks import dispatch_outbox
from glowderma.integrations.fcm.fake import FakeFcm
from glowderma.integrations.fcm.ports import PushMessage
from glowderma.notifications import services, tasks, topics
from glowderma.notifications.models import Campaign, CampaignRecipient, InboxNotification
from glowderma.notifications.tests.conftest import ANCHOR
from glowderma.notifications.tests.factories import CampaignFactory

pytestmark = pytest.mark.django_db

EVENTS = ("social_linked", "social_unlinked", "password_reset", "password_changed")


def security_payload(customer: Customer, event: str, **extra: Any) -> dict[str, Any]:
    provider = "google" if event.startswith("social") else ""
    return account_topics.account_security_payload(customer.pk, event, provider=provider, **extra)


def order_push(
    customer: Customer, status: str, *, pickup: bool = False, order_id: int = 10248
) -> dict[str, Any]:
    OrderFactory.create(
        id=order_id,
        customer=customer,
        created_at=ANCHOR,
        **({"pickup": True} if pickup else {}),
    )
    return {"order_id": order_id, "status": status, "event_id": 1, "customer_id": customer.pk}


# --- push.account_security ----------------------------------------------------------------------------------


def test_the_payload_keys_are_the_accounts_definition() -> None:
    assert topics.PUSH_ACCOUNT_SECURITY in topics.HANDLED_TOPICS
    assert topics.PAYLOAD_KEYS[topics.PUSH_ACCOUNT_SECURITY] is account_topics.ACCOUNT_SECURITY_PAYLOAD_KEYS
    assert (
        tuple(security_payload(CustomerFactory.create(), "password_reset"))
        == (topics.PAYLOAD_KEYS[topics.PUSH_ACCOUNT_SECURITY])
    )


@pytest.mark.parametrize("event", EVENTS)
def test_account_security_notice_per_event(clock: Any, fcm: FakeFcm, event: str) -> None:
    customer = CustomerFactory.create(name="Malika Karimova", phone="+998901234567")
    device = DeviceFactory.create(customer=customer)
    payload = security_payload(customer, event)
    inbox = services.deliver_account_security(payload)
    assert inbox is not None
    assert (inbox.kind, inbox.source_type, inbox.source_id, inbox.template_status, inbox.link) == (
        "general",
        "account",
        customer.pk,
        "",
        {"kind": "none"},
    )
    assert inbox.dedupe_key == f"account:{customer.pk}:{event}:{payload['event_id']}"
    assert (inbox.push_state, inbox.language) == ("sent", "uz")
    [message] = fcm.sent
    assert message.token == device.fcm_token
    assert message.data == {
        "type": "account_security",
        "inbox_id": str(inbox.pk),
        "event": event,
        "provider": payload["provider"],
    }
    assert (message.android_channel, message.android_priority, message.apns_priority) == (
        "general",
        "high",
        10,
    )
    assert (message.title, message.body) == (inbox.title, inbox.body)
    # no PII (C1)
    for text in (message.title, message.body, *message.data.values()):
        assert "Malika" not in text and "901234567" not in text
    # a redelivery of the same event adds no row and sends nothing
    assert services.deliver_account_security(payload) == inbox
    assert InboxNotification.objects.filter(source_type="account").count() == 1 and len(fcm.sent) == 1
    # another event_id is another notice
    services.deliver_account_security(security_payload(customer, event))
    assert InboxNotification.objects.filter(source_type="account").count() == 2


def test_account_security_texts(clock: Any, fcm: FakeFcm) -> None:
    customer = CustomerFactory.create(language="ru")
    linked = services.deliver_account_security(security_payload(customer, "social_linked"))
    assert linked is not None
    assert (linked.title, linked.body, linked.language) == (
        "Добавлен новый способ входа",
        "К аккаунту привязан профиль Google. Если это были не вы, восстановите пароль.",
        "ru",
    )
    Customer.objects.filter(pk=customer.pk).update(language="uz")
    customer.refresh_from_db()
    reset = services.deliver_account_security(security_payload(customer, "password_reset"))
    assert reset is not None
    assert (
        reset.title == "Parol o‘zgartirildi" and "{provider}" not in reset.body and "Google" not in reset.body
    )
    apple = services.deliver_account_security(
        account_topics.account_security_payload(customer.pk, "social_unlinked", provider="apple")
    )
    assert apple is not None and apple.body == "Apple profili hisobingizdan uzildi."
    assert linked.push_state == "no_device" and fcm.sent == []  # no device: the row still exists


def test_farewell_tokens_get_the_same_notice_and_are_blanked(clock: Any, fcm: FakeFcm) -> None:
    customer = CustomerFactory.create()
    current = DeviceFactory.create(customer=customer)
    payload = security_payload(
        customer, "password_reset", farewell_tokens=["old-device-token", current.fcm_token]
    )
    message = OutboxMessage.objects.create(
        topic=account_topics.PUSH_ACCOUNT_SECURITY,
        payload=payload,
        dedupe_key=account_topics.account_security_key(customer.pk, "password_reset", payload["event_id"]),
    )
    dispatch_outbox(batch=5)
    message.refresh_from_db()
    assert message.status == "done" and message.payload["farewell_tokens"] == []
    inbox = InboxNotification.objects.get(source_type="account")
    assert [m.token for m in fcm.sent] == [current.fcm_token, "old-device-token"]  # the current one only once
    assert all(m.data["inbox_id"] == str(inbox.pk) and m.title == inbox.title for m in fcm.sent)
    # a redelivery sends nothing more
    tasks.handle_push_account_security(message)
    assert len(fcm.sent) == 2


def test_account_security_edge_cases(clock: Any, fcm: FakeFcm, caplog: pytest.LogCaptureFixture) -> None:
    with pytest.raises(ValueError, match="misses"):
        services.deliver_account_security({"customer_id": 1, "event": "password_reset"})
    customer = CustomerFactory.create()
    bad = {**security_payload(customer, "password_reset"), "event": "deleted"}
    with pytest.raises(ValueError, match="Unknown account security event"):
        services.deliver_account_security(bad)
    gone = {**security_payload(customer, "password_reset"), "customer_id": 999_999}
    assert services.deliver_account_security(gone) is None
    farewell = security_payload(customer, "password_reset", farewell_tokens=["x-payload", "y-bad"])
    with caplog.at_level(logging.ERROR, logger="glowderma.notifications.services"):
        services.deliver_account_security(farewell)
    assert "push.payload_invalid" in caplog.text


def test_a_dead_farewell_token_is_pruned_wherever_it_sits(clock: Any, fcm: FakeFcm) -> None:
    customer = CustomerFactory.create()
    DeviceFactory.create(customer=customer)
    # the install that held the token was deactivated by the reset; another inactive row still holds it
    stale = DeviceFactory.create(
        customer=CustomerFactory.create(),
        fcm_token="old-device-bad",  # noqa: S106
        is_active=False,
        push_enabled=True,
    )
    services.deliver_account_security(
        security_payload(customer, "password_reset", farewell_tokens=["old-device-bad"])
    )
    stale.refresh_from_db()
    assert (stale.fcm_token, stale.push_enabled, stale.fcm_invalidated_at) == ("", False, ANCHOR)


def test_a_farewell_token_now_on_an_active_device_gets_no_notice(clock: Any, fcm: FakeFcm) -> None:
    customer = CustomerFactory.create()
    current = DeviceFactory.create(customer=customer)
    # the install signed into another account after the reset: its token is now B's active device
    other = DeviceFactory.create(customer=CustomerFactory.create(), fcm_token="moved-token")  # noqa: S106
    inbox = services.deliver_account_security(
        security_payload(customer, "password_reset", farewell_tokens=["moved-token", "gone-token"])
    )
    assert inbox is not None
    assert [m.token for m in fcm.sent] == [current.fcm_token, "gone-token"]
    other.refresh_from_db()
    assert (other.fcm_token, other.push_enabled) == ("moved-token", True)


def test_dead_account_security_message_is_audited_and_keeps_no_token(clock: Any) -> None:
    customer = CustomerFactory.create()
    payload = security_payload(customer, "password_reset", farewell_tokens=["old-token"])
    message = OutboxMessage.objects.create(
        topic=account_topics.PUSH_ACCOUNT_SECURITY, payload=payload, dedupe_key="push.account_security:dead"
    )
    tasks.on_account_security_dead(message)
    message.refresh_from_db()
    assert message.payload["farewell_tokens"] == []
    from glowderma.core.models import AuditEntry

    assert (AuditEntry.objects.get(action="notification.push_dead").after or {})["customer_id"] == customer.pk


# --- order status, appointment and campaign payloads ------------------------------------------------------


@pytest.mark.parametrize(
    ("language", "status", "pickup", "title"),
    [
        ("ru", "handed_to_courier", False, "Передан курьеру"),
        ("en", "processing", False, "Being prepared"),
        ("uz", "delivered", True, "Olib ketildi"),
        ("uz", "delivered", False, "Yetkazildi"),
        ("en", "delivered", True, "Picked up"),
        ("uz", "cancelled", False, "Bekor qilindi"),
    ],
)
def test_order_status_title_is_the_customer_label(
    clock: Any, fcm: FakeFcm, language: str, status: str, pickup: bool, title: str
) -> None:
    customer = CustomerFactory.create(language=language)
    DeviceFactory.create(customer=customer)
    inbox = services.deliver_order_status(order_push(customer, status, pickup=pickup))
    assert inbox is not None and inbox.title == title
    assert inbox.language == language  # the row is in the customer's language, like its title
    [message] = fcm.sent
    assert message.title == title
    assert message.data == {
        "type": "order_status",
        "inbox_id": str(inbox.pk),
        "order_id": "10248",
        "status": status,
    }
    assert (message.android_channel, message.android_priority, message.apns_push_type) == (
        "orders",
        "high",
        "alert",
    )


def test_no_push_for_an_order_before_the_history_horizon(clock: Any, fcm: FakeFcm) -> None:
    customer = CustomerFactory.create(history_from=ANCHOR + timedelta(days=1))
    DeviceFactory.create(customer=customer)
    assert services.deliver_order_status(order_push(customer, "processing")) is None
    assert not InboxNotification.objects.exists() and fcm.sent == []
    assert (
        services.deliver_order_status(
            {"order_id": 1, "status": "processing", "event_id": 1, "customer_id": customer.pk}
        )
        is None
    )


@pytest.mark.parametrize(
    ("action", "status"),
    [
        ("confirmed", "cancelled"),
        ("rescheduled", "rejected"),
        ("rejected", "requested"),
        ("cancelled", "confirmed"),
    ],
)
def test_a_stale_appointment_action_is_skipped(
    clock: Any, fcm: FakeFcm, caplog: pytest.LogCaptureFixture, action: str, status: str
) -> None:
    # e.g. staff confirm, then the customer cancels before the outbox delivers the confirm push
    timestamps: dict[str, dict[str, Any]] = {
        "requested": {},
        "confirmed": {"confirmed_at": ANCHOR},
        "rejected": {"rejected_at": ANCHOR},
        "cancelled": {"cancelled_at": ANCHOR, "cancelled_by": "customer"},
    }
    appointment = AppointmentFactory.create(status=status, **timestamps[status])
    DeviceFactory.create(customer=appointment.customer)
    payload = {
        "appointment_id": appointment.pk,
        "action": action,
        "event_id": 1,
        "customer_id": appointment.customer_id,
        "reschedule_no": None,
    }
    with caplog.at_level(logging.INFO, logger="glowderma.notifications.services"):
        assert services.deliver_appointment(payload) is None
    assert not InboxNotification.objects.exists() and fcm.sent == []
    assert "push.appointment skipped" in caplog.text


def test_appointment_push_channel(clock: Any, fcm: FakeFcm) -> None:
    appointment = AppointmentFactory.create()
    DeviceFactory.create(customer=appointment.customer)
    base = {"appointment_id": appointment.pk, "event_id": 1, "customer_id": appointment.customer_id}
    inbox = services.deliver_appointment({**base, "action": "confirmed", "reschedule_no": None})
    assert inbox is not None
    [message] = fcm.sent
    assert message.data["inbox_id"] == str(inbox.pk)
    assert (message.android_channel, message.android_priority) == ("appointments", "high")


@pytest.mark.parametrize(("kind", "channel"), [("promo", "promo"), ("general", "general")])
def test_campaign_push_channel_and_payload(clock: Any, fcm: FakeFcm, kind: str, channel: str) -> None:
    customer = CustomerFactory.create()
    DeviceFactory.create(customer=customer)
    campaign = CampaignFactory.create(kind=kind)
    services.materialise(campaign)
    campaign.save()
    services.send_batch(campaign.pk, 0)
    [message] = fcm.sent
    inbox = InboxNotification.objects.get(source_type="campaign")
    assert message.data == {
        "type": "campaign",
        "inbox_id": str(inbox.pk),
        "campaign_id": str(campaign.pk),
        "product_id": "",
        "link_url": "",
    }
    assert (message.android_channel, message.android_priority, message.apns_priority) == (
        channel,
        "normal",
        5,
    )


def test_push_texts_carry_no_pii_for_seeded_customers(clock: Any, fcm: FakeFcm) -> None:
    customers = [
        CustomerFactory.create(name="Malika Karimova", phone="+998901234567", language=lang)
        if index == 0
        else CustomerFactory.create(name=f"Dilnoza {index}", language=lang)
        for index, lang in enumerate(("uz", "ru", "en"))
    ]
    for index, customer in enumerate(customers):
        DeviceFactory.create(customer=customer)
        services.deliver_order_status(order_push(customer, "processing", order_id=20_000 + index))
        services.deliver_account_security(security_payload(customer, "social_linked"))
    assert len(fcm.sent) == 6
    for message, customer in zip(fcm.sent, [c for c in customers for _ in range(2)], strict=True):
        texts = (message.title, message.body, *message.data.values())
        for text in texts:
            assert customer.name not in text and customer.phone[4:] not in text


# --- token pruning and payload_invalid --------------------------------------------------------------------


def test_unregistered_prunes_the_device(clock: Any, fcm: FakeFcm) -> None:
    customer = CustomerFactory.create()
    device = DeviceFactory.create(customer=customer, fcm_token="tok-bad")  # noqa: S106
    inbox = services.deliver_order_status(order_push(customer, "processing"))
    assert inbox is not None and (inbox.push_state, inbox.push_error) == ("failed", "unregistered")
    device.refresh_from_db()
    assert (device.fcm_token, device.push_enabled, device.fcm_invalidated_at) == ("", False, ANCHOR)


def test_unregistered_campaign_recipient_prunes_its_device(clock: Any, fcm: FakeFcm) -> None:
    customer = CustomerFactory.create()
    device = DeviceFactory.create(customer=customer, fcm_token="campaign-bad")  # noqa: S106
    campaign = CampaignFactory.create()
    services.materialise(campaign)
    campaign.save()
    services.send_batch(campaign.pk, 0)
    device.refresh_from_db()
    assert device.fcm_token == "" and device.push_enabled is False
    assert CampaignRecipient.objects.get(campaign=campaign).error == "unregistered"


def test_payload_invalid_fails_the_message_and_leaves_devices_alone(
    clock: Any, fcm: FakeFcm, caplog: pytest.LogCaptureFixture
) -> None:
    customer = CustomerFactory.create()
    device = DeviceFactory.create(customer=customer, fcm_token="tok-payload")  # noqa: S106
    other = DeviceFactory.create(customer=CustomerFactory.create())
    payload = order_push(customer, "processing")
    message = OutboxMessage.objects.create(
        topic="push.order_status", payload=payload, dedupe_key="push.order_status:10248:processing"
    )
    with caplog.at_level(logging.ERROR, logger="glowderma.notifications.services"):
        dispatch_outbox(batch=5)
    message.refresh_from_db()
    assert message.status == "done"  # returned normally: no retry, no dead letter
    inbox = InboxNotification.objects.get()
    assert (inbox.push_state, inbox.push_error) == ("failed", "payload_invalid")
    assert "push.payload_invalid topic=push.order_status" in caplog.text
    for row in (device, other):
        before = (row.fcm_token, row.push_enabled, row.fcm_invalidated_at)
        row.refresh_from_db()
        assert (row.fcm_token, row.push_enabled, row.fcm_invalidated_at) == before
    assert Device.objects.filter(fcm_token="").count() == 0


def test_campaign_payload_invalid_keeps_the_device(
    clock: Any, fcm: FakeFcm, caplog: pytest.LogCaptureFixture
) -> None:
    customer = CustomerFactory.create()
    device = DeviceFactory.create(customer=customer, fcm_token="c-payload")  # noqa: S106
    campaign = CampaignFactory.create()
    services.materialise(campaign)
    campaign.save()
    with caplog.at_level(logging.ERROR, logger="glowderma.notifications.services"):
        services.send_batch(campaign.pk, 0)
    device.refresh_from_db()
    assert device.fcm_token == "c-payload" and device.push_enabled  # noqa: S105
    assert InboxNotification.objects.get().push_error == "payload_invalid"
    assert "push.payload_invalid topic=campaign.send_batch" in caplog.text
    assert Campaign.objects.get(pk=campaign.pk).state == "sent"


def test_push_message_profiles() -> None:
    data: Any = {"type": "campaign", "x": 1}
    message = services.push_message("t", "T", "B", data, channel="promo", priority="normal")
    assert isinstance(message, PushMessage) and message.data == {"type": "campaign", "x": "1"}
    assert (message.apns_push_type, message.apns_priority) == ("alert", 5)
    assert set(services.PUSH_PROFILES) == {
        enums.PushDataType.ORDER_STATUS,
        enums.PushDataType.APPOINTMENT,
        enums.PushDataType.ACCOUNT_SECURITY,
    }
