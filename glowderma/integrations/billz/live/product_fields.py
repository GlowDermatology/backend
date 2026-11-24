"""Reading a Billz product: the barcode, the shop's price and stock, and the volume."""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from typing import Any

BILLZ_ID = re.compile(r"[0-9]{6,14}")  # ASCII digits only, like the catalog's billz_id check


STOCK_LIMIT = 2_000_000_000


NAME_MAX = 255


VOLUME_MAX = 40


def text_of(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _shop_entry(entries: Any, shop_id: str) -> Mapping[str, Any] | None:
    if not isinstance(entries, list):
        return None
    for entry in entries:
        if isinstance(entry, Mapping) and text_of(entry.get("shop_id")).lower() == shop_id.lower():
            return entry
    return None


def in_shop(product: Mapping[str, Any], shop_id: str) -> bool:
    """The product carries a stock or a price entry for ``shop_id``."""
    return (
        _shop_entry(product.get("shop_measurement_values"), shop_id) is not None
        or _shop_entry(product.get("shop_prices"), shop_id) is not None
    )


def barcode_of(product: Mapping[str, Any]) -> str | None:
    barcode = text_of(product.get("barcode"))
    return barcode if BILLZ_ID.fullmatch(barcode) else None


def price_of(product: Mapping[str, Any], shop_id: str) -> int | None:
    """``shop_prices[shop].retail_price`` in so'm; ``None`` without an UZS price for the shop."""
    entry = _shop_entry(product.get("shop_prices"), shop_id)
    if entry is None:
        return None
    currency = text_of(entry.get("retail_currency")).upper()
    price = _number(entry.get("retail_price"))
    if currency not in ("", "UZS") or price is None or price < 0:
        return None
    return round(price)


def stock_of(product: Mapping[str, Any], shop_id: str) -> int:
    """``shop_measurement_values[shop].active_measurement_value`` (whole units, floored); 0 when absent."""
    entry = _shop_entry(product.get("shop_measurement_values"), shop_id)
    value = _number(entry.get("active_measurement_value")) if entry is not None else None
    if value is None:
        return 0
    return max(-STOCK_LIMIT, min(STOCK_LIMIT, math.floor(value)))


def volume_of(product: Mapping[str, Any], field_name: str) -> str | None:
    """The value of the attribute or custom field named ``field_name`` (case-insensitive), "" when the product
    lacks it; ``None`` when no field is configured (the volume is then not synced at all)."""
    wanted = field_name.strip().casefold()
    if not wanted:
        return None
    for attribute in product.get("product_attributes") or []:
        if isinstance(attribute, Mapping) and text_of(attribute.get("attribute_name")).casefold() == wanted:
            return text_of(attribute.get("attribute_value"))[:VOLUME_MAX]
    for custom in product.get("custom_fields") or []:
        if isinstance(custom, Mapping) and wanted in (
            text_of(custom.get("custom_field_name")).casefold(),
            text_of(custom.get("custom_field_system_name")).casefold(),
        ):
            return text_of(custom.get("custom_field_value"))[:VOLUME_MAX]
    return ""


__all__ = [
    "BILLZ_ID",
    "NAME_MAX",
    "STOCK_LIMIT",
    "VOLUME_MAX",
    "barcode_of",
    "in_shop",
    "price_of",
    "stock_of",
    "text_of",
    "volume_of",
]
