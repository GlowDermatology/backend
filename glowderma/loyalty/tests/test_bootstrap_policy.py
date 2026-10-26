"""loyalty.0006 (gap CB-9.2.4): the first cashback policy of an install with the L1–L3 defaults, inserted only
into an empty policy table, audited, idempotent, insert-only and reversible as a no-op."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from importlib import import_module
from types import SimpleNamespace
from typing import Any

import pytest
from django.apps import apps as global_apps
from django.core.management import call_command
from django.db import connection
from django.test import override_settings
from rest_framework.test import APIClient

from glowderma.accounts.tests.factories import CustomerFactory
from glowderma.core import timeutils
from glowderma.core.models import AuditEntry
from glowderma.core.timeutils import TASHKENT
from glowderma.loyalty import engine, selectors, services
from glowderma.loyalty.models import CashbackPolicy, CashbackTier
from glowderma.loyalty.tests.support import seed_policies

MIGRATION = import_module("glowderma.loyalty.migrations.0006_bootstrap_cashback_policy")
EFFECTIVE_FROM = datetime(2026, 6, 1, tzinfo=TASHKENT)


def run_bootstrap() -> None:
    MIGRATION.bootstrap_policy(global_apps, SimpleNamespace(connection=connection))


def policy_rows() -> list[tuple[Any, ...]]:
    return list(
        CashbackPolicy.objects.order_by("version").values_list(
            "version", "effective_from", "basis", "earn_on_cashback_paid", "author", "created_at"
        )
    )


@pytest.mark.django_db
@override_settings(LOYALTY_BOOTSTRAP_POLICY=True)
def test_an_empty_table_gets_v1_with_the_l3_defaults(staff_client: APIClient) -> None:
    run_bootstrap()
    assert policy_rows() == [
        (1, EFFECTIVE_FROM, "before_purchase", False, None, datetime(2026, 5, 20, 10, 0, tzinfo=TASHKENT))
    ]
    assert list(
        CashbackTier.objects.order_by("from_amount").values_list("from_amount", "to_amount", "percent")
    ) == [
        (0, 20_000_000, Decimal("1.00")),
        (20_000_000, 30_000_000, Decimal("2.00")),
        (30_000_000, None, Decimal("3.00")),
    ]
    entry = AuditEntry.objects.get(action="cashback.policy_version_created")
    assert (entry.actor_type, entry.actor_id, entry.entity_type, entry.entity_id) == (
        "system",
        None,
        "cashback_policy",
        "1",
    )
    assert entry.reason == "migration loyalty.0006: first cashback policy with the L1–L3 defaults"
    assert entry.after == {
        "version": 1,
        "effective_from": "2026-06-01T00:00:00+05:00",
        "basis": "before_purchase",
        "earn_on_cashback_paid": False,
        "tiers": [
            {"from": 0, "to": 20_000_000, "percent": "1.00"},
            {"from": 20_000_000, "to": 30_000_000, "percent": "2.00"},
            {"from": 30_000_000, "to": None, "percent": "3.00"},
        ],
        "superseded_version": None,
    }
    # in force from 1 June 2026 on, for every lifetime total (the top band is open-ended)
    assert CashbackPolicy.objects.active_at(datetime(2026, 5, 31, 23, 59, tzinfo=TASHKENT)) is None
    rules = selectors.rules_of(selectors.policy_at(EFFECTIVE_FROM))
    assert [
        engine.rate_for(total, rules) for total in (0, 19_999_999, 20_000_000, 30_000_000, 900_000_000)
    ] == [
        Decimal("1.00"),
        Decimal("1.00"),
        Decimal("2.00"),
        Decimal("3.00"),
        Decimal("3.00"),
    ]
    assert not selectors.cashback_policy_missing()
    policy = staff_client.get("/api/v1/admin/cashback/policy/").json()
    assert policy["active"]["version"] == 1 and policy["active"]["author"] is None
    assert policy["active"]["effective_to"] is None and policy["scheduled"] is None
    assert policy["active"]["tiers"][-1]["to"] is None
    # a purchase now earns at once
    customer = CustomerFactory.create()
    result = services.record_store_purchase(
        customer.pk, billz_sale_id="S-1", billz_sale_ref="S-1", amount=500_000, sold_at=timeutils.now()
    )
    assert (result.policy_version, result.percent, result.cashback_amount) == (1, Decimal("1.00"), 5_000)
    # the next version is v2; v1 expires when it starts and is never superseded (created after 1 June)
    tiers = [engine.TierBand(0, None, Decimal("1.5"))]
    created = services.create_policy_version(
        selectors.next_effective_from(), "before_purchase", False, tiers, None
    )
    assert created.version == 2 and created.superseded_version is None


@pytest.mark.django_db
@override_settings(LOYALTY_BOOTSTRAP_POLICY=True)
def test_it_is_idempotent_and_never_touches_an_existing_policy() -> None:
    run_bootstrap()
    run_bootstrap()
    assert CashbackPolicy.objects.count() == 1 and CashbackTier.objects.count() == 3
    assert AuditEntry.objects.filter(action="cashback.policy_version_created").count() == 1


@pytest.mark.django_db
@override_settings(LOYALTY_BOOTSTRAP_POLICY=True)
def test_a_table_with_any_version_is_left_alone() -> None:
    seed_policies()
    before = policy_rows()
    run_bootstrap()
    assert policy_rows() == before
    assert not AuditEntry.objects.filter(action="cashback.policy_version_created").exists()


@pytest.mark.django_db
def test_the_test_settings_switch_it_off() -> None:
    run_bootstrap()
    assert not CashbackPolicy.objects.exists()
    assert selectors.cashback_policy_missing()


@pytest.mark.django_db(transaction=True)
def test_migration_round_trip_inserts_once() -> None:
    call_command("migrate", verbosity=0)
    assert not CashbackPolicy.objects.exists()  # the suite runs with the switch off
    call_command("migrate", "loyalty", "0005", verbosity=0)
    with override_settings(LOYALTY_BOOTSTRAP_POLICY=True):
        call_command("migrate", "loyalty", "0006", verbosity=0)
        assert list(CashbackPolicy.objects.values_list("version", flat=True)) == [1]
        call_command("migrate", "loyalty", "0005", verbosity=0)  # the reverse is a no-op: rows stay
        assert CashbackPolicy.objects.count() == 1
        call_command("migrate", "loyalty", "0006", verbosity=0)
    assert CashbackPolicy.objects.count() == 1 and CashbackTier.objects.count() == 3
    assert AuditEntry.objects.filter(action="cashback.policy_version_created").count() == 1
    call_command("migrate", verbosity=0)
