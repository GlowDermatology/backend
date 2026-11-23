import importlib

import pytest
from django.test import Client, override_settings
from django.urls import Resolver404, clear_url_caches, resolve

import config.urls


def test_django_admin_not_mounted_when_disabled() -> None:
    try:
        with override_settings(DJANGO_ADMIN_ENABLED=False):
            importlib.reload(config.urls)
            clear_url_caches()
            with pytest.raises(Resolver404):
                resolve("/django-admin/")
    finally:
        importlib.reload(config.urls)
        clear_url_caches()


@pytest.mark.django_db
def test_django_admin_login_locked_after_5_failures() -> None:
    client = Client()
    for _ in range(5):
        assert (
            client.post("/django-admin/login/", {"username": "unknown@x.uz", "password": "wrong"}).status_code
            == 200
        )
    response = client.post("/django-admin/login/", {"username": "unknown@x.uz", "password": "wrong"})
    assert response.status_code == 429
    assert response["Retry-After"] == "60"
    assert "Juda ko‘p urinish. 60 soniyadan so‘ng qayta urinib ko‘ring." in response.content.decode()
    assert b'name="username"' in response.content  # the Unfold login page, not a JSON error
