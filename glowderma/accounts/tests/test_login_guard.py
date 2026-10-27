import pytest
from django.core.cache import cache
from rest_framework.exceptions import Throttled

from glowderma.accounts.api.guard import LoginGuard, email_hash


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
