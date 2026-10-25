"""``push.order_status`` / ``push.appointment`` handlers: language-aware rendering, idempotent redelivery."""

from datetime import datetime
from typing import Any

import pytest

from glowderma.accounts.tests.factories import CustomerFactory, DeviceFactory
from glowderma.appointments import services as appointment_services
from glowderma.appointments.tests.factories import AppointmentFactory
from glowderma.core import outbox
from glowderma.core.models import OutboxMessage
from glowderma.core.tasks import dispatch_outbox
from glowderma.core.timeutils import TASHKENT
from glowderma.integrations.fcm.fake import FakeFcm
from glowderma.integrations.fcm.ports import FcmUnavailable
from glowderma.notifications import services, tasks, texts
from glowderma.notifications.models import InboxNotification, StatusTemplate
from glowderma.notifications.tests.conftest import ANCHOR

pytestmark = pytest.mark.django_db


def order_payload(customer_id: int, status: str = "processing", order_id: int = 10248) -> dict[str, Any]:
    return {"order_id": order_id, "status": status, "event_id": 3, "customer_id": customer_id}


def test_order_status_push_renders_the_template_in_the_customer_language(clock: Any, fcm: FakeFcm) -> None:
    customer = CustomerFactory.create(language="ru")
    device = DeviceFactory.create(customer=customer)
    inbox = services.deliver_order_status(order_payload(customer.pk))
    assert inbox is not None
    assert inbox.title == "Jarayonda" and inbox.body == "Оплата принята. Заказ №10248 готовится."
    assert (inbox.kind, inbox.source_type, inbox.source_id, inbox.template_status) == (
        "order_status",
        "order",
        10248,
        "processing",
    )
    assert (inbox.language, inbox.push_state, inbox.pushed_at) == ("ru", "sent", ANCHOR)
    assert (
        inbox.link == {"kind": "order", "order_id": 10248}
        and inbox.dedupe_key == "order_status:10248:processing"
    )
    [message] = fcm.sent
    assert message.token == device.fcm_token and message.title == "Jarayonda"
    assert message.data == {"type": "order_status", "order_id": "10248", "status": "processing"}
    # a missing translation falls back to Uzbek (delivered has no English text)
    english = CustomerFactory.create(language="en")
    DeviceFactory.create(customer=english)
    delivered = services.deliver_order_status(order_payload(english.pk, "delivered"))
    assert delivered is not None and delivered.language == "uz"
    assert delivered.body == "Buyurtma №10248 yetkazildi. Xaridingiz uchun rahmat."
    assert delivered.title == "Yetkazildi"


def test_order_status_push_is_idempotent_on_redelivery(clock: Any, fcm: FakeFcm) -> None:
    customer = CustomerFactory.create()
    DeviceFactory.create(customer=customer)
    message = OutboxMessage.objects.create(
        topic="push.order_status",
        payload=order_payload(customer.pk),
        dedupe_key="push.order_status:10248:processing",
    )
    tasks.handle_push_order_status(message)
    tasks.handle_push_order_status(message)  # redelivered
    outbox.deliver(message)  # through the registry too
    assert InboxNotification.objects.filter(dedupe_key="order_status:10248:processing").count() == 1
    assert len(fcm.sent) == 1
    # another status of the same order is another push
    tasks.handle_push_order_status(
        OutboxMessage.objects.create(
            topic="push.order_status",
            payload=order_payload(customer.pk, "handed_to_courier"),
            dedupe_key="push.order_status:10248:handed_to_courier",
        )
    )
    assert InboxNotification.objects.filter(source_id=10248).count() == 2 and len(fcm.sent) == 2


def test_order_status_push_without_a_device_keeps_the_inbox_row(clock: Any, fcm: FakeFcm) -> None:
    customer = CustomerFactory.create()
    DeviceFactory.create(customer=customer, push_enabled=False, fcm_token="")
    inbox = services.deliver_order_status(order_payload(customer.pk))
    assert inbox is not None and inbox.push_state == "no_device" and fcm.sent == []
    # a device registered later does not resend an old status (the row is settled)
    DeviceFactory.create(customer=customer)
    again = services.deliver_order_status(order_payload(customer.pk))
    assert again is not None and again.pk == inbox.pk and again.push_state == "no_device" and fcm.sent == []


def test_order_status_push_failures_and_outages(clock: Any, fcm: FakeFcm) -> None:
    customer = CustomerFactory.create()
    DeviceFactory.create(customer=customer, fcm_token="tok-bad")  # noqa: S106
    inbox = services.deliver_order_status(order_payload(customer.pk))
    assert inbox is not None and (inbox.push_state, inbox.push_error) == ("failed", "unregistered")
    other = CustomerFactory.create()
    DeviceFactory.create(customer=other, fcm_token="tok-down")  # noqa: S106
    with pytest.raises(FcmUnavailable):
        services.deliver_order_status(order_payload(other.pk, order_id=10249))
    queued = InboxNotification.objects.get(dedupe_key="order_status:10249:processing")
    assert queued.push_state == "queued"  # released for the retry
    other.devices.update(fcm_token="tok-fine")  # noqa: S106
    retried = services.deliver_order_status(order_payload(other.pk, order_id=10249))
    assert retried is not None and retried.pk == queued.pk and retried.push_state == "sent"


def test_order_status_push_edge_cases(clock: Any) -> None:
    assert services.deliver_order_status(order_payload(999_999)) is None
    with pytest.raises(ValueError, match="awaiting_payment"):
        services.deliver_order_status(order_payload(1, "awaiting_payment"))
    template = StatusTemplate.objects.get(status="processing")
    assert services.render_template(template, "xx", 7) == (
        "To‘lov qabul qilindi. Buyurtma №7 tayyorlanmoqda.",
        "uz",
    )


def test_appointment_push_through_the_outbox(clock: Any, fcm: FakeFcm, staff: Any) -> None:
    appointment = AppointmentFactory.create(start=datetime(2026, 10, 26, 9, tzinfo=TASHKENT))
    appointment.customer.language = "ru"
    appointment.customer.save()
    device = DeviceFactory.create(customer=appointment.customer)
    appointment_services.confirm(appointment.pk, actor=staff)
    dispatch_outbox(batch=5)
    inbox = InboxNotification.objects.get(dedupe_key=f"appointment:{appointment.pk}:confirmed")
    assert (inbox.kind, inbox.source_type, inbox.source_id, inbox.language) == (
        "appointment",
        "appointment",
        appointment.pk,
        "ru",
    )
    assert (
        inbox.title == "Приём подтверждён"
        and inbox.body == "Ваш приём 26.10.2026 09:00 подтверждён. Ждём вас!"
    )
    assert (
        inbox.link == {"kind": "appointment", "appointment_id": appointment.pk} and inbox.push_state == "sent"
    )
    [message] = fcm.sent
    assert message.token == device.fcm_token
    assert message.data == {
        "type": "appointment",
        "appointment_id": str(appointment.pk),
        "action": "confirmed",
    }
    assert OutboxMessage.objects.get(topic="push.appointment").status == "done"
    # redelivery of the same message is a no-op
    tasks.handle_push_appointment(OutboxMessage.objects.get(topic="push.appointment"))
    assert len(fcm.sent) == 1


def test_appointment_push_actions_are_numbered_for_reschedules(clock: Any, fcm: FakeFcm) -> None:
    appointment = AppointmentFactory.create(start=datetime(2026, 10, 26, 9, tzinfo=TASHKENT))
    DeviceFactory.create(customer=appointment.customer)
    base = {"appointment_id": appointment.pk, "event_id": 1, "customer_id": appointment.customer_id}
    first = services.deliver_appointment({**base, "action": "rescheduled", "reschedule_no": 1})
    second = services.deliver_appointment({**base, "action": "rescheduled", "reschedule_no": 2})
    cancelled = services.deliver_appointment({**base, "action": "cancelled", "reschedule_no": None})
    assert first is not None and second is not None and cancelled is not None
    assert {first.dedupe_key, second.dedupe_key, cancelled.dedupe_key} == {
        f"appointment:{appointment.pk}:rescheduled:1",
        f"appointment:{appointment.pk}:rescheduled:2",
        f"appointment:{appointment.pk}:cancelled",
    }
    assert first.title == "Qabul vaqti o‘zgardi" and cancelled.body.startswith(
        "26.10.2026 09:00 dagi qabulingiz bekor"
    )
    assert len(fcm.sent) == 3
    assert (
        services.deliver_appointment(
            {**base, "appointment_id": 999_999, "action": "rejected", "reschedule_no": None}
        )
        is None
    )
    with pytest.raises(ValueError, match="attended"):
        services.deliver_appointment({**base, "action": "attended", "reschedule_no": None})


def test_appointment_texts_fall_back_to_uzbek() -> None:
    start = datetime(2026, 10, 26, 9, tzinfo=TASHKENT)
    title, body, language = texts.appointment_text("rejected", "xx", start)
    assert language == "uz" and title == "Qabul rad etildi" and "26.10.2026 09:00" in body
    assert texts.appointment_text("cancelled", "en", start)[0] == "Appointment cancelled"
