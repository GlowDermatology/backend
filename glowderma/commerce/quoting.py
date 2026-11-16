"""Delivery options of the customer API (plan §3.4): which carriers and methods can take the cart to an
address, at what fee and when — and the re-check of a stored quote at checkout.

Everything here that calls a carrier (:func:`delivery_options`, :func:`checkout_quote`) runs **outside** any
transaction: the views call it in ``post()`` before their ``@idempotent`` inner method. The in-transaction
re-check (:func:`method_usable`) reads the database only.

Rules:

- Yandex is offered when ``core.integration_modes.delivery_providers()`` lists it and the customer is in
  ``YANDEX_DELIVERY_PILOT_CUSTOMER_IDS`` (when that list is set); a method switched off in
  ``DeliverySettings`` is **omitted** (the list stays stable for the customer: a method is listed as
  unavailable only when it is switched on but cannot take this order).
- Per enabled method, the first refusal wins: ``not_configured`` (warehouse point A incomplete),
  ``dispatch_suspended``, ``coordinates_required`` (no pin on the address), ``too_heavy`` (parcel weight =
  default item weight × total quantity above the class limit), ``warehouse_closed`` (no open day within the
  horizon), then Yandex ``check-price`` (one shared customer-path deadline for both classes):
  ``YandexUnavailable`` → ``provider_unavailable``, a documented refusal → ``out_of_zone``. An unavailable
  option never carries a price.
- An available Yandex option is stored as a ``DeliveryQuote`` (15 min) and reused while it is unexpired and
  unused for the same customer, address snapshot, method, weight and class.
- BTS is computed exactly as the released ``delivery/calculate/`` does (its region/city reference list, then
  its tariff); without BTS codes, or for a route BTS does not serve, it is ``out_of_zone``; a BTS outage is
  ``provider_unavailable``. A BTS option has no quote id (checkout without one stays the BTS path).
- Default selection: Yandex Standard, else Yandex Express, else BTS (the first available in that order).
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from django.conf import settings

from glowderma.commerce import selectors
from glowderma.core import enums, i18n, integration_modes, timeutils
from glowderma.delivery import pricing
from glowderma.delivery import quotes as delivery_quotes
from glowderma.delivery import selectors as delivery_selectors
from glowderma.delivery import services as delivery_services
from glowderma.delivery.models import DeliverySettings
from glowderma.delivery.quotes import AddressSnapshot, QuoteRow
from glowderma.delivery.warehouse import EtaWindow, WarehouseCalendar
from glowderma.integrations.bts.ports import BtsRejected, BtsUnavailable
from glowderma.integrations.bts.registry import get_bts
from glowderma.integrations.httpclient import Deadline
from glowderma.integrations.yandex_delivery.ports import (
    CUSTOMER_PATH_SECONDS,
    ParcelItem,
    Point,
    YandexError,
    YandexRejected,
    YandexUnavailable,
)
from glowderma.integrations.yandex_delivery.registry import get_yandex_delivery

logger = logging.getLogger(__name__)

_P = enums.DeliveryProvider
_M = enums.DeliveryMethod
_R = enums.DeliveryUnavailableReason
YANDEX_METHODS: tuple[str, ...] = (_M.STANDARD, _M.EXPRESS)  # also the default preference order
# Yandex refusals that say nothing about the destination (our request, the fake under production)
_NOT_ZONE_CODES = frozenset({"invalid_request", "fake_mode"})


class QuoteExpired(Exception):
    """The quote can no longer pay for an order and no equal fresh quote exists (409 ``quote_expired``).
    ``options`` are the fresh options when a re-quote ran (``None`` otherwise)."""

    def __init__(self, options: Sequence[DeliveryOption] | None = None) -> None:
        super().__init__("quote_expired")
        self.options = list(options) if options is not None else None


class DeliveryFeeChanged(Exception):
    """A re-quote of the same method gives another fee (409 ``price_changed`` with the fresh options)."""

    def __init__(self, option: DeliveryOption, options: Sequence[DeliveryOption]) -> None:
        super().__init__("price_changed")
        self.option = option
        self.options = list(options)


@dataclass(frozen=True, slots=True)
class QuoteRequest:
    """What the options are computed for: the customer, the destination, the cart (total quantity → weight,
    the items subtotal) and the cashback the customer wants to use (→ ``expected_total``)."""

    customer_id: int
    address: AddressSnapshot
    quantity: int
    items_subtotal: int
    cashback_available: int
    cashback_to_use: int
    lang: str
    now: datetime
    scenario: Mapping[str, Any] | None = None  # X-GD-Fake-Scenario (local/test only)


@dataclass(frozen=True, slots=True)
class DeliveryOption:
    provider: str  # DeliveryProvider
    method: str | None  # DeliveryMethod; None for BTS
    label: str
    available: bool
    unavailable_reason: str | None  # DeliveryUnavailableReason
    fee: int | None
    eta_from: datetime | None
    eta_to: datetime | None
    scheduled: bool
    quote_id: UUID | None
    expires_at: datetime | None
    expected_total: int | None
    weight_g: int | None = None  # the parcel weight the fee is for (not part of ``options[]``)

    def as_dict(self) -> dict[str, Any]:
        """One ``options[]`` entry of ``delivery/calculate/``."""
        return {
            "provider": self.provider,
            "method": self.method,
            "label": self.label,
            "available": self.available,
            "unavailable_reason": self.unavailable_reason,
            "fee": self.fee,
            "eta_from": self.eta_from,
            "eta_to": self.eta_to,
            "scheduled": self.scheduled,
            "quote_id": self.quote_id,
            "expires_at": self.expires_at,
            "expected_total": self.expected_total,
        }


@dataclass(frozen=True, slots=True)
class CheckoutQuote:
    """The quote checkout uses (checked, or freshly re-quoted at an unchanged fee)."""

    quote_id: UUID
    method: str
    fee: int


# --- helpers ------------------------------------------------------------------------------------------------


def settings_row() -> DeliverySettings:
    """The stored delivery settings (defaults, never created, when the row is missing)."""
    return DeliverySettings.objects.filter(pk=1).first() or DeliverySettings()


def parcel_weight_g(quantity: int, row: DeliverySettings | None = None) -> int:
    """The parcel weight of ``quantity`` units: the configured default item weight × quantity (≥ 1 g)."""
    return max(1, int((row or settings_row()).default_item_weight_g) * max(1, int(quantity)))


def yandex_offered(customer_id: int) -> bool:
    """Yandex may serve this customer at all: the provider is available (``core.integration_modes``) and the
    pilot list, when set, names the customer."""
    if _P.YANDEX not in integration_modes.delivery_providers():
        return False
    pilot = list(getattr(settings, "YANDEX_DELIVERY_PILOT_CUSTOMER_IDS", []) or [])
    return not pilot or customer_id in pilot


def option_label(provider: str, method: str | None, lang: str) -> str:
    if provider == _P.YANDEX and method:
        return i18n.t(f"label.delivery_option.yandex_{method}", lang)
    return i18n.t("label.delivery_option.bts", lang)


def _expected_total(request: QuoteRequest, fee: int) -> int:
    maximum = selectors.cashback_max(request.items_subtotal, fee, request.cashback_available)
    return request.items_subtotal + fee - min(request.cashback_to_use, maximum)


def _unavailable(request: QuoteRequest, provider: str, method: str | None, reason: str) -> DeliveryOption:
    return DeliveryOption(
        provider=provider,
        method=method,
        label=option_label(provider, method, request.lang),
        available=False,
        unavailable_reason=reason,
        fee=None,
        eta_from=None,
        eta_to=None,
        scheduled=False,
        quote_id=None,
        expires_at=None,
        expected_total=None,
    )


def _from_quote(request: QuoteRequest, quote: QuoteRow) -> DeliveryOption:
    return DeliveryOption(
        provider=quote.provider,
        method=quote.method,
        label=option_label(quote.provider, quote.method, request.lang),
        available=True,
        unavailable_reason=None,
        fee=quote.customer_fee,
        eta_from=quote.eta_from,
        eta_to=quote.eta_to,
        scheduled=quote.scheduled,
        quote_id=quote.id,
        expires_at=quote.expires_at,
        expected_total=_expected_total(request, quote.customer_fee),
        weight_g=quote.weight_g,
    )


def _refusal(
    request: QuoteRequest,
    calendar: WarehouseCalendar,
    method: delivery_selectors.MethodSettings,
    weight_g: int,
) -> tuple[str | None, EtaWindow | None]:
    """The first reason the enabled ``method`` cannot take this order before asking Yandex, and the window."""
    if not calendar.configured:
        return _R.NOT_CONFIGURED, None
    if calendar.suspended_at(request.now):
        return _R.DISPATCH_SUSPENDED, None
    if not request.address.has_pin:
        return _R.COORDINATES_REQUIRED, None
    limit = delivery_quotes.CLASS_WEIGHT_LIMIT_G.get(method.taxi_class)
    if limit is not None and weight_g > limit:
        return _R.TOO_HEAVY, None
    window = calendar.eta_window(method.eta_min_minutes, method.eta_max_minutes, request.now)
    if window is None:
        return _R.WAREHOUSE_CLOSED, None
    return None, window


def _account_failure(exc: YandexError) -> str | None:
    """A check-price failure that is the account's (401/403, a payment refusal): counted on
    ``IntegrationState(yandex)`` (``attention.yandex_failing``; the third in a row alerts
    ``delivery.provider_failing``). A timeout or an address Yandex does not serve is not counted."""
    code = delivery_services.yandex_account_failure(exc)
    if code is not None:
        delivery_services.record_yandex_failure("check_price", code)
    return code


def _yandex_options(request: QuoteRequest) -> list[DeliveryOption]:
    if not yandex_offered(request.customer_id):
        return []
    row = settings_row()
    methods = [
        method
        for method in (delivery_selectors.method_settings(m, row) for m in YANDEX_METHODS)
        if method.enabled
    ]
    if not methods:
        return []
    calendar = delivery_selectors.warehouse_calendar(timeutils.local_date(request.now))
    weight_g = parcel_weight_g(request.quantity, row)
    scenario = request.scenario.get("yandex") if request.scenario else None
    port = get_yandex_delivery(scenario if isinstance(scenario, Mapping) else None)
    deadline = Deadline(CUSTOMER_PATH_SECONDS)  # one budget across both classes
    options: list[DeliveryOption] = []
    for method in methods:
        reason, window = _refusal(request, calendar, method, weight_g)
        if reason is not None or window is None:
            options.append(_unavailable(request, _P.YANDEX, method.method, reason or _R.WAREHOUSE_CLOSED))
            continue
        reused = delivery_quotes.reusable_quote(
            request.customer_id,
            address=request.address,
            method=method.method,
            weight_g=weight_g,
            taxi_class=method.taxi_class,
            now=request.now,
        )
        if reused is not None and reused.scheduled == window.scheduled:
            options.append(_from_quote(request, reused))
            continue
        assert request.address.latitude is not None and request.address.longitude is not None  # noqa: S101
        source = Point(
            lon=float(row.warehouse_longitude or 0),
            lat=float(row.warehouse_latitude or 0),
            fullname=row.warehouse_address,
        )
        destination = Point(
            lon=float(request.address.longitude),
            lat=float(request.address.latitude),
            fullname=request.address.text,
        )
        item = ParcelItem(
            title="GlowDerma",
            quantity=1,
            weight_kg=pricing_weight_kg(weight_g),
        )
        try:
            estimate = port.check_price(source, destination, item, method.taxi_class, deadline=deadline)
        except YandexUnavailable as exc:
            logger.warning("delivery.quote_unavailable provider=yandex method=%s reason=down", method.method)
            _account_failure(exc)
            options.append(_unavailable(request, _P.YANDEX, method.method, _R.PROVIDER_UNAVAILABLE))
            continue
        except YandexRejected as exc:
            account = _account_failure(exc)
            reason = (
                _R.PROVIDER_UNAVAILABLE
                if exc.code in _NOT_ZONE_CODES or account is not None
                else _R.OUT_OF_ZONE
            )
            logger.info(
                "delivery.quote_refused provider=yandex method=%s code=%s", method.method, exc.code[:80]
            )
            options.append(_unavailable(request, _P.YANDEX, method.method, reason))
            continue
        except YandexError:
            logger.warning("delivery.quote_unavailable provider=yandex method=%s reason=error", method.method)
            options.append(_unavailable(request, _P.YANDEX, method.method, _R.PROVIDER_UNAVAILABLE))
            continue
        delivery_services.record_yandex_success()  # an answered price check ends a failure run, if any
        fee = pricing.customer_fee(
            estimate.price, markup_percent=row.markup_percent, vat_percent=row.estimate_vat_percent
        )
        quote = delivery_quotes.create_quote(
            request.customer_id,
            address=request.address,
            method=method.method,
            carrier_price=estimate.price,
            customer_fee=fee,
            weight_g=weight_g,
            eta_from=window.eta_from,
            eta_to=window.eta_to,
            scheduled=window.scheduled,
            meta={
                "zone_id": estimate.zone_id[:64],
                "taxi_class": method.taxi_class,
                "eta_minutes": estimate.eta_minutes,
                "distance_m": estimate.distance_m,
            },
            now=request.now,
        )
        options.append(_from_quote(request, quote))
    return options


def pricing_weight_kg(weight_g: int) -> Decimal:
    """Grams → kilograms as a ``Decimal`` with three places (what ``ParcelItem.weight_kg`` takes)."""
    return (Decimal(weight_g) / Decimal(1000)).quantize(Decimal("0.001"))


def _bts_option(request: QuoteRequest) -> DeliveryOption | None:
    """BTS exactly as the released ``delivery/calculate/``: its reference list, then its tariff."""
    if not integration_modes.delivery_available():
        return None
    region, city = request.address.region_code, request.address.city_code
    if not region or not city:
        return _unavailable(request, _P.BTS, None, _R.OUT_OF_ZONE)
    try:
        known = delivery_selectors.region_known(region, city)
    except BtsUnavailable:
        return _unavailable(request, _P.BTS, None, _R.PROVIDER_UNAVAILABLE)
    if not known:
        return _unavailable(request, _P.BTS, None, _R.OUT_OF_ZONE)
    scenario = request.scenario.get("bts") if request.scenario else None
    try:
        fee = get_bts(scenario if isinstance(scenario, dict) else None).quote(region, city)
    except BtsRejected:
        return _unavailable(request, _P.BTS, None, _R.OUT_OF_ZONE)
    except BtsUnavailable:
        return _unavailable(request, _P.BTS, None, _R.PROVIDER_UNAVAILABLE)
    return DeliveryOption(
        provider=_P.BTS,
        method=None,
        label=option_label(_P.BTS, None, request.lang),
        available=True,
        unavailable_reason=None,
        fee=fee,
        eta_from=None,
        eta_to=None,
        scheduled=False,
        quote_id=None,
        expires_at=None,
        expected_total=_expected_total(request, fee),
        weight_g=int(settings.BTS_DEFAULT_PARCEL_WEIGHT_G),
    )


# --- public -------------------------------------------------------------------------------------------------


def delivery_options(request: QuoteRequest) -> list[DeliveryOption]:
    """Every option for the request, Yandex methods first (standard, express), then BTS. Calls the carriers:
    never inside a transaction."""
    options = _yandex_options(request)
    bts = _bts_option(request)
    if bts is not None:
        options.append(bts)
    return options


def select_option(
    options: Sequence[DeliveryOption], provider: str | None = None, method: str | None = None
) -> DeliveryOption | None:
    """The option the customer asked for (``provider`` and/or ``method``; a method alone means Yandex) when it
    is available, else the default: Yandex Standard, Yandex Express, BTS. ``None`` when none is available."""
    available = [option for option in options if option.available]
    if provider or method:
        wanted_provider = provider or _P.YANDEX
        for option in available:
            if option.provider != wanted_provider:
                continue
            if wanted_provider == _P.YANDEX and method and option.method != method:
                continue
            return option
    for wanted in ((_P.YANDEX, _M.STANDARD), (_P.YANDEX, _M.EXPRESS), (_P.BTS, None)):
        for option in available:
            if (option.provider, option.method) == wanted:
                return option
    return None


def method_usable(customer_id: int, method: str, now: datetime) -> bool:
    """The Yandex ``method`` can still take an order now (database only, for the checkout transaction):
    Yandex offered to the customer, the method switched on, the warehouse configured, dispatch not
    suspended."""
    if method not in YANDEX_METHODS or not yandex_offered(customer_id):
        return False
    row = settings_row()
    if not delivery_selectors.method_settings(method, row).enabled:
        return False
    if not delivery_selectors.warehouse_configured(row):
        return False
    calendar = delivery_selectors.warehouse_calendar(timeutils.local_date(now))
    return not calendar.suspended_at(now)


def _quote_still_good(request: QuoteRequest, quote: QuoteRow) -> bool:
    if quote.provider != _P.YANDEX or not quote.usable_at(request.now):
        return False
    if not quote.address.same_place(request.address):
        return False
    row = settings_row()
    if quote.weight_g != parcel_weight_g(request.quantity, row):
        return False
    if not method_usable(request.customer_id, quote.method, request.now):
        return False
    method = delivery_selectors.method_settings(quote.method, row)
    if quote.taxi_class and quote.taxi_class != method.taxi_class:
        return False
    calendar = delivery_selectors.warehouse_calendar(timeutils.local_date(request.now))
    window = calendar.eta_window(method.eta_min_minutes, method.eta_max_minutes, request.now)
    return window is not None and window.scheduled == quote.scheduled


def checkout_quote(request: QuoteRequest, quote_id: UUID) -> CheckoutQuote:
    """Checkout's ``post()`` (no transaction): the customer's quote ``quote_id`` when it is still good for
    this address, cart and moment; otherwise a re-quote of the same method — an equal fee proceeds with the
    fresh quote, another fee raises :class:`DeliveryFeeChanged`, no available option :class:`QuoteExpired`.
    An unknown or foreign id raises :class:`QuoteExpired` without asking any carrier."""
    quote = delivery_quotes.get_quote(quote_id, request.customer_id)
    if quote is None or quote.provider != _P.YANDEX:
        raise QuoteExpired()
    if _quote_still_good(request, quote):
        return CheckoutQuote(quote_id=quote.id, method=quote.method, fee=quote.customer_fee)
    options = delivery_options(request)
    fresh = next(
        (
            option
            for option in options
            if option.provider == _P.YANDEX and option.method == quote.method and option.available
        ),
        None,
    )
    if fresh is None or fresh.quote_id is None or fresh.fee is None:
        raise QuoteExpired(options)
    if fresh.fee != quote.customer_fee:
        raise DeliveryFeeChanged(fresh, options)
    return CheckoutQuote(quote_id=fresh.quote_id, method=quote.method, fee=fresh.fee)


__all__ = [
    "YANDEX_METHODS",
    "AddressSnapshot",
    "CheckoutQuote",
    "DeliveryFeeChanged",
    "DeliveryOption",
    "QuoteExpired",
    "QuoteRequest",
    "checkout_quote",
    "delivery_options",
    "method_usable",
    "option_label",
    "parcel_weight_g",
    "select_option",
    "settings_row",
    "yandex_offered",
]
