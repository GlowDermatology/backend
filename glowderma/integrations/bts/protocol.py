"""BTS public API protocol (docs.bts.uz, English pages, read 2026-10-26) — pure helpers, no database, no HTTP.

Every request carries ``Authorization: Bearer <access_token>`` (``POST /auth/login {login, password}`` →
``data.access_token``, valid one day; ``data.refresh_token`` 30 days), ``Accept`` / ``Content-Type:
application/json`` and ``language: uz``. Answers are ``{"status": bool, "message", "status_code", "data"}``;
lists carry ``data.items`` with Yii-style ``_links`` / ``_meta`` pagination (``page``, ``per-page``).

Endpoints used by the live adapter (the documented test host is ``https://apitest.bts.uz:28345``):

* ``regions()``: ``GET /v1/directory/regions``, then ``GET /v1/directory/cities?regionCode=`` per region;
* ``quote(region, city)``: ``POST /v1/order-calculate/index`` with ``is_multiple_cost: 0``;
* ``create(request)``: ``POST /v1/order/add`` with ``clientId`` = our order id;
* ``find_by_client_id(client_id)``: ``GET /v1/order/list?beginDate=&endDate=`` — no ``clientId`` filter is
  documented, so a date window is scanned page by page (verify M3);
* ``track(tracking_number)``: ``GET /v1/order/track?orderId=`` — our tracking number is BTS's ``orderId``
  (the waybill number), the only key ``track`` and ``cancel`` accept;
* ``cancel(parcel)``: ``GET /v1/order-cancel/index?orderId=`` — BTS cancels only at status ``100``.

**Statuses.** BTS's reference list (``GET /v1/status/index``) is ``100`` at the sender, ``200`` courier took
it, ``300`` in the sending office, ``400`` internal transport, ``500`` sorting centre, ``600`` in a bag,
``700`` in transit, ``800`` in the delivery office, ``1100`` courier delivering, ``1200`` delivered, ``1300``
deleted (also what a cancel answers), ``1400`` expired. Our port vocabulary (``delivery/bts.py``) is the
contract's: ``100`` accepted (cancellable), ``200`` handed to the courier, ``300`` delivered. BTS's own
``300`` would read as "delivered", so :func:`port_status` translates: ``100`` → ``"100"``, ``200``–``1100`` →
``"200"``, ``1200`` → ``"300"``, anything else (``1300``, ``1400``, unknown codes) its own code, which only
gets recorded.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final

from .ports import STATUS_ACCEPTED, CreateRequest, Parcel

LANGUAGE: Final = "uz"
PATH_LOGIN: Final = "/auth/login"
PATH_REGIONS: Final = "/v1/directory/regions"
PATH_CITIES: Final = "/v1/directory/cities"
PATH_CALCULATE: Final = "/v1/order-calculate/index"
PATH_CREATE: Final = "/v1/order/add"
PATH_LIST: Final = "/v1/order/list"
PATH_TRACK: Final = "/v1/order/track"
PATH_CANCEL: Final = "/v1/order-cancel/index"
# Yii pagination: documented on the city list; assumed on the order list (verify M3).
PER_PAGE: Final = 50
MAX_PAGES: Final = 40
TOKEN_TTL_SECONDS: Final = 23 * 3600  # the access token is valid one day
DROPOFF_TYPE: Final = "courier"  # home delivery: the courier brings the parcel to the recipient
PICKUP_TYPES: Final = frozenset({"courier", "branch"})
ALREADY_CANCELLED: Final = "error.already_cancelled"

PORT_HANDED_TO_COURIER: Final = "200"
PORT_DELIVERED: Final = "300"
BTS_AT_SENDER: Final = 100
BTS_IN_NETWORK: Final = frozenset({200, 300, 400, 500, 600, 700, 800, 1100})
BTS_DELIVERED: Final = 1200
BTS_DELETED: Final = 1300
BTS_EXPIRED: Final = 1400
UNKNOWN_STATUS: Final = "unknown"

# Typed refusals (``BtsRejected.error_code``).
ROUTE_NOT_SERVED: Final = "route_not_served"
PARCEL_NOT_FOUND: Final = "parcel_not_found"
VALIDATION_ERROR: Final = "validation_error"


@dataclass(frozen=True, slots=True)
class Sender:
    """Our side of every parcel (``BTS_SENDER_*``)."""

    name: str
    phone: str
    address: str
    city_code: str
    branch_code: str = ""


def bts_code(value: object) -> int | None:
    """BTS's status code as an integer (``None`` for a missing or non-numeric code)."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value.strip())
    return None


def port_status(status: object) -> str:
    """The port status of a BTS ``status`` object (``{"code", "info" | "name"}``) or bare code.

    A just-created order reports ``code: null`` with ``info`` "У отправителя" (at the sender); a missing code
    therefore reads ``"100"`` (verify M3)."""
    raw = status.get("code") if isinstance(status, dict) else status
    if raw is None or raw == "":
        return STATUS_ACCEPTED
    code = bts_code(raw)
    if code is None:
        return UNKNOWN_STATUS
    if code == BTS_AT_SENDER:
        return STATUS_ACCEPTED
    if code in BTS_IN_NETWORK:
        return PORT_HANDED_TO_COURIER
    if code == BTS_DELIVERED:
        return PORT_DELIVERED
    return str(code)


def weight_kg(weight_g: int) -> float:
    return round(weight_g / 1000, 3)


def calculate_payload(
    *, sender_city_code: str, city_code: str, pickup_type: str, weight_g: int
) -> dict[str, Any]:
    return {
        "senderCityCode": sender_city_code,
        "receiverCityCode": city_code,
        "pickup_type": pickup_type,
        "dropoff_type": DROPOFF_TYPE,
        "is_multiple_cost": 0,
        "weight": weight_kg(weight_g),
    }


def quote_option(data: object, pickup_type: str) -> dict[str, Any] | None:
    """The tariff option of our direction: ``data`` itself for ``is_multiple_cost: 0`` (verify M3: the single
    answer's shape is not shown), else ``data["<pickup>_to_courier"]`` of the multiple-cost shape."""
    if not isinstance(data, dict):
        return None
    if "price" in data or "available" in data:
        return data
    option = data.get(f"{pickup_type}_to_{DROPOFF_TYPE}")
    return option if isinstance(option, dict) else None


def price_of(option: dict[str, Any]) -> int | None:
    price = option.get("price")
    if isinstance(price, bool) or not isinstance(price, int | float | str):
        return None
    try:
        value = float(price)
    except ValueError:
        return None
    return int(round(value)) if value >= 0 else None


def create_payload(
    request: CreateRequest, *, sender: Sender, pickup_type: str, weight_g: int
) -> dict[str, Any]:
    """``POST /v1/order/add`` for one home-delivery parcel. The recipient comment has no field of its own, so
    it is appended to the address in brackets (verify M3); no cash on delivery, no goods list (paid
    online)."""
    address = request.address.strip()
    if request.comment.strip():
        address = f"{address} ({request.comment.strip()})"
    sender_block: dict[str, Any] = {
        "name": sender.name,
        "phone": sender.phone,
        "address": sender.address,
        "city_code": sender.city_code,
    }
    if pickup_type == "branch" and sender.branch_code:
        sender_block["branch_code"] = sender.branch_code
    return {
        "clientId": request.client_id,
        "pickup_type": pickup_type,
        "dropoff_type": DROPOFF_TYPE,
        "is_sender_location": False,
        "is_receiver_location": False,
        "sender": sender_block,
        "receiver": {
            "name": request.recipient_name,
            "phone": request.recipient_phone,
            "address": address,
            "city_code": request.city_code,
        },
        "cargo": {"weight": weight_kg(weight_g), "piece": 1},
    }


def parcel_from_order(item: object) -> Parcel | None:
    """A parcel from an order document (create answer, list item); ``None`` without a numeric ``orderId``.

    Our tracking number is BTS's ``orderId`` (the waybill number ``track`` and ``cancel`` take); the tariff
    is not read from ``cost`` (its meaning is not documented, verify M3), so the order's quoted fee is
    used."""
    if not isinstance(item, dict):
        return None
    order_id = bts_code(item.get("orderId"))
    if order_id is None or order_id <= 0:
        return None
    client_id = item.get("clientId")
    return Parcel(
        client_id="" if client_id is None else str(client_id),
        tracking_number=str(order_id),
        bts_order_id=str(order_id),
        tariff=None,
        status=port_status(item.get("status")),
    )


def is_gone(parcel: Parcel) -> bool:
    """A parcel BTS deleted (cancelled) or let expire: never adopted by the lookup before a create."""
    return parcel.status in (str(BTS_DELETED), str(BTS_EXPIRED))


def error_text(body: dict[str, Any]) -> str:
    """BTS's message plus its field errors, as one line (sanitised by the caller)."""
    parts: list[str] = []
    message = body.get("message")
    if isinstance(message, str) and message:
        parts.append(message)
    errors = body.get("errors")
    if isinstance(errors, dict):
        for field, messages in errors.items():
            if isinstance(messages, list):
                parts.append(f"{field}: " + "; ".join(str(item) for item in messages))
            else:
                parts.append(f"{field}: {messages}")
    return " | ".join(parts)
