"""Deterministic Yandex Delivery for tests, the demo seed and local runs (``YANDEX_DELIVERY_MODE=fake``).

The fake keeps its "remote" claims in process memory (:func:`reset` forgets them): claims by id and the
``request_id`` that created each, so ``create_claim`` is idempotent per ``request_id`` like the real API (a
replay returns the existing claim whatever the body). Inputs are validated with the live payload builders,
so a request the live adapter would refuse (a phone that is not +998…, an unknown class) is refused here too
(:class:`YandexRejected` ``invalid_request``).

Every rule may be overridden through ``scenario`` — ``Order.metadata["fake"]["yandex"]`` or the
``X-GD-Fake-Scenario`` header (local/test only):

``tariffs(point)``
    ``scenario["tariffs"]`` (class names), else ``courier`` and ``express``.

``check_price(source, destination, item, taxi_class)``
    ``scenario["check_price"]``: ``"rejected"`` → :class:`YandexRejected` ``errors.suitable_offer_not_found``;
    ``"no_tariff"`` → :class:`YandexRejected` ``estimating.tariff.no_categories_in_zone``; ``"down"`` →
    :class:`YandexUnavailable`. Else the price ``scenario["price"]`` or :func:`price_for` (a base per class
    plus a per-kilometre rate over the straight-line distance, rounded up to 100 so'm), ETA 15 min + 3 min/km.

``create_claim(request)``
    a known ``request_id`` returns its claim (before any scenario rule). ``scenario["create"]``:
    ``"rejected"`` → :class:`YandexRejected` ``address_not_found``; ``"down"`` → :class:`YandexUnavailable`
    (nothing created); ``"down_after_create"`` → the claim is created, then :class:`YandexUnavailable`
    (the retry with the same ``request_id`` returns it). The new claim is ``ready_for_approval`` at once
    with the offer ``scenario["offer_price"]`` (else the check-price price, VAT-inclusive), valid 10 minutes,
    and ``scenario["warnings"]`` (warning codes); ``scenario["estimate"]``: ``"failed"`` →
    ``estimating_failed`` (error code ``estimating.cant_construct_route``), ``"pending"`` → it stays
    ``estimating`` until :func:`advance`.

``accept(claim_id, version)``
    ``scenario["accept"]``: ``"conflict"`` → :class:`YandexConflict` ``inappropriate_status``; ``"down"`` →
    :class:`YandexUnavailable` (nothing accepted, ``outcome_unknown`` false); ``"down_once"`` → the same for
    the claim's first accept only (a later one goes through); ``"old_version_once"`` → the claim's first
    accept finds it edited: its version moves on by one (the next read shows it) and :class:`YandexConflict`
    ``old_version``, a later accept of the new version goes through; ``"timeout"`` →
    :class:`YandexUnavailable` with ``outcome_unknown`` true although nothing was accepted (the claim stays
    ``ready_for_approval``); ``"expired"`` → answered ``accepted``, but the next read shows ``failed`` with
    error code ``offer_expired`` (FAQ 14). A claim that is not ``ready_for_approval`` →
    ``inappropriate_status``, another version → ``old_version``.

``claim(claim_id)`` / ``claims(ids)``
    ``scenario["info"] == "not_found"`` → ``claim`` raises :class:`YandexRejected` ``not_found`` for a claim
    the fake does hold (``claims`` is unaffected; the poll reads without a scenario anyway). Otherwise
    each read of an accepted claim applies the next status of ``scenario["statuses"]`` (default
    :data:`DEFAULT_SCRIPT`: ``performer_lookup`` → ``performer_found`` → ``pickup_arrived`` → ``pickuped`` →
    ``delivery_arrived`` → ``delivered`` → ``delivered_finish``); the courier's name and car appear from
    ``performer_found``; a finished claim gets ``final_price`` = ``scenario["final_price"]`` or the offer.
    An unknown id → :class:`YandexRejected` ``not_found`` (``claims`` skips it). The script is taken from the
    scenario of the call that **created** the claim.

``cancel_terms`` / ``cancel``
    ``scenario["cancel"]``: ``"free"`` / ``"paid"`` / ``"unavailable"`` force the terms, ``"down"`` →
    :class:`YandexUnavailable`; otherwise free until the courier reaches the warehouse, paid (fee = the
    offer) at ``pickup_arrived`` / ``ready_for_pickup_confirmation``, unavailable after. A cancel the terms do
    not allow → :class:`YandexConflict`; done → ``cancelled`` / ``cancelled_with_payment``.

``request_return(claim_id, point_id, comment)``
    after pickup only (else :class:`YandexConflict` ``state_mismatch``); ``scenario["return"] == "refused"``
    → :class:`YandexConflict` ``not_allowed``; otherwise the claim turns ``returning`` and the next reads go
    ``return_arrived`` → ``returned`` → ``returned_finish``.

``tracking_link(claim_id)``
    ``https://tracking.fake-yandex.invalid/<claim id>`` once a courier is assigned, else ``None``.

:func:`advance` sets a claim's status directly (dev tools, tests); the scripted remainder is dropped.

**Under the production settings** (``core.integration_modes.fake_in_production("yandex")``) the fake invents
nothing: ``check_price`` / ``create_claim`` → :class:`YandexRejected` ``fake_mode``; every other call →
:class:`YandexUnavailable`; scenarios are ignored and nothing is stored.
"""

from __future__ import annotations

import hashlib
import math
import threading
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Any

from django.conf import settings

from glowderma.core import integration_modes, timeutils

from ..httpclient import Deadline
from . import protocol
from .ports import (
    CANCEL_FREE,
    CANCEL_PAID,
    CANCEL_UNAVAILABLE,
    CancelState,
    CancelTerms,
    Claim,
    ClaimRequest,
    Estimate,
    ParcelItem,
    Point,
    Tariff,
    YandexConflict,
    YandexRejected,
    YandexUnavailable,
)

FAKE_MODE = "fake_mode"
DEFAULT_SCRIPT: tuple[str, ...] = (
    "performer_lookup",
    "performer_found",
    "pickup_arrived",
    "pickuped",
    "delivery_arrived",
    "delivered",
    "delivered_finish",
)
RETURN_SCRIPT: tuple[str, ...] = ("return_arrived", "returned", "returned_finish")
# class → (base so'm, so'm per km); tariffs() lists the first two by default.
RATES: dict[str, tuple[int, int]] = {
    "courier": (15_000, 2_000),
    "express": (20_000, 2_500),
    "cargo": (60_000, 5_000),
}
DEFAULT_TARIFFS: tuple[str, ...] = ("courier", "express")
OFFER_VALID_MINUTES = 10
COURIER_NAME = "Fake Kuryer"
COURIER_VEHICLE = "Chevrolet Cobalt oq 01A777AA"
TRACKING_BASE = "https://tracking.fake-yandex.invalid/"
_BEFORE_COURIER = frozenset(
    {"new", "estimating", "ready_for_approval", "accepted", "performer_lookup", "performer_draft"}
)
_FREE_CANCEL = _BEFORE_COURIER | {"performer_found"}
_PAID_CANCEL = frozenset({"pickup_arrived", "ready_for_pickup_confirmation"})
_RETURNABLE = frozenset({"pickuped", "delivery_arrived", "ready_for_delivery_confirmation"})
_FINISHED = frozenset(
    {
        "delivered_finish",
        "returned_finish",
        "cancelled",
        "cancelled_with_payment",
        "cancelled_by_taxi",
        "cancelled_with_items_on_hands",
        "failed",
        "estimating_failed",
        "performer_not_found",
    }
)
_PRICED_ENDS = frozenset({"delivered_finish", "returned_finish"})


@dataclass(slots=True)
class _Remote:
    """One claim as the fake "Yandex" holds it."""

    claim_id: str
    request_id: str
    status: str
    taxi_class: str
    external_order_id: str
    offer_price: int | None
    offer_valid_until: datetime | None
    updated_ts: datetime
    version: int = 1
    script: list[str] = field(default_factory=list)
    final_price_override: int | None = None
    final_price: int | None = None
    warnings: tuple[str, ...] = ()
    error_codes: tuple[str, ...] = ()
    expired: bool = False
    accepts: int = 0  # accept calls that reached this claim ("down_once" / "old_version_once")


_claims: dict[str, _Remote] = {}
_requests: dict[str, str] = {}
_lock = threading.Lock()


def reset() -> None:
    """Forget every claim the fake created (tests)."""
    with _lock:
        _claims.clear()
        _requests.clear()


def claim_id_for(request_id: str) -> str:
    """The deterministic 32-character claim id of a ``request_id``."""
    return "fake" + hashlib.sha256(request_id.encode()).hexdigest()[:28]


def distance_km(source: Point, destination: Point) -> float:
    """Great-circle distance in kilometres (haversine over a 6371 km sphere)."""
    lat1, lat2 = math.radians(source.lat), math.radians(destination.lat)
    dlat = lat2 - lat1
    dlon = math.radians(destination.lon - source.lon)
    a = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * 6371.0 * math.asin(min(1.0, math.sqrt(a)))


def price_for(source: Point, destination: Point, taxi_class: str) -> int:
    """The fake's default estimate in so'm: base + rate × km, rounded up to 100 so'm."""
    base, per_km = RATES.get(taxi_class, RATES["courier"])
    raw = base + per_km * distance_km(source, destination)
    return math.ceil(raw / 100) * 100


def _production() -> bool:
    return integration_modes.fake_in_production("yandex")


def _snapshot(remote: _Remote) -> Claim:
    with_courier = remote.status not in _BEFORE_COURIER and remote.status not in {
        "estimating_failed",
        "performer_not_found",
    }
    return Claim(
        claim_id=remote.claim_id,
        status=remote.status,
        version=remote.version,
        updated_ts=remote.updated_ts,
        offer_price=remote.offer_price,
        offer_valid_until=remote.offer_valid_until,
        final_price=remote.final_price,
        currency=str(settings.YANDEX_DELIVERY_CURRENCY),
        warnings=remote.warnings,
        error_codes=remote.error_codes,
        courier_name=COURIER_NAME if with_courier else "",
        courier_vehicle=COURIER_VEHICLE if with_courier else "",
        destination_point_id=protocol.DESTINATION_POINT_ID,
        external_order_id=remote.external_order_id,
        taxi_class=remote.taxi_class,
        item_extra_ids=(remote.external_order_id,),
    )


def _set_status(remote: _Remote, status: str) -> None:
    remote.status = status
    remote.updated_ts = timeutils.now()
    if status in _PRICED_ENDS and remote.final_price is None:
        remote.final_price = (
            remote.final_price_override if remote.final_price_override is not None else remote.offer_price
        )


def _step(remote: _Remote) -> None:
    """One read of an accepted claim: apply the next scripted status, if any."""
    if remote.expired:
        remote.expired = False
        remote.script.clear()
        remote.error_codes = ("offer_expired",)
        _set_status(remote, "failed")
        return
    if remote.script:
        _set_status(remote, remote.script.pop(0))


def advance(claim_id: str, status: str) -> Claim:
    """Set the claim's status (dev tools and tests); the scripted remainder is dropped. An unknown claim
    raises ``KeyError``."""
    with _lock:
        remote = _claims[claim_id]
        remote.script.clear()
        remote.expired = False
        _set_status(remote, status)
        return _snapshot(remote)


def _known(claim_id: str) -> _Remote:
    remote = _claims.get(claim_id)
    if remote is None:
        raise YandexRejected("not_found", "Yandex does not know this claim (fake)")
    return remote


def _int(value: object) -> int | None:
    """A scenario amount (an int or a digit string, as JSON gives it), else ``None``."""
    if isinstance(value, bool) or value is None or value == "":
        return None
    return int(str(value))


class FakeYandexDelivery:
    name = "yandex"

    def __init__(self, scenario: Mapping[str, Any] | None = None) -> None:
        self.scenario: Mapping[str, Any] = {} if _production() else (scenario or {})

    @staticmethod
    def _unavailable_in_production() -> None:
        if _production():
            raise YandexUnavailable(
                "Yandex Delivery is fake under the production settings: nothing is made up",
                outcome_unknown=False,
            )

    @staticmethod
    def _refused_in_production() -> None:
        if _production():
            raise YandexRejected(FAKE_MODE, "Yandex Delivery is fake under the production settings")

    # --- quotes ---------------------------------------------------------------------------------------------

    def tariffs(self, point: Point, *, deadline: Deadline | None = None) -> list[Tariff]:
        self._unavailable_in_production()
        protocol.tariffs_payload(point)  # the same validation as live
        names = [str(name) for name in (self.scenario.get("tariffs") or DEFAULT_TARIFFS)]
        return [Tariff(name=name, minimal_price=RATES[name][0] if name in RATES else None) for name in names]

    def _estimate(self, source: Point, destination: Point, taxi_class: str) -> int:
        override = _int(self.scenario.get("price"))
        return override if override is not None else price_for(source, destination, taxi_class)

    def check_price(
        self,
        source: Point,
        destination: Point,
        item: ParcelItem,
        taxi_class: str,
        *,
        deadline: Deadline | None = None,
    ) -> Estimate:
        self._refused_in_production()
        try:
            protocol.check_price_payload(source, destination, item, taxi_class)
        except ValueError as exc:
            raise YandexRejected("invalid_request", str(exc)) from exc
        answer = self.scenario.get("check_price")
        if answer == "rejected":
            raise YandexRejected("errors.suitable_offer_not_found", "No suitable vehicles (fake)")
        if answer == "no_tariff":
            raise YandexRejected("estimating.tariff.no_categories_in_zone", "No tariff in this zone (fake)")
        if answer == "down":
            raise YandexUnavailable("Yandex did not answer (fake: check_price down)", outcome_unknown=False)
        price = self._estimate(source, destination, taxi_class)
        km = distance_km(source, destination)
        return Estimate(
            price=price,
            currency=str(settings.YANDEX_DELIVERY_CURRENCY),
            eta_minutes=15 + math.ceil(3 * km),
            distance_m=round(km * 1000),
            zone_id="tashkent",
            raw_price=f"{price}.0000",
        )

    # --- claims ---------------------------------------------------------------------------------------------

    def create_claim(self, request: ClaimRequest, *, deadline: Deadline | None = None) -> Claim:
        self._refused_in_production()
        with _lock:
            known = _requests.get(request.request_id)
            if known is not None:
                return _snapshot(_claims[known])
        try:
            protocol.create_payload(request, currency=str(settings.YANDEX_DELIVERY_CURRENCY))
        except ValueError as exc:
            raise YandexRejected("invalid_request", str(exc)) from exc
        answer = self.scenario.get("create")
        if answer == "rejected":
            raise YandexRejected("address_not_found", "Address not found (fake)")
        if answer == "down":
            raise YandexUnavailable("Yandex did not answer (fake: create down)", outcome_unknown=False)
        now = timeutils.now()
        offer = _int(self.scenario.get("offer_price"))
        if offer is None:
            offer = self._estimate(request.source.point, request.destination.point, request.taxi_class)
        estimate = self.scenario.get("estimate")
        status = {"failed": "estimating_failed", "pending": "estimating"}.get(
            str(estimate), "ready_for_approval"
        )
        priced = status == "ready_for_approval"
        remote = _Remote(
            claim_id=claim_id_for(request.request_id),
            request_id=request.request_id,
            status=status,
            taxi_class=request.taxi_class,
            external_order_id=request.external_order_id,
            offer_price=offer if priced else None,
            offer_valid_until=now + timedelta(minutes=OFFER_VALID_MINUTES) if priced else None,
            updated_ts=now,
            script=[str(step) for step in (self.scenario.get("statuses") or DEFAULT_SCRIPT)],
            final_price_override=_int(self.scenario.get("final_price")),
            warnings=tuple(str(code) for code in (self.scenario.get("warnings") or ())),
            error_codes=("estimating.cant_construct_route",) if status == "estimating_failed" else (),
        )
        with _lock:
            known = _requests.get(request.request_id)
            if known is not None:  # a concurrent replay won
                return _snapshot(_claims[known])
            _claims[remote.claim_id] = remote
            _requests[request.request_id] = remote.claim_id
            snapshot = _snapshot(remote)
        if answer == "down_after_create":
            raise YandexUnavailable("Yandex did not answer after creating the claim (fake)")
        return snapshot

    def claim(self, claim_id: str, *, deadline: Deadline | None = None) -> Claim:
        self._unavailable_in_production()
        with _lock:
            remote = _known(claim_id)
            if self.scenario.get("info") == "not_found":
                raise YandexRejected("not_found", "Yandex does not know this claim (fake: info not_found)")
            self._read_step(remote)
            return _snapshot(remote)

    @staticmethod
    def _read_step(remote: _Remote) -> None:
        """A read moves only an accepted claim along its script (an estimate waits for :func:`advance`)."""
        if remote.expired or remote.status not in ("new", "estimating", "ready_for_approval"):
            _step(remote)

    def claims(self, claim_ids: list[str], *, deadline: Deadline | None = None) -> list[Claim]:
        self._unavailable_in_production()
        found: list[Claim] = []
        with _lock:
            for claim_id in dict.fromkeys(claim_ids):
                remote = _claims.get(claim_id)
                if remote is None:
                    continue
                self._read_step(remote)
                found.append(_snapshot(remote))
        return found

    def accept(self, claim_id: str, version: int, *, deadline: Deadline | None = None) -> Claim | None:
        self._unavailable_in_production()
        answer = self.scenario.get("accept")
        if answer == "down":
            raise YandexUnavailable("Yandex did not answer (fake: accept down)", outcome_unknown=False)
        if answer == "timeout":  # the answer is lost; this fake accepted nothing
            raise YandexUnavailable("Yandex did not answer in time (fake: accept timeout)")
        with _lock:
            remote = _known(claim_id)
            remote.accepts += 1
            if answer == "down_once" and remote.accepts == 1:
                raise YandexUnavailable(
                    "Yandex did not answer (fake: accept down once)", outcome_unknown=False
                )
            if answer == "old_version_once" and remote.accepts == 1:
                remote.version += 1
                remote.updated_ts = timeutils.now()
                raise YandexConflict("old_version", "The claim was edited meanwhile (fake)")
            if answer == "conflict" or remote.status != "ready_for_approval":
                raise YandexConflict("inappropriate_status", "The claim is not awaiting confirmation (fake)")
            if version != remote.version:
                raise YandexConflict("old_version", "Another claim version (fake)")
            _set_status(remote, "accepted")
            remote.expired = answer == "expired"
            return replace(_snapshot(remote), offer_price=None, offer_valid_until=None, currency="")

    # --- cancellation and return ----------------------------------------------------------------------------

    def _terms(self, remote: _Remote) -> CancelTerms:
        forced = self.scenario.get("cancel")
        if forced in (CANCEL_FREE, CANCEL_PAID, CANCEL_UNAVAILABLE):
            state: CancelState = forced
        elif remote.status in _FINISHED:
            state = CANCEL_UNAVAILABLE
        elif remote.status in _FREE_CANCEL:
            state = CANCEL_FREE
        elif remote.status in _PAID_CANCEL:
            state = CANCEL_PAID
        else:
            state = CANCEL_UNAVAILABLE
        return CancelTerms(state=state, price=remote.offer_price if state == CANCEL_PAID else None)

    def _cancel_down(self) -> None:
        if self.scenario.get("cancel") == "down":
            raise YandexUnavailable("Yandex did not answer (fake: cancel down)", outcome_unknown=False)

    def cancel_terms(self, claim_id: str, *, deadline: Deadline | None = None) -> CancelTerms:
        self._unavailable_in_production()
        self._cancel_down()
        with _lock:
            return self._terms(_known(claim_id))

    def cancel(
        self, claim_id: str, version: int, state: CancelState, *, deadline: Deadline | None = None
    ) -> None:
        self._unavailable_in_production()
        protocol.cancel_payload(version, state)
        self._cancel_down()
        with _lock:
            remote = _known(claim_id)
            terms = self._terms(remote)
            if terms.state == CANCEL_UNAVAILABLE:
                raise YandexConflict("inappropriate_status", "The claim can no longer be cancelled (fake)")
            if version != remote.version:
                raise YandexConflict("old_version", "Another claim version (fake)")
            if state != terms.state:
                code = "free_cancel_is_unavailable" if state == CANCEL_FREE else "state_mismatch"
                raise YandexConflict(code, "Other cancellation terms apply (fake)")
            remote.script.clear()
            remote.expired = False
            if state == CANCEL_PAID:
                remote.final_price = terms.price
                _set_status(remote, "cancelled_with_payment")
            else:
                _set_status(remote, "cancelled")

    def request_return(
        self, claim_id: str, point_id: int, comment: str, *, deadline: Deadline | None = None
    ) -> None:
        self._unavailable_in_production()
        with _lock:
            remote = _known(claim_id)
            if point_id != protocol.DESTINATION_POINT_ID:
                raise YandexRejected("state_mismatch", "Incorrect point (fake)")
            if remote.status not in _RETURNABLE:
                raise YandexConflict("state_mismatch", "The courier has not picked the parcel up (fake)")
            if self.scenario.get("return") == "refused":
                raise YandexConflict("not_allowed", "Return not allowed (fake)")
            remote.script = list(RETURN_SCRIPT)
            _set_status(remote, "returning")

    def tracking_link(self, claim_id: str, *, deadline: Deadline | None = None) -> str | None:
        self._unavailable_in_production()
        with _lock:
            remote = _known(claim_id)
            if remote.status in _BEFORE_COURIER or remote.status in _FINISHED:
                return None
            return TRACKING_BASE + remote.claim_id
