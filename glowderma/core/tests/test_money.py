from decimal import Decimal

import pytest

from glowderma.core.money import apply_bp, percent_to_bp


def test_apply_bp_integer_math() -> None:
    assert apply_bp(10_000, percent_to_bp(Decimal("0.57"))) == 57
    assert percent_to_bp(Decimal("2.3")) == 230
    assert apply_bp(999, percent_to_bp(Decimal("1.5"))) == 14


@pytest.mark.parametrize("value", ["0.001", "1.000", "0", "-1", "100.01", "NaN", "Infinity"])
def test_percent_to_bp_rejects_three_decimals(value: str) -> None:
    with pytest.raises(ValueError):
        percent_to_bp(Decimal(value))
