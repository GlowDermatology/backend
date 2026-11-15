"""Yandex Delivery Express API protocol (``https://yandex.com/support/delivery-profile/en/api/express``,
revision r22073422, read 2026-11-15) — pure helpers, no database, no HTTP.

Every request goes to ``https://b2b.taxi.yandex.net`` with ``Authorization: Bearer <OAuth token>`` and the
required ``Accept-Language`` (``ru`` or ``en``). Ids travel in the query (``claim_id``, ``request_id``), the
rest in a JSON body. Error answers are ``{"code", "message"}``.

Endpoints (all ``POST`` under :data:`PREFIX` except ``tracking-links``, which is ``GET``):

* ``tariffs`` ``{start_point: [lon, lat], fullname}`` → ``available_tariffs[]``;
* ``check-price`` ``{route_points[{id, coordinates, fullname}], items[], requirements{taxi_class}}`` →
  ``price`` (Decimal(18, 4) string), ``currency_rules.code``, ``eta`` (minutes), ``distance_meters``,
  ``zone_id``; a non-binding estimate;
* ``claims/create?request_id=`` → the claim; ``claims/info?claim_id=``; ``claims/bulk_info {claim_ids}``
  (1–1000) → ``{claims: []}``;
* ``claims/accept?claim_id= {version}``; ``claims/cancel-info?claim_id=`` → ``cancel_state``, ``price``,
  ``price_with_vat``, ``currency``; ``claims/cancel?claim_id= {version, cancel_state}``;
  ``claims/return?claim_id= {point_id, comment}`` (200 without a body);
* ``GET claims/tracking-links?claim_id=`` → ``route_points[{type, sharing_link}]``.

Coordinates are ``[longitude, latitude]`` — exactly that order — and always sent. The item's destination
field is spelled ``droppof_point`` in the English ``claims/create`` schema (required there); the Russian
schema prefers ``dropoff_point`` and keeps ``droppof_point`` as deprecated, so only ``droppof_point`` is sent
(accepted by both; a question for the account manager) and either spelling is read. ``check-price`` items use
``dropoff_point`` in both schemas.

Money: Yandex sends decimal strings in the claim's ``pricing.currency``; we keep integer so'm rounded **up**
(``Decimal`` → ceiling, never float) and refuse any other currency than ``YANDEX_DELIVERY_CURRENCY``.
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterator
from datetime import datetime
from decimal import ROUND_CEILING, Decimal, InvalidOperation
from typing import Any, Final, cast
from urllib.parse import urlsplit

from ..httpclient import int_code
from .ports import (
    BULK_INFO_MAX,
    CANCEL_FREE,
    CANCEL_PAID,
    CANCEL_UNAVAILABLE,
    TAXI_CLASSES,
    CancelState,
    CancelTerms,
    Claim,
    ClaimRequest,
    Estimate,
    ParcelItem,
    Point,
    Tariff,
)

PREFIX: Final = "/b2b/cargo/integration/v2"
PATH_TARIFFS: Final = f"{PREFIX}/tariffs"
PATH_CHECK_PRICE: Final = f"{PREFIX}/check-price"
PATH_CREATE: Final = f"{PREFIX}/claims/create"
PATH_INFO: Final = f"{PREFIX}/claims/info"
PATH_BULK_INFO: Final = f"{PREFIX}/claims/bulk_info"
PATH_ACCEPT: Final = f"{PREFIX}/claims/accept"
PATH_CANCEL_INFO: Final = f"{PREFIX}/claims/cancel-info"
PATH_CANCEL: Final = f"{PREFIX}/claims/cancel"
PATH_RETURN: Final = f"{PREFIX}/claims/return"
PATH_TRACKING_LINKS: Final = f"{PREFIX}/claims/tracking-links"

LANGUAGES: Final = frozenset({"ru", "en"})
SOURCE_POINT_ID: Final = 1
DESTINATION_POINT_ID: Final = 2
# Documented limits: request_id 1–128, comments 7000, external_order_id / extra_id 512, claim id 32–64.
REQUEST_ID_MAX: Final = 128
COMMENT_MAX: Final = 7000
ORDER_ID_MAX: Final = 512
RETURN_COMMENT_MAX: Final = 1000
# The largest price we accept (so'm); anything above is not a delivery price.
MAX_PRICE: Final = 100_000_000
TEXT_MAX: Final = 120
_MONEY = re.compile(r"-?[0-9]{1,14}(?:\.[0-9]{0,4})?")
_CLAIM_ID = re.compile(r"[A-Za-z0-9_.-]{1,64}")
_CODE = re.compile(r"[A-Za-z0-9_.:-]{1,64}")
_CURRENCY = re.compile(r"[A-Z]{3}")
_PHONE_NOISE = re.compile(r"[\s()-]")
_UZ_PHONE = re.compile(r"\+998[0-9]{9}")
_EMAIL = re.compile(r"\S+@\S+\.\S+")


class ShapeError(ValueError):
    """An answer the adapter cannot read (mapped to ``YandexUnavailable``)."""


class CurrencyMismatch(ShapeError):
    """A price in another currency than ``YANDEX_DELIVERY_CURRENCY``, or one without a currency."""


# --- values ---------------------------------------------------------------------------------------------


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


def _text(value: object, limit: int = TEXT_MAX) -> str:
    return " ".join(value.split())[:limit] if isinstance(value, str) else ""


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value) if math.isfinite(value) else None


def _aware(value: object) -> datetime | None:
    """An ISO 8601 timestamp with an offset; a naive or unreadable one reads as unknown (``None``)."""
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _dict(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _list(value: object) -> list[Any]:
    return value if isinstance(value, list) else []


def _codes(entries: object) -> tuple[str, ...]:
    return tuple(
        entry["code"]
        for entry in _list(entries)
        if isinstance(entry, dict) and isinstance(entry.get("code"), str) and _CODE.fullmatch(entry["code"])
    )


# --- payloads -------------------------------------------------------------------------------------------


def _item_measures(item: ParcelItem) -> dict[str, Any]:
    measures: dict[str, Any] = {}
    if item.weight_kg is not None:
        if item.weight_kg <= 0:
            raise ValueError("weight must be positive")
        measures["weight"] = float(round(item.weight_kg, 3))
    if item.size_m is not None:
        length, width, height = item.size_m
        if min(length, width, height) <= 0:
            raise ValueError("sizes must be positive")
        measures["size"] = {
            "length": float(round(length, 3)),
            "width": float(round(width, 3)),
            "height": float(round(height, 3)),
        }
    return measures


def _taxi_class(taxi_class: str) -> str:
    if taxi_class not in TAXI_CLASSES:
        raise ValueError(f"unknown taxi class {taxi_class!r}")
    return taxi_class


def tariffs_payload(point: Point) -> dict[str, Any]:
    return {"start_point": coordinates(point), "fullname": point.fullname}


def check_price_payload(
    source: Point, destination: Point, item: ParcelItem, taxi_class: str
) -> dict[str, Any]:
    if item.quantity < 1:
        raise ValueError("quantity must be at least 1")
    return {
        "route_points": [
            {"id": SOURCE_POINT_ID, "coordinates": coordinates(source), "fullname": source.fullname},
            {
                "id": DESTINATION_POINT_ID,
                "coordinates": coordinates(destination),
                "fullname": destination.fullname,
            },
        ],
        "items": [
            {
                "quantity": item.quantity,
                "pickup_point": SOURCE_POINT_ID,
                "dropoff_point": DESTINATION_POINT_ID,
                **_item_measures(item),
            }
        ],
        "requirements": {"taxi_class": _taxi_class(taxi_class)},
    }


def _contact(name: str, phone: str, email: str) -> dict[str, Any]:
    contact: dict[str, Any] = {"name": name.strip() or "-", "phone": normalise_phone(phone)}
    if email.strip():
        if not _EMAIL.fullmatch(email.strip()):
            raise ValueError("contact e-mail is malformed")
        contact["email"] = email.strip()
    return contact


def _address(point: Point, comment: str) -> dict[str, Any]:
    if not point.fullname.strip():
        raise ValueError("address fullname is required")
    address: dict[str, Any] = {"fullname": point.fullname.strip(), "coordinates": coordinates(point)}
    if comment.strip():
        address["comment"] = comment.strip()[:COMMENT_MAX]
    return address


def create_payload(request: ClaimRequest, *, currency: str) -> dict[str, Any]:
    """The ``claims/create`` body for one parcel from the warehouse (point 1) to the customer (point 2).
    Nothing optional that changes who pays or when the courier comes is sent (no ``due``, no
    ``auto_accept``, no payment on delivery). A ``ValueError`` means our data is unusable: nothing is sent."""
    if not 1 <= len(request.request_id) <= REQUEST_ID_MAX:
        raise ValueError("request_id must be 1-128 characters")
    if not request.external_order_id or len(request.external_order_id) > ORDER_ID_MAX:
        raise ValueError("external_order_id must be 1-512 characters")
    if request.item.quantity < 1:
        raise ValueError("quantity must be at least 1")
    if request.item.cost_value < 0:
        raise ValueError("cost_value must not be negative")
    if request.callback_url is not None:
        parts = urlsplit(request.callback_url)
        if parts.scheme not in {"http", "https"} or not parts.netloc or request.callback_url[-1] not in "?&":
            raise ValueError("callback_url must be an absolute URL ending in '?' or '&'")
    source, destination = request.source, request.destination
    payload: dict[str, Any] = {
        "items": [
            {
                "title": request.item.title.strip()[:TEXT_MAX] or "-",
                "cost_value": str(request.item.cost_value),
                "cost_currency": currency,
                "quantity": request.item.quantity,
                "pickup_point": SOURCE_POINT_ID,
                "droppof_point": DESTINATION_POINT_ID,
                "extra_id": request.external_order_id,
                **_item_measures(request.item),
            }
        ],
        "route_points": [
            {
                "point_id": SOURCE_POINT_ID,
                "visit_order": 1,
                "type": "source",
                "contact": _contact(source.contact.name, source.contact.phone, source.contact.email),
                "address": _address(source.point, source.comment),
                "skip_confirmation": request.skip_confirmation_source,
            },
            {
                "point_id": DESTINATION_POINT_ID,
                "visit_order": 2,
                "type": "destination",
                "contact": _contact(
                    destination.contact.name, destination.contact.phone, destination.contact.email
                ),
                "address": _address(destination.point, destination.comment),
                "skip_confirmation": request.skip_confirmation_destination,
                "external_order_id": request.external_order_id,
            },
        ],
        "client_requirements": {"taxi_class": _taxi_class(request.taxi_class)},
        "optional_return": request.optional_return,
    }
    if request.callback_url is not None:
        payload["callback_properties"] = {"callback_url": request.callback_url}
    return payload


def cancel_payload(version: int, state: str) -> dict[str, Any]:
    if state not in (CANCEL_FREE, CANCEL_PAID):
        raise ValueError("cancel_state must be 'free' or 'paid'")
    return {"version": version, "cancel_state": state}


def return_payload(point_id: int, comment: str) -> dict[str, Any]:
    return {"point_id": point_id, "comment": comment.strip()[:RETURN_COMMENT_MAX] or "-"}


# --- answers --------------------------------------------------------------------------------------------


def error_code(body: object, status: int) -> str:
    """Yandex's ``code`` when it is a plain code, else ``http_<status>``."""
    code = _dict(body).get("code")
    return code if isinstance(code, str) and _CODE.fullmatch(code) else f"http_{status}"


def error_message(body: object) -> str:
    message = _dict(body).get("message")
    return message if isinstance(message, str) else ""


def estimate_from(body: dict[str, Any], *, currency: str) -> Estimate:
    raw = body.get("price")
    price = so_m(raw)
    rules = _dict(body.get("currency_rules"))
    check_currency(rules.get("code"), currency)
    eta = _number(body.get("eta"))
    distance = _number(body.get("distance_meters"))
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
        name = _dict(entry).get("name")
        if not isinstance(name, str) or not _CODE.fullmatch(name):
            continue
        minimal = _dict(entry).get("minimal_price")
        try:
            minimal_price = optional_so_m(minimal)
        except ShapeError:
            minimal_price = None
        tariffs.append(Tariff(name=name, minimal_price=minimal_price, title=_text(_dict(entry).get("title"))))
    return tariffs


def _vehicle(performer: dict[str, Any]) -> str:
    parts = (_text(performer.get(key), 40) for key in ("car_model", "car_color", "car_number"))
    return " ".join(part for part in parts if part)[:TEXT_MAX]


def _destination(route_points: object) -> dict[str, Any]:
    return next(
        (
            point
            for point in _list(route_points)
            if isinstance(point, dict) and point.get("type") == "destination"
        ),
        {},
    )


def _claim_taxi_class(body: dict[str, Any]) -> str | None:
    requested = _dict(body.get("client_requirements")).get("taxi_class")
    if isinstance(requested, str) and requested:
        return requested[:32]
    matched = next((car for car in _list(body.get("matched_cars")) if isinstance(car, dict)), {})
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
    pricing = _dict(body.get("pricing"))
    offer = _dict(pricing.get("offer"))
    offer_raw = offer.get("price_with_vat")
    if offer_raw is None or offer_raw == "":
        offer_raw = offer.get("price")
    offer_price = optional_so_m(offer_raw)
    final_price = optional_so_m(pricing.get("final_price"))
    found_currency = pricing.get("currency") or _dict(pricing.get("currency_rules")).get("code")
    if offer_price is not None or final_price is not None:
        found_currency = check_currency(found_currency, currency)
    destination = _destination(body.get("route_points"))
    point_id = int_code(destination.get("id"))
    external = destination.get("external_order_id")
    performer = _dict(body.get("performer_info"))
    return Claim(
        claim_id=claim_id,
        status=status,
        version=version,
        updated_ts=_aware(body.get("updated_ts")),
        offer_price=offer_price,
        offer_valid_until=_aware(offer.get("valid_until")),
        final_price=final_price,
        currency=found_currency
        if isinstance(found_currency, str) and _CURRENCY.fullmatch(found_currency)
        else "",
        warnings=_codes(body.get("warnings")),
        error_codes=_codes(body.get("error_messages")),
        courier_name=_text(performer.get("courier_name")),
        courier_vehicle=_vehicle(performer),
        destination_point_id=point_id if point_id is not None and point_id > 0 else None,
        return_reasons=tuple(
            reason[:64] for reason in _list(destination.get("return_reasons")) if isinstance(reason, str)
        ),
        external_order_id=external if isinstance(external, str) and external else None,
        taxi_class=_claim_taxi_class(body),
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
