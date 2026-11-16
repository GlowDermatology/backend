"""The Yandex Delivery port (plan §3.2; the documented flow outside Russia: ``check-price`` →
``claims/create`` → ``claims/info`` → ``claims/accept``).

``delivery`` and ``commerce`` talk to Yandex only through :class:`YandexDeliveryPort`;
``registry.get_yandex_delivery(scenario)`` picks the fake or the live adapter from
``settings.YANDEX_DELIVERY_MODE`` (default ``INTEGRATIONS_MODE``). The ``scenario`` mapping drives the fake
(``Order.metadata["fake"]["yandex"]`` or the ``X-GD-Fake-Scenario`` header); the live adapter ignores it.

Money is integer so'm: every Yandex price (a ``Decimal(18, 4)`` string) is rounded **up**; a claim's offer is
the VAT-inclusive ``price_with_vat`` when Yandex sends it. Statuses are Yandex's own literals, kept raw (an
unknown one is a valid snapshot: the caller stores it and moves nothing).

Failures are typed so callers can tell "it did not happen" from "we do not know":

* :class:`YandexRejected` — a definitive refusal (a documented 4xx; for ``check-price`` also its documented
  409 "no suitable vehicle" codes): nothing was created or changed;
* :class:`YandexConflict` — the 409 of ``accept`` / ``cancel`` / ``return`` (``old_version``,
  ``offer_expired``, ``inappropriate_status``, …): the claim is not in the state the call needs, re-read it;
* :class:`YandexUnavailable` — no usable answer: a transport failure, a 5xx, 401/403/408/425/429, a redirect,
  an answer of an unexpected shape or another currency. ``outcome_unknown`` says whether the request may have
  been processed (a timeout, a 5xx, an unreadable 2xx): the adapter never resends those, and a create is
  repeated by the caller with the **same** ``request_id`` (documented idempotency key).

Bounds: every call connects within :data:`CONNECT_TIMEOUT_SECONDS` and ends within
:data:`TOTAL_TIMEOUT_SECONDS`, unless the caller passes its own ``deadline``: a customer request shares one
:data:`CUSTOMER_PATH_SECONDS` budget across all its Yandex calls (``Deadline(CUSTOMER_PATH_SECONDS)``). No
caller holds a database transaction across a call.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Final, Literal, Protocol

from glowderma.integrations.httpclient import Deadline

CONNECT_TIMEOUT_SECONDS = 3.0
TOTAL_TIMEOUT_SECONDS = 10.0
CUSTOMER_PATH_SECONDS = 4.0
# claims/bulk_info takes 1–1000 claim ids per request.
BULK_INFO_MAX = 1000

# The documented taxi classes (``client_requirements.taxi_class``); ``tariffs`` says which a point has.
TAXI_CLASSES: tuple[str, ...] = ("courier", "express", "cargo")
# cancel-info ``cancel_state``; ``cancel`` accepts the first two only.
CANCEL_FREE: Final = "free"
CANCEL_PAID: Final = "paid"
CANCEL_UNAVAILABLE: Final = "unavailable"
type CancelState = Literal["free", "paid", "unavailable"]


@dataclass(frozen=True, slots=True)
class Point:
    """A route point's address: coordinates (sent as ``[lon, lat]``, always — the FAQ calls them mandatory)
    and the full address text (city, street, house; no flat or floor)."""

    lon: float
    lat: float
    fullname: str


@dataclass(frozen=True, slots=True)
class Contact:
    """The person at a point. ``phone`` is ``+998XXXXXXXXX``; ``email`` is documented as mandatory for the
    source (and the automatic return) point, so the source carries the warehouse e-mail."""

    name: str
    phone: str
    email: str = ""


@dataclass(frozen=True, slots=True)
class RoutePoint:
    """One end of the route: the address, the contact and a free comment for the courier."""

    point: Point
    contact: Contact
    comment: str = ""


@dataclass(frozen=True, slots=True)
class ParcelItem:
    """The one parcel of a claim. ``weight_kg`` / ``size_m`` (length, width, height in metres) are optional:
    without them Yandex assumes the class maximum. ``cost_value`` is the declared value in so'm."""

    title: str
    quantity: int = 1
    weight_kg: Decimal | None = None
    size_m: tuple[Decimal, Decimal, Decimal] | None = None
    cost_value: int = 0


@dataclass(frozen=True, slots=True)
class Estimate:
    """A ``check-price`` answer: non-binding (no offer id, no validity), so it is a quote, never a price we
    hold Yandex to. ``raw_price`` keeps Yandex's decimal string for the audit trail."""

    price: int
    currency: str
    eta_minutes: int | None
    distance_m: int | None
    zone_id: str
    raw_price: str


@dataclass(frozen=True, slots=True)
class ClaimRequest:
    """Everything ``claims/create`` sends. ``request_id`` (a UUID string we store before the call) is the
    idempotency key: a replay returns the claim it created, whatever the body. ``callback_url`` must end in
    ``?`` or ``&`` (Yandex appends ``updated_ts=…&claim_id=…`` by concatenation)."""

    request_id: str
    external_order_id: str
    source: RoutePoint
    destination: RoutePoint
    item: ParcelItem
    taxi_class: str
    optional_return: bool = False
    skip_confirmation_source: bool = False
    skip_confirmation_destination: bool = False
    callback_url: str | None = None


@dataclass(frozen=True, slots=True)
class Claim:
    """A claim as Yandex reports it (``claims/create`` / ``info`` / ``bulk_info``). Every field but the id,
    status and version is optional in the documentation and ``None`` / empty when absent. Prices are so'm in
    :data:`currency` (checked against ``YANDEX_DELIVERY_CURRENCY``); ``courier_vehicle`` is the car model,
    colour and plate joined by spaces — the courier's phone is never part of a snapshot."""

    claim_id: str
    status: str
    version: int
    updated_ts: datetime | None = None
    offer_price: int | None = None
    offer_valid_until: datetime | None = None
    final_price: int | None = None
    currency: str = ""
    warnings: tuple[str, ...] = ()
    error_codes: tuple[str, ...] = ()
    courier_name: str = ""
    courier_vehicle: str = ""
    destination_point_id: int | None = None
    return_reasons: tuple[str, ...] = ()
    external_order_id: str | None = None
    taxi_class: str | None = None


@dataclass(frozen=True, slots=True)
class CancelTerms:
    """``claims/cancel-info``: whether the claim can be cancelled now and at what fee (``price`` so'm,
    VAT-inclusive when Yandex says; ``None`` when it sends none)."""

    state: CancelState
    price: int | None = None


@dataclass(frozen=True, slots=True)
class Tariff:
    """One class a point offers (``tariffs``). ``minimal_price`` is Yandex's number rounded up; the
    documentation gives it no currency, so it is informational only."""

    name: str
    minimal_price: int | None = None
    title: str = ""


class YandexError(Exception):
    """Base class of the typed Yandex failures."""


class YandexRejected(YandexError):
    """A definitive refusal: nothing was created or changed (``code`` is Yandex's, else ``http_<status>``)."""

    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code
        self.message = message or code


class YandexConflict(YandexError):
    """The 409 of ``accept`` / ``cancel`` / ``return``: the claim's version or status forbids the call."""

    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code
        self.message = message or code


class YandexUnavailable(YandexError):
    """No usable answer. ``outcome_unknown`` is true when the request may have been processed (never
    resent by the adapter), false when it provably was not (not sent, or refused unprocessed: 401/403/408/
    425/429, a redirect). ``status`` is the HTTP status of an answer that was refused or failed (``None``
    without an answer): 401 / 403 tell an account problem (token, contract) from an outage."""

    def __init__(self, message: str = "", *, outcome_unknown: bool = True, status: int | None = None) -> None:
        super().__init__(message)
        self.outcome_unknown = outcome_unknown
        self.status = status


class YandexDeliveryPort(Protocol):
    def tariffs(self, point: Point, *, deadline: Deadline | None = None) -> list[Tariff]:
        """The classes the point offers (``manage.py yandex_delivery_check``; a method is enabled only if its
        class is listed). Raises :class:`YandexRejected` (``address_not_found``) or
        :class:`YandexUnavailable`."""
        ...

    def check_price(
        self,
        source: Point,
        destination: Point,
        item: ParcelItem,
        taxi_class: str,
        *,
        deadline: Deadline | None = None,
    ) -> Estimate:
        """A non-binding estimate for one parcel of ``taxi_class`` (checkout quote). A point or class Yandex
        does not serve raises :class:`YandexRejected`; anything else :class:`YandexUnavailable`."""
        ...

    def create_claim(self, request: ClaimRequest, *, deadline: Deadline | None = None) -> Claim:
        """Create (or, for a known ``request_id``, return) the claim. Nothing is searched for before
        :meth:`accept`. A timeout / 5xx is :class:`YandexUnavailable`: repeat with the same request."""
        ...

    def claim(self, claim_id: str, *, deadline: Deadline | None = None) -> Claim:
        """``claims/info``; may lag (``updated_ts`` is the reference). Unknown claim → :class:`YandexRejected`
        ``not_found``."""
        ...

    def claims(self, claim_ids: list[str], *, deadline: Deadline | None = None) -> list[Claim]:
        """``claims/bulk_info`` in chunks of :data:`BULK_INFO_MAX`; a claim Yandex does not return (or returns
        unreadably) is simply absent from the list."""
        ...

    def accept(self, claim_id: str, version: int, *, deadline: Deadline | None = None) -> Claim | None:
        """Confirm the claim's offer: the only call that sends a courier, so callers send it once per claim.
        Returns the answer's partial snapshot (id, status, version — the status is not proof of acceptance:
        re-read) or ``None`` for a 2xx without a readable body. 409 → :class:`YandexConflict`."""
        ...

    def cancel_terms(self, claim_id: str, *, deadline: Deadline | None = None) -> CancelTerms:
        """``claims/cancel-info``: ``free``, ``paid`` (with its fee) or ``unavailable``."""
        ...

    def cancel(
        self, claim_id: str, version: int, state: CancelState, *, deadline: Deadline | None = None
    ) -> None:
        """Cancel at the terms ``cancel_terms`` gave (``free`` or ``paid``). 409 → :class:`YandexConflict`."""
        ...

    def request_return(
        self, claim_id: str, point_id: int, comment: str, *, deadline: Deadline | None = None
    ) -> None:
        """``claims/return`` for the destination point (only after pickup): the courier brings the parcel
        back. 409 → :class:`YandexConflict`; the claim's status (``returning`` …) is the proof, not this
        answer."""
        ...

    def tracking_link(self, claim_id: str, *, deadline: Deadline | None = None) -> str | None:
        """The destination's ``sharing_link`` (https only), or ``None`` while Yandex has none."""
        ...
