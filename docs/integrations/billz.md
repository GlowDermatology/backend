# Billz live adapter (M3)

> **Not verified against a live Billz account.** Everything below is built from the BILLZ 2.0 public API
> documentation at docs.billz.io — `openapi.yaml` (version `2026.08`, the source of truth) and its prose guide —
> and tested only against recorded answers built from that schema. Run the checklist in
> [§11](#11-verifying-against-a-real-billz-account) before switching production to `BILLZ_MODE=live`.

Billz is the shop's POS/ERP. It owns **stock** and the **Billz product name**; Glow Derma owns texts, images,
visibility, the promo flag and (provisionally, O6) the price. Glow Derma reads the catalog from Billz and pushes
every paid online order back as a Billz sale.

Code: `glowderma/integrations/billz/` — `ports.py` (the port, typed errors, the sale journal, stubs), `fake.py`,
`live.py` (catalog sync, sale push), `client.py` (shared cache, auth, rate limiter, profiles and deadlines, circuit
breaker, retries, error mapping), `registry.py`. Orchestration: `glowderma/catalog/tasks.py` (import job, beat),
`glowderma/catalog/services.py` (`upsert_from_billz`, `hide_missing_from_billz`), `glowderma/commerce/tasks.py`
(`billz.push_sale`), `glowderma/commerce/billz_sales.py` (`PurchaseJournal`), `glowderma/commerce/selectors.py`
(`available_stock`), `glowderma/loyalty/services.py` (`record_billz_order`, `record_billz_payment_attempt`,
`clear_billz_payment_attempt`, `link_billz_sale`), `glowderma/core/outbox.py` (`RetryLater`),
`glowderma/core/management/commands/requeue_outbox.py`.

**Lead decisions (2026-10-26, provisional; `docs/decisions/open-questions.md` BZ-D1–BZ-D4):**

* **D1 — sale price.** The Billz sale records the goods at the price the customer was charged: each line is sent with
  `use_free_price: true`, `free_price` = `OrderItem.unit_price` (the order's snapshot), so the expected bill is
  Σ `unit_price` × `quantity`. The cashback spent and the delivery fee stay outside Billz (pushed sales are
  `without_cashback`; cashback lives in Glow Derma's ledger). After the recalculation the sale is paid **only** when
  Billz's total equals the expected amount exactly; otherwise nothing is paid: `BillzCheckByHand` → dead letter
  with the reason (staff check the draft). Never another amount, never rounded.
* **D2 — closed shift.** 20010 / 20082 / 20033 are not permanent: the push is deferred every
  `BILLZ_SHIFT_RETRY_MINUTES` (15) without spending the outbox attempt budget (`outbox.RetryLater`), for at most
  `BILLZ_SHIFT_RETRY_MAX_HOURS` (36) after the first refusal, then dead-lettered with the reason "shift closed".
* **D3 — configuration-type failures.** 3xx, 403, a 404 of a route (not `GET /v2/order/{id}`), 20009, 20085 and a
  refused key are `BillzMisconfigured` (retryable) with an ERROR `billz.configuration_error` so monitoring alerts; a
  circuit breaker stops a failing Billz from holding workers; once the setting is fixed,
  `manage.py requeue_outbox --topic billz.push_sale --dead [--since ISO] [--dry-run]` replays the dead sales.
* **D4 — `skip_ofd`** stays `false` (not sent) until the product owner decides (§12).

## 1. Modes

| `BILLZ_MODE` | Adapter | Behaviour |
|---|---|---|
| `fake` (default, `INTEGRATIONS_MODE`) | `FakeBillz` | Deterministic catalog (unchanged M1 rules). Sale push accepted without a Billz sale id; scenario `{"push": "down"}` → transient, `{"push": "rejected"}` → permanent. Under the production settings the fake records no sale and no stock (customer contract §6.14): the import is refused, the `billz.push_sale` handler never calls it (WARNING `billz.sale_not_pushed`). The beat syncs do nothing. |
| `live` | `LiveBillz` | The BILLZ 2.0 public API. The production settings refuse to start without the key and the three ids (§10). |

## 2. Endpoints used

All paths are relative to `BILLZ_BASE_URL` (default `https://api-admin.billz.ai`, `servers[0]` of `openapi.yaml`).

| Method and path | `operationId` | Used for | Sync/async | Repeated after an unknown outcome? |
|---|---|---|---|---|
| `POST /v1/auth/login` | `post_v1_auth_login` | key → access + refresh token (`platform-id` header) | sync | yes, once (creates a session only) |
| `POST /v2/auth/refresh` | `post_v2_auth_refresh` | token rotation (`platform-id` header) | sync | **no**: a failed or lost refresh falls back to a login |
| `GET /v2/products` | `get_v2_products` | catalog pages; barcode lookup (`search` + `fields=barcode`, paged until the exact barcode) | sync | yes (read) |
| `POST /v2/order` | `post_v2_order` | create the sale (`{shop_id, cashbox_id}`); the sale id is `data.id` (the envelope's own `id` names the reply message) | async → `Billz-Response-Channel: HTTP` (header and query) | **no** (see §6 window) |
| `GET /v2/order/{id}` | `get_v2_order_id` | state of a sale on resume (deleted, paid, items, client) | sync | yes (read) |
| `POST /v2/order-product/{order_id}` | `post_v2_order_product_order_id` | add an item (upsert: absolute quantity, `use_free_price`/`free_price`) | async → header | yes (idempotent upsert) |
| `GET /v1/client?phone_number=` | `get_v1_client` | find the customer by phone | sync | yes (read) |
| `POST /v1/client` | `post_v1_client` | create the customer (`{phone_number}` only) | sync | **no** (Billz does not de-duplicate phones) |
| `PUT /v2/order-customer-new/{order_id}` | `put_v2_order_customer_new_order_id` | link the customer (`check_auth_code: false`) | async → header | yes (idempotent) |
| `POST /v1/recalculate-order-bill/{order_id}` | `post_v1_recalculate_order_bill_order_id` | the bill total to pay | sync | yes |
| `POST /v2/order-payment/{order_id}` | `post_v2_order_payment_order_id` | pay and close the sale | sync | **never**: the attempt is persisted first; the next attempt reads the sale and waits (§6) |

Setup only (by hand, to fill the settings): `GET /v1/shop` (`get_v1_shop`), `GET /v1/cash-box`
(`get_v1_cash_box`), `GET /v1/company-payment-type` (`get_v1_company_payment_type`).

## 3. Auth and tokens

* `POST /v1/auth/login {"secret_token": BILLZ_SECRET_KEY}` with header `platform-id: BILLZ_PLATFORM_ID` (`openapi.yaml`
  declares it on the login too) → `data.access_token` (`expires_in`, 15 days for a key login), `data.refresh_token`
  (one use, 30 days). An answer without `expires_in` gets 1 hour; every lifetime is clamped to [10 min, 30 days].
* `POST /v2/auth/refresh {"refresh_token"}` with the `platform-id` header → a new pair (`expires_in`,
  `refresh_expires_in` read from the answer). Rotation: a spent refresh token presented again revokes the whole
  session family (`400 invalid_grant`, "token reuse detected").
* The pair lives **only in the Django cache** (Redis DB 0, key `billz:auth:tokens`, TTL = the longer of the two
  lifetimes), never in the database, never in a log line, never in a URL. The key is kept out of `repr`.
* Renewal is **single-flight across processes**: the winner of `cache.add("billz:auth:lock", owner, 90 s)` renews
  within 40 s (its own deadline, at most half the lock's TTL, so the lock cannot expire under it). **Before it
  presents the refresh token it removes it from the cache**, so no other worker can ever present the same token
  again; a refresh that is refused, answers 5xx or times out falls back to **one** login, never to a second
  refresh. The new pair is stored only while the renewer still owns the lock. Every other worker re-reads the cache
  every 0.2 s until the pair changes (45 s for a batch call, 5 s for an admin lookup, then `BillzUnavailable`). A
  token within 5 minutes of expiry is renewed before use. A refresh token past its stored expiry is not tried.
* A `401` on a normal call renews once and repeats the call once; a second `401` is `BillzUnavailable` (never a
  loop). A refused login (4xx) is `BillzAuthError` (a `BillzMisconfigured`, job code `billz_auth_failed`, ERROR
  `billz.configuration_error`: an operator must check the key).

## 4. Shared cache, rate limit, retries, timeouts, circuit breaker

* **Shared cache.** Tokens, the limiter, the circuit breaker, the cached product and client ids and the sale lock
  live in the Django cache. Every operation goes through `client.SafeCache`: Redis down is `BillzUnavailable`
  ("Billz: shared cache unavailable", ERROR `billz.cache_unavailable`) before anything is sent — an admin product
  create/PATCH answers 503 `provider_unavailable`, the outbox retries a push.
* **Rate limit.** Billz answers `429` above about **2 requests/second per IP**, counted over the whole integration.
  One limiter in the cache serves every worker: time is cut into slots of `1 / BILLZ_RATE_LIMIT_PER_SECOND` seconds
  (default 1.5/s, at most 2) and a request may start only at the beginning of a slot it won with `cache.add`
  (atomic `SET NX`), so request starts are at least one slot apart whichever process sends them. Hosts need
  NTP-synced clocks. Login and refresh go through the limiter too. A request is never queued further ahead than its
  profile allows (`BillzUnavailable`, nothing sent). A request always waits for the next slot boundary (≤ one slot,
  ≈ 0.7 s): kept on purpose, it is what makes the spacing hold across processes.
* **429**: every worker pauses (`billz:rate:pause`) for `Retry-After` seconds when Billz sends it (delta or HTTP
  date, capped at 60 s — the docs do not mention the header), else for an exponential backoff with jitter; at most 5
  times per batch call, then `BillzUnavailable`. Two 429s at once may keep the shorter pause (the cache API has no
  atomic max); the next request then meets another 429 and pauses again.
* **Retries**: transport errors and `5xx`: up to 4 attempts with exponential backoff and jitter (0.5 s · 2ⁿ⁻¹ ·
  [0.5, 1), capped at 8 s) — **only** for calls that are safe to repeat (reads, upserts, login) or when the request
  certainly never reached Billz (connect errors). A write whose outcome is unknown (read timeout, `5xx`, `408` of an
  async method, an async answer carrying only the correlation id) raises `BillzUnavailable` and is never repeated
  blindly (the docs: "408 … повторять запись вслепую нельзя"). Any other httpx error (a body that does not decode)
  is `BillzUnavailable` too, never an untyped exception. *AGENTS.md asks adapters to retry transport errors only;
  Billz is the documented exception: its `429` is the documented rate-limit signal, and `5xx` are retried only for
  repeatable calls.*
* **Profiles** (`client.CallProfile`): the batch profile (catalog sync, sale push) waits up to 60 s for a limiter
  slot and retries 4 times. The **interactive** profile of the admin lookups (`POST products/`, a `billz_id`
  change — they run inside the idempotency transaction that holds the key's lock) waits 3 s for a slot, retries
  once, reads for at most 10 s, fails at once while a 429 pause is in force, waits 5 s for another worker's token
  renewal and gives up after **25 s in total**.
* **Deadlines**: a sale push ends within **240 s** (`live.PUSH_DEADLINE_SECONDS`, below the outbox lease of 5 minutes
  and the 10-minute sale lock): every limiter wait, backoff sleep and HTTP timeout is cut to what is left, and a
  request that cannot start in time is not sent (`BillzUnavailable`).
* **Timeouts**: connect `BILLZ_CONNECT_TIMEOUT_SECONDS` (5, ≤ 30), read/write `BILLZ_READ_TIMEOUT_SECONDS` (30, ≤ 60).
  Billz holds an async call open for up to 600 s before its own `408`; a read timeout before that is an unknown
  outcome.
* **Circuit breaker**: 5 transport errors, timeouts or `5xx`/`408` answers in a row (across every worker, counted in
  the cache) open the circuit for 90 s (ERROR `billz.circuit_open`): every call then fails fast with
  `BillzUnavailable` (nothing sent) instead of holding a worker of the shared outbox pool for minutes. Any other
  answer closes it.
* httpx's own request log lines (full URL) are silenced (`LOGGING`: `httpx`/`httpcore` at WARNING) because
  `GET /v1/client?phone_number=…` carries a phone number. Our log lines use `pii_ref(phone)`; Billz's own free text is
  scrubbed (control characters and lone surrogates dropped, runs of ≥ 7 digits masked as `<number>`) before it
  reaches an error, `last_error`, the `outbox.dead` audit row or a log line.

## 5. Catalog sync

**Mapping** (`GET /v2/products`, `include_empty_stocks=true` so products at zero stock are listed):

| Glow Derma (`BillzProduct`) | Billz field | Rule |
|---|---|---|
| `billz_id` | `barcode` | The catalog's `billz_id` is 6–14 ASCII digits; Billz's own product id is a UUID, so products are keyed by **barcode**. A product without such a barcode is skipped (logged count). A barcode listed twice keeps the **first** product (WARNING) — for the row and for the cached product UUID the sale push uses alike. |
| `billz_name` | `name` | trimmed, ≤ 255 |
| `volume` | the `product_attributes[].attribute_value` or `custom_fields[].custom_field_value` named `BILLZ_VOLUME_FIELD` (case-insensitive; custom fields also by `custom_field_system_name`) | `""` when the product lacks it; **not asserted at all** (`None`: the stored volume is never touched) when the setting is empty (the docs define no volume field) |
| `price` (stored as `billz_price`, reference only) | `shop_prices[shop_id = BILLZ_SHOP_ID].retail_price` | rounded so'm; `None` without an entry for the shop or with a currency other than UZS. `promo_price` is ignored (Glow Derma owns the promo flag and price, O6). A new product starts at this price, `hidden`. |
| `stock` | `shop_measurement_values[shop_id = BILLZ_SHOP_ID].active_measurement_value` | sellable stock (holds excluded); floored to whole units; `0` without an entry for the shop |

The Billz product UUID of each barcode is cached (`billz:product:<barcode>`, 7 days) for the sale push.
An admin lookup (`get_product`) pages through `search` results (50 a page, at most 10 pages) until the exact barcode
turns up; more matches than that without an exact one is `BillzUnavailable`, never "not found".

**Delta** (hourly, or admin "delta"): `last_updated_date` = start of the last successful run − 5 minutes (UTC,
`yyyy-mm-dd hh:mm:ss`); without a cursor it lists everything. The cursor lives in `IntegrationState(billz).data`
(`delta_since`, `full_count`, `full_synced_at`) and moves **only** through `BillzPort.sync_succeeded()`, which the
import task calls after every row was applied; a run that fails anywhere (a page, an upsert, the reconciliation)
is fetched again next time. A delta tolerates the result count changing while paging (sales update products), but
when it saw fewer **distinct** products than Billz counted (a shifted page repeated some) it does not move the
watermark, so the next delta re-reads; the daily full catches anything a shifted page skipped.

**Full** (daily, or admin "full") = reconciliation. The listing is refused (`BillzListingIncomplete`, job error code
`billz_listing_incomplete`, nothing applied, nothing hidden, no cursor move) unless:
* every page arrived (`received ≥ count`, at most 10 000 pages),
* `count` did not change while paging (pages are offsets: a shift could skip a product and hide it wrongly),
* the pages did not overlap: the **distinct** Billz product ids equal `count` (a repeated product means another one
  was never listed — it would be hidden although Billz still sells it),
* it is plausible: `count > 0` and `count ≥ ceil(BILLZ_FULL_SYNC_MIN_RATIO × previous full count)`,
* at least one listed product carries a stock or price entry for `BILLZ_SHOP_ID` (a wrong — but well-formed — shop
  id would zero every stock).

Then the job upserts every row and `catalog.services.hide_missing_from_billz` turns every **active** product whose
`billz_id` Billz no longer lists into `hidden` — unless that would hide more than
`max(3, active products − ceil(BILLZ_FULL_SYNC_MIN_RATIO × active products))` in one run (a barcode-keying
mismatch or a wrong `BILLZ_SHOP_ID` would "lose" most of the catalog): then nothing is hidden, the job fails with
`billz_listing_incomplete` and the cursor stays. A product created, edited or looked up in Billz after the listing
started is never hidden by that run (staff may have created and activated it while the run paged); the next daily
full judges it. Hidden products are **never deleted** (texts, images, orders and history stay; staff
re-activate it when it returns). Each hide sets `last_change = hidden` without an actor and writes
`product.updated` (`status` diff, actor `system`, reason `billz_missing: …`). `Job.result` of a full run gains
`hidden` (count); a delta's result is unchanged.

**Job errors**: every Billz error fails the job with the generic message `provider_unavailable` ("Tashqi xizmat
javob bermadi. Qayta urinib ko‘ring."); the code is `provider_unavailable` (also when the run outlives its own soft
time limit of 840 s), `billz_listing_incomplete` or `billz_auth_failed` (Billz refused the key). The import task has
its own limits (840 s soft / 870 s hard, below the 15-minute stale-job reaper, which now also fails a job still
`queued` after 15 minutes); the beat warns when it joins an import job older than that.

**Beat** (`config/celery.py`, Tashkent time): `billz-sync-delta` → `catalog.billz_sync_delta` every hour at :07;
`billz-sync-full` → `catalog.billz_sync_full` daily at 04:23. Both start the `billz_import` job through
`integrations.services.start_job` (actor `system`), so they single-flight with an admin import and the stale-job
reaper covers them. With `BILLZ_MODE=fake` (any settings) they do nothing: no job, no audit row, no `synced_at`
churn. With live but unconfigured they log a WARNING and do nothing. A full that finds another import running
retries every 15 minutes, 4 times, then skips the day (WARNING).

## 6. Sale push (`billz.push_sale`)

Enqueued at payment confirmation (M2). The handler skips a purchase already pushed (`billz_pushed_at`), refuses a
payload of another purchase, never calls a fake Billz under the production settings (§1), and never completes the
sale of an order cancelled meanwhile: checked before the push (WARNING `billz.sale_skipped_cancelled`; ERROR
`billz.sale_of_cancelled_order … billz_order_id=…` when a draft exists), again **under the Order lock right before the
payment** (`PurchaseJournal.before_payment` → `SaleCancelled`: nothing paid, ERROR naming the draft to delete), and
after it (a sale paid while the order was being cancelled is linked but gets no `billz_sale_recorded`; ERROR asks for
a refund in Billz by hand). `commerce.services.cancel_order` itself logs ERROR `billz.refund_by_hand order_id=…
billz_order_id=… pushed=…` once the cancel commits whenever the purchase holds a Billz sale (Billz has no refund API).

A per-sale cache lock (`billz:sale:lock:<order uuid>`, 10 min) **owned by one worker** keeps two workers off the
same sale: only the owner releases it, and it is re-checked and renewed right before the payment (a worker whose
lock expired stops there). Under the lock the journal re-reads the purchase (`SaleJournal.progress`), so an attempt
that read it before another attempt finished the push returns that sale instead of creating a second one. A push
ends within 240 s (§4).

Steps (llms-full "Интернет-заказ из внешней системы"), each safe to resume:

| Step | Call | Resume rule |
|---|---|---|
| 0. resolve | barcode → product UUID from the cache or `GET /v2/products?search=<barcode>&fields=barcode` (exact match, paged) | **Before anything is written**: an unknown product (20036) is dead-lettered without an orphan draft. Lines of one product at different unit prices cannot carry one free price → `BillzCheckByHand`. |
| a. look up | `GET /v2/order/{Purchase.billz_sale_id}` when an earlier attempt created the sale | `deleted` → permanent (20017). **Paid** = closed (a real `finished_at` *or* `sold_at`; Go's zero time is a draft) and no explicit empty `order_detail.order_payments` → nothing more is sent. **Open** = not closed, no payment. **Contradicting** signals (closed with no payment, a payment on an open sale) → `BillzUnavailable` with ERROR `billz.sale_state_unclear`, never paid. An open sale whose payment was **attempted** (`Purchase.billz_payment_attempted_at`) is deferred (`BillzPaymentPending` → `outbox.RetryLater`, no attempt spent) until 10 minutes after that attempt, then stopped for staff (`BillzCheckByHand`, dead: "not paying again") — a payment is never sent twice. Billz has no lookup by external reference and no idempotency header, so **our persisted id is the lookup**. |
| b. create | `POST /v2/order {shop_id: BILLZ_SHOP_ID, cashbox_id: BILLZ_CASHBOX_ID}` + `Billz-Response-Channel: HTTP` (header and query) | `data.id` (the sale; the envelope's `id` names the reply message) and `data.order_number` are committed onto the purchase **before anything else is sent** (`loyalty.services.record_billz_order` → `billz_sale_id`, `billz_sale_ref`; `billz_pushed_at` stays null). |
| c. items | lines in the draft the order does not hold (a cashier's, a replaced product id) → `sold_measurement_value: 0`; then `POST /v2/order-product/{id} {product_id, sold_measurement_value, used_wholesale_price: false, use_free_price: true, free_price: <unit_price>}` + header | One call per product (lines of one product summed). An upsert with the absolute quantity **and the price the customer was charged** (D1), re-sent for every line on every attempt (so a line Billz holds at its own price is corrected before the bill is read). A cached id Billz rejects with 20036 or 20037 is looked up once more. |
| d. customer | `GET /v1/client?phone_number=<+998…>` (exact digits match on `phone_numbers`), `POST /v1/client {phone_number}` only when none, then `PUT /v2/order-customer-new/{id} {customer_id, check_auth_code: false}` + header | The client id is cached by `pii_ref(phone)` for 30 days (Billz's search lags behind a create: "Только что созданный клиент может не найтись сразу"). A cached id Billz refuses (4xx: merged, deleted) is forgotten, looked up once more and linked once more. Skipped when the sale already carries that client. No name or other PII is sent. |
| e. bill | `POST /v1/recalculate-order-bill/{id}` → `total_price`, `is_calculated` | Not calculated yet → retry. The total must equal Σ `unit_price` × `quantity` **exactly** (D1); otherwise ERROR `billz.sale_total_differs` and `BillzCheckByHand` (dead with "Billz bills X so'm … the customer was charged Y"), nothing paid. |
| f. pay | `POST /v2/order-payment/{id} {payments: [{company_payment_type_id: BILLZ_PAYMENT_TYPE_ID, paid_amount: <expected>, returned_amount: 0}], comment: "Glow Derma #<order>", without_cashback: true}` | First the lock is renewed and `journal.before_payment()` re-checks the order and **commits the attempt** (`record_billz_payment_attempt`). A refusal Billz answered (a business code, 4xx, 429) or a request never sent forgets the attempt (`payment_not_applied`); an unknown outcome (timeout, 5xx) keeps it → step a on the next attempt. |

Then the handler links the purchase (`link_billz_sale`, `billz_pushed_at` = now) and writes `billz_sale_recorded`
once with `billz_order_id` = Billz's sale UUID (the fake echoes our `billz_order_uuid`, as in M2).

**Available stock** (`commerce.selectors.available_stock`): the units of a paid order keep counting against
Billz's stock until its sale is in Billz **and** the stock was read after the push (`billz_pushed_at <
ProductStock.synced_at`) — a sale deferred for a closed shift or dead for staff never puts the bought unit back on
sale at the next sync. A cancelled order whose sale never reached Billz stops counting at the next sync.

**The window that cannot be closed**: Billz created the sale (b) and the worker died before our commit — or Billz
answered the create with the correlation id only — so the retry creates a second, empty draft (no items, no stock
effect, visible to the cashier). Staff delete it in Billz. If Billz confirms that `POST /v2/order` honours a
client-supplied `id` (the schema lists the field without a description), sending `Purchase.billz_order_uuid` there
would close the window.

## 7. Error mapping and what happens to the outbox message

Business codes come from `data.error_code` (the async envelope and the sale methods' `400`), string codes from
`error`/`error.code`. Outcomes of the `billz.push_sale` message:

* **Retryable** → `BillzUnavailable` → the outbox retries with backoff; ten failures make the message dead.
* **Deferred** (no attempt spent, `outbox.RetryLater`): `BillzShiftClosed` every `BILLZ_SHIFT_RETRY_MINUTES` for up
  to `BILLZ_SHIFT_RETRY_MAX_HOURS` after the first refusal (`OutboxMessage.deferred_since`), then dead "shift closed:
  … (deferred since …, gave up after 36 h)" (D2); `BillzPaymentPending` until the payment's quiet period ends.
* **Configuration** → `BillzMisconfigured` (retryable, ERROR `billz.configuration_error` on every occurrence, D3).
* **Permanent** → `BillzRejected` (incl. `BillzCheckByHand`) → the handler raises `outbox.PermanentFailure` → the
  message is **dead at the first attempt**, `last_error` = "Billz refused the sale of order N: <reason> (Billz error
  <code>) [HTTP 400]", audited `outbox.dead` (admin audit log), ERROR lines `billz.sale_rejected` and
  `billz.sale_dead … billz_order_id=<the draft, if any>`; the purchase stays unpushed and staff record (or complete)
  the sale in Billz by hand.

| Code / status | Meaning (docs) | Class |
|---|---|---|
| 20084 | bill not calculated yet (async recalculation running) | retryable |
| 20020 | payments do not match the bill | retryable (the next attempt re-reads the bill) |
| 20015 | sale has no items | retryable (the next attempt re-adds missing items) |
| 20010, 20082, 20033 | no open cash box / cash box closed / no shift | **deferred** (D2) |
| 20009 | cash box of another shop | configuration (D3) |
| 20085 | seller has no access to the shop | configuration (D3) |
| 20011 | cash box has not enough money | permanent |
| 20016 | not enough stock in the shop | permanent |
| 20017 | sale deleted | permanent |
| 20036, 20037 | product not found / archived | permanent (after one fresh lookup) |
| 20047 | not enough customer balance (we never pay with cashback) | permanent |
| 20063 | sale not sent to the OFD (`skip_ofd` not used, D4) | permanent — see open questions |
| any other `2xxxx` | undocumented | permanent (never retried forever) |
| `400`/`422` without a business code | validation | permanent |
| `404` of `GET /v2/order/{id}` | the persisted sale is gone | permanent |
| `404` of any other route, `3xx`, `403` | wrong base URL, route not granted to the key, company blocked | configuration (D3) |
| `401` | token | renew once, then retryable |
| `408`, `429`, `5xx`, transport, undecodable answer, `TIMEOUT`, `TOO_MANY_REQUESTS`, `rate_limited` | transient | retryable (+ circuit breaker, §4) |
| Redis (shared cache) down | — | retryable, nothing sent |
| bill ≠ expected, payment outcome still unknown after 10 min | our own stop (`BillzCheckByHand`) | permanent, staff check |

**Replaying dead sales** (D3): after fixing the setting, `uv run python manage.py requeue_outbox --topic
billz.push_sale --dead [--since 2026-10-26T09:00:00+05:00] [--dry-run]` lists the dead messages (dedupe key, attempts,
reason), puts them back to `pending` with a fresh attempt budget and audits each (`outbox.requeued`). Run it with
`--dry-run` first and check that none of the listed orders was entered in Billz by hand meanwhile (a replay would
record it a second time); a sale whose draft exists resumes on that draft.

## 8. Explicit stubs (product-owner decision 2026-10-26)

Each is a `BillzPort` method that raises `BillzNotSupported` (code `billz_not_supported`) in the live **and** the
fake adapter — nothing is invented:

| Stub | Why | What happens instead |
|---|---|---|
| `refund_sale(billz_order_id)` | `openapi.yaml` documents no refund/return of a paid sale (only draft deletion, postpone/return-postpone of drafts) | Staff refund the sale by hand in Billz: cancelling an order whose purchase holds a Billz sale logs ERROR `billz.refund_by_hand order_id=… billz_order_id=… pushed=…` (monitoring alerts). An admin attention item would need a contract change — proposed to the lead. |
| `find_store_sale(receipt_ref)` | no lookup of a sale by receipt and no sales feed for per-receipt discovery after a POS QR scan (open questions L6/L7) | In-store purchases are not credited automatically; `loyalty.services.record_store_purchase` stays the entry point once a scheme is agreed with Billz. |
| `parse_webhook(body)` | Billz sends no webhooks ("Вебхуков пока не предусмотрено") | The catalog is polled (§5). |

## 9. Fake mode guarantees kept

The M1/M2 fake rules are unchanged (the e2e scripts rely on them). Fake + production settings: no import, no stock,
no `billz_id` lookups (409), no sale recorded (the handler does not call the fake), no beat activity.

## 10. Environment variables

Every Billz value is stripped where it is read (a `.env` saved with CRLF line endings would otherwise leave a `\r`
that the shop id never matches), and the production settings validate exactly the value the adapter uses.

| Variable | Required | Default / example | Secret |
|---|---|---|---|
| `BILLZ_MODE` | no (default `INTEGRATIONS_MODE`, i.e. `fake`) | `live` | no |
| `BILLZ_SECRET_KEY` | live (production refuses to start without it) | issued in Billz → Settings → Company → Integration keys | **yes** |
| `BILLZ_SHOP_ID` | live (a UUID; the all-zero placeholder is refused) | UUID from `GET /v1/shop` | no |
| `BILLZ_CASHBOX_ID` | live (idem) | UUID from `GET /v1/cash-box` | no |
| `BILLZ_PAYMENT_TYPE_ID` | live (idem) | UUID from `GET /v1/company-payment-type` (the "online"/card type) | no |
| `BILLZ_BASE_URL` | no (`https://api-admin.billz.ai`; production requires https) | | no |
| `BILLZ_PLATFORM_ID` | no (`7d4a4c38-dd84-4902-b744-0488b80a4c01`, documented static value) | | no |
| `BILLZ_RATE_LIMIT_PER_SECOND` | no (`1.5`; 0 < rate ≤ 2, Billz's own limit) | `1.2` | no |
| `BILLZ_CONNECT_TIMEOUT_SECONDS` / `BILLZ_READ_TIMEOUT_SECONDS` | no (`5` / `30`; at most `30` / `60`) | | no |
| `BILLZ_PAGE_SIZE` | no (`300`, Billz's maximum; 1..300) | | no |
| `BILLZ_FULL_SYNC_MIN_RATIO` | no (`0.8`; 0 < r ≤ 1) | | no |
| `BILLZ_VOLUME_FIELD` | no (`""` = volume not synced: the stored volume is kept) | `Объём` | no |
| `BILLZ_SHIFT_RETRY_MINUTES` | no (`15`; 1..1440) | closed-shift retry interval (D2) | no |
| `BILLZ_SHIFT_RETRY_MAX_HOURS` | no (`36`; 1..168) | how long a closed shift is waited for (D2) | no |

`is_configured()` (admin `products/stats` `billz.connected`) is true for live only when the key and the three ids
are set. Constants (not settings): push deadline 240 s, payment quiet period 10 min, sale lock 10 min, circuit
breaker 5 failures / 90 s, renewal bound 40 s within a 90 s lock, interactive lookup budget 25 s.

## 11. Verifying against a real Billz account

Once the client issues an integration key (with the sale and client routes granted):

1. Read the shop, cash box and payment type ids: `GET /v1/shop`, `GET /v1/cash-box?shop_id=…`,
   `GET /v1/company-payment-type` with the token from `POST /v1/auth/login` (curl, never committed).
2. Set the variables of §10 on a **staging** deployment, `BILLZ_MODE=live`; start a worker and beat.
3. `uv run python manage.py shell -c "from glowderma.catalog import billz; print(billz.get_port().get_product('<a barcode>'))"`
   → the product with the shop's stock and retail price. Check: `active_measurement_value` is the sellable stock,
   `retail_price` is in so'm (not tiyin), `BILLZ_VOLUME_FIELD` yields the volume.
4. Admin → Products → Billz import `full`: the job succeeds; `IntegrationState(billz).data` holds `full_count`; no
   product hidden that Billz still sells. Check that `include_empty_stocks=true` lists zero-stock products and that
   paging is stable (no duplicates/missing barcodes between pages).
5. Change one product in Billz, wait a minute, run a `delta`: it arrives (verifies the UTC watermark — the docs
   ask to check the offset with "change time − 1 minute").
6. Refresh: set the cached access expiry in the past (`cache.get("billz:auth:tokens")` in a shell, rewrite
   `access_expires_at`) and import again → one `POST /v2/auth/refresh` with the `platform-id` header, no login.
7. Sale: pay a test order on staging (Click/Payme sandbox) for one cheap product. Verify in Billz → Sales: the
   sale in the configured shop, the item and quantity, the client by phone, comment `Glow Derma #<order>`, no
   cashback accrued, and in Glow Derma `Purchase.billz_sale_id` = the Billz sale id. Then open `GET /v2/order/{id}`
   and **confirm what `finished_at` (and `order_status`) look like for a draft and for a paid sale** — the resume
   rule (§6 a) relies on `finished_at` being empty or Go's zero time on a draft.
8. Kill the worker between steps (e.g. after the item add) and let the outbox retry: one sale, one payment.
9. Close the shift and push a sale: expect 20010/20082 → the message is deferred (`failed`, attempts unchanged,
   `available_at` 15 min later, `last_error` "shift closed: …"); open the shift: the next run pays the same draft.
10. **Free price (D1)**: on a product whose Billz retail price differs from the admin price, check that the sale
    shows the admin price per item, that `POST /v1/recalculate-order-bill` returns exactly Σ unit price × quantity
    (no Billz client discount, promo or rounding on top — otherwise every such sale stops for staff), and whether the
    documented `free_price` example's `is_manual: true` is needed. Link a client with a Billz discount and check the
    bill again.
11. **Paid signals**: after a payment, check `finished_at`, `sold_at`, `order_status` and
    `order_detail.order_payments` of `GET /v2/order/{id}`; the resume rule (§6 a) reads `finished_at`/`sold_at` and
    an explicit empty `order_payments`.
12. Redis down / Billz down: stop Redis → admin product create answers 503; block the Billz host → after 5 failures
    the ERROR `billz.circuit_open` appears and calls fail fast for 90 s.

## 12. Open points for the product owner / Billz

Decided provisionally by the lead (2026-10-26, `docs/decisions/open-questions.md` BZ-D1–BZ-D4): the closed shift
(D2, was question 1), the price recorded in Billz (D1, was question 3), configuration failures (D3). Still open:

1. **Fiscal receipt (20063, D4)**: online payments are fiscalised by the payment provider; should Billz sales use
   `skip_ofd: true`? Not sent today (permanent error if Billz requires OFD). **Product owner.**
2. **Ask Billz**: does `use_free_price`/`free_price` fix the line price against client discounts and promos, and is
   `is_manual: true` (in the documented example) required? Does `POST /v2/order` honour a client-supplied `id`?
   Which signals mark a paid sale (`finished_at`, `sold_at`, `order_status` literals, `order_payments`)? Is the
   `/v2/products` paging order stable? Is `Retry-After` ever sent?
3. **Billz refunds**: no API — every online refund needs a manual refund in Billz (ERROR `billz.refund_by_hand`);
   an admin attention item is proposed.
4. **Night orders**: with D2 a sale paid at night waits up to 36 h for the shift; orders paid on a long holiday are
   dead-lettered "shift closed" and entered by hand (or replayed with `requeue_outbox`).

## 13. Tests and fixtures

`glowderma/integrations/tests/fixtures/billz/*.json` are answers built from the `openapi.yaml` response schemas and
the guide's worked examples (each names its source operation in `_source`); `billz_support.BillzServer` serves them
through `httpx.MockTransport` with the state a real Billz keeps (tokens, sales, items, clients, payments), so tests
can crash a push after any step; it honours `use_free_price` in the bill. No test touches the network. Coverage:
`integrations/tests/test_billz_live.py` (auth, rotation, single-flight refresh under thread contention, 401, limiter
across clients and threads, 429, 5xx bounds, error mapping, pagination, shop mapping, watermark, reconciliation
guards, sale push, free price, bill check, resume, payment attempts, locks, deadline, stubs),
`integrations/tests/test_billz_live_robustness.py` (Redis down, undecodable answers, renewal under lock expiry,
token lifetimes, the interactive profile, circuit breaker, search paging, listing consistency),
`integrations/tests/test_billz_settings.py`, `catalog/tests/test_billz_live_sync.py` (hide-not-delete, partial
failure, plausibility, overlapping pages, products created during a run, volume, job codes, beat, 503 answers),
`commerce/tests/test_billz_push.py` (dispatcher end to end, dead letters, deferrals, cancel races, requeue, §6.14),
`commerce/tests/test_billz_stock.py` (available stock while a sale is unpushed), `core/tests/test_outbox_retry_later.py`
and `core/tests/test_requeue_outbox.py`. Each finding of the two reviews has a regression test that names it.

Where the docs disagree, the stricter reading is implemented: `GET /v1/client` `phone_number` is "exact" in
`openapi.yaml` but "substring" in the guide, so results are filtered on an exact digits match.
