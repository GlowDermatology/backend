"""loyalty.engine (design 02 §1.9.1): the vector table, the basis × earn_on_cashback_paid matrix, rounding
down, promo exclusion and the tier bounds; the first four vectors are the frontend's ``cashback.test.ts``
samples."""

from decimal import Decimal
from types import SimpleNamespace

import pytest

from glowderma.core import enums
from glowderma.loyalty import engine
from glowderma.loyalty.engine import CreditResult, PolicyRules, TierBand

SEED_TIERS = (
    TierBand(0, 20_000_000, Decimal("1.00")),
    TierBand(20_000_000, 30_000_000, Decimal("2.00")),
    TierBand(30_000_000, 50_000_000, Decimal("3.00")),
)


def rules(
    *,
    basis: str = enums.RateBasis.BEFORE_PURCHASE,
    earn: bool = False,
    tiers: tuple[TierBand, ...] = SEED_TIERS,
    version: int | None = 3,
) -> PolicyRules:
    return PolicyRules(basis=basis, earn_on_cashback_paid=earn, tiers=tiers, version=version)


def single(percent: str) -> PolicyRules:
    return rules(tiers=(TierBand(0, None, Decimal(percent)),))


V3 = rules(earn=True)  # seed v3 (G-2)
SAMPLE = {
    "amount": 1_000_000,
    "promo_amount": 200_000,
    "cashback_paid": 100_000,
    "lifetime_before": 19_500_000,
}

VECTORS: list[tuple[str, dict[str, int], PolicyRules | None, int, int, str | None, int]] = [
    # name, inputs, policy, eligible, lifetime basis, percent, credit
    ("FE sample", SAMPLE, rules(), 700_000, 19_500_000, "1.00", 7_000),
    ("FE sample with_purchase", SAMPLE, rules(basis="with_purchase"), 700_000, 20_500_000, "2.00", 14_000),
    ("FE sample earn_on_cashback_paid", SAMPLE, rules(earn=True), 800_000, 19_500_000, "1.00", 8_000),
    (
        "floor",
        dict(amount=999, promo_amount=0, cashback_paid=0, lifetime_before=0),
        rules(),
        999,
        0,
        "1.00",
        9,
    ),
    (
        "0.57 %",
        dict(amount=231_000, promo_amount=0, cashback_paid=0, lifetime_before=0),
        single("0.57"),
        231_000,
        0,
        "0.57",
        1_316,
    ),  # fmt: skip  # noqa: E501
    (
        "0.57 % b",
        dict(amount=700_000, promo_amount=0, cashback_paid=0, lifetime_before=0),
        single("0.57"),
        700_000,
        0,
        "0.57",
        3_990,
    ),  # fmt: skip  # noqa: E501
    (
        "2.3 %",
        dict(amount=568_000, promo_amount=0, cashback_paid=0, lifetime_before=0),
        single("2.3"),
        568_000,
        0,
        "2.30",
        13_064,
    ),  # fmt: skip  # noqa: E501
    (
        "2.3 % floor",
        dict(amount=999, promo_amount=0, cashback_paid=0, lifetime_before=0),
        single("2.3"),
        999,
        0,
        "2.30",
        22,
    ),  # fmt: skip  # noqa: E501
    (
        "1.5 %",
        dict(amount=700_000, promo_amount=0, cashback_paid=0, lifetime_before=0),
        single("1.5"),
        700_000,
        0,
        "1.50",
        10_500,
    ),  # fmt: skip  # noqa: E501
    (
        "1.5 % floor",
        dict(amount=999, promo_amount=0, cashback_paid=0, lifetime_before=0),
        single("1.5"),
        999,
        0,
        "1.50",
        14,
    ),  # fmt: skip  # noqa: E501
    (
        "no covering tier",
        dict(amount=1_000_000, promo_amount=0, cashback_paid=0, lifetime_before=50_000_000),
        rules(),
        1_000_000,
        50_000_000,
        None,
        0,
    ),  # fmt: skip  # noqa: E501
    (
        "order №10248 (seed, v3 earn=true)",
        dict(amount=808_000, promo_amount=240_000, cashback_paid=48_600, lifetime_before=12_772_000),
        V3,
        568_000,
        12_772_000,
        "1.00",
        5_680,
    ),  # fmt: skip  # noqa: E501
    (
        "order №10248 if earn=false",
        dict(amount=808_000, promo_amount=240_000, cashback_paid=48_600, lifetime_before=12_772_000),
        rules(),
        519_400,
        12_772_000,
        "1.00",
        5_194,
    ),  # fmt: skip  # noqa: E501
    (
        "Malika S-88213",
        dict(amount=231_000, promo_amount=0, cashback_paid=0, lifetime_before=13_349_000),
        V3,
        231_000,
        13_349_000,
        "1.00",
        2_310,
    ),  # fmt: skip  # noqa: E501
    (
        "Malika №10231",
        dict(amount=554_000, promo_amount=240_000, cashback_paid=20_000, lifetime_before=12_795_000),
        V3,
        314_000,
        12_795_000,
        "1.00",
        3_140,
    ),  # fmt: skip  # noqa: E501
    (
        "seed store sale 361 000",
        dict(amount=361_000, promo_amount=0, cashback_paid=0, lifetime_before=5_967_000),
        rules(),
        361_000,
        5_967_000,
        "1.00",
        3_610,
    ),  # fmt: skip  # noqa: E501
    (
        "seed store sale 1 245 000",
        dict(amount=1_245_000, promo_amount=0, cashback_paid=0, lifetime_before=0),
        rules(),
        1_245_000,
        0,
        "1.00",
        12_450,
    ),  # fmt: skip  # noqa: E501
    ("no policy", SAMPLE, None, 700_000, 19_500_000, None, 0),
    (
        "eligible floors at zero",
        dict(amount=100, promo_amount=80, cashback_paid=50, lifetime_before=0),
        rules(),
        0,
        0,
        "1.00",
        0,
    ),  # fmt: skip  # noqa: E501
]


@pytest.mark.parametrize(
    ("inputs", "policy", "eligible", "basis", "percent", "credit"),
    [row[1:] for row in VECTORS],
    ids=[row[0] for row in VECTORS],
)
def test_credit_vectors(
    inputs: dict[str, int],
    policy: PolicyRules | None,
    eligible: int,
    basis: int,
    percent: str | None,
    credit: int,
) -> None:
    result = engine.credit_for_purchase(policy=policy, **inputs)
    assert result == CreditResult(
        eligible=eligible,
        lifetime_basis=basis,
        percent=None if percent is None else Decimal(percent),
        credit=credit,
        promo_excluded=inputs["promo_amount"] > 0,
    )
    assert isinstance(result.credit, int)
    if result.percent is not None:
        assert result.percent.as_tuple().exponent == -2  # always 2 dp


@pytest.mark.parametrize(
    ("lifetime", "expected"),
    [(0, "1.00"), (19_999_999, "1.00"), (20_000_000, "2.00"), (49_999_999, "3.00"), (50_000_000, None)],
)
def test_rate_for_bounds(lifetime: int, expected: str | None) -> None:
    assert engine.rate_for(lifetime, rules()) == (None if expected is None else Decimal(expected))


def test_rate_for_without_policy_and_open_ended_tier() -> None:
    assert engine.rate_for(1, None) is None
    assert engine.rate_for(10**12, single("0.5")) == Decimal("0.50")
    assert engine.tier_for_lifetime(None, 0) is None
    assert engine.tier_index(SEED_TIERS, 25_000_000) == 1


@pytest.mark.parametrize("basis", list(enums.RateBasis.values))
@pytest.mark.parametrize("earn", [False, True])
def test_basis_and_earn_matrix(basis: str, earn: bool) -> None:
    policy = rules(basis=basis, earn=earn)
    result = engine.credit_for_purchase(policy=policy, **SAMPLE)
    expected_eligible = 800_000 if earn else 700_000
    expected_percent = Decimal("2.00") if basis == enums.RateBasis.WITH_PURCHASE else Decimal("1.00")
    assert (result.eligible, result.percent) == (expected_eligible, expected_percent)
    assert result.credit == expected_eligible * int(expected_percent * 100) // 10_000
    assert result.promo_excluded is True


def test_promo_exclusion_and_flag() -> None:
    with_promo = engine.credit_for_purchase(
        amount=500_000, promo_amount=200_000, cashback_paid=0, lifetime_before=0, policy=rules()
    )
    without = engine.credit_for_purchase(
        amount=300_000, promo_amount=0, cashback_paid=0, lifetime_before=0, policy=rules()
    )
    assert (with_promo.eligible, with_promo.credit, with_promo.promo_excluded) == (300_000, 3_000, True)
    assert (without.eligible, without.credit, without.promo_excluded) == (300_000, 3_000, False)
    # promo is excluded even when the policy earns on the cashback-paid part
    assert engine.eligible_base(500_000, 200_000, 100_000, rules(earn=True)) == 300_000
    assert engine.eligible_base(500_000, 200_000, 100_000, rules(earn=False)) == 200_000
    assert engine.eligible_base(500_000, 200_000, 100_000, None) == 200_000


def test_rounding_is_floor_never_bankers() -> None:
    # 0.57 % of 231 000 = 1 316.7 → 1 316; 2.3 % of 999 = 22.977 → 22; 1 % of 150 = 1.5 → 1
    assert engine.credit_for_purchase(amount=150, promo_amount=0, cashback_paid=0, lifetime_before=0, policy=rules()).credit == 1  # fmt: skip  # noqa: E501
    assert engine.credit_for_purchase(amount=250, promo_amount=0, cashback_paid=0, lifetime_before=0, policy=rules()).credit == 2  # fmt: skip  # noqa: E501


def test_lifetime_basis() -> None:
    assert engine.lifetime_basis(rules(), 19_500_000, 1_000_000) == 19_500_000
    assert engine.lifetime_basis(rules(basis="with_purchase"), 19_500_000, 1_000_000) == 20_500_000
    assert engine.lifetime_basis(None, 19_500_000, 1_000_000) == 19_500_000


def test_invalid_inputs() -> None:
    with pytest.raises(ValueError, match="non-negative"):
        engine.eligible_base(-1, 0, 0, rules())
    with pytest.raises(ValueError, match="basis"):
        PolicyRules(basis="after_purchase", earn_on_cashback_paid=False, tiers=())


def test_policy_rules_sort_tiers_and_rules_for_snapshots_rows() -> None:
    unsorted = (SEED_TIERS[2], SEED_TIERS[0], SEED_TIERS[1])
    assert rules(tiers=unsorted).tiers == SEED_TIERS
    policy = SimpleNamespace(version=7, basis="with_purchase", earn_on_cashback_paid=True)
    tiers = [
        SimpleNamespace(from_amount=20_000_000, to_amount=None, percent=Decimal("2.00")),
        SimpleNamespace(from_amount=0, to_amount=20_000_000, percent=Decimal("1.00")),
    ]
    snapshot = engine.rules_for(policy, tiers)
    assert snapshot == PolicyRules(
        basis="with_purchase",
        earn_on_cashback_paid=True,
        tiers=(TierBand(0, 20_000_000, Decimal("1.00")), TierBand(20_000_000, None, Decimal("2.00"))),
        version=7,
    )
    assert snapshot.tiers[1].covers(10**12) and not snapshot.tiers[0].covers(20_000_000)
