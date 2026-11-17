"""The accounts admin texts in Russian and English (feat/admin-ux, DESIGN §G): the marked Uzbek messages of
staff management, the password change and the panel MFA reach the reader in the request's language, and stay
byte for byte Uzbek without an ``Accept-Language``."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from django.conf import settings
from rest_framework.test import APIClient

from glowderma.accounts import tokens
from glowderma.accounts.models import StaffUser
from glowderma.accounts.tests.factories import TEST_PASSWORD, AuthSessionFactory, StaffUserFactory
from glowderma.accounts.tests.languages import overlay

pytestmark = pytest.mark.django_db

STAFF = "/api/v1/admin/staff/"
AUTH_CHANGE = "/api/v1/admin/auth/change-password/"
# Test entries on top of the committed catalog (``console_messages`` brings the real ones in later).
TRANSLATIONS: dict[str, dict[str, str]] = {
    "O‘z hisobingizni faolsizlantira olmaysiz.": {
        "en": "You cannot deactivate your own account.",
        "ru": "Нельзя деактивировать собственную учётную запись.",
    },
    "O‘z ikki bosqichli himoyangizni «Sozlamalar»da o‘chiring.": {
        "en": "Turn off your own two-factor authentication in “Settings”.",
        "ru": "Свою двухфакторную защиту отключите в разделе «Настройки».",
    },
    "Kamida 12 ta belgi.": {"en": "At least 12 characters.", "ru": "Не менее 12 символов."},
    "Joriy parol noto‘g‘ri.": {
        "en": "The current password is incorrect.",
        "ru": "Текущий пароль неверен.",
    },
}


@pytest.fixture
def translations(monkeypatch: pytest.MonkeyPatch) -> None:
    overlay(monkeypatch, TRANSLATIONS)


def client_for(user: StaffUser) -> APIClient:
    access, _ = tokens.issue_access(AuthSessionFactory.create(staff=user))
    client = APIClient()
    client.default_format = "json"
    client.credentials(
        HTTP_AUTHORIZATION=f"Bearer {access}", HTTP_X_GD_INTERNAL_SECRET=settings.INTERNAL_API_SECRET
    )
    return client


def post(client: APIClient, path: str, body: Any = None, language: str | None = None) -> Any:
    headers: dict[str, Any] = {"HTTP_IDEMPOTENCY_KEY": str(uuid.uuid4())}
    if language is not None:
        headers["HTTP_ACCEPT_LANGUAGE"] = language
    return client.post(path, body, format="json", **headers)


def test_staff_management_conflicts_follow_the_language(translations: None) -> None:
    manager = StaffUserFactory.create(email="boss@x.uz", is_manager=True)
    StaffUserFactory.create(email="other-boss@x.uz", is_manager=True)
    client = client_for(manager)
    own = f"{STAFF}{manager.pk}/deactivate/"
    english = post(client, own, language="en")
    assert english.status_code == 409 and english["Content-Language"] == "en"
    assert english.json()["code"] == "conflict"
    assert english.json()["message"] == "You cannot deactivate your own account."
    assert post(client, own, language="ru").json()["message"] == (
        "Нельзя деактивировать собственную учётную запись."
    )
    assert post(client, own).json()["message"] == "O‘z hisobingizni faolsizlantira olmaysiz."
    reset_mfa = post(client, f"{STAFF}{manager.pk}/reset-mfa/", {"reason": "telefon"}, "en")
    assert reset_mfa.status_code == 409
    assert reset_mfa.json()["message"] == "Turn off your own two-factor authentication in “Settings”."


def test_password_change_messages_follow_the_language(translations: None) -> None:
    user = StaffUserFactory.create(email="me@x.uz")
    client = client_for(user)
    short = client.post(
        AUTH_CHANGE,
        {"current_password": TEST_PASSWORD, "new_password": "Qisqa-1"},
        format="json",
        HTTP_ACCEPT_LANGUAGE="ru",
    )
    assert short.status_code == 400
    assert [(e["field"], e["code"], e["message"]) for e in short.json()["errors"]] == [
        ("new_password", "password_too_short", "Не менее 12 символов.")
    ]
    wrong = client.post(
        AUTH_CHANGE,
        {"current_password": "not-the-password", "new_password": "Juda-uzun-yangi-parol-7"},
        format="json",
        HTTP_ACCEPT_LANGUAGE="en",
    )
    assert [(e["field"], e["code"], e["message"]) for e in wrong.json()["errors"]] == [
        ("current_password", "password_incorrect", "The current password is incorrect.")
    ]
    uzbek = client.post(
        AUTH_CHANGE,
        {"current_password": "not-the-password", "new_password": "Juda-uzun-yangi-parol-7"},
        format="json",
    )
    assert uzbek.json()["errors"][0]["message"] == "Joriy parol noto‘g‘ri."
