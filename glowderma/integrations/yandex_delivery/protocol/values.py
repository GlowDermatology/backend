"""The values the Yandex Delivery protocol reads and writes: money in so'm, currencies, coordinates, phones,
claim ids, and the tolerant readers of loose JSON."""

from __future__ import annotations

import math
import re
from collections.abc import Iterator
from datetime import datetime
from decimal import ROUND_CEILING, Decimal, InvalidOperation
from typing import Any

from glowderma.integrations.yandex_delivery.ports import (
    BULK_INFO_MAX,
    Point,
)
from glowderma.integrations.yandex_delivery.protocol.endpoints import MAX_PRICE, TEXT_MAX

_MONEY = re.compile(r"-?[0-9]{1,14}(?:\.[0-9]{0,4})?")


_CLAIM_ID = re.compile(r"[A-Za-z0-9_.-]{1,64}")


CODE_PATTERN = re.compile(r"[A-Za-z0-9_.:-]{1,64}")


CURRENCY_PATTERN = re.compile(r"[A-Z]{3}")


_PHONE_NOISE = re.compile(r"[\s()-]")


_UZ_PHONE = re.compile(r"\+998[0-9]{9}")


EMAIL_PATTERN = re.compile(r"\S+@\S+\.\S+")


class ShapeError(ValueError):
    """An answer the adapter cannot read (mapped to ``YandexUnavailable``)."""


class CurrencyMismatch(ShapeError):
    """A price in another currency than ``YANDEX_DELIVERY_CURRENCY``, or one without a currency."""


def so_m(value: object) -> int:
    """A Yandex amount (``Decimal(18, 4)`` string, or a JSON number) as integer so'm rounded up; a negative,
    malformed, non-finite or absurd value is a :class:`ShapeError`."""
    if isinstance(value, bool) or not isinstance(value, int | float | str):
        raise ShapeError("amount is not a number")
    if isinstance(value, float) and not math.isfinite(value):
        raise ShapeError("amount is not finite")
    text = value.strip() if isinstance(value, str) else repr(value)
    if isinstance(value, str) and not _MONEY.fullmatch(text):
        raise ShapeError("amount is not a Decimal(18, 4) string")
    try:
        amount = Decimal(text)
    except InvalidOperation as exc:
        raise ShapeError("amount is not a number") from exc
    if not amount.is_finite() or amount < 0 or amount > MAX_PRICE:
        raise ShapeError("amount out of range")
    return int(amount.to_integral_value(rounding=ROUND_CEILING))


def optional_so_m(value: object) -> int | None:
    return None if value is None or value == "" else so_m(value)


def check_currency(currency: object, expected: str) -> str:
    """``currency`` when it is exactly ``expected``; anything else (absent included) is a
    :class:`CurrencyMismatch`."""
    if currency != expected:
        raise CurrencyMismatch(f"currency {currency!r} is not {expected}")
    return expected


def coordinates(point: Point) -> list[float]:
    """``[lon, lat]`` rounded to six decimals; out-of-range or non-finite coordinates are a ``ValueError``
    (our data, never sent)."""
    lon, lat = float(point.lon), float(point.lat)
    if not (math.isfinite(lon) and math.isfinite(lat) and -180 <= lon <= 180 and -90 <= lat <= 90):
        raise ValueError("coordinates out of range")
    return [round(lon, 6), round(lat, 6)]


def normalise_phone(phone: str) -> str:
    """``+998XXXXXXXXX`` from a phone with spaces, dashes or brackets (a bare ``998…`` gains its ``+``);
    any other number is a ``ValueError`` (Yandex would answer ``country_phone_code_not_supported``)."""
    digits = _PHONE_NOISE.sub("", phone or "")
    if digits.startswith("998"):
        digits = "+" + digits
    if not _UZ_PHONE.fullmatch(digits):
        raise ValueError("phone is not +998XXXXXXXXX")
    return digits


def valid_claim_id(claim_id: str) -> bool:
    return isinstance(claim_id, str) and bool(_CLAIM_ID.fullmatch(claim_id))


def chunks(ids: list[str], size: int = BULK_INFO_MAX) -> Iterator[list[str]]:
    for start in range(0, len(ids), size):
        yield ids[start : start + size]


def text_value(value: object, limit: int = TEXT_MAX) -> str:
    return " ".join(value.split())[:limit] if isinstance(value, str) else ""


def number_value(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value) if math.isfinite(value) else None


def aware_value(value: object) -> datetime | None:
    """An ISO 8601 timestamp with an offset; a naive or unreadable one reads as unknown (``None``)."""
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def dict_value(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def list_value(value: object) -> list[Any]:
    return value if isinstance(value, list) else []


def code_values(entries: object) -> tuple[str, ...]:
    return tuple(
        entry["code"]
        for entry in list_value(entries)
        if isinstance(entry, dict)
        and isinstance(entry.get("code"), str)
        and CODE_PATTERN.fullmatch(entry["code"])
    )


__all__ = [
    "CODE_PATTERN",
    "CURRENCY_PATTERN",
    "EMAIL_PATTERN",
    "CurrencyMismatch",
    "ShapeError",
    "aware_value",
    "check_currency",
    "chunks",
    "code_values",
    "coordinates",
    "dict_value",
    "list_value",
    "normalise_phone",
    "number_value",
    "optional_so_m",
    "so_m",
    "text_value",
    "valid_claim_id",
]
