from datetime import UTC, datetime

import pytest
from django.db import IntegrityError, transaction


@pytest.mark.django_db
def test_manager_normalizes_email() -> None:
    from glowderma.accounts.models import StaffUser

    user = StaffUser.objects.create_user(" Admin@X.uz ", "password")
    assert user.email == "admin@x.uz"
    assert user.check_password("password")


@pytest.mark.django_db
def test_email_is_case_insensitive_unique() -> None:
    from glowderma.accounts.models import StaffUser

    StaffUser.objects.create_user("Admin@X.uz")
    with pytest.raises(IntegrityError), transaction.atomic():
        StaffUser.objects.create(email="ADMIN@X.UZ")


def test_date_joined_uses_shared_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    from glowderma.accounts.models import StaffUser

    instant = datetime(2020, 1, 1, tzinfo=UTC)
    monkeypatch.setattr("glowderma.core.timeutils.now", lambda: instant)
    assert StaffUser(email="admin@x.uz").date_joined == instant
