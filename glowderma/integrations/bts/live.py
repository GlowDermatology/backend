"""Live BTS adapter (``BTS_MODE=live``; customer contract §2.14.3, ``docs/integrations/bts.md``).

The endpoints, the status translation and the payloads are in :mod:`.protocol`. Every port call shares one
:class:`~glowderma.integrations.httpclient.Deadline`: 10 s for the outbox and polling calls, but
:data:`REQUEST_PATH_SECONDS` (4 s) for ``quote()``, which a customer request waits for. No caller holds the
Order row lock across a BTS call (``commerce.tasks.create_shipment`` commits its checks first, calls BTS, then
re-locks to record).

Auth: ``POST /auth/login`` → a bearer token kept only in the Django cache
(``gd:bts:token:<sha256(base URL|login)[:16]>``, 23 h) and fetched single-flight across processes
(:class:`~glowderma.integrations.tokens.SharedToken`: fails closed when the cache is down, a refused login is
remembered for a minute); a 401 renews it once and repeats the request (a refused request was not processed).
A login BTS refuses is an ERROR ``bts.auth_failed`` and :class:`BtsUnavailable` (a credentials problem is not
the parcel's fault).

Failure mapping: no answer (connect failure, timeout, broken or undecodable body), HTTP 5xx, and an answer
that says the request was **not processed** (a redirect — never followed —, 408 / 425 / 429, 401 / 403) →
:class:`BtsUnavailable`; an answer of a shape the adapter cannot read → :class:`BtsUnavailable`; another
HTTP 4xx → :class:`BtsRejected` (``route_not_served`` for a quote, ``validation_error`` for a create the data
of which BTS refuses, ``parcel_not_found`` for an unknown parcel). Exceptions: ``regions()`` and
``find_by_client_id()`` raise only :class:`BtsUnavailable` (a reference list or a lookup that failed proves
nothing), and ``cancel()`` answers a refusal as ``CancelResult(ok=False)`` (an already cancelled parcel is
``ok``). BTS answers some data errors of a create with HTTP 500 ("Failed to create order"); those are retried
like any 5xx until verified (verify M3).

``clientId`` is ``BTS_CLIENT_ID_PREFIX`` + our order id (the prefix tells environments sharing a BTS account
apart; empty = the bare order id); ``ready_to_take`` is ``BTS_READY_TO_TAKE``. A quote is cached for
:data:`QUOTE_CACHE_SECONDS` per (sender city, city, weight, pickup type).

Nothing personal or secret is logged: no token, password, phone, name or address — log lines carry the
endpoint path and the HTTP status only.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from typing import Any

import httpx
from django.conf import settings
from django.core.cache import cache

from glowderma.core import timeutils

from .. import httpclient
from ..tokens import LoginRefused, SharedToken, TokenBusy, account_scope
from . import protocol
from .ports import (
    BtsError,
    BtsRejected,
    BtsUnavailable,
    CancelResult,
    City,
    CreateRequest,
    Parcel,
    Region,
)

logger = logging.getLogger(__name__)
PROVIDER = "bts"
CITY_FETCH_WORKERS = 4
REQUEST_PATH_SECONDS = 4.0  # quote(): a customer request waits for it
QUOTE_CACHE_SECONDS = 300

# What reading an unexpected answer may raise; mapped to BtsUnavailable, never an untyped exception.
_SHAPE_ERRORS = (KeyError, TypeError, ValueError, AttributeError, OverflowError, IndexError)


def _readable[T](what: str, read: Callable[[], T]) -> T:
    """``read()``, with an answer of an unexpected shape reported as :class:`BtsUnavailable`."""
    try:
        return read()
    except BtsError:
        raise
    except _SHAPE_ERRORS as exc:
        raise BtsUnavailable(f"BTS {what}: unreadable answer ({type(exc).__name__})") from exc


class LiveBts:
    name = "bts"

    def __init__(
        self,
        *,
        base_url: str,
        login: str,
        password: str,
        sender: protocol.Sender,
        pickup_type: str,
        weight_g: int,
        client_prefix: str = "",
        ready_to_take: bool = True,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.login = login
        self.password = password
        self.sender = sender
        self.pickup_type = pickup_type
        self.weight_g = weight_g
        self.client_prefix = client_prefix
        self.ready_to_take = ready_to_take
        self.transport = transport

    @classmethod
    def from_settings(cls) -> LiveBts:
        return cls(
            base_url=str(settings.BTS_BASE_URL),
            login=str(settings.BTS_LOGIN),
            password=str(settings.BTS_PASSWORD),
            sender=protocol.Sender(
                name=str(settings.BTS_SENDER_NAME),
                phone=str(settings.BTS_SENDER_PHONE),
                address=str(settings.BTS_SENDER_ADDRESS),
                city_code=str(settings.BTS_SENDER_CITY_CODE),
                branch_code=str(settings.BTS_SENDER_BRANCH_CODE),
            ),
            pickup_type=str(settings.BTS_PICKUP_TYPE),
            weight_g=int(settings.BTS_DEFAULT_PARCEL_WEIGHT_G),
            client_prefix=str(getattr(settings, "BTS_CLIENT_ID_PREFIX", "")),
            ready_to_take=bool(getattr(settings, "BTS_READY_TO_TAKE", True)),
        )

    # --- transport ------------------------------------------------------------------------------------------

    def _headers(self, token: str | None = None) -> dict[str, str]:
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "language": protocol.LANGUAGE,
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return headers

    def _login(self, http: httpx.Client, deadline: httpclient.Deadline) -> str:
        try:
            answer = httpclient.send(
                http,
                "POST",
                self.base_url + protocol.PATH_LOGIN,
                provider=PROVIDER,
                deadline=deadline,
                json={"login": self.login, "password": self.password},
                headers=self._headers(),
            )
        except httpclient.TransportFailure as exc:
            raise BtsUnavailable(f"BTS login: {exc}") from exc
        data = answer.json_object().get("data")
        token = data.get("access_token") if isinstance(data, dict) else None
        if answer.ok and isinstance(token, str) and token:
            return token
        if answer.status in (400, 401, 403, 422):
            logger.error("bts.auth_failed status=%s (check BTS_LOGIN / BTS_PASSWORD)", answer.status)
            raise LoginRefused(f"BTS refused the login: HTTP {answer.status}")
        raise BtsUnavailable(f"BTS login: HTTP {answer.status}")

    def _tokens(self, http: httpx.Client) -> SharedToken:
        return SharedToken(
            "bts",
            ttl_seconds=protocol.TOKEN_TTL_SECONDS,
            fetch=lambda stale, bounded: self._login(http, bounded),
            scope=account_scope(self.base_url, self.login),
        )

    def _call(
        self,
        http: httpx.Client,
        deadline: httpclient.Deadline,
        method: str,
        path: str,
        **kwargs: Any,
    ) -> httpclient.Answer:
        """One authenticated request; a 401 renews the token once. Transport failures, an answer that says
        "not processed" and HTTP 5xx → BtsUnavailable; every other answer is returned."""
        tokens = self._tokens(http)
        try:
            token = tokens.get(deadline)
            answer = self._send(http, deadline, method, path, token, **kwargs)
            if answer.status == 401:
                token = tokens.renew(token, deadline)
                answer = self._send(http, deadline, method, path, token, **kwargs)
                if answer.status == 401:
                    logger.error("bts.auth_failed status=401 after a fresh login")
        except (TokenBusy, LoginRefused) as exc:
            raise BtsUnavailable(str(exc)) from exc
        if answer.not_processed:
            raise BtsUnavailable(f"BTS {path}: HTTP {answer.status} (not processed)")
        if answer.status >= 500:
            raise BtsUnavailable(f"BTS {path}: HTTP {answer.status}")
        return answer

    def _send(
        self,
        http: httpx.Client,
        deadline: httpclient.Deadline,
        method: str,
        path: str,
        token: str,
        **kwargs: Any,
    ) -> httpclient.Answer:
        try:
            return httpclient.send(
                http,
                method,
                self.base_url + path,
                provider=PROVIDER,
                deadline=deadline,
                headers=self._headers(token),
                **kwargs,
            )
        except httpclient.TransportFailure as exc:
            raise BtsUnavailable(f"BTS {path}: {exc}") from exc

    @staticmethod
    def _message(answer: httpclient.Answer) -> str:
        return httpclient.safe_text(protocol.error_text(answer.json_object()))

    def _ok_data(
        self, answer: httpclient.Answer, path: str, codes: dict[int, str] | None = None, default: str = ""
    ) -> Any:
        """``data`` of a successful (2xx) answer. A 4xx, or ``"status": false`` → BtsRejected with
        ``codes[status]`` (else ``http_<status>``) and BTS's sanitised message (else ``default``); a 2xx that
        is not a JSON object → BtsUnavailable."""
        body = answer.json_object()
        if not answer.ok or body.get("status") is False:
            code = (codes or {}).get(answer.status, f"http_{answer.status}")
            raise BtsRejected(code, self._message(answer) or default or f"BTS refused: HTTP {answer.status}")
        if not body:
            raise BtsUnavailable(f"BTS {path}: answer is not a JSON object")
        return body.get("data")

    def _pages(
        self, http: httpx.Client, deadline: httpclient.Deadline, path: str, params: dict[str, str]
    ) -> list[dict[str, Any]]:
        """Every item of a paginated list. The next page is asked while ``_meta`` says there is one or
        ``_links.next`` names it; a **full** page with neither is unprovable (a list that does not say
        whether it ends) and fails closed. Any failure, or more than MAX_PAGES pages → BtsUnavailable."""
        items: list[dict[str, Any]] = []
        for page in range(1, protocol.MAX_PAGES + 1):
            query = {**params, "page": str(page), "per-page": str(protocol.PER_PAGE)}
            answer = self._call(http, deadline, "GET", path, params=query)
            data = self._ok_data(answer, path)
            page_items = data.get("items") if isinstance(data, dict) else None
            if not isinstance(page_items, list):
                raise BtsUnavailable(f"BTS {path}: answer without items")
            items.extend(item for item in page_items if isinstance(item, dict))
            more = self._more_pages(data, page_items)
            if more is None:
                raise BtsUnavailable(f"BTS {path}: a full page without pagination data")
            if not more:
                return items
        raise BtsUnavailable(f"BTS {path}: more than {protocol.MAX_PAGES} pages")

    @staticmethod
    def _more_pages(data: dict[str, Any], page_items: list[Any]) -> bool | None:
        """Whether another page follows: ``True`` / ``False``, or ``None`` when it cannot be told."""
        if not page_items:
            return False
        meta = data.get("_meta")
        current = protocol.bts_code(meta.get("currentPage")) if isinstance(meta, dict) else None
        count = protocol.bts_code(meta.get("pageCount")) if isinstance(meta, dict) else None
        if current is not None and count is not None:
            return current < count
        links = data.get("_links")
        following = links.get("next") if isinstance(links, dict) else None
        if following:
            return True
        if len(page_items) < protocol.PER_PAGE:
            return False  # a short page is the last one
        return None

    # --- port -----------------------------------------------------------------------------------------------

    def regions(self) -> list[Region]:
        deadline = httpclient.Deadline()
        try:
            with httpclient.new_client(self.transport) as http:
                region_items = self._pages(http, deadline, protocol.PATH_REGIONS, {})
                listed = _readable("regions", lambda: _coded(region_items))

                def cities(code: str) -> tuple[City, ...]:
                    items = self._pages(http, deadline, protocol.PATH_CITIES, {"regionCode": code})
                    pairs = _readable("cities", lambda: _coded(items))
                    return tuple(City(city, name) for city, name in pairs)

                workers = max(1, min(CITY_FETCH_WORKERS, len(listed)))
                with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="bts-cities") as pool:
                    city_lists = list(pool.map(cities, [code for code, _ in listed]))
        except BtsRejected as exc:  # a reference list BTS refuses is as unusable as one it does not send
            raise BtsUnavailable(f"BTS reference list: {exc.message}") from exc
        regions = [
            Region(code, name, cities) for (code, name), cities in zip(listed, city_lists, strict=True)
        ]
        if not regions:
            raise BtsUnavailable("BTS sent an empty region list")
        return regions

    def _quote_key(self, city_code: str) -> str:
        parts = "|".join(
            (self.base_url, self.sender.city_code, city_code, str(self.weight_g), self.pickup_type)
        )
        return "gd:bts:quote:" + hashlib.sha256(parts.encode()).hexdigest()[:24]

    def quote(self, region_code: str, city_code: str) -> int:
        key = self._quote_key(city_code)
        try:
            cached = cache.get(key)
        except Exception:  # noqa: BLE001 - a quote cache outage only costs a BTS call
            cached = None
        if isinstance(cached, int) and not isinstance(cached, bool):
            return cached
        price = self._quote(city_code)
        try:
            cache.set(key, price, QUOTE_CACHE_SECONDS)
        except Exception:  # noqa: BLE001
            logger.warning("bts quote cache unwritable")
        return price

    def _quote(self, city_code: str) -> int:
        deadline = httpclient.Deadline(REQUEST_PATH_SECONDS)
        payload = protocol.calculate_payload(
            sender_city_code=self.sender.city_code,
            city_code=city_code,
            pickup_type=self.pickup_type,
            weight_g=self.weight_g,
        )
        with httpclient.new_client(self.transport) as http:
            answer = self._call(http, deadline, "POST", protocol.PATH_CALCULATE, json=payload)
        refused = dict.fromkeys((200, 201, 400, 404, 422), protocol.ROUTE_NOT_SERVED)
        data = self._ok_data(answer, protocol.PATH_CALCULATE, refused, "BTS refused the quote")
        option = _readable("quote", lambda: protocol.quote_option(data, self.pickup_type))
        if option is None:
            raise BtsUnavailable("BTS quote: answer without a tariff for this direction")
        if option.get("available") is False:
            reason = httpclient.safe_text(option.get("reason")) or "BTS does not deliver to this area"
            raise BtsRejected(protocol.ROUTE_NOT_SERVED, reason)
        price = _readable("quote", lambda: protocol.price_of(option))
        if price is None:
            raise BtsUnavailable("BTS quote: answer without a usable price")
        return price

    def find_by_client_id(self, client_id: str, *, since: datetime) -> Parcel | None:
        """The newest live parcel BTS holds for our order ``client_id`` (deleted and expired parcels are
        skipped), scanning BTS's order list from the day before ``since`` (the shipment row's creation, before
        its first create request) to :data:`.protocol.LOOKUP_END_DAYS` days ahead — a few pages, not a month.
        A lookup that fails for any reason raises BtsUnavailable: absence must be proven before a create. More
        than one live parcel is an ERROR ``bts.duplicate_parcels`` (the newest is returned)."""
        today = timeutils.local_date(timeutils.now())
        begin = timeutils.local_date(since) - timedelta(days=1)
        params = {
            "beginDate": min(begin, today).isoformat(),
            "endDate": (today + timedelta(days=protocol.LOOKUP_END_DAYS)).isoformat(),
        }
        deadline = httpclient.Deadline()
        try:
            with httpclient.new_client(self.transport) as http:
                items = self._pages(http, deadline, protocol.PATH_LIST, params)
        except BtsRejected as exc:
            raise BtsUnavailable(f"BTS order lookup refused: {exc.message}") from exc
        parcels = _readable(
            "order list", lambda: [protocol.parcel_from_order(item, self.client_prefix) for item in items]
        )
        matches = [
            parcel
            for parcel in parcels
            if parcel is not None and parcel.client_id == client_id and not protocol.is_gone(parcel)
        ]
        if not matches:
            return None
        matches.sort(key=lambda parcel: int(parcel.bts_order_id), reverse=True)
        if len(matches) > 1:
            logger.error(
                "bts.duplicate_parcels %s",
                {"client_id": client_id, "bts_order_ids": [parcel.bts_order_id for parcel in matches]},
            )
        return matches[0]

    def create(self, request: CreateRequest) -> Parcel:
        deadline = httpclient.Deadline()
        payload = protocol.create_payload(
            request,
            sender=self.sender,
            pickup_type=self.pickup_type,
            weight_g=self.weight_g,
            client_id=protocol.client_id_for(request.client_id, self.client_prefix),
            ready_to_take=self.ready_to_take,
        )
        with httpclient.new_client(self.transport) as http:
            answer = self._call(http, deadline, "POST", protocol.PATH_CREATE, json=payload)
        refused = dict.fromkeys((200, 201, 400, 422), protocol.VALIDATION_ERROR)
        data = self._ok_data(answer, protocol.PATH_CREATE, refused, "BTS refused the parcel")
        parcel = _readable("create", lambda: protocol.parcel_from_order(data, self.client_prefix))
        if parcel is None:
            # BTS may have created it: the lookup before the next attempt adopts it instead of a duplicate
            raise BtsUnavailable("BTS create: answer without an orderId")
        return Parcel(
            client_id=request.client_id,
            tracking_number=parcel.tracking_number,
            bts_order_id=parcel.bts_order_id,
            tariff=parcel.tariff,
            status=parcel.status,
        )

    def cancel(self, parcel: Parcel) -> CancelResult:
        order_id = parcel.bts_order_id or parcel.tracking_number
        if not order_id.isascii() or not order_id.isdigit():
            return CancelResult(ok=False, status=parcel.status, reason="BTS order id unknown")
        deadline = httpclient.Deadline()
        with httpclient.new_client(self.transport) as http:
            answer = self._call(http, deadline, "GET", protocol.PATH_CANCEL, params={"orderId": order_id})
        body = answer.json_object()
        if not body:  # an answer we cannot read never reports a cancellation (nor a refusal)
            raise BtsUnavailable(f"BTS cancel: HTTP {answer.status} without a JSON object")
        cancelled = str(protocol.BTS_DELETED)
        if body.get("message") == protocol.ALREADY_CANCELLED:
            return CancelResult(ok=True, status=cancelled)
        if answer.ok and body.get("status") is True:
            data = body.get("data")
            status = data.get("status") if isinstance(data, dict) else None
            return CancelResult(ok=True, status=protocol.port_status(status) if status else cancelled)
        return CancelResult(ok=False, status=parcel.status, reason=self._message(answer) or "BTS refused")

    def track(self, tracking_number: str) -> Parcel:
        if not tracking_number.isascii() or not tracking_number.isdigit():
            raise BtsRejected(protocol.PARCEL_NOT_FOUND, "not a BTS order number")
        deadline = httpclient.Deadline()
        with httpclient.new_client(self.transport) as http:
            answer = self._call(
                http, deadline, "GET", protocol.PATH_TRACK, params={"orderId": tracking_number}
            )
        refused = {404: protocol.PARCEL_NOT_FOUND}
        data = self._ok_data(answer, protocol.PATH_TRACK, refused, "BTS does not know this parcel")
        if not isinstance(data, dict) or not isinstance(data.get("status"), dict):
            raise BtsUnavailable("BTS track: answer without a status")
        order_id = protocol.bts_code(data.get("orderId"))
        return Parcel(
            client_id="",
            tracking_number=tracking_number,
            bts_order_id=str(order_id) if order_id else tracking_number,
            tariff=None,
            status=protocol.port_status(data["status"]),
        )


def _coded(items: list[dict[str, Any]]) -> list[tuple[str, str]]:
    """``(code, name)`` of a reference list's items that carry a code (the name defaults to the code)."""
    return [
        (str(item["code"]), httpclient.safe_text(item.get("name") or item["code"], 120))
        for item in items
        if isinstance(item.get("code"), int | str) and item.get("code") not in ("", True, False)
    ]
