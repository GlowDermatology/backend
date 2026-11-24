"""Reading the Yandex Delivery answers: errors, estimates, tariffs, claims, cancel terms and the sharing
link."""

from __future__ import annotations

import math
from typing import Any, cast
from urllib.parse import urlsplit

from glowderma.integrations.httpclient import int_code
from glowderma.integrations.yandex_delivery.ports import (
    CANCEL_FREE,
    CANCEL_PAID,
    CANCEL_UNAVAILABLE,
    CancelState,
    CancelTerms,
    Claim,
    Estimate,
    Tariff,
)
from glowderma.integrations.yandex_delivery.protocol.endpoints import ORDER_ID_MAX, TEXT_MAX
from glowderma.integrations.yandex_delivery.protocol.values import (
    CODE_PATTERN,
    CURRENCY_PATTERN,
    ShapeError,
    aware_value,
    check_currency,
    code_values,
    dict_value,
    list_value,
    number_value,
    optional_so_m,
    so_m,
    text_value,
    valid_claim_id,
)


def error_message(body: object) -> str:
    message = dict_value(body).get("message")
    return message if isinstance(message, str) else ""


def estimate_from(body: dict[str, Any], *, currency: str) -> Estimate:
    raw = body.get("price")
    price = so_m(raw)
    rules = dict_value(body.get("currency_rules"))
    check_currency(rules.get("code"), currency)
    eta = number_value(body.get("eta"))
    distance = number_value(body.get("distance_meters"))
    zone = body.get("zone_id")
    return Estimate(
        price=price,
        currency=currency,
        eta_minutes=math.ceil(eta) if eta is not None and eta >= 0 else None,
        distance_m=round(distance) if distance is not None and distance >= 0 else None,
        zone_id=zone if isinstance(zone, str) else "",
        raw_price=str(raw).strip(),
    )


def tariffs_from(body: dict[str, Any]) -> list[Tariff]:
    listed = body.get("available_tariffs")
    if not isinstance(listed, list):
        raise ShapeError("answer without available_tariffs")
    tariffs: list[Tariff] = []
    for entry in listed:
        name = dict_value(entry).get("name")
        if not isinstance(name, str) or not CODE_PATTERN.fullmatch(name):
            continue
        minimal = dict_value(entry).get("minimal_price")
        try:
            minimal_price = optional_so_m(minimal)
        except ShapeError:
            minimal_price = None
        tariffs.append(
            Tariff(name=name, minimal_price=minimal_price, title=text_value(dict_value(entry).get("title")))
        )
    return tariffs


def _vehicle(performer: dict[str, Any]) -> str:
    parts = (text_value(performer.get(key), 40) for key in ("car_model", "car_color", "car_number"))
    return " ".join(part for part in parts if part)[:TEXT_MAX]


def _destination(route_points: object) -> dict[str, Any]:
    return next(
        (
            point
            for point in list_value(route_points)
            if isinstance(point, dict) and point.get("type") == "destination"
        ),
        {},
    )


def _claim_taxi_class(body: dict[str, Any]) -> str | None:
    requested = dict_value(body.get("client_requirements")).get("taxi_class")
    if isinstance(requested, str) and requested:
        return requested[:32]
    matched = next((car for car in list_value(body.get("matched_cars")) if isinstance(car, dict)), {})
    found = matched.get("taxi_class")
    return found[:32] if isinstance(found, str) and found else None


def claim_identity(body: dict[str, Any]) -> tuple[str, str, int]:
    """``(id, status, version)`` — the three fields every claim answer carries (accept and cancel answers
    carry little else)."""
    claim_id, status, version = body.get("id"), body.get("status"), int_code(body.get("version"))
    if not isinstance(claim_id, str) or not valid_claim_id(claim_id):
        raise ShapeError("claim without a usable id")
    if not isinstance(status, str) or not status.strip():
        raise ShapeError("claim without a status")
    if version is None or version < 0:
        raise ShapeError("claim without a version")
    return claim_id, status.strip()[:64], version


def claim_from(body: dict[str, Any], *, currency: str) -> Claim:
    """A claim snapshot from ``claims/create`` / ``info`` / a ``bulk_info`` entry. A price in another
    currency (or a price without one) is a :class:`CurrencyMismatch`; an unknown status is kept as it is."""
    claim_id, status, version = claim_identity(body)
    pricing = dict_value(body.get("pricing"))
    offer = dict_value(pricing.get("offer"))
    offer_raw = offer.get("price_with_vat")
    if offer_raw is None or offer_raw == "":
        offer_raw = offer.get("price")
    offer_price = optional_so_m(offer_raw)
    final_price = optional_so_m(pricing.get("final_price"))
    found_currency = pricing.get("currency") or dict_value(pricing.get("currency_rules")).get("code")
    if offer_price is not None or final_price is not None:
        found_currency = check_currency(found_currency, currency)
    destination = _destination(body.get("route_points"))
    point_id = int_code(destination.get("id"))
    external = destination.get("external_order_id")
    performer = dict_value(body.get("performer_info"))
    return Claim(
        claim_id=claim_id,
        status=status,
        version=version,
        updated_ts=aware_value(body.get("updated_ts")),
        offer_price=offer_price,
        offer_valid_until=aware_value(offer.get("valid_until")),
        final_price=final_price,
        currency=found_currency
        if isinstance(found_currency, str) and CURRENCY_PATTERN.fullmatch(found_currency)
        else "",
        warnings=code_values(body.get("warnings")),
        error_codes=code_values(body.get("error_messages")),
        courier_name=text_value(performer.get("courier_name")),
        courier_vehicle=_vehicle(performer),
        destination_point_id=point_id if point_id is not None and point_id > 0 else None,
        return_reasons=tuple(
            reason[:64] for reason in list_value(destination.get("return_reasons")) if isinstance(reason, str)
        ),
        external_order_id=external if isinstance(external, str) and external else None,
        taxi_class=_claim_taxi_class(body),
        item_extra_ids=tuple(
            item["extra_id"][:ORDER_ID_MAX]
            for item in list_value(body.get("items"))
            if isinstance(item, dict) and isinstance(item.get("extra_id"), str) and item["extra_id"]
        ),
    )


def bulk_entries(body: dict[str, Any]) -> list[dict[str, Any]]:
    claims = body.get("claims")
    if not isinstance(claims, list):
        raise ShapeError("bulk_info answer without claims")
    return [entry for entry in claims if isinstance(entry, dict)]


def cancel_terms_from(body: dict[str, Any], *, currency: str) -> CancelTerms:
    """The terms of ``claims/cancel-info``. A fee in another currency is a :class:`CurrencyMismatch`; a fee
    without any currency is dropped (``price=None``), so the state alone still lets a cancellation proceed."""
    state = body.get("cancel_state")
    if state not in (CANCEL_FREE, CANCEL_PAID, CANCEL_UNAVAILABLE):
        raise ShapeError("cancel-info without a known cancel_state")
    raw = body.get("price_with_vat")
    if raw is None or raw == "":
        raw = body.get("price")
    price = optional_so_m(raw)
    found_currency = body.get("currency")
    if price is not None:
        if found_currency in (None, ""):
            price = None
        else:
            check_currency(found_currency, currency)
    return CancelTerms(state=cast(CancelState, state), price=price)


def sharing_link_from(body: dict[str, Any]) -> str | None:
    """The destination point's ``sharing_link`` when it is an https URL, else ``None``."""
    link = _destination(body.get("route_points")).get("sharing_link")
    if not isinstance(link, str) or len(link) > 2000:
        return None
    parts = urlsplit(link)
    return link if parts.scheme == "https" and parts.netloc else None


__all__ = [
    "bulk_entries",
    "cancel_terms_from",
    "claim_from",
    "claim_identity",
    "error_message",
    "estimate_from",
    "sharing_link_from",
    "tariffs_from",
]
