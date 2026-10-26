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
| (auth) | `POST /auth/login {login, password}` → `data.access_token` (1 day) | kept only in the cache (`gd:bts:token`, 23 h), fetched single-flight across processes; a 401 renews it once and repeats the request |
| `regions()` | `GET /v1/directory/regions`, then `GET /v1/directory/cities?regionCode=` per region (4 in parallel) | every page; any failure or an empty list → `BtsUnavailable` (never an empty catalog cached for 24 h) |
| `quote(region, city)` | `POST /v1/order-calculate/index {senderCityCode, receiverCityCode, pickup_type, dropoff_type: "courier", is_multiple_cost: 0, weight}` | weight = `BTS_DEFAULT_PARCEL_WEIGHT_G` in kg; `available: false`, 4xx → `BtsRejected("route_not_served")` (→ 409 `delivery_unavailable`) |
| `find_by_client_id(id)` | `GET /v1/order/list?beginDate=&endDate=&page=&per-page=50` | no `clientId` filter is documented: the last `BTS_CLIENT_LOOKUP_DAYS` (30) are scanned, newest matching parcel wins, deleted (1300) and expired (1400) parcels are skipped; any failure → `BtsUnavailable` (absence must be proven before a create) |
| `create(request)` | `POST /v1/order/add` | `clientId` = our order id; sender = `BTS_SENDER_*`; receiver = the order's recipient snapshot; `cargo {weight, piece: 1}`; the recipient comment is appended to the address in brackets |
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
| `1300`, `1400`, any other code | the code itself | recorded only |

Without the translation BTS's `300` ("in the sending office") would have marked orders delivered.

## Failure mapping

- No answer (connect failure, timeout, broken body) or HTTP 5xx → `BtsUnavailable` (the outbox retries, the
  admin cancel answers 503, polling counts it as unavailable). Only a request that provably never left is
  repeated by the adapter, so a create is never sent twice by it; a create whose outcome is unknown is adopted by
  the clientId lookup before the next attempt.
- HTTP 4xx → `BtsRejected`: `validation_error` (create 400/422 or `status: false`), `route_not_served` (quote),
  `parcel_not_found` (track 404), else `http_<status>`; the message is BTS's text with phone-like digit runs
  masked.
- A login BTS refuses, or a 401 after a fresh login → ERROR `bts.auth_failed` and `BtsUnavailable`
  (a credentials problem is not the parcel's fault).
- `cancel`: an unreadable answer → `BtsUnavailable` (never a made-up cancellation); a refusal →
  `CancelResult(ok=False, reason=<BTS message>)`.

Logs carry the endpoint path and the HTTP status only — never the token, password, phone, name or address.

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
| `BTS_CLIENT_LOOKUP_DAYS` | optional (30) | `30` | no |
| `BTS_DEFAULT_PARCEL_WEIGHT_G` | unchanged (1000) | `1000` | no |
| `BTS_WEBHOOK_SECRET` | always in production (unchanged; the inbound webhook) | ours, ≥ 32 chars | **yes** |

## Verify M3

1. **Status codes.** The tracking page's example (`400` "В офисе доставки") contradicts the status list (`400`
   internal transport, `800` delivery office), and the cancel answer calls `1300` "Cancelled Order" while the list
   calls it "Удалён". Confirm the codes on `apitest` and adjust `protocol.BTS_IN_NETWORK` / `port_status`.
2. **`code: null` on a new order** is read as `100` (at the sender). Confirm.
3. **clientId lookup.** No filter is documented, so a 30-day window is scanned (≤ 40 pages of 50, within the
   10 s bound). Ask BTS for a `clientId` filter on `/v1/order/list` (or whether `/v1/order/add` rejects a duplicate
   `clientId`); either would make the lookup one call. Also confirm which date `beginDate`/`endDate` filter on and
   that `per-page` is honoured on this list.
4. **`is_multiple_cost: 0` answer shape.** Only the multiple-cost answer is shown; the adapter accepts
   `data.price` or `data.<pickup>_to_courier.price`.
5. **`cost` of a created order** is not read as the tariff (its meaning is not documented); the order's quoted
   fee is stored. Confirm and switch if `cost` is the delivery charge.
6. **Create fields not sent:** `wcs_code` (unexplained), `ready_to_take` (should a parcel created right after
   payment be marked ready for the courier?), `takePhoto`, `bringBackWaybill`, cash on delivery, goods/IKPU
   (`cargo.options`). The recipient comment has no field and is appended to the address.
7. **HTTP 500 on data errors.** The docs show `500 "Failed to create order: …"` for a missing city id; such
   answers are retried as unavailable (then the outbox dead-letters → `shipment_failed`). If BTS confirms they
   are definitive, map them to `validation_error`.
8. **Production host.** `BTS_BASE_URL` defaults to `https://api.bts.uz`; take the real production URL from BTS.
9. **Webhook (not this adapter, the M2 view).** BTS's webhook page shows a **form-data** POST
   (`{"clientId", "orderId", "barcode", "status": {"id", "name"}, "date"}` inside the form) configured through
   `/v1/webhook/webhook-config` with an optional BTS token (`has_token`), not our `X-GD-Webhook-Token` header.
   The view parses JSON and reads `trackingNumber`/`barcode`; it must be checked against a real delivery (see the
   report to the lead).

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
- [ ] Configure the webhook URL and verify the view against a real notification.
