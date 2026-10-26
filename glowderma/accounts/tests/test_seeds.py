"""The accounts demo seeder: frontend-identical customers, devices, Malika's address, six deletions."""

import importlib
from datetime import datetime
from typing import Any

import pytest
from django.core.exceptions import ImproperlyConfigured
from django.db import models

from glowderma.accounts import seeds
from glowderma.accounts.models import Address, Customer, Device, StaffUser
from glowderma.core import enums, seeding
from glowderma.core.seeding import MockRandom, SeedContext, pinned_clock
from glowderma.core.timeutils import TASHKENT

ANCHOR = datetime.fromisoformat(seeding.DEFAULT_ANCHOR)
CONTEXT = SeedContext(anchor=ANCHOR, keep_open=True)


def test_mock_random_matches_the_frontend_vectors() -> None:
    """Values produced by ``createRandom(id × 97)`` in node for the same draw order (``customers.ts``)."""
    expected = {
        1: (34, 3, 24, True, True, True),
        2: (48, 5, 21, False, True, True),
        13: (59, 1, 16, True, True, True),
        100: (42, 7, 26, True, False, True),
        432: (49, 9, 9, True, False, False),
    }
    for customer_id, vector in expected.items():
        random = MockRandom(customer_id * 97)
        assert (
            random.int(0, 60),
            random.int(1, 12),
            random.int(1, 28),
            random.chance(0.75),
            random.chance(0.55),
            random.chance(0.8),
        ) == vector


def test_generated_customers_equal_the_frontend_fixture() -> None:
    rows = {row[0]: row for row in seeds.generated_customers()}
    assert len(rows) == 420 and min(rows) == 13 and max(rows) == 432
    assert rows[13] == (13, "Lola Nazarova", "+998997022200")
    assert rows[14] == (14, "Zarina Tursunova", "+998916720799")
    assert rows[100] == (100, "Yulduz Hasanova", "+998947853830")
    assert rows[112] == (112, "Gulchehra Sultonova", "+998954236060")
    assert rows[432] == (432, "Barno Jo‘rayeva", "+998933631313")
    names = [name for _, name, _ in seeds.all_customers()]
    phones = [phone for _, _, phone in seeds.all_customers()]
    assert len(set(names)) == len(names) == 432 and len(set(phones)) == 432


def test_customer_facts_follow_the_mock_draw_order() -> None:
    facts = seeds.CustomerFacts(13)
    assert facts.store_total == 59 * 50_000
    assert facts.registered_at == datetime(2025, 1, 16, 12, 0, tzinfo=TASHKENT)
    assert facts.language == enums.ContentLanguage.UZ
    assert facts.device == (Device.Platform.IOS, "iPhone 13", "iOS 18.1") and facts.push_enabled
    facts = seeds.CustomerFacts(432)
    assert facts.device == (Device.Platform.ANDROID, "Samsung Galaxy A54", "Android 14")
    assert not facts.push_enabled


@pytest.mark.django_db
def test_seed_accounts_builds_the_dataset_and_is_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SEED_ADMIN_PASSWORD", raising=False)
    seeds.seed_accounts(CONTEXT)
    admin = StaffUser.objects.get(email="admin@glowderma.uz")
    assert admin.pk == 1 and admin.is_staff and admin.full_name == "Administrator"
    assert admin.check_password("glowderma-demo-2026")
    assert Customer.objects.count() == 432
    malika = Customer.objects.get(pk=5)
    assert malika.name == "Malika Karimova" and malika.phone == "+998901234567"
    assert malika.registered_at == datetime(2025, 3, 12, 10, 20, tzinfo=TASHKENT)
    assert malika.created_at == malika.updated_at == malika.registered_at  # add_address ran at registration
    assert malika.address_text == "Toshkent, Chilonzor, 9-kvartal"
    assert Address.objects.get(customer=malika).is_default
    device = malika.devices.get()
    assert (device.model, device.os, device.app_version, device.push_enabled) == (
        "iPhone 14",
        "iOS 18.1",
        "1.0.0",
        True,
    )
    assert device.last_active_at == datetime(2026, 10, 24, 9, 12, tzinfo=TASHKENT)
    assert device.updated_at == device.last_active_at and device.created_at == malika.registered_at
    assert device.fid == "seed-fid-5" and device.fcm_token == "seed-fcm-5"  # noqa: S105 - seed token
    thirteen = Customer.objects.get(pk=13)
    assert thirteen.name == "Lola Nazarova" and thirteen.phone == "+998997022200"
    assert thirteen.registered_at == datetime(2025, 1, 16, 12, 0, tzinfo=TASHKENT)
    assert thirteen.created_at == thirteen.updated_at == thirteen.registered_at
    assert thirteen.devices.get().last_active_at == datetime(2026, 10, 23, 20, 15, tzinfo=TASHKENT)
    deleted = Customer.objects.filter(status=enums.CustomerStatus.DELETED)
    assert sorted(deleted.values_list("pk", flat=True)) == list(range(427, 433))
    assert set(deleted.values_list("name", flat=True)) == {""}
    assert set(deleted.values_list("deleted_at", flat=True)) == {
        datetime(2026, 10, 20, 10, 0, tzinfo=TASHKENT)
    }
    assert set(deleted.values_list("updated_at", flat=True)) == {
        datetime(2026, 10, 20, 10, 0, tzinfo=TASHKENT)
    }
    assert Device.objects.filter(is_active=True).count() == 426
    assert Device.objects.filter(customer__in=deleted, is_active=False).count() == 6
    # every bookkeeping timestamp is pinned (design 03 §7.1): nothing carries the real clock
    assert not Customer.objects.filter(created_at__gt=ANCHOR).exists()
    assert not Customer.objects.filter(updated_at__gt=ANCHOR).exists()
    assert not Customer.objects.filter(updated_at__lt=models.F("created_at")).exists()
    assert not Device.objects.filter(created_at__gt=ANCHOR).exists()
    assert not Device.objects.filter(updated_at__gt=ANCHOR).exists()
    assert not Device.objects.filter(updated_at__lt=models.F("created_at")).exists()
    assert set(Device.objects.exclude(customer__in=deleted).values_list("updated_at", flat=True)) == {
        datetime(2026, 10, 23, 20, 15, tzinfo=TASHKENT),
        datetime(2026, 10, 24, 9, 12, tzinfo=TASHKENT),
    }
    counts = (
        Customer.objects.count(),
        Device.objects.count(),
        Address.objects.count(),
        StaffUser.objects.count(),
    )
    seeds.seed_accounts(CONTEXT)
    assert (
        Customer.objects.count(),
        Device.objects.count(),
        Address.objects.count(),
        StaffUser.objects.count(),
    ) == counts
    assert seeding.reset_sequences() > 0
    assert Customer.objects.create(phone="+998900000000").pk == 433


@pytest.mark.django_db
def test_seed_admin_password_comes_from_the_environment_when_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SEED_ADMIN_PASSWORD", "Override!Harbor-Long72")
    seeds.ensure_admin()
    admin = StaffUser.objects.get(email="admin@glowderma.uz")
    assert admin.check_password("Override!Harbor-Long72")
    assert not admin.check_password("glowderma-demo-2026")
    monkeypatch.delenv("SEED_ADMIN_PASSWORD")
    assert seeds.ensure_admin() == admin  # an existing admin keeps its password


@pytest.mark.django_db
@pytest.mark.parametrize(
    "password",
    [None, "", "short-pw-11", "glowderma-demo-2026", "change-me-demo-admin-password", "Change-Me-0123456789"],
)
def test_production_settings_refuse_a_known_or_missing_seed_admin_password(
    settings: Any, monkeypatch: pytest.MonkeyPatch, password: str | None
) -> None:
    """A staging stack runs the production settings and faces the internet: no demo admin with a known
    password."""
    settings.SETTINGS_MODULE = "config.settings.production"
    if password is None:
        monkeypatch.delenv("SEED_ADMIN_PASSWORD", raising=False)
    else:
        monkeypatch.setenv("SEED_ADMIN_PASSWORD", password)
    message = seeds.preflight()
    assert message is not None and "SEED_ADMIN_PASSWORD" in message
    assert password is None or password == "" or password not in message
    assert f"accounts: {message}" in seeding.preflight_problems()
    with pytest.raises(ImproperlyConfigured, match="SEED_ADMIN_PASSWORD"):
        seeds.ensure_admin()
    assert not StaffUser.objects.filter(email="admin@glowderma.uz").exists()


@pytest.mark.django_db
def test_production_settings_accept_a_strong_seed_admin_password_and_an_existing_admin(
    settings: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings.SETTINGS_MODULE = "config.settings.production"
    monkeypatch.setenv("SEED_ADMIN_PASSWORD", "Staging!Harbor-Long72")
    assert seeds.preflight() is None
    admin = seeds.ensure_admin()
    assert admin.check_password("Staging!Harbor-Long72")
    monkeypatch.delenv("SEED_ADMIN_PASSWORD")
    assert seeds.preflight() is None  # an existing admin keeps its password
    assert seeds.ensure_admin() == admin


@pytest.mark.django_db
def test_other_settings_keep_the_demo_default(settings: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    settings.SETTINGS_MODULE = "config.settings.local"
    monkeypatch.delenv("SEED_ADMIN_PASSWORD", raising=False)
    assert seeds.preflight() is None


@pytest.mark.django_db
def test_seed_accounts_keeps_an_existing_staff_id_1() -> None:
    other = StaffUser.objects.create_user("other@x.uz", "Saffron!Harbor-Long72", id=1)
    assert other.pk == 1
    seeds.seed_accounts(CONTEXT)
    assert StaffUser.objects.get(email="admin@glowderma.uz").pk != 1


def test_seeder_is_registered_first(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(seeding, "_registry", {})
    importlib.reload(seeds)
    assert [(name, order) for name, order, _ in seeding.registered_seeders()] == [("accounts", 10)]


def test_pinned_clock_restores_the_module_clock() -> None:
    from glowderma.core import timeutils

    original = timeutils.now
    instant = datetime(2026, 10, 20, 10, 0, tzinfo=TASHKENT)
    with pinned_clock(instant):
        assert timeutils.now() == instant
    assert timeutils.now is original
