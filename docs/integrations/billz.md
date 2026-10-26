# Billz live adapter (M3)

> **Not verified against a live Billz account.** Everything below is built from the BILLZ 2.0 public API
> documentation at docs.billz.io — `openapi.yaml` (version `2026.08`, the source of truth) and its prose guide —
> and tested only against recorded answers built from that schema. Run the checklist in
> [§11](#11-verifying-against-a-real-billz-account) before switching production to `BILLZ_MODE=live`.

Billz is the shop's POS/ERP. It owns **stock** and the **Billz product name**; Glow Derma owns texts, images,
visibility, the promo flag and (provisionally, O6) the price. Glow Derma reads the catalog from Billz and pushes
every paid online order back as a Billz sale.

Code: `glowderma/integrations/billz/` — `ports.py` (the port, typed errors, stubs), `fake.py`, `live.py`
(catalog sync, sale push), `client.py` (auth, rate limiter, retries, error mapping), `registry.py`. Orchestration:
`glowderma/catalog/tasks.py` (import job, beat), `glowderma/catalog/services.py` (`upsert_from_billz`,
`hide_missing_from_billz`), `glowderma/commerce/tasks.py` (`billz.push_sale`), `glowderma/commerce/billz_sales.py`,
`glowderma/loyalty/services.py` (`record_billz_order`, `link_billz_sale`).

## 1. Modes

| `BILLZ_MODE` | Adapter | Behaviour |
|---|---|---|
| `fake` (default, `INTEGRATIONS_MODE`) | `FakeBillz` | Deterministic catalog (unchanged M1 rules). Sale push accepted without a Billz sale id; scenario `{"push": "down"}` → transient, `{"push": "rejected"}` → permanent. Under the production settings the fake records no sale and no stock (customer contract §6.14): the import is refused, the `billz.push_sale` handler never calls it (WARNING `billz.sale_not_pushed`). The beat syncs do nothing. |
| `live` | `LiveBillz` | The BILLZ 2.0 public API. The production settings refuse to start without the key and the three ids (§10). |

## 2. Endpoints used

All paths are relative to `BILLZ_BASE_URL` (default `https://api-admin.billz.ai`, `servers[0]` of `openapi.yaml`).

| Method and path | `operationId` | Used for | Sync/async | Repeated after an unknown outcome? |
|---|---|---|---|---|
| `POST /v1/auth/login` | `post_v1_auth_login` | key → access + refresh token | sync | yes (creates a session only) |
| `POST /v2/auth/refresh` | `post_v2_auth_refresh` | token rotation (`platform-id` header) | sync | yes, within Billz's 5 s same-answer window; then login |
| `GET /v2/products` | `get_v2_products` | catalog pages; barcode lookup (`search` + `fields=barcode`) | sync | yes (read) |
| `POST /v2/order` | `post_v2_order` | create the sale (`{shop_id, cashbox_id}`) | async → `Billz-Response-Channel: HTTP` | **no** (see §7 window) |
| `GET /v2/order/{id}` | `get_v2_order_id` | state of a sale on resume (deleted, paid, items, client) | sync | yes (read) |
| `POST /v2/order-product/{order_id}` | `post_v2_order_product_order_id` | add an item (upsert, absolute quantity) | async → header | yes (idempotent upsert) |
| `GET /v1/client?phone_number=` | `get_v1_client` | find the customer by phone | sync | yes (read) |
| `POST /v1/client` | `post_v1_client` | create the customer (`{phone_number}` only) | sync | **no** (Billz does not de-duplicate phones) |
| `PUT /v2/order-customer-new/{order_id}` | `put_v2_order_customer_new_order_id` | link the customer (`check_auth_code: false`) | async → header | yes (idempotent) |
| `POST /v1/recalculate-order-bill/{order_id}` | `post_v1_recalculate_order_bill_order_id` | the bill total to pay | sync | yes |
| `POST /v2/order-payment/{order_id}` | `post_v2_order_payment_order_id` | pay and close the sale | sync | **no**: the next attempt reads the sale first |

Setup only (by hand, to fill the settings): `GET /v1/shop` (`get_v1_shop`), `GET /v1/cash-box`
(`get_v1_cash_box`), `GET /v1/company-payment-type` (`get_v1_company_payment_type`).

## 3. Auth and tokens

* `POST /v1/auth/login {"secret_token": BILLZ_SECRET_KEY}` → `data.access_token` (`expires_in`, 15 days for a key
  login), `data.refresh_token` (one use, 30 days).
* `POST /v2/auth/refresh {"refresh_token"}` with header `platform-id: BILLZ_PLATFORM_ID` (the only call that needs
  it, per `openapi.yaml` `info`) → a new pair (`expires_in`, `refresh_expires_in` read from the answer).
  Rotation: a spent refresh token presented again revokes the whole session family (`400 invalid_grant`, "token
  reuse detected").
* The pair lives **only in the Django cache** (Redis DB 0, key `billz:auth:tokens`, TTL = the longer of the two
  lifetimes), never in the database, never in a log line, never in a URL. The key is kept out of `repr`.
* Renewal is **single-flight across processes**: the winner of `cache.add("billz:auth:lock", …, 90 s)` refreshes
  (or logs in) and stores the new pair; every other worker re-reads the cache every 0.2 s until the pair changes
  (gives up after 45 s with `BillzUnavailable`). A token within 5 minutes of expiry is renewed before use.
* A refused refresh (400/401/403: expired, reused, revoked) falls back to **one** fresh login. A refresh token past
  its stored expiry is not tried.
* A `401` on a normal call renews once and repeats the call once; a second `401` is `BillzUnavailable` (never a
  loop). A refused login (4xx) is `BillzAuthError` (an operator must check the key).

## 4. Rate limit, retries, timeouts

* Billz answers `429` above about **2 requests/second per IP**, counted over the whole integration. One limiter in
  the cache serves every worker: time is cut into slots of `1 / BILLZ_RATE_LIMIT_PER_SECOND` seconds and a request
  may start only at the beginning of a slot it won with `cache.add` (atomic `SET NX`), so request starts are at
  least one slot apart whichever process sends them. Hosts need NTP-synced clocks. Login and refresh go through
  the limiter too. A request is never queued more than 60 s ahead (`BillzUnavailable`).
* `429`: every worker pauses (`billz:rate:pause`) for `Retry-After` seconds when Billz sends it (delta or HTTP
  date, capped at 60 s — the docs do not mention the header), else for an exponential backoff with jitter; at most
  5 times per call, then `BillzUnavailable`.
* Transport errors and `5xx`: up to 4 attempts with exponential backoff and jitter (0.5 s · 2ⁿ⁻¹ · [0.5, 1),
  capped at 8 s) — **only** for calls that are safe to repeat (reads, upserts, login/refresh) or when the request
  certainly never reached Billz (connect errors). A write whose outcome is unknown (read timeout, `5xx`, `408` of an
  async method, an async answer carrying only the correlation id) raises `BillzUnavailable` and is never repeated
  blindly (the docs: "408 … повторять запись вслепую нельзя").
* Timeouts: connect `BILLZ_CONNECT_TIMEOUT_SECONDS` (5), read/write `BILLZ_READ_TIMEOUT_SECONDS` (30). Billz holds
  an async call open for up to 600 s before its own `408`; a read timeout before that is an unknown outcome.
* httpx's own request log lines (full URL) are silenced (`LOGGING`: `httpx`/`httpcore` at WARNING) because
  `GET /v1/client?phone_number=…` carries a phone number. Our log lines use `pii_ref(phone)`.

## 5. Catalog sync

**Mapping** (`GET /v2/products`, `include_empty_stocks=true` so products at zero stock are listed):

| Glow Derma (`BillzProduct`) | Billz field | Rule |
|---|---|---|
| `billz_id` | `barcode` | The catalog's `billz_id` is 6–14 ASCII digits; Billz's own product id is a UUID, so products are keyed by **barcode**. A product without such a barcode is skipped (logged count). A barcode listed twice keeps the first product (WARNING). |
| `billz_name` | `name` | trimmed, ≤ 255 |
| `volume` | the `product_attributes[].attribute_value` or `custom_fields[].custom_field_value` named `BILLZ_VOLUME_FIELD` (case-insensitive; custom fields also by `custom_field_system_name`) | `""` when the setting is empty or the product lacks it (the docs define no volume field) |
| `price` (stored as `billz_price`, reference only) | `shop_prices[shop_id = BILLZ_SHOP_ID].retail_price` | rounded so'm; `None` without an entry for the shop or with a currency other than UZS. `promo_price` is ignored (Glow Derma owns the promo flag and price, O6). A new product starts at this price, `hidden`. |
| `stock` | `shop_measurement_values[shop_id = BILLZ_SHOP_ID].active_measurement_value` | sellable stock (holds excluded); floored to whole units; `0` without an entry for the shop |

The Billz product UUID of each barcode is cached (`billz:product:<barcode>`, 7 days) for the sale push.

**Delta** (hourly, or admin "delta"): `last_updated_date` = start of the last successful run − 5 minutes (UTC,
`yyyy-mm-dd hh:mm:ss`); without a cursor it lists everything. The cursor lives in `IntegrationState(billz).data`
(`delta_since`, `full_count`, `full_synced_at`) and moves **only** through `BillzPort.sync_succeeded()`, which the
import task calls after every row was applied; a run that fails anywhere (a page, an upsert, the reconciliation)
is fetched again next time. A delta tolerates the result count changing while paging (sales update products); the
daily full catches anything a shifted page skipped.

**Full** (daily, or admin "full") = reconciliation. The listing is refused (`BillzListingIncomplete`, job error code
`billz_listing_incomplete`, nothing applied, nothing hidden, no cursor move) unless:
* every page arrived (`received ≥ count`, at most 10 000 pages),
* `count` did not change while paging (pages are offsets: a shift could skip a product and hide it wrongly),
* it is plausible: `count > 0` and `count ≥ ceil(BILLZ_FULL_SYNC_MIN_RATIO × previous full count)`.

Then the job upserts every row and `catalog.services.hide_missing_from_billz` turns every **active** product whose
`billz_id` Billz no longer lists into `hidden` — unless that would hide more than
`max(3, active products − ceil(BILLZ_FULL_SYNC_MIN_RATIO × active products))` in one run (a barcode-keying
mismatch or a wrong `BILLZ_SHOP_ID` would "lose" most of the catalog): then nothing is hidden, the job fails with
`billz_listing_incomplete` and the cursor stays. Hidden products are **never deleted** (texts, images, orders and history stay; staff
re-activate it when it returns). Each hide sets `last_change = hidden` without an actor and writes
`product.updated` (`status` diff, actor `system`, reason `billz_missing: …`). `Job.result` of a full run gains
`hidden` (count); a delta's result is unchanged.

**Job errors**: every Billz error fails the job with the generic message `provider_unavailable` ("Tashqi xizmat
javob bermadi. Qayta urinib ko‘ring."); the code is `provider_unavailable` or `billz_listing_incomplete`.

**Beat** (`config/celery.py`, Tashkent time): `billz-sync-delta` → `catalog.billz_sync_delta` every hour at :07;
`billz-sync-full` → `catalog.billz_sync_full` daily at 04:23. Both start the `billz_import` job through
`integrations.services.start_job` (actor `system`), so they single-flight with an admin import and the stale-job
reaper covers them. With `BILLZ_MODE=fake` (any settings) they do nothing: no job, no audit row, no `synced_at`
churn. With live but unconfigured they log a WARNING and do nothing. A full that finds another import running
retries every 15 minutes, 4 times, then skips the day (WARNING).

## 6. Sale push (`billz.push_sale`)

Enqueued at payment confirmation (M2). The handler skips a purchase already pushed (`billz_pushed_at`), refuses a
payload of another purchase, never calls a fake Billz under the production settings (§1), and never completes the
sale of an order cancelled meanwhile (WARNING `billz.sale_skipped_cancelled`; when a Billz sale was already
created, ERROR `billz.sale_of_cancelled_order … billz_order_id=…` so staff delete the draft or refund it in Billz).
A per-sale cache lock (`billz:sale:lock:<order uuid>`, 10 min) keeps two workers off the same sale (the second
retries later).

Steps (llms-full "Интернет-заказ из внешней системы"), each safe to resume:

| Step | Call | Resume rule |
|---|---|---|
| a. look up | `GET /v2/order/{Purchase.billz_sale_id}` when an earlier attempt created the sale | `deleted` → permanent (20017); `finished_at` holds a real date → already paid: nothing more is sent. Billz has no lookup by external reference and no idempotency header (docs: "Заголовка идемпотентности" — not provided), so **our persisted id is the lookup**. |
| b. create | `POST /v2/order {shop_id: BILLZ_SHOP_ID, cashbox_id: BILLZ_CASHBOX_ID}` + `Billz-Response-Channel: HTTP` | The answer's `id` (= `data.id`) and `data.order_number` are committed onto the purchase **before anything else is sent** (`loyalty.services.record_billz_order` → `billz_sale_id`, `billz_sale_ref`; `billz_pushed_at` stays null). |
| c. items | `POST /v2/order-product/{id} {product_id, sold_measurement_value, used_wholesale_price: false}` + header | One call per product (lines of one product summed). An upsert with the absolute quantity; skipped when the sale already holds that quantity. Barcode → product UUID from the cache or `GET /v2/products?search=<barcode>&fields=barcode` (exact match); a cached id Billz rejects with 20036 is looked up once more. |
| d. customer | `GET /v1/client?phone_number=<+998…>` (exact digits match on `phone_numbers`), `POST /v1/client {phone_number}` only when none, then `PUT /v2/order-customer-new/{id} {customer_id, check_auth_code: false}` + header | The client id is cached by `pii_ref(phone)` for 30 days (Billz's search lags behind a create: "Только что созданный клиент может не найтись сразу"). Skipped when the sale already carries that client. No name or other PII is sent. |
| e. bill | `POST /v1/recalculate-order-bill/{id}` → `total_price` | Payment must equal the bill (20020). A bill that differs from Glow Derma's total (Billz prices vs admin prices, O6) is **paid as billed** and logged (WARNING `billz.sale_total_differs`). |
| f. pay | `POST /v2/order-payment/{id} {payments: [{company_payment_type_id: BILLZ_PAYMENT_TYPE_ID, paid_amount: total, returned_amount: 0}], comment: "Glow Derma #<order>", without_cashback: true}` | Synchronous. An unknown outcome is not repeated: the next attempt starts at (a). |

Then the handler links the purchase (`link_billz_sale`, `billz_pushed_at` = now) and writes `billz_sale_recorded`
once with `billz_order_id` = Billz's sale UUID (the fake echoes our `billz_order_uuid`, as in M2).

**The window that cannot be closed**: Billz created the sale (b) and the worker died before our commit — the retry
creates a second, empty draft (no items, no stock effect, visible to the cashier). Staff delete it in Billz. If
Billz confirms that `POST /v2/order` honours a client-supplied `id` (the schema lists the field without a
description), sending `Purchase.billz_order_uuid` there would close the window.

## 7. Error mapping

Business codes come from `data.error_code` (the async envelope and the sale methods' `400`), string codes from
`error`/`error.code`. Retryable → `BillzUnavailable` (the outbox retries with backoff; ten failures make the message
dead). Permanent → `BillzRejected` → the handler raises `outbox.PermanentFailure` → the message is **dead at the
first attempt**, `last_error` = "Billz refused the sale of order N: <reason> (Billz error <code>) [HTTP 400]",
audited `outbox.dead` (admin audit log), ERROR lines `billz.sale_rejected` and `billz.sale_dead`; the purchase stays
unpushed and staff record the sale in Billz by hand.

| Code / status | Meaning (docs) | Class |
|---|---|---|
| 20084 | bill not calculated yet (async recalculation running) | retryable |
| 20020 | payments do not match the bill | retryable (the next attempt re-reads the bill) |
| 20015 | sale has no items | retryable (the next attempt re-adds missing items) |
| 20009 | cash box of another shop | permanent (configuration) |
| 20010, 20082, 20033 | no open cash box / cash box closed / no shift | permanent (brief: closed shift) — see open questions |
| 20011 | cash box has not enough money | permanent |
| 20016 | not enough stock in the shop | permanent |
| 20017 | sale deleted | permanent |
| 20036, 20037 | product not found / archived | permanent |
| 20047 | not enough customer balance (we never pay with cashback) | permanent |
| 20063 | sale not sent to the OFD (`skip_ofd` not used) | permanent — see open questions |
| 20085 | seller has no access to the shop | permanent |
| any other `2xxxx` | undocumented | permanent (never retried forever) |
| `400`/`422` without a business code, `404` | validation / not found | permanent |
| `403` | the key may not call this route / company blocked / key not activated | permanent |
| `401` | token | renew once, then retryable |
| `408`, `429`, `5xx`, transport, `TIMEOUT`, `TOO_MANY_REQUESTS`, `rate_limited` | transient | retryable |

## 8. Explicit stubs (product-owner decision 2026-10-26)

Each is a `BillzPort` method that raises `BillzNotSupported` (code `billz_not_supported`) in the live **and** the
fake adapter — nothing is invented:

| Stub | Why | What happens instead |
|---|---|---|
| `refund_sale(billz_order_id)` | `openapi.yaml` documents no refund/return of a paid sale (only draft deletion, postpone/return-postpone of drafts) | Staff refund the sale by hand in Billz. Not wired into the refund flow yet: the cancel/refund code has no hook that could show staff "refund the Billz sale by hand" without a contract change — proposed to the lead (attention item or order event). |
| `find_store_sale(receipt_ref)` | no lookup of a sale by receipt and no sales feed for per-receipt discovery after a POS QR scan (open questions L6/L7) | In-store purchases are not credited automatically; `loyalty.services.record_store_purchase` stays the entry point once a scheme is agreed with Billz. |
| `parse_webhook(body)` | Billz sends no webhooks ("Вебхуков пока не предусмотрено") | The catalog is polled (§5). |

## 9. Fake mode guarantees kept

The M1/M2 fake rules are unchanged (the e2e scripts rely on them). Fake + production settings: no import, no stock,
no `billz_id` lookups (409), no sale recorded (the handler does not call the fake), no beat activity.

## 10. Environment variables

| Variable | Required | Example | Secret |
|---|---|---|---|
| `BILLZ_MODE` | no (default `INTEGRATIONS_MODE`, i.e. `fake`) | `live` | no |
| `BILLZ_SECRET_KEY` | live (production refuses to start without it) | issued in Billz → Settings → Company → Integration keys | **yes** |
| `BILLZ_SHOP_ID` | live | UUID from `GET /v1/shop` | no |
| `BILLZ_CASHBOX_ID` | live | UUID from `GET /v1/cash-box` | no |
| `BILLZ_PAYMENT_TYPE_ID` | live | UUID from `GET /v1/company-payment-type` (the "online"/card type) | no |
| `BILLZ_BASE_URL` | no (`https://api-admin.billz.ai`; production requires https) | | no |
| `BILLZ_PLATFORM_ID` | no (`7d4a4c38-dd84-4902-b744-0488b80a4c01`, documented static value) | | no |
| `BILLZ_RATE_LIMIT_PER_SECOND` | no (`2`) | `1.5` | no |
| `BILLZ_CONNECT_TIMEOUT_SECONDS` / `BILLZ_READ_TIMEOUT_SECONDS` | no (`5` / `30`) | | no |
| `BILLZ_PAGE_SIZE` | no (`300`, Billz's maximum; 1..300) | | no |
| `BILLZ_FULL_SYNC_MIN_RATIO` | no (`0.8`; 0 < r ≤ 1) | | no |
| `BILLZ_VOLUME_FIELD` | no (`""` = volume not synced) | `Объём` | no |

`is_configured()` (admin `products/stats` `billz.connected`) is true for live only when the key and the three ids
are set.

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
9. Close the shift and push a sale: expect 20010/20082 → dead letter with the reason (decide §12 question 1).

## 12. Open points for the product owner / Billz

1. **Night orders and closed shifts**: payments need an open shift; per the brief a closed shift (20010, 20082,
   20033) is permanent, so every online order paid while the shop is closed dead-letters and is entered by hand.
   Alternative: treat them as "retry at shift opening" (a delayed retry) — needs a decision.
2. **Fiscal receipt (20063)**: online payments are fiscalised by the payment provider; should Billz sales use
   `skip_ofd: true`? Not sent today (permanent error if Billz requires OFD).
3. **Price authority in Billz (O6)**: sales are paid at Billz's bill total; when the admin price differs, Billz
   revenue differs from what the customer paid. Options: cart `order-manual-discount` (`CURRENCY` sets the final
   cart price, documented) or `use_free_price` per item (semantics not documented).
4. **Ask Billz**: does `POST /v2/order` honour a client-supplied `id`? What are the `order_status` literals of a
   draft and a paid sale? Is the `/v2/products` paging order stable? Is `Retry-After` ever sent? Does a key login
   need `platform-id` too?
5. **Billz refunds**: no API — every online refund needs a manual refund in Billz; proposed staff attention item.

## 13. Tests and fixtures

`glowderma/integrations/tests/fixtures/billz/*.json` are answers built from the `openapi.yaml` response schemas and
the guide's worked examples (each names its source operation in `_source`); `billz_support.BillzServer` serves them
through `httpx.MockTransport` with the state a real Billz keeps (tokens, sales, items, clients, payments), so tests
can crash a push after any step. No test touches the network. Coverage: `integrations/tests/test_billz_live.py`
(auth, rotation, single-flight refresh under thread contention, 401, limiter across clients and threads, 429,
5xx bounds, error mapping, pagination, shop mapping, watermark, reconciliation guards, sale push and resume, stubs),
`integrations/tests/test_billz_settings.py`, `catalog/tests/test_billz_live_sync.py` (hide-not-delete, partial
failure, plausibility, beat), `commerce/tests/test_billz_push.py` (dispatcher end to end, dead letters, §6.14).

Where the docs disagree, the stricter reading is implemented: `GET /v1/client` `phone_number` is "exact" in
`openapi.yaml` but "substring" in the guide, so results are filtered on an exact digits match.
