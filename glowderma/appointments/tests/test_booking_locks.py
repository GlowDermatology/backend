"""Bookings read the offer and the schedule only under the specialist's (and the service's) row lock.

The specialists admin API writes hours, overrides, blocks and the offered services under the specialist's
``FOR UPDATE`` and a service's status under the service's ``FOR UPDATE``; a booking takes both rows
``FOR KEY SHARE`` first and reads the link, its price / duration and the day's schedule afterwards, so a
change that committed meanwhile is always seen (fix pass of the FND-1 package, findings 1-4)."""

import threading
from collections.abc import Callable
from datetime import date, datetime, time
from typing import Any

import pytest
from django.db import connection
from rest_framework.exceptions import ValidationError

from glowderma.accounts.tests.factories import CustomerFactory, StaffUserFactory
from glowderma.appointments import services
from glowderma.appointments.models import Appointment
from glowderma.appointments.tests.factories import AppointmentFactory
from glowderma.core.timeutils import TASHKENT
from glowderma.professionals import services as professionals_services
from glowderma.professionals.models import BlockedPeriod, ProfessionalService, Service
from glowderma.professionals.tests.factories import ProfessionalServiceFactory, WorkingHoursFactory

pytestmark = pytest.mark.django_db

ANCHOR = datetime(2026, 10, 24, 12, 5, tzinfo=TASHKENT)  # Saturday
MONDAY = date(2026, 10, 26)


def at(day: date, hour: int, minute: int = 0) -> datetime:
    return datetime.combine(day, time(hour, minute), tzinfo=TASHKENT)


def bookable() -> ProfessionalService:
    link = ProfessionalServiceFactory.create(price=150_000, duration_min=30)
    WorkingHoursFactory.create(professional=link.professional, weekday=0)
    return link


def committed_before_the_lock(monkeypatch: pytest.MonkeyPatch, change: Callable[[], None]) -> None:
    """Run ``change`` right before the booking locks the specialist: what a concurrent admin write that
    committed between the booking's first reads and its lock looks like to the booking."""
    hold = services._hold_active_specialist

    def changed_first(pk: int) -> bool:
        change()
        return hold(pk)

    monkeypatch.setattr(services, "_hold_active_specialist", changed_first)


# --- customer booking: the offer is re-read under the lock --------------------------------------------------


def test_a_customer_booking_of_an_offer_withdrawn_meanwhile_is_refused(
    clock: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    link = bookable()
    customer = CustomerFactory.create()
    committed_before_the_lock(monkeypatch, lambda: ProfessionalService.objects.filter(pk=link.pk).delete())
    with pytest.raises(ValidationError) as caught:
        services.request_by_customer(customer.pk, link.professional_id, link.service_id, at(MONDAY, 10))
    assert caught.value.get_codes() == {"service_id": ["does_not_exist"]}
    assert not Appointment.objects.exists()


def test_a_customer_booking_snapshots_the_price_and_duration_changed_meanwhile(
    clock: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    link = bookable()
    customer = CustomerFactory.create()
    committed_before_the_lock(
        monkeypatch,
        lambda: ProfessionalService.objects.filter(pk=link.pk).update(price=99_000, duration_min=60),
    )
    appointment = services.request_by_customer(
        customer.pk, link.professional_id, link.service_id, at(MONDAY, 10)
    )
    assert (appointment.price, appointment.duration_min) == (99_000, 60)
    assert appointment.end == at(MONDAY, 11)


def test_a_customer_booking_of_a_service_retired_meanwhile_is_refused(
    clock: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    link = bookable()
    customer = CustomerFactory.create()
    committed_before_the_lock(
        monkeypatch, lambda: Service.objects.filter(pk=link.service_id).update(is_active=False)
    )
    with pytest.raises(ValidationError) as caught:
        services.request_by_customer(customer.pk, link.professional_id, link.service_id, at(MONDAY, 10))
    assert caught.value.get_codes() == {"service_id": ["does_not_exist"]}
    assert not Appointment.objects.exists()


# --- admin booking and reschedule: link and slot are checked under the lock --------------------------------


def test_an_admin_booking_into_a_block_created_meanwhile_is_refused(
    clock: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    link = bookable()
    staff = StaffUserFactory.create()
    committed_before_the_lock(
        monkeypatch,
        lambda: BlockedPeriod.objects.create(
            professional_id=link.professional_id, start=at(MONDAY, 9), end=at(MONDAY, 12), kind="busy"
        ),
    )
    with pytest.raises(ValidationError) as caught:
        services.create_by_admin(
            professional=link.professional,
            service=link.service,
            customer=CustomerFactory.create(),
            start=at(MONDAY, 10),
            actor=staff,
        )
    assert caught.value.get_codes() == {"start": ["slot_unavailable"]}
    assert not Appointment.objects.exists()


def test_an_admin_booking_of_an_offer_withdrawn_or_a_service_retired_meanwhile_is_refused(
    clock: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    link = bookable()
    staff = StaffUserFactory.create()

    def book() -> Appointment:
        return services.create_by_admin(
            professional=link.professional,
            service=link.service,
            customer=CustomerFactory.create(),
            start=at(MONDAY, 10),
            actor=staff,
        )

    committed_before_the_lock(
        monkeypatch, lambda: ProfessionalService.objects.filter(pk=link.pk).update(is_active=False)
    )
    with pytest.raises(ValidationError) as withdrawn:
        book()
    assert withdrawn.value.get_codes() == {"service_id": ["does_not_exist"]}
    ProfessionalService.objects.filter(pk=link.pk).update(is_active=True)
    committed_before_the_lock(
        monkeypatch, lambda: Service.objects.filter(pk=link.service_id).update(is_active=False)
    )
    with pytest.raises(ValidationError) as retired:
        book()
    assert retired.value.get_codes() == {"service_id": ["does_not_exist"]}
    assert not Appointment.objects.exists()


def test_a_reschedule_with_the_same_specialist_checks_the_slot_under_the_lock(
    clock: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    link = bookable()
    appointment = AppointmentFactory.create(
        professional=link.professional, service=link.service, start=at(MONDAY, 10)
    )
    locked: list[int] = []
    hold = services._hold_active_specialist

    def spy(pk: int) -> bool:
        locked.append(pk)
        BlockedPeriod.objects.create(
            professional_id=pk, start=at(MONDAY, 14), end=at(MONDAY, 16), kind="busy"
        )
        return hold(pk)

    monkeypatch.setattr(services, "_hold_active_specialist", spy)
    with pytest.raises(ValidationError) as caught:
        services.reschedule(appointment.pk, start=at(MONDAY, 15), actor=StaffUserFactory.create())
    assert locked == [link.professional_id]
    assert caught.value.get_codes() == {"start": ["slot_unavailable"]}
    assert Appointment.objects.get(pk=appointment.pk).start == at(MONDAY, 10)


def test_a_reschedule_of_a_deactivated_specialists_appointment_on_the_same_specialist_still_works(
    clock: Any,
) -> None:
    link = bookable()
    appointment = AppointmentFactory.create(
        professional=link.professional, service=link.service, start=at(MONDAY, 10)
    )
    link.professional.is_active = False
    link.professional.save(update_fields=["is_active"])
    moved = services.reschedule(appointment.pk, start=at(MONDAY, 11), actor=StaffUserFactory.create())
    assert moved.start == at(MONDAY, 11)


# --- real concurrency: the booking waits for the admin write and then sees it -------------------------------


def _run(outcomes: dict[str, Any], key: str, call: Callable[[], Any]) -> threading.Thread:
    def target() -> None:
        try:
            outcomes[key] = call()
        except Exception as exc:  # noqa: BLE001 - the outcome is the assertion
            outcomes[key] = exc
        finally:
            connection.close()

    thread = threading.Thread(target=target)
    thread.start()
    return thread


def _held_on_second_call(
    monkeypatch: pytest.MonkeyPatch, module: Any, name: str
) -> tuple[threading.Event, threading.Event]:
    """Patch ``module.name`` so that its second call (the "after" state, taken once the write is done and
    the row lock held) signals ``inside`` and waits for ``release``."""
    inside, release = threading.Event(), threading.Event()
    original = getattr(module, name)
    calls = {"n": 0}

    def wrapper(*args: Any, **kwargs: Any) -> Any:
        calls["n"] += 1
        if calls["n"] == 2:
            inside.set()
            release.wait(10)
        return original(*args, **kwargs)

    monkeypatch.setattr(module, name, wrapper)
    return inside, release


def _race(
    write: Callable[[], Any], book: Callable[[], Any], inside: threading.Event, release: threading.Event
) -> dict[str, Any]:
    outcomes: dict[str, Any] = {}
    writer = _run(outcomes, "write", write)
    assert inside.wait(10)  # the admin write is done and holds its row lock, not yet committed
    booking = _run(outcomes, "booking", book)
    booking.join(timeout=1)
    assert booking.is_alive()  # the booking waits on the row lock
    release.set()
    writer.join(timeout=10)
    booking.join(timeout=10)
    return outcomes


@pytest.mark.django_db(transaction=True)
def test_a_customer_booking_waits_for_put_services_and_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("glowderma.core.timeutils.now", lambda: ANCHOR)
    link = bookable()
    customer, staff = CustomerFactory.create(), StaffUserFactory.create()
    inside, release = _held_on_second_call(monkeypatch, professionals_services, "_links_state")
    outcomes = _race(
        lambda: professionals_services.set_offered_services(link.professional_id, [], actor=staff),
        lambda: services.request_by_customer(
            customer.pk, link.professional_id, link.service_id, at(MONDAY, 10)
        ),
        inside,
        release,
    )
    refused = outcomes["booking"]
    assert isinstance(refused, ValidationError), outcomes
    assert refused.get_codes() == {"service_id": ["does_not_exist"]}
    assert not Appointment.objects.exists()


@pytest.mark.django_db(transaction=True)
def test_a_customer_booking_waits_for_a_service_deactivation_and_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("glowderma.core.timeutils.now", lambda: ANCHOR)
    link = bookable()
    customer, staff = CustomerFactory.create(), StaffUserFactory.create()
    inside, release = _held_on_second_call(monkeypatch, professionals_services, "_service_state")
    outcomes = _race(
        lambda: professionals_services.update_service(link.service_id, {"is_active": False}, actor=staff),
        lambda: services.request_by_customer(
            customer.pk, link.professional_id, link.service_id, at(MONDAY, 10)
        ),
        inside,
        release,
    )
    assert isinstance(outcomes["write"], Service), outcomes
    refused = outcomes["booking"]
    assert isinstance(refused, ValidationError), outcomes
    assert refused.get_codes() == {"service_id": ["does_not_exist"]}
    assert not Appointment.objects.exists()


@pytest.mark.django_db(transaction=True)
def test_an_admin_booking_waits_for_a_block_and_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("glowderma.core.timeutils.now", lambda: ANCHOR)
    link = bookable()
    customer, staff = CustomerFactory.create(), StaffUserFactory.create()
    inside, release = threading.Event(), threading.Event()
    state = professionals_services._block_state

    def held(block: BlockedPeriod) -> dict[str, Any]:
        inside.set()
        release.wait(10)
        return state(block)

    monkeypatch.setattr(professionals_services, "_block_state", held)
    outcomes = _race(
        lambda: professionals_services.create_block(
            link.professional_id,
            {"start": at(MONDAY, 9), "end": at(MONDAY, 12), "kind": "busy"},
            actor=staff,
        ),
        lambda: services.create_by_admin(
            professional=link.professional,
            service=link.service,
            customer=customer,
            start=at(MONDAY, 10),
            actor=staff,
        ),
        inside,
        release,
    )
    assert isinstance(outcomes["write"], BlockedPeriod), outcomes
    refused = outcomes["booking"]
    assert isinstance(refused, ValidationError), outcomes
    assert refused.get_codes() == {"start": ["slot_unavailable"]}
    assert not Appointment.objects.exists()


@pytest.mark.django_db(transaction=True)
def test_a_reschedule_waits_for_an_hours_change_and_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("glowderma.core.timeutils.now", lambda: ANCHOR)
    link = bookable()
    staff = StaffUserFactory.create()
    appointment = AppointmentFactory.create(
        professional=link.professional, service=link.service, start=at(MONDAY, 10)
    )
    inside, release = _held_on_second_call(monkeypatch, professionals_services, "_week_state")
    outcomes = _race(
        lambda: professionals_services.set_working_week(
            link.professional_id, [{"weekday": 0, "start": time(9), "end": time(12)}], actor=staff
        ),
        lambda: services.reschedule(appointment.pk, start=at(MONDAY, 15), actor=staff),
        inside,
        release,
    )
    refused = outcomes["booking"]
    assert isinstance(refused, ValidationError), outcomes
    assert refused.get_codes() == {"start": ["outside_working_hours"]}
    assert Appointment.objects.get(pk=appointment.pk).start == at(MONDAY, 10)
