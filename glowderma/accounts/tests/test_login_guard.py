import time
from datetime import UTC, datetime

import pytest
from django.core.cache import cache
from rest_framework.exceptions import Throttled

from glowderma.accounts.api import guard as guard_module
from glowderma.accounts.api.guard import Lockout, LoginGuard, email_hash
from glowderma.core import timeutils


def test_locks_escalate_and_cap_without_success_erasing_history() -> None:
    guard = LoginGuard()
    hashed = email_hash(" Admin@X.uz ")
    assert hashed == email_hash("admin@x.uz")
    for expected in (60, 120, 240, 480, 900, 900):
        for _ in range(5):
            guard.record_failure("login", hashed)
        with pytest.raises(Throttled) as caught:
            guard.check("login", hashed)
        assert getattr(caught.value, "wait", None) == expected
        cache.delete(f"auth:lock:login:{hashed}")
        guard.reset("login", hashed)


# --- the long-window bound (``streak_limit``; the admin panel's TOTP codes) ---------------------------------


class Clock:
    """A fake clock for the cache's expiries (``time.time``) and the lock ends (``timeutils.now``)."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.at = 1_800_000_000.0
        monkeypatch.setattr(time, "time", lambda: self.at)
        monkeypatch.setattr(timeutils, "now", lambda: datetime.fromtimestamp(self.at, tz=UTC))

    def advance(self, seconds: float) -> None:
        self.at += seconds


def slow_guesser(clock: Clock, minutes: int, *, streak_limit: int | None) -> tuple[int, int]:
    """Four wrong codes a minute (one under the minute's lock) for ``minutes``, each attempt reserved first
    as ``mfa.guarded`` does: (checked guesses, refused attempts)."""
    guard = LoginGuard()
    hashed = email_hash("victim@x.uz")
    guesses = refused = 0
    for _ in range(minutes):
        for _ in range(4):
            try:
                guard.check("panel_otp", hashed)
            except Throttled:
                refused += 1
                continue
            guard.record_failure("panel_otp", hashed, streak_limit=streak_limit)
            guesses += 1
            clock.advance(1)
        clock.advance(56)
    return guesses, refused


def test_four_failures_a_minute_never_trip_the_minute_ladder(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = Clock(monkeypatch)
    assert slow_guesser(clock, 60, streak_limit=None) == (240, 0)


def test_a_streak_limit_stops_a_guesser_under_five_a_minute(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = Clock(monkeypatch)
    # one day: 10 in a row lock 15 min, 20 → 1 h, 30 → 4 h, 40 → 16 h, 50 → 24 h
    guesses, refused = slow_guesser(clock, 24 * 60, streak_limit=10)
    assert guesses == 50 and refused > 5000
    # the streak outlives the longest lock: the next day allows ten more, then a day's lock again
    clock.advance(guard_module.STREAK_MAX_WAIT_SECONDS)
    more, _ = slow_guesser(clock, 24 * 60, streak_limit=10)
    assert more <= 10


def test_streak_locks_escalate_and_a_success_clears_the_streak(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = Clock(monkeypatch)
    guard = LoginGuard()
    hashed = email_hash("ali@x.uz")

    def ten_wrong_codes() -> list[Lockout | None]:
        lockouts = []
        for _ in range(10):
            lockouts.append(guard.record_failure("panel_otp", hashed, streak_limit=10))
            clock.advance(61)  # never five in one minute
        return lockouts

    for rank, expected in enumerate((900, 3600, 14_400, 57_600, 86_400, 86_400), start=1):
        lockouts = ten_wrong_codes()
        assert lockouts == [None] * 9 + [Lockout(expected, 10 * rank)]
        with pytest.raises(Throttled) as caught:
            guard.check("panel_otp", hashed)
        assert getattr(caught.value, "wait", None) == expected - 61  # the tenth failure was 61 s ago
        clock.advance(expected)
    # a correct code (``reset``) starts the streak again: nine wrong codes after it lock nothing
    guard.reset("panel_otp", hashed)
    assert ten_wrong_codes()[:9] == [None] * 9
    # the streak forgets failures more than STREAK_TTL_SECONDS apart
    clock.advance(guard_module.STREAK_TTL_SECONDS + 1)
    assert guard.record_failure("panel_otp", hashed, streak_limit=10) is None


def test_a_streak_lock_never_shortens_a_longer_running_lock(monkeypatch: pytest.MonkeyPatch) -> None:
    Clock(monkeypatch)
    guard = LoginGuard()
    hashed = email_hash("ali@x.uz")
    cache.set(f"auth:lock:panel_otp:{hashed}", timeutils.now().timestamp() + 5000, timeout=5000)
    for _ in range(9):
        guard.record_failure("panel_otp", hashed, streak_limit=10)
    lockout = guard.record_failure("panel_otp", hashed, streak_limit=10)
    assert lockout is not None and lockout.wait == 5000 and lockout.streak == 10
    assert float(cache.get(f"auth:lock:panel_otp:{hashed}")) == timeutils.now().timestamp() + 5000


def test_scopes_without_a_streak_limit_keep_no_streak() -> None:
    guard = LoginGuard()
    hashed = email_hash("ali@x.uz")
    for _ in range(4):
        assert guard.record_failure("login", hashed) is None
    assert cache.get(f"auth:streak:login:{hashed}") is None
