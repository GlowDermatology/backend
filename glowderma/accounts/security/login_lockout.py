"""The per-account login lockout ladder (``LoginGuard``).

``LoginGuard`` is the per-account ladder (five failures a minute lock the account for 1, 2, 4, 8, then 15
minutes). A scope can add a long-window bound (``streak_limit``, the admin panel's TOTP codes): every failure
also counts in a consecutive-failure streak that only a success (or an unlock) clears and that lives
:data:`STREAK_TTL_SECONDS` after the last failure; each ``streak_limit``-th failure in a row locks the account
for 15 minutes, 1, 4, 16, then 24 hours, so a guesser who stays under five a minute is stopped too.
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass

from django.core.cache import cache
from rest_framework.exceptions import Throttled

from glowderma.accounts.security import counters
from glowderma.core import timeutils


def email_hash(email: str) -> str:
    return hashlib.sha256(email.strip().lower().encode()).hexdigest()


# The long-window bound of ``LoginGuard.record_failure(streak_limit=…)`` (module doc): the streak outlives the
# longest lock, so guessing on after it keeps locking for a day each time.
STREAK_TTL_SECONDS = 48 * 60 * 60
STREAK_FIRST_WAIT_SECONDS = 15 * 60
STREAK_MAX_WAIT_SECONDS = 24 * 60 * 60


@dataclass(frozen=True)
class Lockout:
    """A lock that a failure just started: how long it runs and the failures in a row behind it (``0``
    without a streak)."""

    wait: int
    streak: int


class LoginGuard:
    def check(self, scope: str, hashed_email: str, *, reserve: bool = True) -> None:
        key = f"auth:lock:{scope}:{hashed_email}"
        expiry = cache.get(key)
        if expiry is not None:
            wait = math.ceil(float(expiry) - timeutils.now().timestamp())
            if wait > 0:
                raise Throttled(wait=wait)
            cache.delete(key)
        # Reserve an attempt before authentication so concurrent guesses share the limit.
        if reserve and counters.increment_counter(f"auth:attempt:{scope}:{hashed_email}", 60) > 5:
            raise Throttled(wait=60)

    def record_failure(
        self, scope: str, hashed_email: str, *, streak_limit: int | None = None
    ) -> Lockout | None:
        """Count one failure; the :class:`Lockout` it started, if any. Five in a minute lock for 1, 2, 4, 8,
        then 15 minutes; with ``streak_limit`` every ``streak_limit``-th failure in a row (no success between,
        none more than :data:`STREAK_TTL_SECONDS` apart) also locks, for 15 minutes, 1, 4, 16, then 24
        hours."""
        waits: list[int] = []
        count = counters.increment_counter(f"auth:fail:{scope}:{hashed_email}", 60)
        if count == 5:
            locks = counters.increment_counter(f"auth:lockn:{scope}:{hashed_email}", 24 * 60 * 60)
            waits.append(min(60 * 2 ** min(locks - 1, 4), 900))
        streak = 0
        if streak_limit:
            streak = counters.increment_counter(
                f"auth:streak:{scope}:{hashed_email}", STREAK_TTL_SECONDS, sliding=True
            )
            if streak % streak_limit == 0:
                rank = min(streak // streak_limit - 1, 4)
                waits.append(min(STREAK_FIRST_WAIT_SECONDS * 4**rank, STREAK_MAX_WAIT_SECONDS))
        if not waits:
            return None
        wait = max(waits)
        key = f"auth:lock:{scope}:{hashed_email}"
        until = timeutils.now().timestamp() + wait
        running = cache.get(key)
        if running is not None and float(running) > until:  # never shorten a longer lock
            return Lockout(math.ceil(float(running) - timeutils.now().timestamp()), streak)
        cache.set(key, until, timeout=wait)
        return Lockout(wait, streak)

    def reset(self, scope: str, hashed_email: str) -> None:
        """A success: the minute's failures and attempts and the failure streak start again."""
        cache.delete_many(
            [
                f"auth:fail:{scope}:{hashed_email}",
                f"auth:attempt:{scope}:{hashed_email}",
                f"auth:streak:{scope}:{hashed_email}",
            ]
        )

    def unlock(self, scope: str, hashed_email: str) -> None:
        """End a running lockout too (a manager reset the password: the guessed one is gone). The escalation
        counter ``auth:lockn`` stays, so guessing on afterwards still locks for longer each time."""
        self.reset(scope, hashed_email)
        cache.delete(f"auth:lock:{scope}:{hashed_email}")


__all__ = [
    "STREAK_FIRST_WAIT_SECONDS",
    "STREAK_MAX_WAIT_SECONDS",
    "STREAK_TTL_SECONDS",
    "Lockout",
    "LoginGuard",
    "email_hash",
]
