"""Rules the specialists admin API shares with other modules (FND-1)."""

from datetime import datetime

import pytest
from django.core.exceptions import ImproperlyConfigured

from glowderma.appointments import services as appointments_services
from glowderma.core.timeutils import TASHKENT
from glowderma.professionals import bookings, services


def test_durations_and_schedule_times_follow_the_admin_booking_grid() -> None:
    """Durations and schedule times are multiples of the grid every booking engine can place: the admin
    booking grid of ``appointments`` (the layers forbid ``professionals`` to import it)."""
    assert services.DURATION_STEP_MIN == appointments_services.SLOT_STEP_MIN == 5


def test_an_unregistered_counter_never_lets_a_deactivation_through(monkeypatch: pytest.MonkeyPatch) -> None:
    instant = datetime(2026, 10, 24, 12, 5, tzinfo=TASHKENT)
    monkeypatch.setattr(bookings, "_counter", None)
    assert bookings.upcoming_counts([], instant) == {}
    with pytest.raises(ImproperlyConfigured):
        bookings.upcoming_counts([1], instant)
    monkeypatch.setattr(bookings, "_counter", lambda ids, at: {1: 3})
    assert bookings.upcoming_counts([1, 2], instant) == {1: 3, 2: 0}
