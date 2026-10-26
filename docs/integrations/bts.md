# BTS — live adapter (M3)

> **Not verified against a live sandbox.** No BTS account exists yet. The adapter is written from BTS's public
> API documentation (docs.bts.uz) and the customer contract (§2.13.3, §2.14.3); its tests replay the documented
> answers through `httpx.MockTransport`. Every point marked **verify M3** must be checked on BTS's test host
> (`https://apitest.bts.uz:28345`) before `BTS_MODE=live` goes to production.

Code: `glowderma/integrations/bts/live.py` (`LiveBts`), endpoints, payloads and the status translation in
`glowderma/integrations/bts/protocol.py`, port in `bts/ports.py`. Selected by `bts.registry.get_bts()` when
`BTS_MODE=live`; the fake is unchanged.

## Sources (read 2026-10-26)

docs.bts.uz (English pages; `/page/<name>`): `auth-login`, `auth-login-refresh`, `order-add`, `order-detail`,
`order-list`, `order-cancel`, `tracking`, `order-calculator`, `directory-regions`, `directory-cities`, `status`,
`webhook`, `order-cancellation`.

## Protocol

Every request: `Accept` / `Content-Type: application/json`, `language: uz`, and — except the login —
`Authorization: Bearer <access_token>`. Answers are `{"status": bool, "message", "status_code", "data"}`; lists
carry `data.items` with `_meta` pagination (`page`, `per-page`).

| Port method | BTS call | Notes |
|---|---|---|
| (auth) | `POST /auth/login {login, password}` → `data.access_token` (1 day) | kept only in the cache (`gd:bts:token:<sha256(base URL\|login)[:16]>`, 23 h), fetched single-flight across processes (45 s lock, the login bounded to half of it); a refused login is remembered 60 s; the cache down fails closed (one process-local login per token lifetime); a 401 renews it once and repeats the request |
| `regions()` | `GET /v1/directory/regions`, then `GET /v1/directory/cities?regionCode=` per region (4 in parallel) | every page; any failure or an empty list → `BtsUnavailable`; cached fresh 24 h and kept a week, refreshed single-flight (a stale list is served while a refresh fails or runs elsewhere) and every 6 h by the beat `delivery.refresh_bts_regions` |
| `quote(region, city)` | `POST /v1/order-calculate/index {senderCityCode, receiverCityCode, pickup_type, dropoff_type: "courier", is_multiple_cost: 0, weight}` | weight = `BTS_DEFAULT_PARCEL_WEIGHT_G` in kg; bounded at 4 s (a customer request waits); cached 5 min per (sender city, city, weight, pickup type); `available: false`, 4xx → `BtsRejected("route_not_served")` (→ 409 `delivery_unavailable`); a price that is not a finite number in [0, 100 000 000] → `BtsUnavailable` |
| `find_by_client_id(id, since)` | `GET /v1/order/list?beginDate=&endDate=&page=&per-page=50` | no `clientId` filter is documented: the window runs from the day before the shipment row was created (`since`, before its first create request) to a week ahead (`endDate`; the filtered date is not documented); the next page is followed while `_meta` says so or `_links.next` names it, and a full page with neither fails closed; the newest matching live parcel wins, deleted (1300) and expired (1400) parcels are skipped, two live ones are an ERROR `bts.duplicate_parcels`; any failure → `BtsUnavailable` (absence must be proven before a create) |
| `create(request)` | `POST /v1/order/add` | `clientId` = `BTS_CLIENT_ID_PREFIX` + our order id; `ready_to_take` = `BTS_READY_TO_TAKE` (true; every documented example sends it, we create only after payment); sender = `BTS_SENDER_*`; receiver = the order's recipient snapshot; `cargo {weight, piece: 1}`; the recipient comment is appended to the address in brackets |
| `track(tracking_number)` | `GET /v1/order/track?orderId=` | our tracking number **is** BTS's `orderId` (the waybill number) |
| `cancel(parcel)` | `GET /v1/order-cancel/index?orderId=` | BTS cancels only at status 100; `error.already_cancelled` → `ok` (idempotent) |

**Tracking number.** BTS's `track` and `cancel` take only the `orderId`, so the live adapter stores BTS's
`orderId` as both `Shipment.tracking_number` and `bts_order_id`. (BTS also prints a `barcode` on the sticker; it
is not stored.)

**Statuses.** BTS's reference list (`GET /v1/status/index`) differs from the codes the contract assumed: `100` at
the sender, `200` courier took it, `300` in the sending office, `400` internal transport, `500` sorting centre,
`600` in a bag, `700` in transit, `800` in the delivery office, `1100` courier delivering, `1200` delivered,
`1300` deleted (also the answer to a cancel), `1400` expired. Our port and `delivery/bts.py` keep the contract's
vocabulary (`100` accepted / cancellable, `200` handed to the courier, `300` delivered), so the adapter
translates:

| BTS code | Port status | Order effect |
|---|---|---|
| `100`, or `null` (a new order reports `code: null`, "У отправителя") | `100` | none (cancellable) |
| `200`, `300`, `400`, `500`, `600`, `700`, `800`, `1100` | `200` | `processing` → `handed_to_courier` |
| `1200` | `300` | → `delivered` |
| `1300`, `1400` | the code itself | recorded only; polling (and a webhook re-track) logs ERROR `bts.parcel_gone` the first time (staff act: re-create or refund) |
| any other code (`unknown` when not a number) | the code itself | recorded only |

Without the translation BTS's `300` ("in the sending office") would have marked orders delivered.

## Failure mapping

- No answer (connect failure, timeout, broken or undecodable body), HTTP 5xx, and an answer that says the request
  was **not processed** — a redirect (never followed), 408 / 425 / 429, 401 / 403 — → `BtsUnavailable` (the
  outbox retries, the admin cancel and the checkout answer 503, polling counts it as unavailable). Only a
  request that provably never left is repeated by the adapter, so a create is never sent twice by it; a create
  whose outcome is unknown is adopted by the clientId lookup before the next attempt.
- An answer of a shape the adapter cannot read (codes parse strictly, prices must be finite) → `BtsUnavailable`.
- Another HTTP 4xx → `BtsRejected`: `validation_error` (create 400/422 or `status: false`), `route_not_served`
  (quote), `parcel_not_found` (track 404), else `http_<status>`; the message is BTS's text with phone-like digit
  runs masked.
- A login BTS refuses, or a 401 after a fresh login → ERROR `bts.auth_failed` and `BtsUnavailable`
  (a credentials problem is not the parcel's fault).
- `cancel`: an unreadable answer → `BtsUnavailable` (never a made-up cancellation); a refusal →
  `CancelResult(ok=False, reason=<BTS message>)`.

**Locks.** `commerce.tasks.create_shipment` never calls BTS under the Order row lock: it commits its checks,
calls the lookup and the create holding only the session advisory lock `bts-create:{order_id}` (a second delivery
of the request retries later), and re-locks the order to record the outcome — a parcel of an order cancelled
meanwhile is cancelled at BTS as an orphan. What still runs BTS HTTP under a lock, by design and bounded: the
admin cancel and the orphan cancel (one `cancel` under the Order lock, which serialises them against the create's
recording), the webhook re-track and polling (`track` under the per-parcel advisory lock `bts:{order_id}`, which
serialises re-tracks so an older status never overwrites a newer one), and the checkout's quote on a cache miss
(4 s, inside the idempotency transaction; `delivery/calculate/` normally fills the cache first).

## Environment

| Variable | Required when | Example | Secret |
|---|---|---|---|
| `BTS_MODE` | — | `live` | no |
| `BTS_BASE_URL` | has a default; production with `BTS_MODE=live` requires https | `https://apitest.bts.uz:28345` (test) | no |
| `BTS_LOGIN` | `BTS_MODE=live` | from BTS | no |
| `BTS_PASSWORD` | `BTS_MODE=live` (no placeholder) | from BTS | **yes** |
| `BTS_SENDER_NAME` | `BTS_MODE=live` | `Glow Derma` | no |
| `BTS_SENDER_PHONE` | `BTS_MODE=live` (`+998XXXXXXXXX`) | `+998712000000` | no |
| `BTS_SENDER_ADDRESS` | `BTS_MODE=live` | `Toshkent sh., …` | no |
| `BTS_SENDER_CITY_CODE` | `BTS_MODE=live` (must be set explicitly: a city code of BTS's list) | `0101` | no |
| `BTS_PICKUP_TYPE` | optional (`courier` default, or `branch`) | `courier` | no |
| `BTS_SENDER_BRANCH_CODE` | `BTS_PICKUP_TYPE=branch` | `0124` | no |
| `BTS_CLIENT_ID_PREFIX` | optional; empty (default) = the bare order id, as every parcel so far. Give each environment that shares one BTS account its own tag so an order id reused after `seed_demo --reset` never adopts another environment's parcel. Letters, digits, `_`, `-`, at most 16, not ending in a digit | `stg-` | no |
| `BTS_READY_TO_TAKE` | optional (`true`) | `true` | no |
| `BTS_DEFAULT_PARCEL_WEIGHT_G` | unchanged (1000) | `1000` | no |
| `BTS_WEBHOOK_SECRET` | always in production (unchanged; the inbound webhook) | ours, ≥ 32 chars | **yes** |

## Verify M3

1. **Status codes.** The tracking page's example (`400` "В офисе доставки") contradicts the status list (`400`
   internal transport, `800` delivery office), and the cancel answer calls `1300` "Cancelled Order" while the list
   calls it "Удалён". Confirm the codes on `apitest` and adjust `protocol.BTS_IN_NETWORK` / `port_status`.
2. **`code: null` on a new order** is read as `100` (at the sender). Confirm.
3. **clientId lookup.** No filter is documented, so a window from the day before the shipment row to a week
   ahead is scanned (a few pages; ≤ 40 pages of 50 within the 10 s bound). Ask BTS for a `clientId` filter on
   `/v1/order/list` (or whether `/v1/order/add` rejects a duplicate `clientId`); either would make the lookup one
   call. Also confirm which date `beginDate`/`endDate` filter on (`senderDate`? `receiverDate`?), that `per-page`
   is honoured, and that the list carries `_meta` (a full page without it fails closed).
4. **`is_multiple_cost: 0` answer shape.** Only the multiple-cost answer is shown; the adapter accepts
   `data.price` or `data.<pickup>_to_courier.price`.
5. **`cost` of a created order** is not read as the tariff (its meaning is not documented); the order's quoted
   fee is stored. Confirm and switch if `cost` is the delivery charge.
6. **Create fields.** `ready_to_take` is sent (`BTS_READY_TO_TAKE`, true: every documented example sends it and
   we create only after payment; set false if staff must pack first). Not sent: `wcs_code` (unexplained),
   `takePhoto`, `bringBackWaybill`, cash on delivery, goods/IKPU (`cargo.options`). The recipient comment has no
   field and is appended to the address.
7. **HTTP 500 on data errors.** The docs show `500 "Failed to create order: …"` for a missing city id; such
   answers are retried as unavailable (then the outbox dead-letters → `shipment_failed`). If BTS confirms they
   are definitive, map them to `validation_error`.
8. **Production host.** `BTS_BASE_URL` defaults to `https://api.bts.uz`; take the real production URL from BTS.
9. **Webhook.** BTS posts its notification as form-data (`/page/webhook`: `{"clientId", "orderId", "barcode",
   "status": {"id", "name"}, "date"}`; `/page/test-update-status` adds `status.code`), configured through
   `/v1/webhook/webhook-config` with an optional BTS token (`has_token`, name and place undocumented). The view
   (`commerce/api/webhooks/bts.py`, contract §2.13.3) reads every shape (JSON, flat form fields with
   `status[id]`-style keys, `status` as a JSON string, a form field holding the JSON), finds the shipment by
   `clientId` or `orderId` (never the barcode), and re-tracks. Record a real delivery (content type, field
   names, `status.id` vs `code`) and BTS's retry policy for a 503.

## Webhook setup

Configure BTS's webhook (`/v1/webhook/webhook-config`, or BTS support) with
`webhook_url = https://<api host>/api/v1/webhooks/bts/?token=<BTS_WEBHOOK_SECRET>` and `has_token` off: BTS
cannot set our `X-GD-Webhook-Token` header (still accepted). The query string is stripped before anything is
stored; the edge proxy must redact `token=` in its access log. Use the same `BTS_CLIENT_ID_PREFIX` the API runs
with, so notifications of another environment's parcels are not ours.

## Sandbox checklist (once credentials exist)

- [ ] Staging: `BTS_MODE=live`, `BTS_BASE_URL=https://apitest.bts.uz:28345`, login/password, sender block.
- [ ] `GET delivery/regions/` lists BTS's regions and cities (Uzbek names with `language: uz`).
- [ ] `POST delivery/calculate/` for two cities → prices; an unserved city → 409 `delivery_unavailable`.
- [ ] Pay a delivery order → `bts.create_shipment` → shipment `created` with BTS's `orderId`; the parcel appears
      in BTS's cabinet with our `clientId`.
- [ ] Kill the worker between create and commit (or replay the outbox message) → the lookup adopts the parcel,
      no duplicate in the cabinet.
- [ ] Move the parcel through statuses on `apitest` (`test-update-status` page) and check polling: `200`/`300`
      → handed to courier, `1200` → delivered; note every code observed.
- [ ] Admin cancel at status 100 → cancelled; again → `already_cancelled` → ok; after 200 → refused (409).
- [ ] Wrong password → ERROR `bts.auth_failed`; expired token → one renewal.
- [ ] Configure the webhook URL (`…/webhooks/bts/?token=<secret>`, see "Webhook setup") and move a parcel on
      `apitest` (`test-update-status`): the delivery is accepted (`{"ok": true}`), re-tracked and applied; note
      its exact format.
