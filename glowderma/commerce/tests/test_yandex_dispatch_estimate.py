"""The checkout estimate a dispatch compares Yandex's offer with (plan §3.6 step 6): ``claims/info`` prices
include VAT, the ``check-price`` estimate may not (``DeliverySettings.estimate_vat_percent``), so the claim
stores the estimate with the same VAT uplift the customer fee got — never the markup or the 500 so'm
rounding — and an unchanged price is no "increase". Yandex is the fake adapter."""

# ruff: noqa: F811 - the fixtures come from yandex_dispatch_support and are requested by name

from __future__ import annotations

from decimal import Decimal

import pytest

from glowderma.accounts.models import StaffUser
from glowderma.commerce.tests.yandex_dispatch_support import (  # noqa: F401 - fixtures
    QUOTED_CARRIER_PRICE,
    dispatch,
    fresh_fake_yandex,
    no_pauses,
    open_warehouse,
    run,
    yandex_order,
)
from glowderma.core import enums
from glowderma.delivery.models import DeliveryIssue, DeliverySettings

pytestmark = pytest.mark.django_db

K = enums.DeliveryIssueKind
VAT_ESTIMATE = 22_400  # ceil(20 000 × 1.12)


@pytest.fixture(autouse=True)
def warehouse_with_vat() -> DeliverySettings:
    open_warehouse()
    DeliverySettings.objects.filter(pk=1).update(
        estimate_vat_percent=Decimal("12.00"), markup_percent=Decimal("5.00"), max_loss_per_shipment=100_000
    )
    return DeliverySettings.objects.get(pk=1)


def test_the_estimate_is_stored_on_the_offer_basis(staff: StaffUser) -> None:
    order = yandex_order()
    claim = dispatch(order, staff.pk)
    assert QUOTED_CARRIER_PRICE == 20_000
    assert claim.estimate_price == VAT_ESTIMATE  # with VAT, without the markup or the 500 so'm step


@pytest.mark.parametrize(
    ("offer", "accepted"),
    [
        (VAT_ESTIMATE, True),  # the checkout price unchanged, VAT included
        (VAT_ESTIMATE + 10_000, True),  # exactly the allowed increase
        (VAT_ESTIMATE + 10_001, False),
    ],
)
def test_an_unchanged_price_with_vat_is_no_increase(staff: StaffUser, offer: int, accepted: bool) -> None:
    order = yandex_order(fake={"offer_price": offer})
    claim = dispatch(order, staff.pk)
    run(claim)
    claim.refresh_from_db()
    assert (claim.accepted_at is not None) is accepted
    review = DeliveryIssue.objects.filter(order=order, kind=K.DISPATCH_REVIEW, state="open").first()
    if accepted:
        assert review is None
    else:
        assert review is not None and review.code == "increase_over_limit"
        assert review.details["increase"] == 10_001 and review.details["estimate_price"] == VAT_ESTIMATE
