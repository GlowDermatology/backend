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
