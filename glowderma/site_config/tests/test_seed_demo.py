"""``manage.py seed_demo`` over the three seeders of this slice: twice → identical row counts (design 03 §10).

The registry is rebuilt from the modules here, so the test does not depend on which test imported them first
(``core`` tests swap the registry while importing).
"""

import importlib
from datetime import datetime, timedelta
from io import StringIO

import pytest
from django.contrib.auth import get_user_model
from django.core.management import call_command

from glowderma.accounts import seeds as accounts_seeds
from glowderma.accounts.models import Address, Customer, Device
from glowderma.core import seeding
from glowderma.integrations import seeds as integrations_seeds
from glowderma.integrations.models import IntegrationState, Job
from glowderma.site_config import seeds as site_config_seeds
from glowderma.site_config.models import SiteSettings


def counts() -> tuple[int, ...]:
    return (
        get_user_model().objects.count(),
        Customer.objects.count(),
        Device.objects.count(),
        Address.objects.count(),
        Job.objects.count(),
        IntegrationState.objects.count(),
        SiteSettings.objects.count(),
    )


@pytest.mark.django_db
def test_seed_demo_runs_the_three_seeders_in_layer_order_and_is_idempotent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(seeding, "_registry", {})
    for module in (site_config_seeds, integrations_seeds, accounts_seeds):
        importlib.reload(module)
    assert [name for name, _, _ in seeding.registered_seeders()] == [
        "accounts",
        "integrations",
        "site_config",
    ]
    # seed_demo's discovery would re-enter every seeder already imported in this process (the commerce
    # seeder adds the named customers' addresses): keep the run to the three modules of this slice
    monkeypatch.setattr(
        "glowderma.core.management.commands.seed_demo.discover_seeders",
        lambda: [m.__name__ for m in (accounts_seeds, integrations_seeds, site_config_seeds)],
    )
    out = StringIO()
    call_command("seed_demo", force=True, stdout=out)
    assert "ran: accounts, integrations, site_config\n" in out.getvalue()
    first = counts()
    assert first == (1, 432, 432, 1, 1, 6, 1)
    assert Job.objects.get().created_by == get_user_model().objects.get(email="admin@glowderma.uz")
    assert SiteSettings.load().support_phone == "+998712000000"
    # design 03 §7.1: every seeded timestamp is absolute, nothing carries the real clock
    anchor = datetime.fromisoformat(seeding.DEFAULT_ANCHOR)
    assert Job.objects.get().updated_at == anchor - timedelta(minutes=2)
    assert not Customer.objects.filter(created_at__gt=anchor).exists()
    assert not Customer.objects.filter(updated_at__gt=anchor).exists()
    assert not Device.objects.filter(updated_at__gt=anchor).exists()
    assert not Address.objects.filter(updated_at__gt=anchor).exists()
    call_command("seed_demo", force=True, stdout=StringIO())
    assert counts() == first
    # sequences are re-synced: the next customer continues after the explicit ids
    assert Customer.objects.create(phone="+998900000001").pk == 433


@pytest.mark.django_db
def test_seed_publishes_the_demo_terms_once_and_never_overwrites_staff_texts() -> None:
    """Gaps X-SET-LEGAL / X-SET-CONTACTS: demo contacts and texts, the terms published once at a fixed
    instant, the seeded customers' consent at their registration; a later run changes nothing and keeps staff
    edits."""
    from glowderma.core.models import AuditEntry
    from glowderma.core.timeutils import TASHKENT
    from glowderma.site_config import selectors

    context = seeding.SeedContext(anchor=datetime.fromisoformat(seeding.DEFAULT_ANCHOR), keep_open=True)
    accounts_seeds.seed_accounts(context)
    site_config_seeds.seed_site_config(context)
    row = SiteSettings.load()
    assert (row.store_phone, row.cosmetologist_phone) == ("+998712000001", "+998712000002")
    assert (row.telegram_url, row.instagram_url, row.website_url) == (
        "https://t.me/glowderma",
        "https://instagram.com/glowderma",
        "https://glowderma.uz",
    )
    assert all(
        getattr(row, f"{doc}_{lang}")
        for doc in ("terms", "privacy_policy", "booking_policy")
        for lang in ("uz", "ru", "en")
    )
    assert row.terms_version == "20241220-1"
    assert row.terms_updated_at == datetime(2024, 12, 20, 10, 0, tzinfo=TASHKENT)
    assert selectors.current_terms().published
    published = AuditEntry.objects.get(action="settings.terms_published")
    assert published.actor_type == "system"
    assert published.after is not None and (published.after["from"], published.after["to"]) == (
        None,
        "20241220-1",
    )
    active = Customer.objects.filter(status="active")
    assert active.count() == 426 and not active.exclude(terms_version="20241220-1").exists()
    assert all(c.consent_at == c.registered_at == c.updated_at for c in active)
    assert not Customer.objects.filter(status="deleted").exclude(terms_version="").exists()
    # staff edits survive a later run; nothing is published or stamped again
    SiteSettings.objects.filter(pk=1).update(
        telegram_url="https://t.me/glowderma_uz", terms_ru="Новые условия"
    )
    site_config_seeds.seed_site_config(context)
    row = SiteSettings.load()
    assert (row.telegram_url, row.terms_ru, row.terms_version) == (
        "https://t.me/glowderma_uz",
        "Новые условия",
        "20241220-1",
    )
    assert AuditEntry.objects.filter(action="settings.terms_published").count() == 1
