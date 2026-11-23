from datetime import timedelta
from hashlib import sha256
from typing import Any

import pytest
from django.core.cache import cache

from glowderma.accounts import services
from glowderma.accounts.models import StaffUser
from glowderma.core import timeutils
from glowderma.core.api.exceptions import AuthFailed

pytestmark = pytest.mark.django_db


def test_refresh_is_nonrotating_and_hashes_token() -> None:
    user = StaffUser.objects.create_user("admin@x.uz")
    session, refresh = services.start_staff_session(user, device_label="browser")
    assert len(refresh) == 43
    assert session.refresh_hash == sha256(refresh.encode()).hexdigest()
    assert services.refresh_staff_session(refresh).pk == session.pk


@pytest.mark.parametrize("reason,code", [("logout", "SESSION_EXPIRED"), ("replaced", "SESSION_REPLACED")])
def test_refresh_rejects_revoked_with_reason_code(reason: str, code: str) -> None:
    user = StaffUser.objects.create_user("admin@x.uz")
    session, refresh = services.start_staff_session(user, device_label="test")
    services.revoke_session(session, reason)
    with pytest.raises(AuthFailed) as caught:
        services.refresh_staff_session(refresh)
    assert caught.value.get_codes() == code


def test_refresh_rejects_after_absolute_expiry(monkeypatch: pytest.MonkeyPatch) -> None:
    user = StaffUser.objects.create_user("admin@x.uz")
    session, refresh = services.start_staff_session(user, device_label="test")
    monkeypatch.setattr(timeutils, "now", lambda: session.absolute_expires_at + timedelta(seconds=1))
    with pytest.raises(AuthFailed) as caught:
        services.refresh_staff_session(refresh)
    assert caught.value.get_codes() == "SESSION_EXPIRED"


def test_revoke_writes_tombstone_on_commit(django_capture_on_commit_callbacks: Any) -> None:
    user = StaffUser.objects.create_user("admin@x.uz")
    session, _ = services.start_staff_session(user, device_label="test")
    key = f"auth:sid:{session.pk}"
    cache.set(key, f"active:{session.absolute_expires_at.timestamp()}")
    with django_capture_on_commit_callbacks(execute=True):
        services.revoke_session(session, "logout")
        assert str(cache.get(key)).startswith("active:")
    assert cache.get(key) == "revoked:logout"


def test_revoke_user_sessions_can_keep_current(django_capture_on_commit_callbacks: Any) -> None:
    user = StaffUser.objects.create_user("admin@x.uz")
    current, _ = services.start_staff_session(user, device_label="one")
    other, _ = services.start_staff_session(user, device_label="two")
    with django_capture_on_commit_callbacks(execute=True):
        assert services.revoke_user_sessions(user, "password_changed", except_sid=current.pk) == 1
    current.refresh_from_db()
    other.refresh_from_db()
    assert current.revoked_at is None
    assert other.revoke_reason == "password_changed"
    assert cache.get(f"auth:sid:{other.pk}") == "revoked:password_changed"
