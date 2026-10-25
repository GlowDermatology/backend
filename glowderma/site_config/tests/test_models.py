"""SiteSettings singleton: constraints, ``load()``, admin, seeder, migrations (design 01 §3, §8)."""

import importlib
from datetime import datetime
from typing import Any

import pytest
from django.contrib import admin
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.db import IntegrityError, connection, transaction
from django.test import Client, RequestFactory
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from glowderma.core import seeding
from glowderma.core.admin import ReadOnlyAdmin
from glowderma.core.seeding import SeedContext
from glowderma.site_config import seeds
from glowderma.site_config.models import SiteSettings
from glowderma.site_config.tests.factories import SiteSettingsFactory

pytestmark = pytest.mark.django_db

ANCHOR = datetime.fromisoformat(seeding.DEFAULT_ANCHOR)
CONTEXT = SeedContext(anchor=ANCHOR, keep_open=True)


def update_fails(**changes: Any) -> None:
    with pytest.raises(IntegrityError), transaction.atomic():
        SiteSettings.objects.filter(pk=1).update(**changes)


def test_load_returns_the_migrated_row_without_an_insert() -> None:
    assert SiteSettings.objects.count() == 1
    with CaptureQueriesContext(connection) as queries:
        row = SiteSettings.load()
    assert len(queries) == 1 and "INSERT" not in queries[0]["sql"]
    assert row.pk == 1 and row.reservation_minutes == 15 and row.support_phone == ""
    assert row.updated_by is None and row.terms_version == ""
    assert str(row) == "Site settings"


def test_singleton_check_refuses_a_second_row() -> None:
    with pytest.raises(IntegrityError), transaction.atomic():
        SiteSettings.objects.create(pk=2)
    with pytest.raises(IntegrityError), transaction.atomic():
        SiteSettings.objects.create()  # default pk 1 collides with the migrated row
    assert SiteSettings.objects.count() == 1


def test_reservation_minutes_bounds() -> None:
    update_fails(reservation_minutes=4)
    update_fails(reservation_minutes=61)
    SiteSettings.objects.filter(pk=1).update(reservation_minutes=5)
    SiteSettings.objects.filter(pk=1).update(reservation_minutes=60)


@pytest.mark.parametrize("field", ["support_phone", "cosmetologist_phone", "store_phone"])
def test_phones_are_empty_or_uzbek_e164(field: str) -> None:
    update_fails(**{field: "998712000000"})
    update_fails(**{field: "+99871200000"})
    SiteSettings.objects.filter(pk=1).update(**{field: "+998712000000"})
    SiteSettings.objects.filter(pk=1).update(**{field: ""})


def test_constraints_in_pg_catalog() -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT conname FROM pg_constraint WHERE conrelid = 'site_config_sitesettings'::regclass"
            " AND contype = 'c'"
        )
        names = {row[0] for row in cursor.fetchall()}
    assert names >= {
        "sitesettings_singleton",
        "sitesettings_reservation_5_60",
        "sitesettings_support_phone_e164",
        "sitesettings_cosmetologist_phone_e164",
        "sitesettings_store_phone_e164",
    }


def test_updated_by_is_set_null_on_staff_delete() -> None:
    staff = get_user_model().objects.create_user("x@x.uz", "Saffron!Harbor-Long72")
    SiteSettings.objects.filter(pk=1).update(updated_by=staff)
    staff.delete()
    assert SiteSettings.load().updated_by is None


def test_factory_updates_the_singleton() -> None:
    row = SiteSettingsFactory.create(support_phone="+998901112233")
    assert row.pk == 1 and SiteSettings.objects.count() == 1
    assert SiteSettings.load().support_phone == "+998901112233"


def test_seed_site_config_values_and_idempotency() -> None:
    seeds.seed_site_config(CONTEXT)
    row = SiteSettings.load()
    assert row.reservation_minutes == 15 and row.support_phone == "+998712000000"
    assert row.pickup_address_uz == "Toshkent, Chilonzor tumani, Bunyodkor shoh ko‘chasi, 12"
    assert row.pickup_address_ru == "" and row.pickup_address_en == "" and row.updated_by is None
    updated_at = row.updated_at
    seeds.seed_site_config(CONTEXT)
    row.refresh_from_db()
    assert row.updated_at == updated_at and SiteSettings.objects.count() == 1


def test_seeder_registered_after_integrations(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(seeding, "_registry", {})
    importlib.reload(seeds)
    assert [(name, order) for name, order, _ in seeding.registered_seeders()] == [("site_config", 30)]


def test_admin_registration_is_read_only() -> None:
    model_admin = admin.site._registry[SiteSettings]
    assert isinstance(model_admin, ReadOnlyAdmin)
    request = RequestFactory().get("/django-admin/")
    request.user = get_user_model().objects.create_user("root@x.uz", "Saffron!Harbor-Long72", is_staff=True)
    assert model_admin.has_view_permission(request)
    assert not model_admin.has_add_permission(request)
    assert not model_admin.has_change_permission(request)
    assert not model_admin.has_delete_permission(request)
    client = Client()
    client.force_login(request.user)
    assert client.get(reverse("admin:site_config_sitesettings_changelist")).status_code == 200
    assert client.get(reverse("admin:site_config_sitesettings_change", args=[1])).status_code == 200
    assert client.get(reverse("admin:site_config_sitesettings_add")).status_code == 403


@pytest.mark.django_db(transaction=True)
def test_site_config_migrations_reverse_to_zero_and_forward_again() -> None:
    SiteSettings.objects.filter(pk=1).update(support_phone="+998712000000")
    call_command("migrate", "site_config", "0001", verbosity=0)
    # reverse of 0002 is a no-op (the row is configuration): pk 1 survives untouched; 0001's reverse drops it
    assert SiteSettings.objects.get().pk == 1 and SiteSettings.load().support_phone == "+998712000000"
    call_command("migrate", "site_config", "zero", verbosity=0)
    assert "site_config_sitesettings" not in connection.introspection.table_names()
    call_command("migrate", "site_config", verbosity=0)
    assert SiteSettings.objects.get().pk == 1
    assert SiteSettings.load().support_phone == ""  # a fresh row, not the flushed one


@pytest.mark.django_db(transaction=True)
def test_flush_keeps_the_singleton() -> None:
    """A transactional test ends with pytest-django's flush; ``post_migrate`` re-inserts pk 1, so later tests
    (and a ``--reuse-db`` rerun) never find ``load()`` inserting."""
    SiteSettings.objects.filter(pk=1).update(reservation_minutes=30)
    call_command("flush", interactive=False, verbosity=0)
    assert SiteSettings.objects.count() == 1
    assert SiteSettings.load().pk == 1 and SiteSettings.load().reservation_minutes == 15
