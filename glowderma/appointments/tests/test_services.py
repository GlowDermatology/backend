"""appointments.services: transition matrix, slot rules, admin create / reschedule / cancel, booking race."""

import threading
from collections.abc import Callable
from datetime import date, datetime, time
from typing import Any, cast

import pytest
from django.db import connection
from rest_framework.exceptions import ValidationError

from glowderma.accounts.models import StaffUser
from glowderma.accounts.tests.factories import CustomerFactory, DeviceFactory, StaffUserFactory
from glowderma.appointments import services, topics
from glowderma.appointments.models import Appointment, AppointmentEvent
from glowderma.appointments.tests.conftest import ANCHOR
from glowderma.appointments.tests.factories import AppointmentFactory
from glowderma.core import enums
from glowderma.core.api.exceptions import Conflict, InvalidTransition
from glowderma.core.models import AuditEntry, OutboxMessage
from glowderma.core.timeutils import TASHKENT
from glowderma.professionals.tests.factories import (
    BlockedPeriodFactory,
    ProfessionalFactory,
    ProfessionalServiceFactory,
    WorkingHoursFactory,
    WorkingHoursOverrideFactory,
)

pytestmark = pytest.mark.django_db

MONDAY = date(2026, 10, 26)


def at(day: date, hour: int, minute: int = 0) -> datetime:
    return datetime.combine(day, time(hour, minute), tzinfo=TASHKENT)


def booking_setup(**link_kwargs: Any) -> Any:
    """A specialist working Mon–Sat 09–18 with a 13–14 break, one 30-minute service at 150 000."""
    link = ProfessionalServiceFactory.create(**{"price": 150_000, "duration_min": 30, **link_kwargs})
    for weekday in range(6):
        WorkingHoursFactory.create(
            professional=link.professional, weekday=weekday, break_start=time(13, 0), break_end=time(14, 0)
        )
    return link


def slot_error(exc: ValidationError) -> tuple[str, str]:
    detail: Any = cast(Any, exc.detail)["start"][0]
    return str(detail.code), str(detail)


@pytest.mark.parametrize(
    "status,action,ok",
    [
        ("requested", "confirm", True),
        ("requested", "reject", True),
        ("requested", "attend", False),
        ("confirmed", "attend", True),
        ("confirmed", "confirm", False),
        ("confirmed", "reject", False),
        ("rejected", "confirm", False),
        ("attended", "attend", False),
        ("cancelled", "confirm", False),
    ],
)
def test_transition_matrix(staff: StaffUser, clock: Any, status: str, action: str, ok: bool) -> None:
    extra: dict[str, Any] = {}
    if status in ("confirmed", "attended"):
        extra["confirmed_at"] = ANCHOR
    if status == "attended":
        extra["attended_at"] = ANCHOR
    if status == "rejected":
        extra["rejected_at"] = ANCHOR
    if status == "cancelled":
        extra.update(cancelled_at=ANCHOR, cancelled_by="admin")
    appointment = AppointmentFactory.create(status=status, **extra)
    DeviceFactory.create(customer=appointment.customer)
    if not ok:
        with pytest.raises(InvalidTransition) as info:
            services.transition(appointment.pk, action, actor=staff)
        assert str(info.value.detail) == "Bu amal ushbu holatda mumkin emas."
        assert appointment.events.count() == 0 and OutboxMessage.objects.count() == 0
        return
    target = {"confirm": "confirmed", "reject": "rejected", "attend": "attended"}[action]
    result = services.transition(appointment.pk, action, actor=staff)
    assert result.status == target and getattr(result, f"{target}_at") == ANCHOR
    event = appointment.events.get()
    assert (event.type, event.source, event.actor) == (target, "admin", staff)
    audit = AuditEntry.objects.get(action=f"appointment.{target}")
    assert audit.entity_type == "appointment" and audit.entity_id == str(appointment.pk)
    assert audit.after is not None and audit.after["status"] == target
    if action == "attend":
        assert event.data == {} and OutboxMessage.objects.count() == 0
    else:
        assert event.data == {"push_sent": True}
        message = OutboxMessage.objects.get()
        assert message.topic == "push.appointment"
        assert message.dedupe_key == f"push.appointment:{appointment.pk}:{target}"
        assert message.payload == {
            "appointment_id": appointment.pk,
            "action": target,
            "event_id": event.pk,
            "customer_id": appointment.customer_id,
            "reschedule_no": None,
        }


def test_push_sent_reflects_a_push_enabled_device(staff: StaffUser, clock: Any) -> None:
    appointment = AppointmentFactory.create()
    DeviceFactory.create(customer=appointment.customer, push_enabled=False, fcm_token="")
    services.confirm(appointment.pk, actor=staff)
    assert appointment.events.get().data == {"push_sent": False}
    assert OutboxMessage.objects.count() == 1  # the inbox row is still created by the handler (N-10)


def test_create_by_admin_happy_path(staff: StaffUser, clock: Any) -> None:
    link = booking_setup()
    customer = CustomerFactory.create()
    appointment = services.create_by_admin(
        professional=link.professional,
        service=link.service,
        customer=customer,
        start=at(MONDAY, 10, 5),
        note="Telefon orqali",
        actor=staff,
    )
    assert appointment.status == "confirmed" and appointment.requested_via == "admin"
    assert appointment.created_by == staff and appointment.requested_at == appointment.confirmed_at == ANCHOR
    assert (appointment.duration_min, appointment.price, appointment.staff_note) == (
        30,
        150_000,
        "Telefon orqali",
    )
    assert appointment.end == at(MONDAY, 10, 35)
    events = list(appointment.events.order_by("id"))
    assert [(e.type, e.source, e.actor_id) for e in events] == [
        ("requested", "admin", staff.pk),
        ("confirmed", "admin", staff.pk),
    ]
    assert events[0].data == {
        "via": "admin",
        "start": "2026-10-26T10:05:00+05:00",
        "end": "2026-10-26T10:35:00+05:00",
        "service_id": link.service_id,
        "professional_id": link.professional_id,
        "price": 150_000,
        "duration_min": 30,
    }
    assert events[1].data == {"push_sent": False}
    assert OutboxMessage.objects.count() == 0  # no push for a booking staff made with the customer
    audit = AuditEntry.objects.get(action="appointment.created")
    assert audit.after is not None and audit.after["requested_via"] == "admin"


@pytest.mark.parametrize(
    "start,code,message",
    [
        (at(MONDAY, 10, 7), "invalid", "Vaqt 5 daqiqalik qadamda bo‘lsin."),
        (at(date(2026, 10, 23), 10), "in_past", "Kelajakdagi vaqtni tanlang."),
        (at(MONDAY, 8, 45), "outside_working_hours", "Mutaxassis bu vaqtda ishlamaydi."),
        (at(MONDAY, 17, 45), "outside_working_hours", "Mutaxassis bu vaqtda ishlamaydi."),  # ends 18:15
        (at(date(2026, 11, 1), 10), "outside_working_hours", "Mutaxassis bu vaqtda ishlamaydi."),  # Sunday
        (at(MONDAY, 13, 30), "slot_unavailable", "Bu vaqt band."),  # the recurring break
        (at(MONDAY, 12, 45), "slot_unavailable", "Bu vaqt band."),  # overlaps the break start
        (at(MONDAY, 16, 15), "slot_unavailable", "Bu vaqt band."),  # a blocked period 16:00–16:30
        (at(MONDAY, 9, 15), "slot_unavailable", "Bu vaqt band."),  # another open appointment 09:00–09:30
        (
            at(date(2026, 10, 27), 9, 30),
            "outside_working_hours",
            "Mutaxassis bu vaqtda ishlamaydi.",
        ),  # override
    ],
)
def test_slot_validation_table(
    staff: StaffUser, clock: Any, start: datetime, code: str, message: str
) -> None:
    link = booking_setup()
    professional = link.professional
    BlockedPeriodFactory.create(professional=professional, start=at(MONDAY, 16), end=at(MONDAY, 16, 30))
    AppointmentFactory.create(professional=professional, start=at(MONDAY, 9), duration_min=30)
    WorkingHoursOverrideFactory.create(
        professional=professional, date=date(2026, 10, 27), start=time(10, 0), end=time(12, 0)
    )
    with pytest.raises(ValidationError) as info:
        services.create_by_admin(
            professional=professional,
            service=link.service,
            customer=CustomerFactory.create(),
            start=start,
            actor=staff,
        )
    assert slot_error(info.value) == (code, message)
    assert (
        Appointment.objects.count() == 1
        and not AuditEntry.objects.filter(action="appointment.created").exists()
    )


def test_slot_rules_ignore_closed_rows_and_honour_override_hours(staff: StaffUser, clock: Any) -> None:
    link = booking_setup()
    professional = link.professional
    AppointmentFactory.create(
        professional=professional, start=at(MONDAY, 9), status="rejected", rejected_at=ANCHOR
    )
    WorkingHoursOverrideFactory.create(
        professional=professional, date=date(2026, 10, 27), start=time(7, 0), end=time(9, 0)
    )
    for start in (at(MONDAY, 9), at(date(2026, 10, 27), 7, 30), at(MONDAY, 17, 30)):
        services.create_by_admin(
            professional=professional,
            service=link.service,
            customer=CustomerFactory.create(),
            start=start,
            actor=staff,
        )
    assert Appointment.objects.filter(status="confirmed").count() == 3


def test_service_must_be_offered_by_the_specialist(staff: StaffUser, clock: Any) -> None:
    link = booking_setup()
    other = ProfessionalServiceFactory.create(professional=link.professional, is_active=False)
    with pytest.raises(ValidationError) as info:
        services.create_by_admin(
            professional=link.professional,
            service=other.service,
            customer=CustomerFactory.create(),
            start=at(MONDAY, 10),
            actor=staff,
        )
    detail: Any = cast(Any, info.value.detail)["service_id"][0]
    assert (detail.code, str(detail)) == ("does_not_exist", "Mutaxassis bu xizmatni ko‘rsatmaydi.")


def test_reschedule_keeps_status_writes_event_and_pushes(staff: StaffUser, clock: Any) -> None:
    link = booking_setup()
    appointment = AppointmentFactory.create(
        professional=link.professional,
        service=link.service,
        start=at(MONDAY, 9),
        duration_min=30,
        price=150_000,
    )
    DeviceFactory.create(customer=appointment.customer)
    moved = services.reschedule(appointment.pk, start=at(MONDAY, 11), actor=staff)
    assert moved.status == "requested" and moved.reschedule_count == 1
    assert (moved.start, moved.end, moved.price) == (at(MONDAY, 11), at(MONDAY, 11, 30), 150_000)
    event = appointment.events.get()
    assert event.type == "rescheduled" and event.data == {
        "from_start": "2026-10-26T09:00:00+05:00",
        "from_end": "2026-10-26T09:30:00+05:00",
        "to_start": "2026-10-26T11:00:00+05:00",
        "to_end": "2026-10-26T11:30:00+05:00",
        "from_professional_id": link.professional_id,
        "to_professional_id": link.professional_id,
        "by": "admin",
        "push_sent": True,
    }
    message = OutboxMessage.objects.get()
    assert message.dedupe_key == f"push.appointment:{appointment.pk}:rescheduled:1"
    assert message.payload["action"] == "rescheduled" and message.payload["reschedule_no"] == 1
    assert AuditEntry.objects.filter(action="appointment.rescheduled").exists()
    # the old slot is free again, the new one is taken
    services.create_by_admin(
        professional=link.professional,
        service=link.service,
        customer=CustomerFactory.create(),
        start=at(MONDAY, 9),
        actor=staff,
    )
    with pytest.raises(ValidationError):
        services.reschedule(appointment.pk, start=at(MONDAY, 9), actor=staff)
    # a second reschedule numbers its push
    services.reschedule(appointment.pk, start=at(MONDAY, 12), actor=staff)
    assert topics.push_appointment_key(appointment.pk, "rescheduled", 2) in set(
        OutboxMessage.objects.values_list("dedupe_key", flat=True)
    )


def test_reschedule_to_another_specialist_resnapshots_the_link(staff: StaffUser, clock: Any) -> None:
    link = booking_setup()
    other = booking_setup(service=link.service, price=90_000, duration_min=20)
    appointment = AppointmentFactory.create(
        professional=link.professional,
        service=link.service,
        start=at(MONDAY, 9),
        duration_min=30,
        price=150_000,
    )
    moved = services.reschedule(
        appointment.pk, start=at(MONDAY, 9), professional=other.professional, actor=staff
    )
    assert moved.professional == other.professional
    assert (moved.duration_min, moved.price, moved.end) == (20, 90_000, at(MONDAY, 9, 20))
    assert appointment.events.get().data["to_professional_id"] == other.professional_id
    third = ProfessionalFactory.create()
    with pytest.raises(ValidationError) as info:  # the third specialist does not offer the service
        services.reschedule(appointment.pk, start=at(MONDAY, 10), professional=third, actor=staff)
    assert "service_id" in info.value.detail


def test_reschedule_and_cancel_refuse_closed_rows(staff: StaffUser, clock: Any) -> None:
    appointment = AppointmentFactory.create(status="rejected", rejected_at=ANCHOR)
    with pytest.raises(InvalidTransition):
        services.reschedule(appointment.pk, start=at(MONDAY, 10), actor=staff)
    with pytest.raises(InvalidTransition):
        services.cancel(appointment.pk, actor=staff)


def test_cancel_frees_the_slot_and_pushes(staff: StaffUser, clock: Any) -> None:
    link = booking_setup()
    appointment = AppointmentFactory.create(
        professional=link.professional,
        service=link.service,
        start=at(MONDAY, 9),
        status="confirmed",
        confirmed_at=ANCHOR,
    )
    cancelled = services.cancel(appointment.pk, reason="Mijoz so‘radi", actor=staff)
    assert (cancelled.status, cancelled.cancelled_by, cancelled.cancelled_at) == (
        "cancelled",
        "admin",
        ANCHOR,
    )
    assert cancelled.staff_note == "Mijoz so‘radi" and cancelled.confirmed_at == ANCHOR
    event = appointment.events.get()
    assert event.type == "cancelled" and event.data == {
        "by": "admin",
        "reason": "Mijoz so‘radi",
        "push_sent": False,
    }
    assert OutboxMessage.objects.get().dedupe_key == f"push.appointment:{appointment.pk}:cancelled"
    audit = AuditEntry.objects.get(action="appointment.cancelled")
    assert audit.after is not None and audit.after["reason"] == "Mijoz so‘radi"
    services.create_by_admin(
        professional=link.professional,
        service=link.service,
        customer=CustomerFactory.create(),
        start=at(MONDAY, 9),
        actor=staff,
    )
    plain = AppointmentFactory.create(professional=link.professional, start=at(MONDAY, 15))
    assert services.cancel(plain.pk, actor=staff).staff_note == ""
    assert plain.events.get().data["reason"] is None


@pytest.mark.django_db(transaction=True)
def test_concurrent_bookings_of_one_slot_yield_one_row_and_one_conflict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both requests pass the slot checks before either row exists; the exclusion constraint settles it."""
    monkeypatch.setattr("glowderma.core.timeutils.now", lambda: ANCHOR)
    link = booking_setup()
    customers = [CustomerFactory.create(), CustomerFactory.create()]
    staff = StaffUserFactory.create()
    # Both threads must meet before either writes; a generous timeout keeps a loaded database server
    # (parallel worktree suites) from turning the rendezvous into a BrokenBarrierError.
    barrier = threading.Barrier(2, timeout=30)
    outcomes: dict[int, Any] = {}

    def wait_at_barrier() -> None:
        barrier.wait()

    monkeypatch.setattr(services, "_before_write", wait_at_barrier)

    def book(index: int) -> None:
        try:
            outcomes[index] = services.create_by_admin(
                professional=link.professional,
                service=link.service,
                customer=customers[index],
                start=at(MONDAY, 10),
                actor=staff,
            )
        except Exception as exc:  # noqa: BLE001 - the outcome is the assertion
            outcomes[index] = exc
        finally:
            connection.close()

    threads = [threading.Thread(target=book, args=(index,)) for index in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=40)
    assert not any(thread.is_alive() for thread in threads)
    results = sorted(outcomes.values(), key=lambda value: isinstance(value, Exception))
    assert isinstance(results[0], Appointment) and isinstance(results[1], Conflict), outcomes
    assert str(results[1].detail) == "Bu vaqt band." and results[1].get_codes() == "conflict"
    assert Appointment.objects.count() == 1
    assert AppointmentEvent.objects.count() == 2
    assert AuditEntry.objects.filter(action="appointment.created").count() == 1


def test_race_hook_default_is_a_no_op() -> None:
    hook: Callable[[], None] = services._before_write
    hook()


def test_topics_helpers() -> None:
    assert topics.push_appointment_key(7, "confirmed") == "push.appointment:7:confirmed"
    assert topics.inbox_key(7, "rescheduled", 2) == "appointment:7:rescheduled:2"
    with pytest.raises(ValueError, match="reschedule_no"):
        topics.push_appointment_key(7, "rescheduled")


def test_check_slot_outside_a_day_spanning_block(staff: StaffUser, clock: Any) -> None:
    """A block past midnight is clipped to 23:59 by the selector; the slot code reads that as the day end."""
    link = booking_setup()
    WorkingHoursOverrideFactory.create(
        professional=link.professional, date=MONDAY, start=time(9, 0), end=time(23, 59)
    )
    BlockedPeriodFactory.create(
        professional=link.professional,
        start=at(MONDAY, 22),
        end=at(date(2026, 10, 27), 9),
        kind=enums.BlockKind.OFF,
    )
    with pytest.raises(ValidationError) as info:
        services.check_slot(link.professional, at(MONDAY, 23, 30), at(MONDAY, 23, 59))
    assert slot_error(info.value)[0] == "slot_unavailable"
    services.check_slot(link.professional, at(MONDAY, 21), at(MONDAY, 21, 30))
