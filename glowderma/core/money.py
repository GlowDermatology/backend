from decimal import Decimal


def percent_to_bp(percent: Decimal) -> int:
    if not percent.is_finite() or not 0 < percent <= 100:
        raise ValueError("Percent must be finite and in (0, 100]")
    exponent = percent.as_tuple().exponent
    if not isinstance(exponent, int) or exponent < -2:
        raise ValueError("Percent allows at most two decimal places")
    return int(percent * 100)


def apply_bp(amount: int, bp: int) -> int:
    return amount * bp // 10_000
