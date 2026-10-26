"""The BTS port (design 02 §3, 03 §5; contract §2.2 cancel/retry rules).

``delivery`` and ``commerce`` talk to BTS only through :class:`BtsPort`; ``registry.get_bts(scenario)`` picks
the fake or the live implementation from ``settings.BTS_MODE`` (default ``INTEGRATIONS_MODE``). The
``scenario`` mapping is the order's ``metadata["fake"]["bts"]`` (design 03 §1.3): the fake reads it, a live
adapter ignores it. Failures are typed: :class:`BtsRejected` is a definitive answer (``shipment_failed``),
:class:`BtsUnavailable` a transport error (the caller retries, or answers 503 for the synchronous cancel).

Bounded calls (normative for every adapter, M3 live included): no call may block longer than
:data:`CONNECT_TIMEOUT_SECONDS` to connect and :data:`TOTAL_TIMEOUT_SECONDS` in total (``quote``, which a
customer request waits for: :data:`REQUEST_PATH_SECONDS`); a timeout is a :class:`BtsUnavailable`. The
``bts.create_shipment`` handler calls ``find_by_client_id`` / ``create`` **outside** the Order row lock: it
checks under the lock and commits, calls BTS while holding only its create advisory lock, then re-locks the
order to record (a create that raced an admin cancel is cancelled at BTS as an orphan). The admin cancel and
the orphan cancel call ``cancel`` under the Order lock (one bounded call; that lock serialises them against
the create's recording).

Idempotency at the provider boundary: ``cancel`` of a parcel BTS has already cancelled answers ``ok`` (an
admin cancel whose transaction rolled back after BTS agreed is repeated), and ``find_by_client_id`` is asked
before every ``create`` (a create that reached BTS before the worker crashed is adopted, never duplicated).
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

# BTS status code of an accepted parcel that has not left the warehouse: the only status BTS cancels (O1).
STATUS_ACCEPTED = "100"
# Transport bounds every adapter honours (see the module docstring); a timeout raises BtsUnavailable.
CONNECT_TIMEOUT_SECONDS = 3.0
TOTAL_TIMEOUT_SECONDS = 10.0
REQUEST_PATH_SECONDS = 4.0


@dataclass(frozen=True, slots=True)
class Parcel:
    """A BTS parcel as the provider reports it (create answer or ``clientId`` lookup)."""

    client_id: str  # our order id as text (BTS ``clientId``)
    tracking_number: str  # live: BTS's ``orderId`` (the waybill number ``track`` and ``cancel`` take)
    bts_order_id: str
    tariff: int | None  # so'm; ``None`` when BTS does not echo it (the order's quoted fee is used)
    # Port status code ("100" accepted, "200" handed to the courier, "300" delivered, anything else recorded
    # only; ``delivery/bts.py``). The live adapter translates BTS's own codes (``protocol.port_status``).
    status: str
    status_at: str = ""  # BTS's status time when ``track`` reports one (a stable payload field), else ""


@dataclass(frozen=True, slots=True)
class City:
    code: str
    name: str


@dataclass(frozen=True, slots=True)
class Region:
    """One region of BTS's reference list with its cities (``delivery/regions/``, address validation)."""

    code: str
    name: str
    cities: tuple[City, ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class CreateRequest:
    client_id: str
    attempt: int  # 1-based create attempt number for this order (BTS answers that got through)
    recipient_name: str
    recipient_phone: str
    address: str
    region_code: str
    city_code: str
    comment: str = ""


@dataclass(frozen=True, slots=True)
class CancelResult:
    """``ok`` when BTS cancelled the parcel; otherwise ``reason`` is BTS's refusal (status beyond 100)."""

    ok: bool
    status: str
    reason: str = ""


class BtsError(Exception):
    """Base class of the typed BTS failures."""


class BtsRejected(BtsError):
    """A definitive refusal (a create with a bad address, a quote for a route BTS does not serve, a track of a
    parcel BTS does not know, …)."""

    def __init__(self, error_code: str, message: str = "") -> None:
        super().__init__(message or error_code)
        self.error_code = error_code
        self.message = message or error_code


class BtsUnavailable(BtsError):
    """BTS did not answer (transport error, 5xx): safe to retry."""


class BtsPort(Protocol):
    def regions(self) -> list[Region]:
        """BTS's region/city reference list (cached 24 h by ``delivery.selectors.region_catalog``)."""
        ...

    def quote(self, region_code: str, city_code: str) -> int:
        """The delivery tariff in so'm for an address (``delivery/calculate/``, checkout's ``delivery_fee``):
        sender city ``BTS_SENDER_CITY_CODE``, weight ``BTS_DEFAULT_PARCEL_WEIGHT_G``, one parcel. A route BTS
        does not serve raises :class:`BtsRejected`; a transport failure :class:`BtsUnavailable`."""
        ...

    def track(self, tracking_number: str) -> Parcel:
        """The parcel with its current status, asked with **our** stored tracking number (webhook re-track,
        polling; customer contract §2.13.3). :class:`BtsRejected` = a definitive per-parcel answer (unknown
        parcel), :class:`BtsUnavailable` = transport error, 5xx or timeout."""
        ...

    def find_by_client_id(self, client_id: str, *, since: datetime) -> Parcel | None:
        """The parcel BTS already holds for our order ``client_id`` (asked before every create), else
        ``None``. ``since`` is when the shipment row was created (before its first create request): the
        lookup need not look further back than the day before it."""
        ...

    def create(self, request: CreateRequest) -> Parcel:
        """Create the parcel; raises :class:`BtsRejected` (definitive) or :class:`BtsUnavailable`."""
        ...

    def cancel(self, parcel: Parcel) -> CancelResult:
        """Cancel the parcel synchronously; refused unless BTS still reports status 100. A parcel that is
        already cancelled at BTS answers ``ok`` (idempotent)."""
        ...
