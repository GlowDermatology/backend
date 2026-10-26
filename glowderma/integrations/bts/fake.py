"""Deterministic BTS for tests, the demo seed and local end-to-end runs (``BTS_MODE=fake``).

The admin e2e scripts and ``commerce.seeds`` rely on these rules; change them only together with them. Every
rule may be overridden per order through ``Order.metadata["fake"]["bts"]`` (design 03 §1.3), handed in as
``scenario``:

``quote(region_code, city_code)``
    ``scenario["tariff"]`` if set, else :data:`TARIFFS` by BTS region code (``"10"`` Tashkent city 25 000,
    ``"11"`` Tashkent region 26 000, ``"12"`` Sirdaryo 27 000, ``"13"`` Samarkand 28 000), else 25 000.

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
"""

from collections.abc import Mapping
from typing import Any

from .ports import STATUS_ACCEPTED, BtsRejected, BtsUnavailable, CancelResult, CreateRequest, Parcel

DEFAULT_TARIFF = 25_000
TARIFFS: dict[str, int] = {"10": 25_000, "11": 26_000, "12": 27_000, "13": 28_000}
TRACKING_BASE = 48_200_000
ADDRESS_NOT_FOUND = "address_not_found"
REFUSAL_REASON = "posilka kuryerda"

# The fake's "remote" parcels by clientId, per process (see the module docstring).
_parcels: dict[str, Parcel] = {}
_cancelled: set[str] = set()


def reset() -> None:
    """Forget every parcel the fake created or cancelled (tests)."""
    _parcels.clear()
    _cancelled.clear()


def tracking_number_for(order_id: int) -> str:
    return str(TRACKING_BASE + order_id % 100_000)


class FakeBts:
    name = "bts"

    def __init__(self, scenario: Mapping[str, Any] | None = None) -> None:
        self.scenario: Mapping[str, Any] = scenario or {}

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

    def quote(self, region_code: str, city_code: str) -> int:
        return self._tariff(region_code)

    def find_by_client_id(self, client_id: str) -> Parcel | None:
        if self.scenario.get("down"):
            raise BtsUnavailable("BTS did not answer (fake: down)")
        found = _parcels.get(client_id)
        if found is None and self.scenario.get("existing") and client_id not in _cancelled:
            found = self._parcel(client_id)
        return found

    def create(self, request: CreateRequest) -> Parcel:
        if self.scenario.get("down"):
            raise BtsUnavailable("BTS did not answer (fake: down)")
        if request.attempt <= int(self.scenario.get("fail_attempts") or 0):
            raise BtsRejected(ADDRESS_NOT_FOUND, "Manzil topilmadi")
        parcel = self._parcel(request.client_id, request.region_code)
        _parcels[request.client_id] = parcel
        return parcel

    def cancel(self, parcel: Parcel) -> CancelResult:
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
