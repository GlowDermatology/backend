"""The appointment seams P0 adds (plan §1 P0): ``last_activity_at`` (dormant rule) and the customer cancel
(a P0 stub, implemented by P3)."""

from datetime import timedelta

import pytest

from glowderma.accounts.models import Customer
from glowderma.accounts.tests.factories import CustomerFactory, StaffUserFactory
from glowderma.appointments import selectors, services
from glowderma.appointments.models import Appointment
from glowderma.appointments.tests.factories import AppointmentFactory
from glowderma.core import timeutils

pytestmark = pytest.mark.django_db


STATUS_FIELDS = {
    "requested": {},
    "confirmed": {"confirmed_at"},
    "rejected": {"rejected_at"},
    "attended": {"confirmed_at", "attended_at"},
    "cancelled": {"cancelled_at"},
}


def make(customer: Customer, status: str, *, staff: bool = False) -> Appointment:
    now = timeutils.now()
    extra: dict[str, object] = dict.fromkeys(STATUS_FIELDS[status], now)
    if status == "cancelled":
        extra["cancelled_by"] = "customer"
    if staff:
        extra |= {"requested_via": "admin", "created_by": StaffUserFactory.create()}
    return AppointmentFactory.create(customer=customer, status=status, **extra)


def at(appointment: Appointment, *, created_days_ago: int, start_days: int) -> Appointment:
    now = timeutils.now()
    start = now + timedelta(days=start_days)
    Appointment.objects.filter(pk=appointment.pk).update(
        created_at=now - timedelta(days=created_days_ago),
        start=start,
        end=start + timedelta(minutes=appointment.duration_min),
    )
    appointment.refresh_from_db()
    return appointment


def test_none_without_appointments() -> None:
    assert selectors.last_activity_at(CustomerFactory.create().pk) is None


def test_a_staff_booking_counts_by_its_creation() -> None:
    customer = CustomerFactory.create()
    booking = at(
        make(customer, "rejected", staff=True),
        created_days_ago=3,
        start_days=-200,
    )
    assert selectors.last_activity_at(customer.pk) == booking.created_at


def test_an_attended_visit_counts_by_its_start() -> None:
    customer = CustomerFactory.create()
    visit = at(make(customer, "attended"), created_days_ago=300, start_days=-10)
    assert selectors.last_activity_at(customer.pk) == visit.start


def test_a_future_confirmed_appointment_wins_with_its_future_start() -> None:
    customer = CustomerFactory.create()
    at(make(customer, "attended"), created_days_ago=300, start_days=-10)
    future = at(make(customer, "confirmed"), created_days_ago=20, start_days=5)
    assert selectors.last_activity_at(customer.pk) == future.start
    assert future.start > timeutils.now()


def test_rejected_and_cancelled_count_only_by_creation() -> None:
    customer = CustomerFactory.create()
    rejected = at(make(customer, "rejected"), created_days_ago=40, start_days=9)
    cancelled = at(make(customer, "cancelled"), created_days_ago=50, start_days=12)
    assert selectors.last_activity_at(customer.pk) == max(rejected.created_at, cancelled.created_at)


def test_cancel_by_customer_is_implemented_by_p3() -> None:
    customer = CustomerFactory.create()
    appointment = at(make(customer, "requested"), created_days_ago=1, start_days=2)  # an open future one
    cancelled = services.cancel_by_customer(appointment.pk, customer.pk, reason="account_deleted")
    assert (cancelled.status, cancelled.cancelled_by) == ("cancelled", "customer")
