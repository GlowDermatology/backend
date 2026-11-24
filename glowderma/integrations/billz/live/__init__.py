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
comes from the attribute or custom field named ``BILLZ_VOLUME_FIELD``; without that setting the volume is not
asserted at all (``None``: the stored volume is left alone). A barcode listed twice keeps the **first**
product, for the row and for the cached product UUID alike.

* ``delta`` asks for ``last_updated_date`` = the start of the last successful run (UTC) minus
  :data:`DELTA_OVERLAP`; without a cursor it lists everything. The cursor moves only in
  :meth:`LiveBillz.sync_succeeded`, i.e. after the catalog applied the whole run — and not even then when the
  run saw fewer distinct products than Billz counted (pages shifted under it), so the next delta re-reads.
* ``full`` is the reconciliation listing: every page must arrive, the count must not change while paging
  (pages are offsets), the pages must not overlap (the **distinct** Billz product ids — the barcode of a
  product without one — must equal the count),
  at least one listed product must carry an entry for ``BILLZ_SHOP_ID`` (a wrong shop id would zero every
  stock), and the count must be plausible (> 0 and ≥ ``BILLZ_FULL_SYNC_MIN_RATIO`` × the previous full
  count); otherwise :class:`BillzListingIncomplete` is raised before a single row is yielded, so nothing is
  applied and nothing hidden.

**Lookup** (``get_product``, admin create / ``billz_id`` change): the interactive client profile (bounded to
seconds, :data:`.client.INTERACTIVE`), ``search`` paged until the exact barcode is found — every page within
one budget of :data:`LOOKUP_BUDGET_SECONDS` in total.

**Stock re-read** (``read_stock``, gap X-STOCK-FRESHNESS): the same exact-barcode lookup for each barcode in
turn, all within one deadline (the caller's budget: 4 s on a customer request with the interactive profile,
a minute in the worker with the batch one); the first Billz failure (or the deadline) ends the run and every
barcode not read yet is reported unavailable — never an exception.

**Sale push** (``push_sale``; llms-full "Интернет-заказ из внешней системы"), under a per-sale lock owned by
this worker (:class:`SaleLock`) and a push-wide deadline (:data:`PUSH_DEADLINE_SECONDS`, below the outbox
lease), each step safe to resume:

0. ``journal.progress()`` re-read under the lock: an attempt that finished meanwhile ends here; every barcode
   is resolved to its Billz product UUID **before** anything is written (an unknown product leaves no draft);
a. a Billz sale an earlier attempt created: ``GET /v2/order/{id}``; deleted → permanent; paid (a real
   ``finished_at`` or ``sold_at`` and no contradicting empty payment list) → nothing is sent again;
   contradicting signals → retry later with an ERROR, never pay; a payment sent earlier whose outcome is
   still unknown → wait :data:`PAYMENT_QUIET_PERIOD` (deferral), then stop for staff, never pay twice.
   Billz has no lookup of a sale by an external reference and no idempotency header, so the persisted id
   **is** the lookup;
b. otherwise ``POST /v2/order {shop_id, cashbox_id}`` (async, result over HTTP) and
   ``journal.order_created(data.id, order_number)`` — the caller commits the id before anything else is sent;
c. items: lines in the draft that are not ours (a cashier's) are set to 0; then
   ``POST /v2/order-product/{id}`` per product — an upsert with the absolute quantity **and the price the
   customer was charged** (``use_free_price: true``, ``free_price`` = the order line's ``unit_price``;
   decision D1);
d. the customer: ``GET /v1/client?phone_number=`` (an exact match on the digits), ``POST /v1/client`` only
   when none exists (Billz does not de-duplicate phones; the new id is cached to bridge Billz's search lag),
   then ``PUT /v2/order-customer-new/{id} {customer_id, check_auth_code: false}``; a cached client id Billz
   refuses is forgotten, looked up once more and linked once more;
e. ``POST /v1/recalculate-order-bill/{id}``: the bill must equal Σ ``unit_price`` × ``quantity`` **exactly**
   (no rounding, no tolerance); otherwise :class:`BillzCheckByHand` and nothing is paid;
f. the lock is re-checked, ``journal.before_payment()`` re-checks the order (cancelled →
   :class:`SaleCancelled`) and commits the payment attempt, then ``POST /v2/order-payment/{id}`` with
   ``BILLZ_PAYMENT_TYPE_ID``, ``paid_amount`` = that total, ``without_cashback: true`` and
   ``Glow Derma #<order>`` as the comment. A payment Billz certainly did not apply (refused, or never sent)
   is forgotten again (``journal.payment_not_applied()``).

The window that cannot be closed: Billz creates the sale (b) and the worker dies before the id is committed;
the retry then creates a second, empty draft (no items, no stock effect) that staff delete in Billz.

The package: ``product_fields`` (reading a Billz product), ``sale_fields`` (reading a Billz order and
building a sale), ``sale_lock`` (the per-order push lock) and ``adapter`` (:class:`LiveBillz`).
"""

from glowderma.integrations.billz.live.adapter import (
    CLIENT_ID_KEY,
    CLIENT_ID_TTL,
    DELTA_OVERLAP,
    LOOKUP_BUDGET_SECONDS,
    MAX_PAGE,
    PAYMENT_QUIET_PERIOD,
    PRODUCT_ID_KEY,
    PRODUCT_ID_TTL,
    PROVIDER,
    PUSH_DEADLINE_SECONDS,
    SALE_BUSY_RETRY,
    SEARCH_LIMIT,
    SEARCH_MAX_PAGES,
    UUID_PATTERN,
    WATERMARK_FORMAT,
    BillzSearchExhausted,
    LiveBillz,
)
from glowderma.integrations.billz.live.product_fields import (
    BILLZ_ID,
    NAME_MAX,
    STOCK_LIMIT,
    VOLUME_MAX,
    barcode_of,
    in_shop,
    price_of,
    stock_of,
    volume_of,
)
from glowderma.integrations.billz.live.sale_fields import (
    REF_MAX,
    ZERO_TIME_PREFIX,
    SaleState,
    aggregate,
    is_finished,
    sale_ref,
    sale_state,
)
from glowderma.integrations.billz.live.sale_lock import (
    SALE_LOCK_KEY,
    SALE_LOCK_TTL,
    SaleLock,
)

__all__ = [
    "BILLZ_ID",
    "CLIENT_ID_KEY",
    "CLIENT_ID_TTL",
    "DELTA_OVERLAP",
    "LOOKUP_BUDGET_SECONDS",
    "MAX_PAGE",
    "NAME_MAX",
    "PAYMENT_QUIET_PERIOD",
    "PRODUCT_ID_KEY",
    "PRODUCT_ID_TTL",
    "PROVIDER",
    "PUSH_DEADLINE_SECONDS",
    "REF_MAX",
    "SALE_BUSY_RETRY",
    "SALE_LOCK_KEY",
    "SALE_LOCK_TTL",
    "SEARCH_LIMIT",
    "SEARCH_MAX_PAGES",
    "STOCK_LIMIT",
    "UUID_PATTERN",
    "VOLUME_MAX",
    "WATERMARK_FORMAT",
    "ZERO_TIME_PREFIX",
    "BillzSearchExhausted",
    "LiveBillz",
    "SaleLock",
    "SaleState",
    "aggregate",
    "barcode_of",
    "in_shop",
    "is_finished",
    "price_of",
    "sale_ref",
    "sale_state",
    "stock_of",
    "volume_of",
]
