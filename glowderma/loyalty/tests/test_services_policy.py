"""loyalty.services.create_policy_version and the policy selectors — contract §2.7 validation rows (code AND
message), warnings, supersession, state derivation over time, ``next_effective_from`` at month edges."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from unittest import mock

import pytest
from django.db import IntegrityError

from glowderma.accounts.tests.factories import StaffUserFactory
from glowderma.core import enums
from glowderma.core.models import AuditEntry
from glowderma.core.seeding import pinned_clock
from glowderma.core.timeutils import TASHKENT
from glowderma.loyalty import selectors, services
from glowderma.loyalty.engine import TierBand
from glowderma.loyalty.models import CashbackPolicy, CashbackTier
from glowderma.loyalty.tests.support import at, seed_policies

pytestmark = pytest.mark.django_db

NOW = at(10, 24, 12, 5)
NOV = at(11, 1)


def band(from_amount: int, to_amount: int | None, percent: str) -> TierBand:
    return TierBand(from_amount, to_amount, Decimal(percent))


SEED = [
    band(0, 20_000_000, "1.00"),
    band(20_000_000, 30_000_000, "2.00"),
    band(30_000_000, 50_000_000, "3.00"),
]


def codes(exc: services.LoyaltyValidationError) -> dict[str, list[tuple[str, str]]]:
    return {field: [(str(e.code), str(e)) for e in details] for field, details in exc.errors.items()}


def create(
    tiers: list[TierBand], effective_from: datetime = NOV, **overrides: Any
) -> services.PolicyVersionResult:
    kwargs: dict[str, Any] = dict(basis="before_purchase", earn_on_cashback_paid=False, author_id=None)
    kwargs.update(overrides)
    with pinned_clock(NOW):
        return services.create_policy_version(effective_from, tiers=tiers, **kwargs)


@pytest.mark.parametrize(
    ("tiers", "effective_from", "expected", "message"),
    [
        ([], NOV, {"tiers": [("min_items", "Kamida bitta daraja kerak.")]}, "Kamida bitta daraja kerak."),
        (
            [band(0, 0, "1")],
            NOV,
            {"tiers.0.to": [("empty_range", "Yuqori chegara quyi chegaradan katta bo‘lsin.")]},
            "Darajalar ustma-ust tushmoqda.",
        ),  # fmt: skip  # noqa: E501
        (
            [band(10, 20, "1"), band(5, 5, "1")],
            NOV,
            {"tiers.1.to": [("empty_range", "Yuqori chegara quyi chegaradan katta bo‘lsin.")]},
            "Darajalar ustma-ust tushmoqda.",
        ),  # fmt: skip  # noqa: E501
        # overlap: ``i`` is the submitted index of the later band in ``from`` order, even when submitted first
        (
            [band(15_000_000, None, "2"), band(0, 20_000_000, "1")],
            NOV,
            {"tiers.0.from": [("overlap", "Darajalar ustma-ust tushmoqda.")]},
            "Darajalar ustma-ust tushmoqda.",
        ),  # fmt: skip  # noqa: E501
        (
            [band(0, None, "1"), band(20_000_000, None, "2")],
            NOV,
            {"tiers.1.from": [("overlap", "Darajalar ustma-ust tushmoqda.")]},
            "Darajalar ustma-ust tushmoqda.",
        ),  # fmt: skip  # noqa: E501
        (
            [band(0, 10, "0")],
            NOV,
            {"tiers.0.percent": [("out_of_range", "Foiz 0 dan katta va 100 dan oshmasligi kerak.")]},
            "Foiz 0 dan katta va 100 dan oshmasligi kerak.",
        ),  # fmt: skip  # noqa: E501
        (
            [band(0, 10, "100.01")],
            NOV,
            {"tiers.0.percent": [("out_of_range", "Foiz 0 dan katta va 100 dan oshmasligi kerak.")]},
            "Foiz 0 dan katta va 100 dan oshmasligi kerak.",
        ),  # fmt: skip  # noqa: E501
        (
            [band(0, 10, "-1")],
            NOV,
            {"tiers.0.percent": [("out_of_range", "Foiz 0 dan katta va 100 dan oshmasligi kerak.")]},
            "Foiz 0 dan katta va 100 dan oshmasligi kerak.",
        ),  # fmt: skip  # noqa: E501
        (
            [band(0, 10, "NaN")],
            NOV,
            {"tiers.0.percent": [("out_of_range", "Foiz 0 dan katta va 100 dan oshmasligi kerak.")]},
            "Foiz 0 dan katta va 100 dan oshmasligi kerak.",
        ),  # fmt: skip  # noqa: E501
        (
            [band(0, 10, "1.005")],
            NOV,
            {"tiers.0.percent": [("max_decimal_places", "Ko‘pi bilan 2 ta kasr raqam.")]},
            "Maydonlarni tekshiring.",
        ),  # fmt: skip  # noqa: E501
        (
            [band(0, 10, "1")],
            NOW,
            {"effective_from": [("effective_from_in_past", "Kelajakdagi sanani tanlang.")]},
            "Maydonlarni tekshiring.",
        ),  # fmt: skip  # noqa: E501
        (
            [band(0, 10, "1")],
            NOW - timedelta(days=1),
            {"effective_from": [("effective_from_in_past", "Kelajakdagi sanani tanlang.")]},
            "Maydonlarni tekshiring.",
        ),  # fmt: skip  # noqa: E501
        # several rules at once: every field error, the first failing rule's top-level message
        (
            [band(5, 5, "0.001"), band(0, 10, "1")],
            NOW,
            {
                "tiers.0.to": [("empty_range", "Yuqori chegara quyi chegaradan katta bo‘lsin.")],
                "tiers.0.from": [("overlap", "Darajalar ustma-ust tushmoqda.")],
                "tiers.0.percent": [("max_decimal_places", "Ko‘pi bilan 2 ta kasr raqam.")],
                "effective_from": [("effective_from_in_past", "Kelajakdagi sanani tanlang.")],
            },
            "Darajalar ustma-ust tushmoqda.",
        ),  # fmt: skip  # noqa: E501
        (
            [band(0, 10, "250.5"), band(0, 10, "1.123")],
            NOW,
            {
                "tiers.1.from": [("overlap", "Darajalar ustma-ust tushmoqda.")],
                "tiers.0.percent": [("out_of_range", "Foiz 0 dan katta va 100 dan oshmasligi kerak.")],
                "tiers.1.percent": [("max_decimal_places", "Ko‘pi bilan 2 ta kasr raqam.")],
                "effective_from": [("effective_from_in_past", "Kelajakdagi sanani tanlang.")],
            },
            "Darajalar ustma-ust tushmoqda.",
        ),  # fmt: skip  # noqa: E501
    ],
)
def test_validation_rows(tiers: list[TierBand], effective_from: datetime, expected: dict[str, Any], message: str) -> None:  # fmt: skip  # noqa: E501
    with pytest.raises(services.PolicyValidationError) as info:
        create(tiers, effective_from)
    assert codes(info.value) == expected
    assert info.value.message == message
    assert not CashbackPolicy.objects.exists() and not AuditEntry.objects.exists()


def test_warnings_do_not_block() -> None:
    assert services.tier_warnings(SEED) == (services.TierWarning("uncovered_above", 50_000_000, None),)
    assert services.tier_warnings([band(0, None, "1")]) == ()
    gaps = services.tier_warnings([band(3_000, None, "2"), band(1_000, 2_000, "1")])
    assert gaps == (services.TierWarning("gap", 0, 1_000), services.TierWarning("gap", 2_000, 3_000))
    both = create([band(10, 20, "1"), band(50, 60, "0.57"), band(20, 50, "2")])
    assert both.warnings == (
        services.TierWarning("gap", 0, 10),
        services.TierWarning("uncovered_above", 60, None),
    )
    assert both.version == 1 and both.superseded_version is None
    # a touching pair (to == next from) is neither a gap nor an overlap; percent 100 is the top of the range
    clean = create([band(0, 10, "100"), band(10, None, "0.01")])
    assert clean.warnings == () and clean.version == 2


def test_create_policy_version_inserts_sorted_tiers_and_audits() -> None:
    staff = StaffUserFactory.create(full_name="Administrator")
    result = create([SEED[2], SEED[0], SEED[1]], basis="with_purchase", earn_on_cashback_paid=True, author_id=staff.pk)  # fmt: skip  # noqa: E501
    policy = CashbackPolicy.objects.with_derived().get(pk=result.policy_id)
    assert (policy.version, policy.created_at, policy.effective_from, policy.basis, policy.earn_on_cashback_paid, policy.author_id) == (1, NOW, NOV, "with_purchase", True, staff.pk)  # fmt: skip  # noqa: E501
    assert (policy.superseded_by, policy.effective_to, policy.state_at(NOW), policy.state_at(NOV)) == (None, None, "scheduled", "active")  # fmt: skip  # noqa: E501
    tiers = list(CashbackTier.objects.filter(policy=policy).order_by("id"))
    assert [(t.from_amount, t.to_amount, t.percent, t.created_at) for t in tiers] == [
        (0, 20_000_000, Decimal("1.00"), NOW),
        (20_000_000, 30_000_000, Decimal("2.00"), NOW),
        (30_000_000, 50_000_000, Decimal("3.00"), NOW),
    ]
    audit = AuditEntry.objects.get(action="cashback.policy_version_created")
    assert (audit.actor_type, audit.actor_id, audit.entity_type, audit.entity_id) == ("staff", staff.pk, "cashback_policy", "1")  # fmt: skip  # noqa: E501
    assert audit.after == {
        "version": 1,
        "effective_from": "2026-11-01T00:00:00+05:00",
        "basis": "with_purchase",
        "earn_on_cashback_paid": True,
        "tiers": [
            {"from": 0, "to": 20_000_000, "percent": "1.00"},
            {"from": 20_000_000, "to": 30_000_000, "percent": "2.00"},
            {"from": 30_000_000, "to": 50_000_000, "percent": "3.00"},
        ],
        "superseded_version": None,
    }
    assert not AuditEntry.objects.filter(action="cashback.policy_version_superseded").exists()
    with pytest.raises(ValueError, match="basis"):
        create(SEED, basis="after_purchase")
    # the percent is stored at 2 dp whatever the submitted exponent
    assert create([band(0, None, "1.5")]).version == 2
    assert CashbackTier.objects.get(policy__version=2).percent == Decimal("1.50")


def test_supersession_of_the_scheduled_version() -> None:
    staff = StaffUserFactory.create()
    seed_policies()  # v1, v2 expired, v3 active at NOW
    first = create(SEED + [band(50_000_000, None, "4")], effective_from=at(12, 1), author_id=staff.pk)
    assert (first.version, first.superseded_version) == (4, None)
    scheduled = selectors.scheduled_policy(NOW)
    assert scheduled is not None and scheduled.version == 4
    second = create([band(0, None, "1.5")], effective_from=at(11, 15), author_id=staff.pk)
    assert (second.version, second.superseded_version) == (5, 4)
    rows = {p.version: p for p in selectors.policy_versions()}
    assert [p.version for p in selectors.policy_versions()] == [5, 4, 3, 2, 1]
    assert (rows[4].superseded_by, rows[4].state_at(NOW), rows[4].state_at(at(12, 2))) == (5, "superseded", "superseded")  # fmt: skip  # noqa: E501
    assert (rows[3].effective_to, rows[3].state_at(NOW), rows[3].state_at(at(11, 15))) == (at(11, 15), "active", "expired")  # fmt: skip  # noqa: E501
    assert (rows[5].superseded_by, rows[5].effective_to, rows[5].state_at(NOW)) == (None, None, "scheduled")
    superseded = AuditEntry.objects.get(action="cashback.policy_version_superseded")
    assert (superseded.entity_type, superseded.entity_id, superseded.actor_id) == (
        "cashback_policy",
        "4",
        staff.pk,
    )
    assert superseded.before == {"state": "scheduled"} and superseded.after == {"state": "superseded", "superseded_by": 5}  # fmt: skip  # noqa: E501
    created = AuditEntry.objects.filter(action="cashback.policy_version_created").order_by("id").last()
    assert created is not None and created.after is not None
    assert created.after["superseded_version"] == 4
    # the superseded version never activates: v3 stays in force until v5 starts, whatever v4 said
    started = selectors.policy_at(at(12, 1, 0, 1))
    assert started is not None and started.version == 5
    assert selectors.policy_at(at(11, 14, 23, 59)).version == 3  # type: ignore[union-attr]
    assert selectors.scheduled_policy(NOW).version == 5  # type: ignore[union-attr]
    # a third one supersedes v5 in turn (v4 keeps its original superseder: the smallest later version)
    third = create([band(0, None, "1.25")], effective_from=at(11, 20), author_id=staff.pk)
    assert third.superseded_version == 5
    rows = {p.version: p for p in selectors.policy_versions()}
    assert (rows[4].superseded_by, rows[5].superseded_by, rows[6].superseded_by) == (5, 6, None)
    assert AuditEntry.objects.filter(action="cashback.policy_version_superseded").count() == 2


def test_state_derivation_over_time_with_the_pinned_clock() -> None:
    seed_policies()
    checkpoints = {
        at(5, 31, 23, 59): (None, ["scheduled", "scheduled", "scheduled"]),
        at(6, 1): (1, ["active", "scheduled", "scheduled"]),
        at(8, 14, 23, 59): (1, ["active", "scheduled", "scheduled"]),
        at(8, 15): (2, ["expired", "active", "scheduled"]),
        at(9, 30, 23, 59): (2, ["expired", "active", "scheduled"]),
        at(10, 1): (3, ["expired", "expired", "active"]),
        NOW: (3, ["expired", "expired", "active"]),
    }
    for instant, (active_version, states) in checkpoints.items():
        with pinned_clock(instant):
            active = selectors.active_policy()
            assert (None if active is None else active.version) == active_version, instant
            assert [
                p.state for p in sorted(selectors.policy_versions(), key=lambda p: p.version)
            ] == states, instant
            assert selectors.scheduled_policy() == (None if active_version == 3 else CashbackPolicy.objects.get(version=(active_version or 0) + 1))  # fmt: skip  # noqa: E501
    assert selectors.policy_at(at(5, 31)) is None
    assert selectors.rules_of(None) is None
    rules = selectors.rules_of(selectors.policy_at(at(10, 2)))
    assert rules is not None and (rules.version, rules.earn_on_cashback_paid, len(rules.tiers)) == (
        3,
        True,
        3,
    )


@pytest.mark.parametrize(
    ("now", "expected"),
    [
        (at(10, 24, 12, 5), at(11, 1)),
        (at(12, 31, 23, 59), at(1, 1, year=2027)),
        (at(1, 1), at(2, 1)),
        (at(1, 31, 23, 59), at(2, 1)),
        (at(2, 28, 12), at(3, 1)),
        (at(11, 1), at(12, 1)),
        # a UTC instant that is already the next month in Tashkent: 2026-10-31 19:30Z = 2026-11-01 00:30+05
        (datetime(2026, 10, 31, 19, 30, tzinfo=UTC), at(12, 1)),
        (datetime(2026, 12, 31, 18, 59, tzinfo=UTC), at(1, 1, year=2027)),
    ],
)
def test_next_effective_from_month_edges(now: datetime, expected: datetime) -> None:
    result = selectors.next_effective_from(now)
    assert result == expected and result.tzinfo is TASHKENT and result.utcoffset() == timedelta(hours=5)
    with pinned_clock(now):
        assert services.next_effective_from() == expected


def test_clock_skew_integrity_error_maps_to_effective_from_in_past() -> None:
    error = IntegrityError('new row violates check constraint "policy_effective_after_created"')
    with mock.patch.object(CashbackPolicy.objects, "create", side_effect=error):
        with pytest.raises(services.PolicyValidationError) as info:
            create(SEED)
    assert codes(info.value) == {
        "effective_from": [("effective_from_in_past", "Kelajakdagi sanani tanlang.")]
    }
    other = IntegrityError(
        'duplicate key value violates unique constraint "loyalty_cashbackpolicy_version_key"'
    )
    with mock.patch.object(CashbackPolicy.objects, "create", side_effect=other):
        with pytest.raises(IntegrityError):
            create(SEED)
    assert not CashbackPolicy.objects.exists() and not AuditEntry.objects.exists()


def test_versions_only_grow_and_the_service_exports_the_policy_reads() -> None:
    seed_policies()
    assert create(SEED, effective_from=at(11, 1)).version == 4
    assert create(SEED, effective_from=at(11, 2)).version == 5
    assert services.active_policy(NOW) is not None and services.active_policy(NOW).version == 3  # type: ignore[union-attr]
    assert services.policy_at(at(9, 1)).version == 2  # type: ignore[union-attr]
    assert services.scheduled_policy(NOW).version == 5  # type: ignore[union-attr]
    assert enums.PolicyVersionState.SUPERSEDED == CashbackPolicy.objects.with_derived().get(
        version=4
    ).state_at(NOW)
