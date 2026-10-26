"""The BTS port (design 02 §3, 03 §5; contract §2.2 cancel/retry rules).

``delivery`` and ``commerce`` talk to BTS only through :class:`BtsPort`; ``registry.get_bts(scenario)`` picks
the fake or the live implementation from ``settings.BTS_MODE`` (default ``INTEGRATIONS_MODE``). The
``scenario`` mapping is the order's ``metadata["fake"]["bts"]`` (design 03 §1.3): the fake reads it, a live
adapter ignores it. Failures are typed: :class:`BtsRejected` is a definitive answer (``shipment_failed``),
:class:`BtsUnavailable` a transport error (the caller retries, or answers 503 for the synchronous cancel).

Bounded calls (normative for every adapter, M3 live included): the callers hold the **Order** row lock
across ``find_by_client_id`` / ``create`` / ``cancel`` (that lock is what serialises a create against an
admin cancel), so no call may block longer than :data:`CONNECT_TIMEOUT_SECONDS` to connect and
:data:`TOTAL_TIMEOUT_SECONDS` in total; a timeout is a :class:`BtsUnavailable`. ``quote`` is called outside
any row lock by checkout.

Idempotency at the provider boundary: ``cancel`` of a parcel BTS has already cancelled answers ``ok`` (an
admin cancel whose transaction rolled back after BTS agreed is repeated), and ``find_by_client_id`` is asked
before every ``create`` (a create that reached BTS before the worker crashed is adopted, never duplicated).
"""

from dataclasses import dataclass
from typing import Protocol

# BTS status code of an accepted parcel that has not left the warehouse: the only status BTS cancels (O1).
STATUS_ACCEPTED = "100"
# Transport bounds every adapter honours (see the module docstring); a timeout raises BtsUnavailable.
CONNECT_TIMEOUT_SECONDS = 3.0
TOTAL_TIMEOUT_SECONDS = 10.0


@dataclass(frozen=True, slots=True)
class Parcel:
    """A BTS parcel as the provider reports it (create answer or ``clientId`` lookup)."""

    client_id: str  # our order id as text (BTS ``clientId``)
    tracking_number: str
    bts_order_id: str
    tariff: int | None  # so'm; ``None`` when BTS does not echo it (the order's quoted fee is used)
    status: str  # raw BTS status code ("100", "200", …)


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
    """A definitive refusal of a create (bad address, unknown region, …)."""

    def __init__(self, error_code: str, message: str = "") -> None:
        super().__init__(message or error_code)
        self.error_code = error_code
        self.message = message or error_code


class BtsUnavailable(BtsError):
    """BTS did not answer (transport error, 5xx): safe to retry."""


class BtsPort(Protocol):
    def quote(self, region_code: str, city_code: str) -> int:
        """The delivery tariff in so'm for an address (checkout's ``delivery_fee``)."""
        ...

    def find_by_client_id(self, client_id: str) -> Parcel | None:
        """The parcel BTS already holds for ``clientId`` (asked before every create), else ``None``."""
        ...

    def create(self, request: CreateRequest) -> Parcel:
        """Create the parcel; raises :class:`BtsRejected` (definitive) or :class:`BtsUnavailable`."""
        ...

    def cancel(self, parcel: Parcel) -> CancelResult:
        """Cancel the parcel synchronously; refused unless BTS still reports status 100. A parcel that is
        already cancelled at BTS answers ``ok`` (idempotent)."""
        ...
