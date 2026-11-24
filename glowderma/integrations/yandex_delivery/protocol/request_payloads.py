"""The request bodies of the Yandex Delivery calls: tariffs, price check, claim create, cancel and return."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

from glowderma.integrations.yandex_delivery.ports import (
    CANCEL_FREE,
    CANCEL_PAID,
    TAXI_CLASSES,
    ClaimRequest,
    ParcelItem,
    Point,
)
from glowderma.integrations.yandex_delivery.protocol.endpoints import (
    COMMENT_MAX,
    DESTINATION_POINT_ID,
    ORDER_ID_MAX,
    REQUEST_ID_MAX,
    RETURN_COMMENT_MAX,
    SOURCE_POINT_ID,
    TEXT_MAX,
)
from glowderma.integrations.yandex_delivery.protocol.values import (
    CODE_PATTERN,
    EMAIL_PATTERN,
    coordinates,
    dict_value,
    normalise_phone,
)


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
        if not EMAIL_PATTERN.fullmatch(email.strip()):
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
    """Skip point ``point_id`` and bring the item back to point A (``need_return_items``: without it the
    documentation leaves what happens to the parcel open)."""
    return {
        "point_id": point_id,
        "comment": comment.strip()[:RETURN_COMMENT_MAX] or "-",
        "need_return_items": True,
    }


def error_code(body: object, status: int) -> str:
    """Yandex's ``code`` when it is a plain code, else ``http_<status>``."""
    code = dict_value(body).get("code")
    return code if isinstance(code, str) and CODE_PATTERN.fullmatch(code) else f"http_{status}"


__all__ = [
    "cancel_payload",
    "check_price_payload",
    "create_payload",
    "error_code",
    "return_payload",
    "tariffs_payload",
]
