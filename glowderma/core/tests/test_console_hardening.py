"""Security hardening of the ops console (glowderma.core): no password change page, secrets masked in JSON,
the console bound to its own host."""

from __future__ import annotations

from typing import Any

import pytest
from django.contrib.auth import get_user_model
from django.test import Client

from glowderma.accounts.tests.factories import TEST_PASSWORD
from glowderma.accounts.tests.ops_helpers import ops_login

pytestmark = pytest.mark.django_db

PASSWORD_CHANGE_URLS = ("/django-admin/password_change/", "/django-admin/password_change/done/")


def verified_client() -> tuple[Client, Any]:
    client = Client()
    user = ops_login(client, get_user_model().objects.create_user("ops@x.uz", TEST_PASSWORD, is_staff=True))
    return client, user


# --- finding 1: the stock password change is not a console page ------------------------------------------


@pytest.mark.parametrize("url", PASSWORD_CHANGE_URLS)
def test_the_stock_password_change_pages_answer_404(url: str) -> None:
    client, user = verified_client()
    assert client.get(url).status_code == 404
    answer = client.post(
        url,
        {"old_password": TEST_PASSWORD, "new_password1": "Brand-New-Passw0rd!x", "new_password2": "x"},
    )
    assert answer.status_code == 404
    user.refresh_from_db()
    assert user.check_password(TEST_PASSWORD)


@pytest.mark.parametrize("url", PASSWORD_CHANGE_URLS)
def test_the_password_change_pages_still_send_strangers_to_the_login(url: str) -> None:
    answer = Client().get(url)
    assert answer.status_code == 302 and answer["Location"].startswith("/django-admin/login/")


def test_the_user_menu_has_no_password_link_but_the_users_own_record() -> None:
    client, user = verified_client()
    content = client.get("/django-admin/").content.decode()
    assert "password_change" not in content
    assert "Change password" not in content and "Parolni o" not in content
    own = f"/django-admin/accounts/staffuser/{user.pk}/change/"
    assert f'href="{own}"' in content and "Mening hisobim" in content
    assert client.get(own).status_code == 200
