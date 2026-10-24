from io import StringIO
from unittest.mock import patch

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from glowderma.accounts.models import StaffUser

pytestmark = pytest.mark.django_db


def test_createstaff_creates_non_superuser() -> None:
    with patch("sys.stdin", StringIO("Long!Saffron-Harbor-72\n")):
        call_command("createstaff", "Admin@X.uz", password_stdin=True, full_name="Administrator")
    user = StaffUser.objects.get()
    assert user.email == "admin@x.uz"
    assert user.full_name == "Administrator"
    assert not user.is_superuser and not user.is_staff


def test_createstaff_rejects_weak_password() -> None:
    with patch("sys.stdin", StringIO("123\n")), pytest.raises(CommandError):
        call_command("createstaff", "admin@x.uz", password_stdin=True)
    assert not StaffUser.objects.exists()


@pytest.mark.parametrize("password", ["  Long!Saffron-Harbor-72  ", "Long!Saffron\nHarbor-72"])
def test_createstaff_preserves_password_whitespace(password: str) -> None:
    with patch("sys.stdin", StringIO(password + "\r\n")):
        call_command("createstaff", "admin@x.uz", password_stdin=True)
    assert StaffUser.objects.get().check_password(password)
