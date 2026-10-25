"""Contract §2.11: ``GET`` / ``PATCH settings/`` shapes, validation rows and the audited diff."""

from typing import Any

import pytest
from django.test import override_settings
from rest_framework.test import APIClient

from glowderma.accounts.models import StaffUser
from glowderma.core.models import AuditEntry
from glowderma.site_config import services
from glowderma.site_config.models import SiteSettings
from glowderma.site_config.tests.factories import SiteSettingsFactory

pytestmark = pytest.mark.django_db

URL = "/api/v1/admin/settings/"


def errors(response: Any) -> list[tuple[str | None, str, str]]:
    return [(e["field"], e["code"], e["message"]) for e in response.data["errors"]]


def test_get_returns_the_typed_singleton(staff_client: APIClient) -> None:
    SiteSettingsFactory.create(pickup_address_uz="Toshkent, Chilonzor", support_phone="")
    response = staff_client.get(URL)
    assert response.status_code == 200
    body = response.data
    assert body == {
        "reservation_minutes": 15,
        "support_phone": None,
        "pickup_address": {"uz": "Toshkent, Chilonzor", "ru": "", "en": ""},
        "integrations": {"mode": "fake"},
        "updated_at": body["updated_at"],
        "updated_by": None,
    }
    assert body["updated_at"].endswith("+05:00")
    with override_settings(INTEGRATIONS_MODE="live"):
        assert staff_client.get(URL).data["integrations"] == {"mode": "live"}


def test_patch_updates_actor_and_audits_only_the_diff(staff_client: APIClient, staff: StaffUser) -> None:
    SiteSettingsFactory.create()
    response = staff_client.patch(
        URL,
        {
            "reservation_minutes": 20,
            "support_phone": None,
            "pickup_address": {"ru": "Ташкент, Чиланзар"},
        },
        format="json",
    )
    assert response.status_code == 200, response.data
    assert response.data["reservation_minutes"] == 20
    assert response.data["support_phone"] is None
    assert response.data["pickup_address"] == {
        "uz": "Toshkent, Chilonzor tumani, Bunyodkor shoh ko‘chasi, 12",
        "ru": "Ташкент, Чиланзар",
        "en": "",
    }
    assert response.data["updated_by"] == {"id": staff.pk, "display_name": staff.full_name}
    entry = AuditEntry.objects.get(action="settings.updated")
    assert (entry.entity_type, entry.entity_id, entry.actor_id) == ("settings", "1", staff.pk)
    assert entry.before == {
        "reservation_minutes": 15,
        "support_phone": "+998712000000",
        "pickup_address": {
            "uz": "Toshkent, Chilonzor tumani, Bunyodkor shoh ko‘chasi, 12",
            "ru": "",
            "en": "",
        },
    }
    assert entry.after == {
        "reservation_minutes": 20,
        "support_phone": None,
        "pickup_address": {
            "uz": "Toshkent, Chilonzor tumani, Bunyodkor shoh ko‘chasi, 12",
            "ru": "Ташкент, Чиланзар",
            "en": "",
        },
    }
    row = SiteSettings.objects.get(pk=1)
    assert row.support_phone == "" and row.updated_by == staff


def test_patch_without_changes_writes_no_audit_entry(staff_client: APIClient) -> None:
    SiteSettingsFactory.create()
    before = SiteSettings.objects.get(pk=1).updated_at
    response = staff_client.patch(URL, {"reservation_minutes": 15}, format="json")
    assert response.status_code == 200
    assert not AuditEntry.objects.filter(action="settings.updated").exists()
    assert SiteSettings.objects.get(pk=1).updated_at == before


@pytest.mark.parametrize(
    "body,expected",
    [
        ({"reservation_minutes": 4}, [("reservation_minutes", "min_value", "Qiymat 5 dan kam bo‘lmasin.")]),
        ({"reservation_minutes": 61}, [("reservation_minutes", "max_value", "Qiymat 60 dan oshmasin.")]),
        ({"reservation_minutes": "abc"}, [("reservation_minutes", "invalid", "Noto‘g‘ri qiymat.")]),
        (
            {"support_phone": "998901234567"},
            [("support_phone", "invalid", "Telefon raqamini to‘g‘ri kiriting.")],
        ),
        (
            {"pickup_address": {"uz": "x" * 201}},
            [("pickup_address.uz", "max_length", "200 belgidan oshmasin.")],
        ),
        (
            {"integrations": {"mode": "live"}},
            [("integrations", "read_only", "Bu maydonni o‘zgartirib bo‘lmaydi.")],
        ),
    ],
)
def test_patch_validation_rows(staff_client: APIClient, body: dict[str, Any], expected: list[Any]) -> None:
    SiteSettingsFactory.create()
    response = staff_client.patch(URL, body, format="json")
    assert response.status_code == 400
    assert response.data["code"] == "validation_error"
    assert response.data["message"] == "Maydonlarni tekshiring."
    assert errors(response) == expected


def test_patch_is_idempotent_when_a_key_is_sent(staff_client: APIClient) -> None:
    SiteSettingsFactory.create()
    key = "6f1c2a0e-9b8d-4c37-8a1e-0123456789ab"
    first = staff_client.patch(URL, {"reservation_minutes": 30}, format="json", HTTP_IDEMPOTENCY_KEY=key)
    second = staff_client.patch(URL, {"reservation_minutes": 30}, format="json", HTTP_IDEMPOTENCY_KEY=key)
    assert first.status_code == second.status_code == 200
    assert second["Idempotent-Replayed"] == "true" and second.data == first.data
    assert AuditEntry.objects.filter(action="settings.updated").count() == 1


def test_service_refuses_unknown_columns(staff: StaffUser) -> None:
    SiteSettingsFactory.create()
    with pytest.raises(ValueError, match="terms_version"):
        services.update_settings({"terms_version": "2"}, actor=staff)
