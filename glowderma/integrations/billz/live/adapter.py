"""The live Billz adapter (:class:`LiveBillz`): catalog listing and lookup, stock reads and the sale push."""

from __future__ import annotations

import logging
import math
import re
import time
from collections.abc import Iterator, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from django.core.cache import cache as default_cache
from django.core.cache.backends.base import BaseCache

from glowderma.core import alerts, timeutils
from glowderma.core.pii import pii_ref
from glowderma.integrations import selectors, services
from glowderma.integrations.billz.client import INTERACTIVE, BillzClient, BillzConfig, SafeCache
from glowderma.integrations.billz.live.product_fields import (
    BILLZ_ID,
    NAME_MAX,
    _number,
    barcode_of,
    in_shop,
    price_of,
    stock_of,
    text_of,
    volume_of,
)
from glowderma.integrations.billz.live.sale_fields import (
    ResolvedLine,
    _payments,
    aggregate,
    order_items,
    phone_digits,
    sale_ref,
    sale_state,
)
from glowderma.integrations.billz.live.sale_lock import SALE_LOCK_KEY, SALE_LOCK_TTL, SaleLock
from glowderma.integrations.billz.ports import (
    BillzCheckByHand,
    BillzError,
    BillzListingIncomplete,
    BillzOutOfTime,
    BillzPaymentPending,
    BillzProduct,
    BillzRejected,
    BillzSaleBusy,
    BillzShiftClosed,
    BillzUnavailable,
    ImportMode,
    SaleJournal,
    SaleProgress,
    SaleReceipt,
    SaleRequest,
    StockRead,
    refuse_refund,
    refuse_store_sale,
    refuse_webhook,
)
from glowderma.integrations.models import IntegrationState

logger = logging.getLogger("glowderma.integrations.billz.live")


UUID_PATTERN = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")


# Billz refuses page > 10 000 (llms-full "Синхронизация каталога").
MAX_PAGE = 10_000


# Re-read this much before the delta watermark (clock skew between us and Billz's updated_at).
DELTA_OVERLAP = timedelta(minutes=5)


WATERMARK_FORMAT = "%Y-%m-%d %H:%M:%S"  # last_updated_date: "yyyy-mm-dd hh:mm:ss", UTC


PRODUCT_ID_KEY = "billz:product:{barcode}"


PRODUCT_ID_TTL = 7 * 86_400


CLIENT_ID_KEY = "billz:client:{ref}"  # keyed by pii_ref(phone), never the number


CLIENT_ID_TTL = 30 * 86_400


# One push ends within this (every wait and timeout honours it): below the outbox lease of core.tasks (5 min),
# so a redelivery never meets a push still running, and well below SALE_LOCK_TTL, so the lock cannot expire.
PUSH_DEADLINE_SECONDS = 240.0


# Another worker holds the sale lock: look again this much later (no attempt spent; the lock expires anyway).
SALE_BUSY_RETRY = timedelta(minutes=1)


# A payment whose answer was lost is looked at again only after this quiet period (review B2).
PAYMENT_QUIET_PERIOD = timedelta(minutes=10)


SEARCH_LIMIT = 50


SEARCH_MAX_PAGES = 10


# The admin lookup (every search page together) ends within the interactive profile's budget.
LOOKUP_BUDGET_SECONDS = INTERACTIVE.call_budget or 25.0


PROVIDER = IntegrationState.Provider.BILLZ


class BillzSearchExhausted(BillzUnavailable):
    """One barcode's substring search filled every page Billz allows without an exact match: Billz answered,
    only that barcode cannot be confirmed (``read_stock`` reports it deferred and goes on)."""


class LiveBillz:
    name = "billz"

    def __init__(
        self,
        config: BillzConfig,
        *,
        client: BillzClient | None = None,
        interactive: BillzClient | None = None,
        cache: BaseCache | SafeCache | None = None,
        clock: Any = time.time,
    ) -> None:
        self.config = config
        self.cache = SafeCache(cache if cache is not None else default_cache)
        self.client = client if client is not None else BillzClient(config, cache=self.cache, clock=clock)
        self.interactive = interactive if interactive is not None else self.client.with_profile(INTERACTIVE)
        self._pending: dict[str, Any] | None = None

    @classmethod
    def from_settings(cls, *, transport: httpx.BaseTransport | None = None) -> LiveBillz:
        config = BillzConfig.from_settings()
        return cls(config, client=BillzClient(config, transport=transport))

    def is_configured(self) -> bool:
        return self.config.configured

    def _require_configured(self) -> None:
        if not self.config.configured:
            raise BillzUnavailable(
                "the live Billz adapter is not configured "
                "(BILLZ_SECRET_KEY, BILLZ_SHOP_ID, BILLZ_CASHBOX_ID, BILLZ_PAYMENT_TYPE_ID)",
                not_applied=True,
            )

    # --- catalog --------------------------------------------------------------------------------------------

    def _row(self, product: Mapping[str, Any], barcode: str, synced_at: datetime) -> BillzProduct:
        return BillzProduct(
            billz_id=barcode,
            billz_name=text_of(product.get("name"))[:NAME_MAX],
            volume=volume_of(product, self.config.volume_field),
            price=price_of(product, self.config.shop_id),
            stock=stock_of(product, self.config.shop_id),
            synced_at=synced_at,
        )

    def _products_page(
        self, params: Mapping[str, Any], client: BillzClient | None = None
    ) -> tuple[int, list[Mapping[str, Any]]]:
        body = (client or self.client).call(
            "GET", "/v2/products", label="GET /v2/products", params=params, idempotent=True
        )
        count = body.get("count")
        products = body.get("products")
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise BillzUnavailable("GET /v2/products: the answer carries no count")
        listed = (
            [item for item in products or [] if isinstance(item, Mapping)]
            if isinstance(products, list)
            else []
        )
        return count, listed

    def _remember_product_id(self, barcode: str, product: Mapping[str, Any]) -> None:
        product_id = text_of(product.get("id"))
        if UUID_PATTERN.fullmatch(product_id):
            self.cache.set(PRODUCT_ID_KEY.format(barcode=barcode), product_id, timeout=PRODUCT_ID_TTL)

    def _find_by_barcode(self, barcode: str, client: BillzClient | None = None) -> Mapping[str, Any] | None:
        """The first product whose barcode is exactly ``barcode``; ``search`` is a substring match, so its
        pages are read until the exact one turns up or the result ends (review N3)."""
        seen = 0
        for page in range(1, SEARCH_MAX_PAGES + 1):
            count, products = self._products_page(
                {
                    "search": barcode,
                    "fields": "barcode",
                    "limit": SEARCH_LIMIT,
                    "page": page,
                    "include_empty_stocks": "true",
                },
                client,
            )
            matches = [product for product in products if barcode_of(product) == barcode]
            if matches:
                if len(matches) > 1:
                    logger.warning(
                        "Billz lists %d products with barcode %s; using the first", len(matches), barcode
                    )
                self._remember_product_id(barcode, matches[0])
                return matches[0]
            seen += len(products)
            if not products or seen >= count:
                return None
        raise BillzSearchExhausted(
            f"GET /v2/products: the search for barcode {barcode} matched more than "
            f"{SEARCH_LIMIT * SEARCH_MAX_PAGES} products without an exact match"
        )

    def get_product(self, billz_id: str) -> BillzProduct | None:
        self._require_configured()
        if not BILLZ_ID.fullmatch(billz_id):
            return None
        # one budget for the whole lookup (up to SEARCH_MAX_PAGES calls), not one per call (review NEW-3): it
        # runs inside the idempotency transaction of POST products/
        with self.interactive.deadline(LOOKUP_BUDGET_SECONDS):
            product = self._find_by_barcode(billz_id, self.interactive)
        return None if product is None else self._row(product, billz_id, timeutils.now())

    def read_stock(
        self, billz_ids: Sequence[str], *, budget_seconds: float, interactive: bool = True
    ) -> StockRead:
        """Re-read each barcode's row (module doc "Stock re-read"); never raises for a Billz failure."""
        wanted = list(dict.fromkeys(billz_ids))
        if not self.config.configured:
            logger.warning("billz.stock_read_skipped: the live adapter is not configured")
            return StockRead(rows={}, unknown=frozenset(), unavailable=frozenset(wanted))
        client = self.interactive if interactive else self.client
        rows: dict[str, BillzProduct] = {}
        unknown: set[str] = set()
        ambiguous: set[str] = set()
        done = 0
        stop: BillzError | None = None
        try:
            with client.deadline(budget_seconds):
                for barcode in wanted:
                    # the stock is at least this fresh (never later than the answer)
                    asked_at = timeutils.now()
                    try:
                        product = (
                            self._find_by_barcode(barcode, client) if BILLZ_ID.fullmatch(barcode) else None
                        )
                    except BillzSearchExhausted:
                        # Billz answered every page; only this barcode cannot be confirmed (review nit)
                        logger.warning("billz.stock_read_ambiguous barcode=%s", barcode)
                        ambiguous.add(barcode)
                    else:
                        if product is None:
                            unknown.add(barcode)
                        else:
                            rows[barcode] = self._row(product, barcode, asked_at)
                    done += 1
        except BillzError as exc:
            stop = exc
            logger.warning(
                "billz.stock_read_incomplete read=%d of %d: %s", done, len(wanted), type(exc).__name__
            )
        rest = frozenset(wanted[done:])
        budget_ran_out = isinstance(stop, BillzOutOfTime) and (done > 0 or not stop.cut_short)
        if stop is not None and not budget_ran_out:
            return StockRead(
                rows=rows, unknown=frozenset(unknown), unavailable=rest, deferred=frozenset(ambiguous)
            )
        return StockRead(
            rows=rows, unknown=frozenset(unknown), unavailable=frozenset(), deferred=rest | ambiguous
        )

    def iter_products(self, mode: ImportMode) -> Iterator[BillzProduct]:
        """Fetch the whole run first (see the module doc), then yield its rows."""
        self._require_configured()
        self._pending = None
        started = timeutils.now()
        cursor = selectors.provider_data(PROVIDER)
        params: dict[str, Any] = {"limit": self.config.page_size, "include_empty_stocks": "true"}
        since = self._watermark(cursor) if mode == "delta" else None
        if since is not None:
            params["last_updated_date"] = (since - DELTA_OVERLAP).astimezone(UTC).strftime(WATERMARK_FORMAT)
        rows: dict[str, BillzProduct] = {}
        product_ids: dict[str, str] = {}  # cache key → the UUID of the product each row was built from
        distinct: dict[str, int] = {}  # product identity → the page it was first seen on (a repeat = overlap)
        repeats = 0
        in_our_shop = 0
        expected: int | None = None
        received = 0
        skipped = 0
        page = 1
        synced_at = timeutils.now()
        while True:
            count, products = self._products_page({**params, "page": page})
            if mode == "full" and expected is not None and count != expected:
                # pages are offsets: a product added or removed meanwhile shifts them, so a known product
                # could be missed and wrongly hidden; a delta only re-reads changes and tolerates it
                raise BillzListingIncomplete(
                    f"Billz product count changed while paging ({expected} → {count}); not reconciling"
                )
            expected = count
            received += len(products)
            for index, product in enumerate(products):
                identity = self._identity(product, page, index, distinct)
                if identity in distinct:
                    repeats += 1
                    continue
                distinct[identity] = page
                if in_shop(product, self.config.shop_id):
                    in_our_shop += 1
                barcode = barcode_of(product)
                if barcode is None:
                    skipped += 1
                    continue
                if barcode in rows:
                    logger.warning("Billz lists barcode %s twice; keeping the first product", barcode)
                    continue
                rows[barcode] = self._row(product, barcode, synced_at)
                product_id = text_of(product.get("id"))
                if UUID_PATTERN.fullmatch(product_id):
                    product_ids[PRODUCT_ID_KEY.format(barcode=barcode)] = product_id
            if not products or received >= expected:
                break
            page += 1
            if page > MAX_PAGE:
                raise BillzListingIncomplete("Billz listing exceeds 10 000 pages")
        if expected is None or received < expected:
            raise BillzListingIncomplete(f"only {received} of {expected} Billz products arrived")
        complete = repeats == 0 and len(distinct) >= expected
        if mode == "full":
            if not complete:
                raise BillzListingIncomplete(
                    f"Billz listed {len(distinct)} distinct products for a count of {expected} ({repeats} "
                    "repeated across pages: the paging shifted); not reconciling"
                )
            self._check_plausible(expected, cursor)
            if not in_our_shop:
                raise BillzListingIncomplete(
                    "no listed product carries an entry for the configured shop (check BILLZ_SHOP_ID); "
                    "not reconciling"
                )
        if skipped:
            logger.info("Billz sync skipped %d products without a 6–14 digit barcode", skipped)
        if product_ids:
            self.cache.set_many(product_ids, timeout=PRODUCT_ID_TTL)
        if complete:
            pending: dict[str, Any] = {"delta_since": started.isoformat()}
            if mode == "full":
                pending.update(full_count=expected, full_synced_at=started.isoformat())
            self._pending = pending
        else:
            logger.warning(
                "Billz delta saw %d distinct products for a count of %d; the watermark stays",
                len(distinct),
                expected,
            )
        yield from rows.values()

    @staticmethod
    def _identity(product: Mapping[str, Any], page: int, index: int, seen: Mapping[str, int]) -> str:
        """What tells a repeated product from another one: Billz's product id; without one, its barcode — a
        barcode met again **on another page** is a shifted page repeating it (review NEW-6), while one listed
        twice on the same page is a duplicate barcode (kept once, the first); else its position."""
        product_id = text_of(product.get("id")).lower()
        if product_id:
            return product_id
        barcode = barcode_of(product)
        if barcode is not None and seen.get(f"barcode:{barcode}") != page:
            return f"barcode:{barcode}"  # new, or first seen on another page (then a repeat)
        return f"#{page}:{index}"

    def _watermark(self, cursor: Mapping[str, Any]) -> datetime | None:
        value = cursor.get("delta_since")
        if not isinstance(value, str):
            return None
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            logger.warning("Ignoring an unreadable Billz delta watermark; listing everything")
            return None
        return parsed if parsed.tzinfo is not None else None

    def _check_plausible(self, count: int, cursor: Mapping[str, Any]) -> None:
        if count == 0:
            raise BillzListingIncomplete("Billz listed no product at all; refusing to reconcile")
        previous = cursor.get("full_count")
        if isinstance(previous, int) and not isinstance(previous, bool) and previous > 0:
            floor = math.ceil(previous * self.config.full_sync_min_ratio)
            if count < floor:
                raise BillzListingIncomplete(
                    f"Billz listed {count} products, fewer than {floor} "
                    f"({self.config.full_sync_min_ratio:.0%} of the previous full sync's {previous})"
                )

    def sync_succeeded(self, mode: ImportMode) -> None:
        if self._pending is None:
            return
        pending, self._pending = self._pending, None
        services.update_provider_data(PROVIDER, pending)

    # --- sale push ------------------------------------------------------------------------------------------

    def push_sale(self, request: SaleRequest, journal: SaleJournal) -> SaleReceipt:
        self._require_configured()
        lock = SaleLock(self.cache, SALE_LOCK_KEY.format(order_uuid=request.order_uuid), SALE_LOCK_TTL)
        if not lock.acquire():
            raise BillzSaleBusy(
                f"another worker is pushing the sale of order {request.order_id}", retry_after=SALE_BUSY_RETRY
            )
        try:
            with self.client.deadline(PUSH_DEADLINE_SECONDS):
                return self._push(request, journal, lock)
        finally:
            lock.release()

    def _order(self, billz_order_id: str) -> dict[str, Any]:
        return self.client.call(
            "GET", f"/v2/order/{billz_order_id}", label="GET /v2/order/{id}", idempotent=True, resource=True
        )

    def _create_order(self) -> tuple[str, str]:
        body = self.client.call(
            "POST",
            "/v2/order",
            label="POST /v2/order",
            json={"shop_id": self.config.shop_id, "cashbox_id": self.config.cashbox_id},
            idempotent=False,
            wait_result=True,
        )
        raw = body.get("data")
        data: Mapping[str, Any] = raw if isinstance(raw, Mapping) else {}
        # openapi post_v2_order_result: data.id is the sale; the envelope's own id names the reply message
        order_id = text_of(data.get("id"))
        envelope_id = text_of(body.get("id"))
        if envelope_id and envelope_id.lower() != order_id.lower():
            logger.info(
                "Billz answered the sale create with message id %s for sale %s", envelope_id, order_id
            )
        if not UUID_PATTERN.fullmatch(order_id):
            raise BillzUnavailable("POST /v2/order: the answer names no sale id")
        return order_id, text_of(data.get("order_number"))

    def _product_id(self, barcode: str, *, fresh: bool = False) -> str:
        key = PRODUCT_ID_KEY.format(barcode=barcode)
        if not fresh:
            cached = self.cache.get(key)
            if isinstance(cached, str) and UUID_PATTERN.fullmatch(cached):
                return cached
        product = self._find_by_barcode(barcode)
        product_id = text_of(product.get("id")) if product is not None else ""
        if not UUID_PATTERN.fullmatch(product_id):
            self.cache.delete(key)
            raise BillzRejected(f"product {barcode} not found in Billz", error_code=20036)
        return product_id

    def _resolve(self, request: SaleRequest) -> list[ResolvedLine]:
        """Every line as Billz receives it, resolved before anything is written (review B10)."""
        prices: dict[str, set[int]] = {}
        for line in request.lines:
            prices.setdefault(line.billz_id, set()).add(line.unit_price)
        lines = []
        for barcode, quantity, amount in aggregate(request.lines):
            if quantity <= 0:
                raise BillzCheckByHand(f"order {request.order_id}: product {barcode} has no quantity")
            if len(prices[barcode]) > 1:
                # Billz takes one free price per product (an upsert per product): lines of one product at
                # different prices cannot each be recorded at what the customer paid for them
                raise BillzCheckByHand(
                    f"order {request.order_id}: product {barcode} is sold at different unit prices"
                )
            lines.append(ResolvedLine(barcode, self._product_id(barcode), quantity, amount // quantity))
        return lines

    def _add_item(self, billz_order_id: str, product_id: str, quantity: int, unit_price: int | None) -> None:
        item: dict[str, Any] = {
            "product_id": product_id,
            "sold_measurement_value": quantity,
            "used_wholesale_price": False,
        }
        if unit_price is not None:
            # decision D1: the price the customer was charged (O6, PRICE_SOURCE=admin), not Billz's own
            item.update(use_free_price=True, free_price=unit_price)
        self.client.call(
            "POST",
            f"/v2/order-product/{billz_order_id}",
            label="POST /v2/order-product/{order_id}",
            json=item,
            idempotent=True,  # an upsert with the absolute quantity and price
            wait_result=True,
        )

    def _put_line(self, billz_order_id: str, line: ResolvedLine) -> None:
        try:
            self._add_item(billz_order_id, line.product_id, line.quantity, line.unit_price)
        except BillzRejected as exc:
            if exc.error_code not in (20036, 20037):
                raise
            # a cached id Billz no longer knows, or a product archived and replaced: look it up once more
            refreshed = self._product_id(line.barcode, fresh=True)
            if refreshed.lower() == line.product_id.lower():
                raise
            self._add_item(billz_order_id, refreshed, line.quantity, line.unit_price)

    def _remove_foreign_items(
        self, billz_order_id: str, order: Mapping[str, Any] | None, lines: Sequence[ResolvedLine]
    ) -> None:
        """A product in the draft that the order does not hold (a cashier's, a replaced id): quantity 0."""
        ours = {line.product_id.lower() for line in lines}
        for product_id, quantity in order_items(order).items():
            if product_id not in ours and quantity:
                logger.warning(
                    "Billz sale %s holds a product the order does not (%s); setting it to 0",
                    billz_order_id,
                    product_id,
                )
                self._add_item(billz_order_id, product_id, 0, None)

    def _client_id(self, phone: str, *, fresh: bool = False) -> tuple[str, bool]:
        """``(Billz client id, taken from the cache)``."""
        key = CLIENT_ID_KEY.format(ref=pii_ref(phone))
        if not fresh:
            cached = self.cache.get(key)
            if isinstance(cached, str) and UUID_PATTERN.fullmatch(cached):
                return cached, True
        body = self.client.call(
            "GET",
            "/v1/client",
            label="GET /v1/client",
            params={"phone_number": phone, "limit": 10, "page": 1},
            idempotent=True,
        )
        wanted = phone_digits(phone)
        client_id = ""
        for candidate in body.get("clients") or []:
            if not isinstance(candidate, Mapping):
                continue
            phones = candidate.get("phone_numbers") or []
            if any(phone_digits(str(number)) == wanted for number in phones if isinstance(number, str)):
                client_id = text_of(candidate.get("id"))
                break
        if not client_id:
            created = self.client.call(
                "POST", "/v1/client", label="POST /v1/client", json={"phone_number": phone}, idempotent=False
            )
            client_id = text_of(created.get("id"))
            logger.info("Created Billz client for phone ref %s", pii_ref(phone))
        if not UUID_PATTERN.fullmatch(client_id):
            raise BillzUnavailable("Billz client lookup: no client id in the answer")
        self.cache.set(key, client_id, timeout=CLIENT_ID_TTL)
        return client_id, False

    def _put_customer(self, billz_order_id: str, client_id: str) -> None:
        self.client.call(
            "PUT",
            f"/v2/order-customer-new/{billz_order_id}",
            label="PUT /v2/order-customer-new/{order_id}",
            json={"customer_id": client_id, "check_auth_code": False},
            idempotent=True,
            wait_result=True,
        )

    def _link_customer(self, billz_order_id: str, phone: str, order: Mapping[str, Any] | None) -> None:
        client_id, cached = self._client_id(phone)
        if order is not None and text_of(order.get("customer_id")).lower() == client_id.lower():
            return
        try:
            self._put_customer(billz_order_id, client_id)
        except BillzError as exc:
            status = exc.http_status
            refused = status is not None and 400 <= status < 500 and status != 429
            if not cached or not refused or isinstance(exc, BillzShiftClosed):
                raise
            # a cached client Billz no longer accepts (merged, deleted): forget it, look it up once more
            # (review B9) instead of failing every sale of this customer for 30 days
            logger.warning(
                "Billz refused the cached client of phone ref %s (%s); looking it up again",
                pii_ref(phone),
                exc,
            )
            self.cache.delete(CLIENT_ID_KEY.format(ref=pii_ref(phone)))
            fresh, _ = self._client_id(phone, fresh=True)
            if fresh.lower() == client_id.lower():
                raise
            self._put_customer(billz_order_id, fresh)

    def _bill_total(self, billz_order_id: str) -> float:
        bill = self.client.call(
            "POST",
            f"/v1/recalculate-order-bill/{billz_order_id}",
            label="POST /v1/recalculate-order-bill/{order_id}",
            idempotent=True,
        )
        if bill.get("is_calculated") is False:
            raise BillzUnavailable(
                "POST /v1/recalculate-order-bill: the bill is not calculated yet", error_code=20084
            )
        total = _number(bill.get("total_price"))
        if total is None or total < 0:
            raise BillzUnavailable("POST /v1/recalculate-order-bill: the answer carries no total")
        return total

    def _pay(self, billz_order_id: str, order_id: int, amount: int) -> None:
        self.client.call(
            "POST",
            f"/v2/order-payment/{billz_order_id}",
            label="POST /v2/order-payment/{order_id}",
            json={
                "payments": [
                    {
                        "company_payment_type_id": self.config.payment_type_id,
                        "paid_amount": amount,
                        "returned_amount": 0,
                    }
                ],
                "comment": f"Glow Derma #{order_id}",
                "without_cashback": True,
            },
            idempotent=False,  # never repeated: the journal's payment attempt guards the next one
        )

    def _payment_outcome_unknown(
        self, request: SaleRequest, billz_order_id: str, attempted_at: datetime
    ) -> None:
        """A payment was sent earlier and Billz still shows the sale open: wait, then stop for staff."""
        elapsed = timeutils.now() - attempted_at
        sent = attempted_at.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")
        if elapsed < PAYMENT_QUIET_PERIOD:
            raise BillzPaymentPending(
                f"the payment of Billz sale {billz_order_id} (order {request.order_id}) was sent at {sent}; "
                "its outcome is not known yet",
                retry_after=PAYMENT_QUIET_PERIOD - elapsed,
            )
        raise BillzCheckByHand(
            f"the payment of Billz sale {billz_order_id} (order {request.order_id}) was sent at {sent} and "
            "Billz still shows the sale open; not paying again: check the sale in Billz, complete it by hand"
        )

    def _push(self, request: SaleRequest, journal: SaleJournal, lock: SaleLock) -> SaleReceipt:
        progress: SaleProgress = journal.progress()  # re-read under the lock (another attempt may have run)
        if progress.pushed:
            logger.info("Billz sale of order %s was recorded by another attempt", request.order_id)
            return SaleReceipt(
                billz_order_id=progress.billz_order_id or str(request.order_uuid),
                billz_sale_id=progress.billz_order_id,
                billz_sale_ref=progress.billz_sale_ref,
            )
        lines = self._resolve(request)
        expected = sum(line.amount for line in lines)
        billz_order_id = progress.billz_order_id
        order: dict[str, Any] | None = None
        if billz_order_id:
            order = self._order(billz_order_id)
            if order.get("deleted") is True:
                raise BillzRejected(f"the Billz sale {billz_order_id} was deleted", error_code=20017)
            ref = sale_ref(text_of(order.get("order_number")), billz_order_id)
            state = sale_state(order)
            if state == "paid":
                logger.info("Billz sale %s of order %s is already paid", billz_order_id, request.order_id)
                return SaleReceipt(
                    billz_order_id=billz_order_id, billz_sale_id=billz_order_id, billz_sale_ref=ref
                )
            if state == "unclear":
                logger.error(
                    "billz.sale_state_unclear order_id=%s billz_order_id=%s finished_at=%r sold_at=%r "
                    "payments=%s: not paying; check the sale in Billz",
                    request.order_id,
                    billz_order_id,
                    text_of(order.get("finished_at")),
                    text_of(order.get("sold_at")),
                    len(_payments(order) or []),
                )
                alerts.alert(
                    "billz.sale_state_unclear", {"order_id": request.order_id}, scope=str(request.order_id)
                )
                raise BillzUnavailable(
                    f"the Billz sale {billz_order_id} shows contradicting paid/open signals; not paying"
                )
            if progress.payment_attempted_at is not None:
                self._payment_outcome_unknown(request, billz_order_id, progress.payment_attempted_at)
        else:
            billz_order_id, number = self._create_order()
            ref = sale_ref(number, billz_order_id)
            journal.order_created(billz_order_id, ref)
        self._remove_foreign_items(billz_order_id, order, lines)
        for line in lines:
            self._put_line(billz_order_id, line)
        if request.customer_phone:
            self._link_customer(billz_order_id, request.customer_phone, order)
        total = self._bill_total(billz_order_id)
        if total != expected:
            # decision D1: pay exactly what the customer was charged for the goods, or nothing
            logger.error(
                "billz.sale_total_differs order_id=%s billz_order_id=%s billz_total=%s expected=%s",
                request.order_id,
                billz_order_id,
                f"{total:g}",
                expected,
            )
            alerts.alert(
                "billz.sale_total_differs", {"order_id": request.order_id}, scope=str(request.order_id)
            )
            raise BillzCheckByHand(
                f"Billz bills {total:g} so'm for sale {billz_order_id} of order {request.order_id}, but the "
                f"customer was charged {expected} so'm for the goods; not paid: check the draft in Billz"
            )
        lock.renew()  # still ours, right before the one request that must never run twice
        journal.before_payment()
        try:
            self._pay(billz_order_id, request.order_id, expected)
        except BillzError as exc:
            if exc.not_applied:
                journal.payment_not_applied()
            raise
        logger.info("Billz sale %s recorded for order %s", billz_order_id, request.order_id)
        return SaleReceipt(billz_order_id=billz_order_id, billz_sale_id=billz_order_id, billz_sale_ref=ref)

    # --- explicit stubs (decision 2026-10-26) --------------------------------------------------------------

    def refund_sale(self, billz_order_id: str) -> None:
        refuse_refund(billz_order_id)

    def find_store_sale(self, receipt_ref: str) -> None:
        refuse_store_sale(receipt_ref)

    def parse_webhook(self, body: bytes) -> None:
        refuse_webhook(body)


__all__ = [
    "CLIENT_ID_KEY",
    "CLIENT_ID_TTL",
    "DELTA_OVERLAP",
    "LOOKUP_BUDGET_SECONDS",
    "MAX_PAGE",
    "PAYMENT_QUIET_PERIOD",
    "PRODUCT_ID_KEY",
    "PRODUCT_ID_TTL",
    "PROVIDER",
    "PUSH_DEADLINE_SECONDS",
    "SALE_BUSY_RETRY",
    "SEARCH_LIMIT",
    "SEARCH_MAX_PAGES",
    "UUID_PATTERN",
    "WATERMARK_FORMAT",
    "BillzSearchExhausted",
    "LiveBillz",
]
