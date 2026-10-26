"""The live Billz adapter (``BILLZ_MODE=live``): catalog sync and online-sale push over the BILLZ 2.0 API.

Built from docs.billz.io (``openapi.yaml`` is the source of truth) and **not verified against a live Billz
account**; docs/integrations/billz.md maps every call, lists what the docs leave open and how to verify it.
HTTP, auth, the shared rate limiter, retries and the error mapping live in :mod:`.client`.

**Products** (``get_v2_products``): ``GET /v2/products?page&limit&include_empty_stocks=true
[&last_updated_date]``, ``{count, products[]}``, ``limit`` ≤ 300. The catalog's ``billz_id`` (6–14 digits)
is the Billz **barcode** (Billz's own product id is a UUID); a product without such a barcode cannot be in
the catalog and is skipped. Stock and price are read for ``BILLZ_SHOP_ID`` only:
``shop_measurement_values[].active_measurement_value`` (sellable stock, holds excluded) and
``shop_prices[].retail_price`` (UZS; reference only, Glow Derma's price stays authoritative, O6). The volume
comes from the attribute or custom field named ``BILLZ_VOLUME_FIELD`` ("" when unset).

* ``delta`` asks for ``last_updated_date`` = the start of the last successful run (UTC) minus
  :data:`DELTA_OVERLAP`; without a cursor it lists everything. The cursor moves only in
  :meth:`LiveBillz.sync_succeeded`, i.e. after the catalog applied the whole run.
* ``full`` is the reconciliation listing: every page must arrive, the count must not change while paging
  (pages are offsets), and it must be plausible (> 0 and ≥ ``BILLZ_FULL_SYNC_MIN_RATIO`` × the previous
  full count); otherwise
  :class:`BillzListingIncomplete` is raised before a single row is yielded, so nothing is applied and
  nothing hidden.

**Sale push** (``push_sale``; llms-full "Интернет-заказ из внешней системы"), each step safe to resume:

a. ``request.billz_order_id`` set (an earlier attempt created the sale): ``GET /v2/order/{id}``; deleted →
   permanent; ``finished_at`` set → already paid, nothing is sent again. Billz has no lookup of a sale by an
   external reference and no idempotency header, so the persisted id **is** the lookup;
b. otherwise ``POST /v2/order {shop_id, cashbox_id}`` with ``Billz-Response-Channel: HTTP`` and
   ``journal.order_created(id, order_number)`` — the caller commits the id before anything else is sent;
c. ``POST /v2/order-product/{id}`` per line (an upsert with the absolute quantity), skipped when the sale
   already holds that quantity; the barcode → product UUID lookup is cached;
d. the customer: ``GET /v1/client?phone_number=`` (an exact match on the digits), ``POST /v1/client`` only
   when none exists (Billz does not de-duplicate phones; the new id is cached to bridge Billz's search lag),
   then ``PUT /v2/order-customer-new/{id} {customer_id, check_auth_code: false}``;
e. ``POST /v1/recalculate-order-bill/{id}`` for the bill total (payment must equal it, 20020);
f. ``POST /v2/order-payment/{id}`` with ``BILLZ_PAYMENT_TYPE_ID``, ``paid_amount`` = the bill total,
   ``without_cashback: true`` and ``Glow Derma #<order>`` as the comment.

The window that cannot be closed: Billz creates the sale (b) and the worker dies before the id is committed;
the retry then creates a second, empty draft (no items, no stock effect) that staff delete in Billz.
"""

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

from glowderma.core import timeutils
from glowderma.core.pii import pii_ref
from glowderma.integrations import selectors, services
from glowderma.integrations.models import IntegrationState

from .client import BillzClient, BillzConfig
from .ports import (
    BillzListingIncomplete,
    BillzProduct,
    BillzRejected,
    BillzUnavailable,
    ImportMode,
    SaleJournal,
    SaleLine,
    SaleReceipt,
    SaleRequest,
    refuse_refund,
    refuse_store_sale,
    refuse_webhook,
)

logger = logging.getLogger(__name__)

BILLZ_ID = re.compile(r"[0-9]{6,14}")  # ASCII digits only, like the catalog's billz_id check
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
SALE_LOCK_KEY = "billz:sale:lock:{order_uuid}"
SALE_LOCK_TTL = 600
SEARCH_LIMIT = 50
STOCK_LIMIT = 2_000_000_000
NAME_MAX = 255
VOLUME_MAX = 40
REF_MAX = 32
PROVIDER = IntegrationState.Provider.BILLZ


def _str(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _shop_entry(entries: Any, shop_id: str) -> Mapping[str, Any] | None:
    if not isinstance(entries, list):
        return None
    for entry in entries:
        if isinstance(entry, Mapping) and _str(entry.get("shop_id")).lower() == shop_id.lower():
            return entry
    return None


def barcode_of(product: Mapping[str, Any]) -> str | None:
    barcode = _str(product.get("barcode"))
    return barcode if BILLZ_ID.fullmatch(barcode) else None


def price_of(product: Mapping[str, Any], shop_id: str) -> int | None:
    """``shop_prices[shop].retail_price`` in so'm; ``None`` without an UZS price for the shop."""
    entry = _shop_entry(product.get("shop_prices"), shop_id)
    if entry is None:
        return None
    currency = _str(entry.get("retail_currency")).upper()
    price = _number(entry.get("retail_price"))
    if currency not in ("", "UZS") or price is None or price < 0:
        return None
    return round(price)


def stock_of(product: Mapping[str, Any], shop_id: str) -> int:
    """``shop_measurement_values[shop].active_measurement_value`` (whole units, floored); 0 when absent."""
    entry = _shop_entry(product.get("shop_measurement_values"), shop_id)
    value = _number(entry.get("active_measurement_value")) if entry is not None else None
    if value is None:
        return 0
    return max(-STOCK_LIMIT, min(STOCK_LIMIT, math.floor(value)))


def volume_of(product: Mapping[str, Any], field_name: str) -> str:
    """The value of the attribute or custom field named ``field_name`` (case-insensitive), else ""."""
    wanted = field_name.strip().casefold()
    if not wanted:
        return ""
    for attribute in product.get("product_attributes") or []:
        if isinstance(attribute, Mapping) and _str(attribute.get("attribute_name")).casefold() == wanted:
            return _str(attribute.get("attribute_value"))[:VOLUME_MAX]
    for custom in product.get("custom_fields") or []:
        if isinstance(custom, Mapping) and wanted in (
            _str(custom.get("custom_field_name")).casefold(),
            _str(custom.get("custom_field_system_name")).casefold(),
        ):
            return _str(custom.get("custom_field_value"))[:VOLUME_MAX]
    return ""


def is_finished(order: Mapping[str, Any]) -> bool:
    """A paid (closed) sale: ``finished_at`` holds a real date (Go's zero time ``0001-…`` is not one)."""
    finished = _str(order.get("finished_at"))
    return bool(finished) and not finished.startswith("0001-") and finished.lower() != "null"


def _order_items(order: Mapping[str, Any] | None) -> dict[str, float]:
    if order is None:
        return {}
    detail = order.get("order_detail")
    items = detail.get("order_items") if isinstance(detail, Mapping) else None
    present: dict[str, float] = {}
    for item in items or []:
        if not isinstance(item, Mapping) or item.get("is_returned"):
            continue
        product_id = _str(item.get("product_id"))
        quantity = _number(item.get("measurement_value"))
        if product_id and quantity is not None:
            present[product_id.lower()] = present.get(product_id.lower(), 0.0) + quantity
    return present


def _digits(phone: str) -> str:
    return "".join(char for char in phone if char in "0123456789")


def sale_ref(order_number: str, billz_order_id: str) -> str:
    """The short reference stored on the purchase (≤ 32): Billz's ``order_number``, else the compact id."""
    number = order_number.strip()
    if 0 < len(number) <= REF_MAX:
        return number
    return billz_order_id.replace("-", "")[:REF_MAX]


def aggregate(lines: Sequence[SaleLine]) -> list[tuple[str, int, int]]:
    """``(billz_id, quantity, amount)`` per product in first-seen order (Billz upserts per product)."""
    totals: dict[str, tuple[int, int]] = {}
    for line in lines:
        quantity, amount = totals.get(line.billz_id, (0, 0))
        totals[line.billz_id] = (quantity + line.quantity, amount + line.quantity * line.unit_price)
    return [(billz_id, quantity, amount) for billz_id, (quantity, amount) in totals.items()]


class LiveBillz:
    name = "billz"

    def __init__(
        self,
        config: BillzConfig,
        *,
        client: BillzClient | None = None,
        cache: BaseCache | None = None,
        clock: Any = time.time,
    ) -> None:
        self.config = config
        self.cache = cache if cache is not None else default_cache
        self.client = client if client is not None else BillzClient(config, cache=self.cache, clock=clock)
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
                "(BILLZ_SECRET_KEY, BILLZ_SHOP_ID, BILLZ_CASHBOX_ID, BILLZ_PAYMENT_TYPE_ID)"
            )

    # --- catalog --------------------------------------------------------------------------------------------

    def _row(self, product: Mapping[str, Any], barcode: str, synced_at: datetime) -> BillzProduct:
        return BillzProduct(
            billz_id=barcode,
            billz_name=_str(product.get("name"))[:NAME_MAX],
            volume=volume_of(product, self.config.volume_field),
            price=price_of(product, self.config.shop_id),
            stock=stock_of(product, self.config.shop_id),
            synced_at=synced_at,
        )

    def _products_page(self, params: Mapping[str, Any]) -> tuple[int, list[Mapping[str, Any]]]:
        body = self.client.call(
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

    def _remember_product_ids(self, products: Sequence[Mapping[str, Any]]) -> None:
        ids = {}
        for product in products:
            barcode = barcode_of(product)
            product_id = _str(product.get("id"))
            if barcode and UUID_PATTERN.fullmatch(product_id):
                ids[PRODUCT_ID_KEY.format(barcode=barcode)] = product_id
        if ids:
            self.cache.set_many(ids, timeout=PRODUCT_ID_TTL)

    def _find_by_barcode(self, barcode: str) -> Mapping[str, Any] | None:
        _, products = self._products_page(
            {
                "search": barcode,
                "fields": "barcode",
                "limit": SEARCH_LIMIT,
                "page": 1,
                "include_empty_stocks": "true",
            }
        )
        matches = [product for product in products if barcode_of(product) == barcode]
        if len(matches) > 1:
            logger.warning("Billz lists %d products with barcode %s; using the first", len(matches), barcode)
        if matches:
            self._remember_product_ids(matches[:1])
        return matches[0] if matches else None

    def get_product(self, billz_id: str) -> BillzProduct | None:
        self._require_configured()
        if not BILLZ_ID.fullmatch(billz_id):
            return None
        product = self._find_by_barcode(billz_id)
        return None if product is None else self._row(product, billz_id, timeutils.now())

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
            self._remember_product_ids(products)
            for product in products:
                barcode = barcode_of(product)
                if barcode is None:
                    skipped += 1
                    continue
                if barcode in rows:
                    logger.warning("Billz lists barcode %s twice; keeping the first product", barcode)
                    continue
                rows[barcode] = self._row(product, barcode, synced_at)
            if not products or received >= expected:
                break
            page += 1
            if page > MAX_PAGE:
                raise BillzListingIncomplete("Billz listing exceeds 10 000 pages")
        if expected is None or received < expected:
            raise BillzListingIncomplete(f"only {received} of {expected} Billz products arrived")
        if mode == "full":
            self._check_plausible(expected, cursor)
        if skipped:
            logger.info("Billz sync skipped %d products without a 6–14 digit barcode", skipped)
        pending: dict[str, Any] = {"delta_since": started.isoformat()}
        if mode == "full":
            pending.update(full_count=expected, full_synced_at=started.isoformat())
        self._pending = pending
        yield from rows.values()

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
        lock = SALE_LOCK_KEY.format(order_uuid=request.order_uuid)
        if not self.cache.add(lock, 1, timeout=SALE_LOCK_TTL):
            raise BillzUnavailable(f"another worker is pushing the sale of order {request.order_id}")
        try:
            return self._push(request, journal)
        finally:
            self.cache.delete(lock)

    def _order(self, billz_order_id: str) -> dict[str, Any]:
        return self.client.call(
            "GET", f"/v2/order/{billz_order_id}", label="GET /v2/order/{id}", idempotent=True
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
        order_id = _str(body.get("id")) or _str(data.get("id"))
        if not UUID_PATTERN.fullmatch(order_id):
            raise BillzUnavailable("POST /v2/order: the answer names no sale id")
        return order_id, _str(data.get("order_number"))

    def _product_id(self, barcode: str, *, fresh: bool = False) -> str:
        key = PRODUCT_ID_KEY.format(barcode=barcode)
        if not fresh:
            cached = self.cache.get(key)
            if isinstance(cached, str) and cached:
                return cached
        product = self._find_by_barcode(barcode)
        product_id = _str(product.get("id")) if product is not None else ""
        if not UUID_PATTERN.fullmatch(product_id):
            self.cache.delete(key)
            raise BillzRejected(f"product {barcode} not found in Billz", error_code=20036)
        return product_id

    def _add_item(self, billz_order_id: str, product_id: str, quantity: int) -> None:
        self.client.call(
            "POST",
            f"/v2/order-product/{billz_order_id}",
            label="POST /v2/order-product/{order_id}",
            json={
                "product_id": product_id,
                "sold_measurement_value": quantity,
                "used_wholesale_price": False,
            },
            idempotent=True,  # an upsert with the absolute quantity
            wait_result=True,
        )

    def _put_line(
        self, billz_order_id: str, barcode: str, quantity: int, present: Mapping[str, float]
    ) -> None:
        product_id = self._product_id(barcode)
        if present.get(product_id.lower()) == quantity:
            return
        try:
            self._add_item(billz_order_id, product_id, quantity)
        except BillzRejected as exc:
            if exc.error_code != 20036:
                raise
            refreshed = self._product_id(barcode, fresh=True)  # a cached id Billz no longer knows
            if refreshed == product_id:
                raise
            self._add_item(billz_order_id, refreshed, quantity)

    def _client_id(self, phone: str) -> str:
        key = CLIENT_ID_KEY.format(ref=pii_ref(phone))
        cached = self.cache.get(key)
        if isinstance(cached, str) and cached:
            return cached
        body = self.client.call(
            "GET",
            "/v1/client",
            label="GET /v1/client",
            params={"phone_number": phone, "limit": 10, "page": 1},
            idempotent=True,
        )
        wanted = _digits(phone)
        client_id = ""
        for candidate in body.get("clients") or []:
            if not isinstance(candidate, Mapping):
                continue
            phones = candidate.get("phone_numbers") or []
            if any(_digits(str(number)) == wanted for number in phones if isinstance(number, str)):
                client_id = _str(candidate.get("id"))
                break
        if not client_id:
            created = self.client.call(
                "POST", "/v1/client", label="POST /v1/client", json={"phone_number": phone}, idempotent=False
            )
            client_id = _str(created.get("id"))
            logger.info("Created Billz client for phone ref %s", pii_ref(phone))
        if not UUID_PATTERN.fullmatch(client_id):
            raise BillzUnavailable("Billz client lookup: no client id in the answer")
        self.cache.set(key, client_id, timeout=CLIENT_ID_TTL)
        return client_id

    def _link_customer(self, billz_order_id: str, phone: str, order: Mapping[str, Any] | None) -> None:
        client_id = self._client_id(phone)
        if order is not None and _str(order.get("customer_id")).lower() == client_id.lower():
            return
        self.client.call(
            "PUT",
            f"/v2/order-customer-new/{billz_order_id}",
            label="PUT /v2/order-customer-new/{order_id}",
            json={"customer_id": client_id, "check_auth_code": False},
            idempotent=True,
            wait_result=True,
        )

    def _bill_total(self, billz_order_id: str) -> int:
        bill = self.client.call(
            "POST",
            f"/v1/recalculate-order-bill/{billz_order_id}",
            label="POST /v1/recalculate-order-bill/{order_id}",
            idempotent=True,
        )
        total = _number(bill.get("total_price"))
        if total is None or total < 0:
            raise BillzUnavailable("POST /v1/recalculate-order-bill: the answer carries no total")
        return round(total)

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
            idempotent=False,  # an unknown outcome is re-checked (step a) before paying again
        )

    def _push(self, request: SaleRequest, journal: SaleJournal) -> SaleReceipt:
        billz_order_id = request.billz_order_id
        order: dict[str, Any] | None = None
        if billz_order_id:
            order = self._order(billz_order_id)
            if order.get("deleted") is True:
                raise BillzRejected(f"the Billz sale {billz_order_id} was deleted", error_code=20017)
            ref = sale_ref(_str(order.get("order_number")), billz_order_id)
            if is_finished(order):
                logger.info("Billz sale %s of order %s is already paid", billz_order_id, request.order_id)
                return SaleReceipt(
                    billz_order_id=billz_order_id, billz_sale_id=billz_order_id, billz_sale_ref=ref
                )
        else:
            billz_order_id, number = self._create_order()
            ref = sale_ref(number, billz_order_id)
            journal.order_created(billz_order_id, ref)
        present = _order_items(order)
        expected = 0
        for barcode, quantity, amount in aggregate(request.lines):
            self._put_line(billz_order_id, barcode, quantity, present)
            expected += amount
        if request.customer_phone:
            self._link_customer(billz_order_id, request.customer_phone, order)
        total = self._bill_total(billz_order_id)
        if total != expected:
            logger.warning(
                "billz.sale_total_differs order_id=%s billz_order_id=%s billz_total=%s glowderma_total=%s",
                request.order_id,
                billz_order_id,
                total,
                expected,
            )
        self._pay(billz_order_id, request.order_id, total)
        logger.info("Billz sale %s recorded for order %s", billz_order_id, request.order_id)
        return SaleReceipt(billz_order_id=billz_order_id, billz_sale_id=billz_order_id, billz_sale_ref=ref)

    # --- explicit stubs (decision 2026-10-26) --------------------------------------------------------------

    def refund_sale(self, billz_order_id: str) -> None:
        refuse_refund(billz_order_id)

    def find_store_sale(self, receipt_ref: str) -> None:
        refuse_store_sale(receipt_ref)

    def parse_webhook(self, body: bytes) -> None:
        refuse_webhook(body)
