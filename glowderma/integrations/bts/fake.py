"""Deterministic BTS for tests, the demo seed and local end-to-end runs (``BTS_MODE=fake``).

The admin e2e scripts and ``commerce.seeds`` rely on these rules; change them only together with them. Every
rule may be overridden per order through ``Order.metadata["fake"]["bts"]`` (design 03 §1.3, plan §6), handed in
as ``scenario``:

``regions()``
    the four regions of the tariff table (``10`` Toshkent shahri, ``11`` Toshkent viloyati, ``12`` Sirdaryo,
    ``13`` Samarqand) with two cities each.

``quote(region_code, city_code)``
    ``scenario["quote"] == "rejected"`` → :class:`BtsRejected` ``route_not_served``; ``"down"`` →
    :class:`BtsUnavailable`; else ``scenario["tariff"]`` if set, else :data:`TARIFFS` by BTS region code
    (``"10"`` 25 000, ``"11"`` 26 000, ``"12"`` 27 000, ``"13"`` 28 000), else 25 000.

``find_by_client_id(client_id)``
    the parcel this process created for ``clientId`` (the fake's in-memory "BTS database", so a redelivery
    after "BTS created the parcel, then the worker crashed" adopts it), or — with ``scenario["existing"]``
    true — the parcel BTS would have created; else ``None``.

``create(request)``
    * ``scenario["down"]`` true → :class:`BtsUnavailable` (transport error; the outbox retries);
    * ``request.attempt ≤ scenario["fail_attempts"]`` → :class:`BtsRejected` ``address_not_found``
      ("Manzil topilmadi") — so ``fail_attempts = 1`` fails the automatic attempt and lets a manual retry
      succeed;
    * otherwise a parcel ``tracking_number = scenario["tracking_number"]`` or ``str(48200000 + order_id %
      100000)``, ``bts_order_id = "BTS-<order_id>"``, ``tariff = scenario["tariff"]`` or the region tariff,
      ``status = scenario["status"]`` or ``"100"``.

``cancel(parcel)``
    ``scenario["cancel"] == "unavailable"`` → :class:`BtsUnavailable`; a parcel this process already
    cancelled → cancelled again (idempotent: a cancel whose transaction rolled back is repeated);
    ``"refused"`` → refused; otherwise the effective status (``scenario["status"]`` if set, else the parcel's
    last known status, ``""`` = none yet) decides: ``""`` or ``"100"`` → cancelled, anything else → refused
    with reason ``"posilka kuryerda"``.

``track(tracking_number)``
    ``scenario["track"] == "rejected"`` → :class:`BtsRejected` ``parcel_not_found``; ``"down"`` →
    :class:`BtsUnavailable`; else the parcel with status ``scenario["track_status"]``, else the status of the
    parcel this process created under that number, else ``"100"``.

**Under the production settings** (``core.integration_modes.fake_in_production("bts")``, customer contract
§2.14.3) the fake never reports a remote effect that did not happen and keeps nothing in :data:`_parcels`:
``regions()`` → ``[]``; ``quote()`` / ``create()`` → :class:`BtsRejected` ``fake_mode``;
``find_by_client_id()`` → ``None``; ``cancel()`` / ``track()`` → :class:`BtsUnavailable`; scenarios are ignored.
"""

from collections.abc import Mapping
from typing import Any

from glowderma.core import integration_modes

from .ports import (
    STATUS_ACCEPTED,
    BtsRejected,
    BtsUnavailable,
    CancelResult,
    City,
    CreateRequest,
    Parcel,
    Region,
)

DEFAULT_TARIFF = 25_000
TARIFFS: dict[str, int] = {"10": 25_000, "11": 26_000, "12": 27_000, "13": 28_000}
TRACKING_BASE = 48_200_000
ADDRESS_NOT_FOUND = "address_not_found"
ROUTE_NOT_SERVED = "route_not_served"
PARCEL_NOT_FOUND = "parcel_not_found"
FAKE_MODE = "fake_mode"
REFUSAL_REASON = "posilka kuryerda"
REGIONS: tuple[Region, ...] = (
    Region("10", "Toshkent shahri", (City("1001", "Chilonzor tumani"), City("1002", "Yunusobod tumani"))),
    Region("11", "Toshkent viloyati", (City("1101", "Chirchiq shahri"), City("1102", "Zangiota tumani"))),
    Region("12", "Sirdaryo viloyati", (City("1201", "Guliston shahri"), City("1202", "Sirdaryo tumani"))),
    Region("13", "Samarqand viloyati", (City("1301", "Samarqand shahri"), City("1302", "Urgut tumani"))),
)

# The fake's "remote" parcels by clientId, per process (see the module docstring).
_parcels: dict[str, Parcel] = {}
_cancelled: set[str] = set()


def reset() -> None:
    """Forget every parcel the fake created or cancelled (tests)."""
    _parcels.clear()
    _cancelled.clear()


def tracking_number_for(order_id: int) -> str:
    return str(TRACKING_BASE + order_id % 100_000)


def _production() -> bool:
    return integration_modes.fake_in_production("bts")


class FakeBts:
    name = "bts"

    def __init__(self, scenario: Mapping[str, Any] | None = None) -> None:
        self.scenario: Mapping[str, Any] = {} if _production() else (scenario or {})

    def _tariff(self, region_code: str) -> int:
        override = self.scenario.get("tariff")
        if override is not None:
            return int(override)
        return TARIFFS.get(region_code, DEFAULT_TARIFF)

    def _parcel(self, client_id: str, region_code: str = "") -> Parcel:
        tracking = self.scenario.get("tracking_number") or tracking_number_for(int(client_id))
        return Parcel(
            client_id=client_id,
            tracking_number=str(tracking),
            bts_order_id=f"BTS-{client_id}",
            tariff=self._tariff(region_code),
            status=str(self.scenario.get("status") or STATUS_ACCEPTED),
        )

    def regions(self) -> list[Region]:
        if _production():
            return []
        return list(REGIONS)

    def quote(self, region_code: str, city_code: str) -> int:
        if _production():
            raise BtsRejected(FAKE_MODE, "BTS is fake under the production settings")
        answer = self.scenario.get("quote")
        if answer == "rejected":
            raise BtsRejected(ROUTE_NOT_SERVED, "BTS does not deliver to this area (fake)")
        if answer == "down":
            raise BtsUnavailable("BTS did not answer (fake: quote down)")
        return self._tariff(region_code)

    def find_by_client_id(self, client_id: str) -> Parcel | None:
        if _production():
            return None
        if self.scenario.get("down"):
            raise BtsUnavailable("BTS did not answer (fake: down)")
        found = _parcels.get(client_id)
        if found is None and self.scenario.get("existing") and client_id not in _cancelled:
            found = self._parcel(client_id)
        return found

    def create(self, request: CreateRequest) -> Parcel:
        if _production():
            raise BtsRejected(FAKE_MODE, "BTS is fake under the production settings")
        if self.scenario.get("down"):
            raise BtsUnavailable("BTS did not answer (fake: down)")
        if request.attempt <= int(self.scenario.get("fail_attempts") or 0):
            raise BtsRejected(ADDRESS_NOT_FOUND, "Manzil topilmadi")
        parcel = self._parcel(request.client_id, request.region_code)
        _parcels[request.client_id] = parcel
        return parcel

    def cancel(self, parcel: Parcel) -> CancelResult:
        if _production():
            raise BtsUnavailable("BTS is fake under the production settings: no cancellation is made up")
        answer = self.scenario.get("cancel")
        if answer == "unavailable":
            raise BtsUnavailable("BTS did not answer (fake: cancel unavailable)")
        status = str(self.scenario.get("status") or parcel.status or "")
        if parcel.client_id in _cancelled:
            return CancelResult(ok=True, status=status)
        if answer == "refused" or status not in ("", STATUS_ACCEPTED):
            return CancelResult(ok=False, status=status, reason=REFUSAL_REASON)
        _parcels.pop(parcel.client_id, None)
        _cancelled.add(parcel.client_id)
        return CancelResult(ok=True, status=status)

    def track(self, tracking_number: str) -> Parcel:
        if _production():
            raise BtsUnavailable("BTS is fake under the production settings: no status is made up")
        answer = self.scenario.get("track")
        if answer == "rejected":
            raise BtsRejected(PARCEL_NOT_FOUND, "BTS does not know this parcel (fake)")
        if answer == "down":
            raise BtsUnavailable("BTS did not answer (fake: track down)")
        known = next((p for p in _parcels.values() if p.tracking_number == tracking_number), None)
        status = self.scenario.get("track_status") or (known.status if known is not None else STATUS_ACCEPTED)
        return Parcel(
            client_id=known.client_id if known is not None else "",
            tracking_number=tracking_number,
            bts_order_id=known.bts_order_id if known is not None else "",
            tariff=known.tariff if known is not None else None,
            status=str(status),
            status_at=str(self.scenario.get("track_status_at") or ""),
        )
