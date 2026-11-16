"""Courier pricing policy (plan decision 5): the checkout fee and the dispatch decision, integer so'm only."""

from decimal import Decimal

import pytest

from glowderma.delivery.pricing import Decision, customer_fee, dispatch_decision, percent_bp

D = Decimal


@pytest.mark.parametrize(
    ("price", "markup", "vat", "fee"),
    [
        (0, "10", "0", 0),
        (20_000, "10", "0", 22_000),  # exact: no rounding
        (20_001, "10", "0", 22_500),  # 22 001.1 → up to the next 500
        (19_999, "10", "0", 22_000),  # 21 998.9 → 22 000
        (20_000, "0", "0", 20_000),
        (20_100, "0", "0", 20_500),
        (20_000, "10", "12", 25_000),  # 20 000 × 1.12 × 1.10 = 24 640 → 25 000
        (20_000, "12.50", "0", 22_500),
        (1, "0", "0", 500),
        (17_850, "10.25", "12", 22_500),  # 22 041.1… → 22 500
    ],
)
def test_customer_fee_rounds_up_to_500(price: int, markup: str, vat: str, fee: int) -> None:
    assert customer_fee(price, markup_percent=D(markup), vat_percent=D(vat)) == fee


def test_customer_fee_step_and_validation() -> None:
    assert customer_fee(20_001, markup_percent=D("0"), vat_percent=D("0"), step=1) == 20_001
    assert customer_fee(20_001, markup_percent=D("0"), vat_percent=D("0"), step=1000) == 21_000
    for bad in (-1, True, 1.5):
        with pytest.raises(ValueError):
            customer_fee(bad, markup_percent=D("10"), vat_percent=D("0"))  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        customer_fee(1, markup_percent=D("10"), vat_percent=D("0"), step=0)
    for percent in (D("-1"), D("100.01"), D("10.005"), D("NaN")):
        with pytest.raises(ValueError):
            customer_fee(1, markup_percent=percent, vat_percent=D("0"))
    with pytest.raises(ValueError):
        percent_bp(10)  # type: ignore[arg-type]
    assert percent_bp(D("12.34")) == 1234 and percent_bp(D("0")) == 0 and percent_bp(D("100")) == 10_000


def decide(dispatch_price: int, *, approved: int | None = None) -> Decision:
    return dispatch_decision(
        quoted_carrier_price=20_000,
        dispatch_price=dispatch_price,
        paid_fee=22_000,
        max_auto_increase=10_000,
        max_loss_per_shipment=5_000,
        approved_price=approved,
    )


def test_dispatch_decision_boundaries() -> None:
    assert decide(18_000) == Decision("accept", 0, 0, "within_limits")  # cheaper than the estimate
    assert decide(22_000) == Decision("accept", 2_000, 0, "within_limits")
    assert decide(27_000) == Decision("accept", 7_000, 5_000, "within_limits")  # loss exactly at the limit
    assert decide(27_001) == Decision("review", 7_001, 5_001, "loss_over_limit")
    wide = dispatch_decision(
        quoted_carrier_price=20_000,
        dispatch_price=30_000,
        paid_fee=22_000,
        max_auto_increase=10_000,
        max_loss_per_shipment=50_000,
    )
    assert wide == Decision("accept", 10_000, 8_000, "within_limits")  # increase exactly at the limit
    over = dispatch_decision(
        quoted_carrier_price=20_000,
        dispatch_price=30_001,
        paid_fee=22_000,
        max_auto_increase=10_000,
        max_loss_per_shipment=50_000,
    )
    assert over.action == "review" and over.reason == "increase_over_limit"


def test_an_approved_price_replaces_the_limits() -> None:
    assert decide(40_000, approved=40_000) == Decision("accept", 20_000, 18_000, "approved")
    assert decide(40_001, approved=40_000) == Decision("review", 20_001, 18_001, "above_approved")
    assert decide(18_000, approved=0).action == "review"


def test_zero_limits_accept_only_no_increase_and_no_loss() -> None:
    strict = dict(quoted_carrier_price=20_000, paid_fee=22_000, max_auto_increase=0, max_loss_per_shipment=0)
    assert dispatch_decision(dispatch_price=20_000, **strict).action == "accept"
    assert dispatch_decision(dispatch_price=20_001, **strict).reason == "increase_over_limit"
    with pytest.raises(ValueError):
        dispatch_decision(dispatch_price=-1, **strict)
