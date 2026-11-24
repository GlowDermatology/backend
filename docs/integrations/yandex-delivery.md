# Yandex Delivery — courier delivery (Express API)

> **Nothing here is verified against a live account.** GlowDerma has no Yandex Delivery account, token or test
> access yet (owner decision 2026-11-15: build against the documentation, live mode off). The adapter is written
> from the official Express API documentation (revision r22073422, read 2026-11-15); its tests replay the
> documented answers through `httpx.MockTransport` (`integrations/tests/live_fixtures.Script`). Yandex documents
> **no test environment** for this API, so the first real check is the supervised pilot order of §14. Every
> question the documentation does not settle is in `docs/decisions/open-questions.md` (YD-*).

Code map:

| Part | Where |
|---|---|
| Port, typed errors, dataclasses | `glowderma/integrations/yandex_delivery/ports.py` |
| Endpoints, payload builders, parsers (pure) | `glowderma/integrations/yandex_delivery/protocol/` |
| Live adapter (`LiveYandexDelivery`) | `glowderma/integrations/yandex_delivery/live.py` |
| Fake (`FakeYandexDelivery`, scenarios, `advance`) | `glowderma/integrations/yandex_delivery/fake.py` |
| Mode switch | `glowderma/integrations/yandex_delivery/registry.py` (`get_yandex_delivery(scenario)`) |
| Account check command | `glowderma/integrations/management/commands/yandex_delivery_check.py` |
| Status → stage / order effect (pure) | `glowderma/delivery/yandex.py` |
| Warehouse calendar, pricing (pure) | `glowderma/delivery/warehouse.py`, `glowderma/delivery/pricing.py` |
| Quotes, models, claim / issue writers | `glowderma/delivery/quotes.py`, `models.py`, `services.py`, `selectors.py` |
| Checkout options | `glowderma/commerce/quoting.py` |
| Dispatch, status sync, poll | `glowderma/commerce/yandex_flow.py` (handlers in `commerce/tasks.py`) |
| Single status writer, carrier cancel, returns | `glowderma/commerce/services.py` (`apply_yandex_claim`, `cancel_at_carrier`, `confirm_return`) |
| Exception-queue beat | `glowderma/commerce/delivery_review.py` |
| Callback | `glowderma/commerce/api/webhooks/yandex.py`, reference `glowderma/commerce/yandex_ref.py` |
| Per-order advisory lock | `glowderma/commerce/locks.py` |
| Admin reads (delivery object, filters, counters) | `glowderma/commerce/delivery_admin.py` |
| Ops console pages | `glowderma/delivery/admin.py` |

## 1. Sources

`https://yandex.com/support/delivery-profile/en/api/express` (EN) and the same paths under `/ru/` (RU), read as raw
Markdown on 2026-11-15: quickstart, overview, claim-process (status table), FAQ, errors, troubleshooting and the
method pages `IntegrationV2Tariffs`, `IntegrationV2CheckPrice`, `IntegrationV2ClaimsCreate`, `…ClaimsInfo`,
`…ClaimsBulkInfo`, `…ClaimsAccept`, `…ClaimsCancelInfo`, `…ClaimsCancel`, `…ClaimsReturn`,
`…ClaimsTrackingLinks`, `…OfferCalculate`, `…ClaimsJournal`. The Uzbekistan facts (Tashkent, prepaid balance,
dynamic tariff) come from `https://delivery.yandex.uz/ru/`; the API pages never name Uzbekistan, Tashkent or UZS.

## 2. What we use of the API, and why

Every request goes to `YANDEX_DELIVERY_BASE_URL` (default `https://b2b.taxi.yandex.net`) under
`/b2b/cargo/integration/v2`, with `Authorization: Bearer <YANDEX_DELIVERY_TOKEN>` (the OAuth token of the business
cabinet's *Integration* tab; documented as valid until the cabinet password changes), the required
`Accept-Language` (`YANDEX_DELIVERY_LANGUAGE`, `ru` or `en`) and JSON bodies. Ids travel in the query
(`claim_id`, `request_id`). The token never appears in a URL, a log line or an exception.

| Port method | Yandex call | Used by | Why / notes |
|---|---|---|---|
| `tariffs(point)` | `POST tariffs {start_point: [lon, lat], fullname}` | `manage.py yandex_delivery_check`, ops console «Yandex bilan tekshirish» | The documented way to learn which classes (`courier`, `express`, `cargo`) a point really has; the answer is stored in `IntegrationState(yandex).data["tariffs"]` and gates switching a method on in live mode |
| `check_price(source, destination, item, class)` | `POST check-price {route_points[{id, coordinates, fullname}], items[{quantity, pickup_point, dropoff_point, weight}], requirements{taxi_class}}` | checkout options (`commerce.quoting`), the check command | Outside Russia the documented first step (`offers/calculate` is "Russia only"). A **non-binding** estimate: `price`, `currency_rules.code`, `eta` (minutes), `distance_meters`, `zone_id`; no offer id, no validity |
| `create_claim(request)` | `POST claims/create?request_id=<UUID>` | dispatch (`commerce.yandex_flow`) | `request_id` is Yandex's idempotency token: "on 5xx or timeout retry with the same value"; a replay returns the old claim whatever the body. We store the UUID on `CourierClaim.request_id` **before** the call and every retry reuses it. Its documented 403 codes (`forbidden_by_antifake`, `buyout_disabled`, `payment_on_delivery_disabled`: `protocol.CREATE_FORBIDDEN_CODES`) are definitive refusals |
| `claim(claim_id)` | `POST claims/info?claim_id=` | dispatch, callback sync, cancel re-reads, dev advance | The authoritative status (`updated_ts` may lag 5–30 s, documented) |
| `claims(ids)` | `POST claims/bulk_info {claim_ids}` (≤ 1000 per call, chunked) | the 60 s poll | The reliable status path (decision 10); an entry Yandex omits or sends unreadably is skipped (WARNING `yandex.unreadable_claim`) |
| `accept(claim_id, version)` | `POST claims/accept?claim_id= {version}` | dispatch (§9 step 7) | **The only call that summons a courier** ("the search for a courier will only start after the claim is confirmed"). Sent at most once per claim, after `accept_sent_at` is committed (released and sent once more only when Yandex provably did not take it: "not processed", 409 `old_version`; §9 step 7); its answer is not proof (the documented example says `"status": "new"`): the claim is re-read |
| `cancel_terms(claim_id)` | `POST claims/cancel-info?claim_id=` → `cancel_state` `free` \| `paid` \| `unavailable`, `price`, `price_with_vat`, `currency` | carrier cancel, best-effort cancel of unaccepted claims | The terms are read, never assumed: the docs state the free/paid boundary three different ways |
| `cancel(claim_id, version, state)` | `POST claims/cancel?claim_id= {version, cancel_state}` | carrier cancel | Before pickup only (`free` → `cancelled`, `paid` → `cancelled_with_payment`) |
| `request_return(claim_id, point_id, comment)` | `POST claims/return?claim_id= {point_id, comment, need_return_items: true}` (200 without a body) | carrier cancel after pickup | "Initiates the return … to the initial point of dispatch"; `point_id` is the destination's response id (`route_points[].id`, kept on `CourierClaim.destination_point_id`); `need_return_items: true` is the documented "return the item to the initial point of dispatch" (optional, no documented default; the documented example sends it) |
| `tracking_link(claim_id)` | `GET claims/tracking-links?claim_id=` → destination `sharing_link` | after acceptance / when a courier is assigned | https links only; 409 ("no courier tracking links yet") → none. Shown to the customer and staff |

Not used (and why): `offers/calculate` and `offer_payload` (Russia only); `claims/journal` (documented as the
replacement of callbacks, but its scope, retention and cursor behaviour are undocumented — `bulk_info` over our own
claims is enough; YD-12); `claims/edit` / `apply-changes` (we never edit: a new claim with a new `request_id`
replaces a failed one); `claims/search`; `delivery-methods`, `same_day_data` and `due` (same-day and scheduled
pickup are not built); `performer-position`, `points-eta`, `driver-voiceforwarding`, `confirmation_code`,
`proof-of-delivery/info`; `auto_accept`, `payment_on_delivery`, `buyout` (never sent: every order is prepaid
through Click/Payme).

### 2.1 Protocol details

- **Coordinates** are always sent, as `[longitude, latitude]` rounded to six decimals (schema: optional; FAQ:
  "mandatory"). The customer's pin comes from the address form (Leaflet/OSM); the order keeps it
  (`Order.address_latitude/longitude`, both or neither).
- **Item spelling.** `claims/create` items carry `droppof_point` (the EN schema's required spelling; RU keeps it as
  deprecated next to `dropoff_point`), `check-price` items `dropoff_point` (both schemas). Answers are read with
  either (YD-14).
- **Phones** are normalised to `+998XXXXXXXXX`; anything else is refused **before** any request
  (`YandexRejected("invalid_request")`, Yandex would answer `country_phone_code_not_supported`). The source point
  carries the warehouse e-mail when configured ("mandatory for source and return points", schema-optional).
- **Money.** Yandex sends `Decimal(18, 4)` strings; we keep integer so'm rounded **up** (`protocol.so_m`, never
  float); amounts above 100 000 000 are unreadable. The claim's `offer_price` is `pricing.offer.price_with_vat`
  (else `price`, documented "without VAT"); `final_price` is documented VAT-inclusive; the cancel fee prefers
  `price_with_vat`. Any price in another currency than `YANDEX_DELIVERY_CURRENCY` (default `UZS`), or a price with
  no currency, is unreadable (`YandexUnavailable` + ERROR `yandex.currency_mismatch`) — never converted, never
  guessed. A cancel-info fee without a currency is dropped (the state alone still lets the cancel proceed).
- **Statuses** are stored verbatim (`CourierClaim.status`, ≤ 40); an unknown one (`pay_waiting` is in the status
  table but not in the enum) is stored and logged, nothing moves (§4).
- **Timeouts.** One `httpclient.Deadline` per port call (10 s); the customer path shares one
  `Deadline(CUSTOMER_PATH_SECONDS)` = 4 s across both classes of a quote; `bulk_info` chunks get 10 s each.

### 2.2 Failure mapping (`live.py`)

| Answer | Error | `outcome_unknown` | Meaning for the caller |
|---|---|---|---|
| Never sent (connect failure) | `YandexUnavailable` | `False` | nothing happened |
| Timeout after send, broken body, 5xx | `YandexUnavailable` (`status` set for 5xx) | `True` | may have been processed: never resent by the adapter; `claims/create` is retried by the outbox with the **same** `request_id` |
| Redirect (never followed), 401, 403, 408, 425, 429 | `YandexUnavailable` (`status` set) | `False` | refused unprocessed; 401 also logs ERROR `yandex.auth_failed` |
| 403 of `claims/create` with a documented code (`forbidden_by_antifake`, `buyout_disabled`, `payment_on_delivery_disabled`) | `YandexRejected(code)` | — | definitive: the dispatch fails with that code; any other 403 stays "not processed" |
| 2xx we cannot read, a price in another currency | `YandexUnavailable` | `True` | — |
| 409 of `accept` / `cancel` / `return` / `tracking-links` | `YandexConflict(code)` | — | `offer_expired`, `old_version`, `inappropriate_status`, `state_mismatch`, `free_cancel_is_unavailable`, `not_allowed`, … |
| Any other 4xx | `YandexRejected(code)` (Yandex's `code`, else `http_<status>`) | — | definitive: nothing created or changed |
| Our data cannot be sent (phone, coordinates, class, callback URL) | `YandexRejected("invalid_request")` | — | raised before any request |
| A claim id that is not one (`[A-Za-z0-9_.-]{1,64}`) | `YandexRejected("invalid_claim_id")` | — | before any request |
| Fake adapter under the production settings | `YandexRejected("fake_mode")` (`check_price`, `create_claim`), else `YandexUnavailable(outcome_unknown=False)` | — | §5 |

Yandex's `message` goes through `httpclient.safe_text`. `httpclient` logs the path, the status and the duration
only.

## 3. From quote to delivery

1. **Quote** (`POST delivery/calculate/` with `with_options` / `delivery_provider` / `delivery_method`, customer
   contract §2.6). For each **enabled** method (`DeliverySettings.standard_enabled` / `express_enabled`, default
   off; a switched-off method is omitted) the first refusal wins: `not_configured` (warehouse point A incomplete),
   `dispatch_suspended`, `coordinates_required` (the saved address has no pin), `too_heavy` (parcel weight =
   `default_item_weight_g` × total quantity, above the class limit: courier 10 kg, express 20 kg),
   `warehouse_closed` (no open day within the 14-day horizon), then `check-price` for the method's class:
   `YandexUnavailable` → `provider_unavailable`; an account failure (401/403, a payment refusal) →
   `provider_unavailable` and the failure is counted (§11); another `YandexRejected` → `out_of_zone`
   (`invalid_request`, `fake_mode` → `provider_unavailable`). **An unavailable option never carries a price.**
   An available one is stored as a `DeliveryQuote` (UUID, 15 minutes: `quotes.QUOTE_TTL`) with the fee of §8 and
   the promised window of the warehouse calendar (`eta_window`: fulfilment start + `prep_minutes` + the method's
   `eta_min/max_minutes`; `scheduled` when it starts at a later opening). An unexpired, unused quote for the same
   customer, address snapshot, method, weight, class and `scheduled` flag is reused (no second `check-price`).
   Default selection: Yandex Standard, else Express, else BTS.
2. **Checkout** (`POST orders/checkout/` with `delivery_quote_id`, customer contract §2.7.1). Before the
   transaction the quote is checked (own, Yandex, unused, unexpired, same address snapshot, same weight, method
   still usable, same class and `scheduled` flag); a stale one is re-quoted: same fee → proceed, other fee → 409
   `price_changed` with the fresh options, nothing available → 409 `quote_expired`. In the transaction (no network)
   the quote row is locked, the order takes `delivery_fee = quote.customer_fee`, the pin and a Yandex `Shipment`
   (`provider=yandex`, `method`, `quote`), and the quote is marked used (a quote lost to a concurrent checkout →
   409 `quote_expired`). Without a quote id the released BTS path runs unchanged.
3. **Payment** — unchanged (one Click/Payme payment for products + delivery). `confirm_payment_effects` never
   calls Yandex: a Yandex shipment gets stage `awaiting_dispatch` and a promised window recomputed from `paid_at`
   (`delivery.services.mark_awaiting_dispatch`).
4. **Dispatch** — staff pack the parcel and press «Kuryer chaqirish» (`POST admin/orders/{id}/dispatch/`, admin
   contract §2.18). The request stores a new open `CourierClaim` (fresh UUID `request_id`, stage
   `courier_requested`) and enqueues `yandex.dispatch`; nothing is sent to Yandex in the request. The handler is §9.
5. **Status sync** — callbacks (a hint) and the 60 s `bulk_info` poll feed every snapshot through
   `commerce.services.apply_yandex_claim`, the single writer (§4, §10).
6. **Delivered** — `delivered` / `delivered_finish` move the order to `delivered` (push); a failed hand-over
   brings the parcel back (§12); staff may dispatch again after a return.

Order statuses are unchanged; finer progress is the shipment `stage`. Manual status transitions are disabled for a
Yandex order, except «delivered» while a `parcel_unaccounted` issue is open (admin contract §2.18): it also sets the
shipment stage `delivered` (`delivery.services.mark_delivered_by_staff`, under the same Order lock; a manual
`status_received` shipment event `{stage, claim_id, claim_number, confirmed_by}`), so the customer and staff no
longer see the stage Yandex left (`cancelled` / `failed`); the state, the costs and the claim stay as Yandex left
them.

## 4. Status → stage → order

`delivery/yandex.py` (26 documented `ClaimStatus` literals, in the docs' order). "Order" applies to the current
claim while it is open (`live`); a terminal order status never regresses.

| Yandex status | Stage | Order effect | Other effects |
|---|---|---|---|
| `new`, `estimating`, `ready_for_approval` | `courier_requested` | — | before acceptance: the claim summons nobody (`PRE_ACCEPT`) |
| `accepted`, `performer_lookup`, `performer_draft` | `courier_requested` | — | acceptance recorded (`accepted_at`, event `shipment_created`, `Shipment.state = created`, `tariff` = offer); search > 30 min → urgent `dispatch_failed` `courier_search_slow` (review beat) |
| `performer_found` | `courier_assigned` | — | courier name / vehicle from `performer_info` (never a phone); tracking link fetched |
| `pickup_arrived`, `ready_for_pickup_confirmation` | `courier_arrived` | — | — |
| `pickuped` | `in_transit` | `processing` → `handed_to_courier` (push, event source `yandex`) | any later picked-up status also moves a `processing` order (a poll may skip `pickuped`) |
| `delivery_arrived`, `ready_for_delivery_confirmation` | `at_destination` | (handed) | — |
| `delivered`, `delivered_finish` | `delivered` | → `delivered` (push) | resolves `delivery_failed`, `return_pending`, `dispatch_unknown`; on a **cancelled** order: the order stays cancelled, urgent `parcel_unaccounted` `delivered_after_cancel` |
| `returning`, `return_arrived`, `ready_for_return_confirmation` | `returning` | — | unless we asked for it (cancel / return): event `delivery_failed` (once per claim, reason = `return_reasons[0]`) + issue `delivery_failed` (code = the reason, else the status) |
| `returned`, `returned_finish` | `returned` | — | event `shipment_returned` (once per claim); issue `return_pending` `returned` unless one is open or the return was confirmed; staff confirm or re-dispatch |
| `cancelled`, `cancelled_with_payment`, `cancelled_with_items_on_hands` | `cancelled` | — | not asked by us and before pickup → stage `failed` + `dispatch_failed`; after pickup → urgent `parcel_unaccounted` (code = status) |
| `cancelled_by_taxi`, `performer_not_found`, `failed`, `estimating_failed` | `failed` | — | before pickup → `dispatch_failed` (code: Yandex's `error_messages` code for `estimating_failed`; `offer_expired` for an offer we confirmed that then failed; else the status); after pickup → urgent `parcel_unaccounted` |
| anything else (`pay_waiting`, a new literal) | none | none | stored verbatim, WARNING `yandex.unknown_status` |

**Finished** (the claim ends, `ended_at`): `delivered_finish`, `returned_finish`, the four `cancelled*`, `failed`,
`estimating_failed`, `performer_not_found`. No documentation page lists terminal statuses: this is our reading of
the wording. `estimating_failed` is documented as recoverable by editing — we never edit, so for us it is over;
whether `cancelled_by_taxi` re-searches on the same claim is YD-7 (until answered it counts as finished: staff
dispatch again). A snapshot whose `updated_ts` is older than the stored one is ignored; the same status, version
and final price is a duplicate. A claim Yandex shows accepted that is not the one we follow (an older claim, or one
a dispatch closed unsettled because Yandex might still take its accept, §9) → urgent `dispatch_unknown`
`unexpected_acceptance`.

## 5. Modes

| | `YANDEX_DELIVERY_MODE=fake` | `YANDEX_DELIVERY_MODE=live` |
|---|---|---|
| Adapter | `FakeYandexDelivery` (process memory; `reset()` forgets it) | `LiveYandexDelivery.from_settings()` |
| Readiness (`health/?ready=1`) | — | `is_configured()`: a token, an https base URL, a `[A-Z]{3}` currency, `ru`/`en` (settings only, nothing sent) |
| Default | `INTEGRATIONS_MODE` (fake) | — |

**The fake** is deterministic: `create_claim` is idempotent per `request_id`, inputs go through the live payload
builders (a bad phone is refused like live), a new claim is `ready_for_approval` at once with a 10-minute offer,
each read of an accepted claim moves one step of `DEFAULT_SCRIPT` (`performer_lookup` → `performer_found` →
`pickup_arrived` → `pickuped` → `delivery_arrived` → `delivered` → `delivered_finish`), the courier is
"Fake Kuryer" in a "Chevrolet Cobalt oq 01A777AA", tracking links are `https://tracking.fake-yandex.invalid/<claim>`.
Prices: a base per class plus a per-kilometre rate over the straight-line distance (`RATES`: courier 15 000 +
2 000/km, express 20 000 + 2 500/km), rounded up to 100 so'm; ETA 15 min + 3 min/km.

**Scenarios** — `Order.metadata["fake"]["yandex"]` (copied from `X-GD-Fake-Scenario` at checkout, local/test only)
or the scenario a test passes:

| Key | Values | Effect |
|---|---|---|
| `price` | int | the check-price price |
| `check_price` | `rejected` · `no_tariff` · `down` | `errors.suitable_offer_not_found` · `estimating.tariff.no_categories_in_zone` · unavailable |
| `tariffs` | list of class names | what `tariffs()` lists (default `courier`, `express`) |
| `create` | `rejected` · `down` · `down_after_create` | `address_not_found` · nothing created · created, then unavailable (the retry returns it) |
| `offer_price`, `warnings` | int, list of codes | the claim's offer (else the check-price price) and warnings |
| `estimate` | `failed` · `pending` | `estimating_failed` (`estimating.cant_construct_route`) · stays `estimating` until `advance` |
| `accept` | `conflict` · `down` · `down_once` · `old_version_once` · `timeout` · `expired` | `inappropriate_status` · not processed (nothing accepted) · not processed for the claim's first accept only · the first accept finds the claim edited (version + 1, 409 `old_version`), an accept of the new version goes through · outcome unknown although nothing was accepted (the claim stays `ready_for_approval`) · answered, next read `failed` with `offer_expired` |
| `info` | `not_found` | `claims/info` answers 404 `not_found` for a claim the fake holds (`bulk_info` is unaffected) |
| `statuses` | list | one status per read after acceptance (taken from the **creating** call's scenario) |
| `final_price` | int | the price of a finished claim (else the offer) |
| `cancel` | `free` · `paid` · `unavailable` · `down` | forced terms (default: free until the courier reaches the warehouse, paid at `pickup_arrived` / `ready_for_pickup_confirmation`, unavailable after) |
| `return` | `refused` · `ok` | `not_allowed` · the claim turns `returning`, then `return_arrived` → `returned` → `returned_finish` |

**Production posture** (`core.integration_modes.fake_in_production("yandex")`: fake mode under the production
settings, staging included). The fake invents nothing: `check_price` / `create_claim` → `YandexRejected
("fake_mode")`, every other call → `YandexUnavailable`; scenarios are ignored. Consequences: `settings/public`
omits `yandex` from `delivery.providers`, checkout offers no Yandex option, the callback answers 403 (inbox row
`fake_mode`), the poll is a no-op. Fake mode under the local/test settings works fully.

**Dev route** (local/test only): `POST /api/v1/admin/dev/yandex/{order_id}/advance/ {status}` moves the order's
current claim in the fake (`fake.advance`) and applies `claims/info` exactly as the poll would (source `dev`).
Mounted only when `DEV_FEATURES_ALLOWED` and `YANDEX_DELIVERY_MODE=fake`, staff only, not in the OpenAPI document.

## 6. `manage.py yandex_delivery_check`

```bash
scripts/manage.sh <env> django yandex_delivery_check --lat 41.311081 --lon 69.240562 \
  --address "Toshkent sh., …" [--to-lat … --to-lon … --to-address …] [--weight-kg 1]
```

Read-only at Yandex (no claim is created): `tariffs` at the given warehouse point, then `check-price` for
`courier` and `express` when listed (without `--to-*` the probe destination is 0.01° north, ~1.1 km, with the same
address text). Prints `mode=`, the classes, and per class the price, currency, raw price, ETA, distance and zone.
When `tariffs` answered it stores `IntegrationState(yandex).data = {"tariffs": [...], "tariffs_checked_at": ISO,
"tariffs_mode": "fake"|"live", "tariffs_point": [lon, lat]}` (also when a price check then failed). Exit status 1 on
any failure (nothing stored when `tariffs` fails). The ops console action «Yandex bilan tekshirish» on the
delivery settings page does the same `tariffs` call at the stored warehouse pin (no transaction around the call;
audited `delivery.tariffs_checked`; logs `yandex.tariffs_checked` / `yandex.tariffs_check_failed`).

**Method switch rule.** In live mode a method can be switched on (or change class) only when its class is in
`data["tariffs"]` and `tariffs_mode` is `live` (`delivery.services.method_class_refusal`); the fake allows every
class.

## 7. Settings

### 7.1 Environment (`config/settings/base.py`; production refusals in `production.py`)

| Variable | Default | Rule | Secret |
|---|---|---|---|
| `YANDEX_DELIVERY_MODE` | `INTEGRATIONS_MODE` | `fake` \| `live`; part of `SCENARIO_PROVIDER_MODES` (`FAKE_SCENARIO_HEADER_ENABLED` needs it fake) | no |
| `YANDEX_DELIVERY_BASE_URL` | `https://b2b.taxi.yandex.net` | trailing `/` stripped; https in production with live mode | no |
| `YANDEX_DELIVERY_TOKEN` | empty | required in production with live mode; placeholders refused | **yes** |
| `YANDEX_DELIVERY_CURRENCY` | `UZS` | `[A-Z]{3}`; every price must come in it | no |
| `YANDEX_DELIVERY_LANGUAGE` | `ru` | `ru` \| `en` (`Accept-Language`) | no |
| `YANDEX_DELIVERY_CALLBACK_URL` | empty (no callback) | absolute http(s) URL **without a query or fragment**; https in production whenever set; each claim registers `<url>?ref=<order>-<hmac>&` | no |
| `YANDEX_DELIVERY_SKIP_CONFIRMATION_SOURCE` | `false` | `skip_confirmation` of point A (false = the documented default: an SMS code confirms the hand-over) | no |
| `YANDEX_DELIVERY_SKIP_CONFIRMATION_DESTINATION` | `false` | `skip_confirmation` of point B | no |
| `YANDEX_DELIVERY_PILOT_CUSTOMER_IDS` | empty (everybody) | comma-separated customer ids; when set only they are offered Yandex | no |

A startup WARNING (`yandex delivery is live without ALERT_WEBHOOK_URL: urgent delivery issues alert nobody`) is
logged when Yandex is live and `ALERT_WEBHOOK_URL` is empty. The callback reference key is derived from
`PAYMENT_REF_KEY` (no setting of its own).

### 7.2 Warehouse and policy (ops console, `DeliverySettings` singleton)

Edited on `/django-admin/delivery/deliverysettings/1/change/` (tabs «Ombor», «Jo‘natish tartibi», «Usullar»,
«Narx siyosati», «Yangilanish»), written through `delivery.services.update_settings` (row lock, audited
`delivery.settings_updated`; the warehouse address and contacts are audited as `{"changed": true}` only). Weekly
hours (`WarehouseHours`: one row per weekday, no row = closed) and special days (`WarehouseClosure`: a date, no
times = closed, else special hours) are their own service-backed pages linked from it.

| Field | Default | Meaning |
|---|---|---|
| `warehouse_name`, `warehouse_address`, `warehouse_latitude/longitude` (Uzbekistan box), `contact_name`, `contact_phone` (E.164), `contact_email`, `courier_comment` | empty | point A of every claim (pin + address + contact name + phone = "configured") |
| `prep_minutes` | 30 | preparation time before a courier is useful; also part of the promised window |
| `dispatch_cutoff_minutes` | 90 | no dispatch later than this before closing (a failed hand-over must be able to come back) |
| `returns_accepted` | true | off → no dispatch (`returns_unavailable`) and returning parcels raise urgent issues |
| `dispatch_suspended`, `suspended_until`, `suspension_note` | off | also `GET/POST admin/delivery/suspension/` (the panel switch) |
| `standard_enabled` / `express_enabled` | **off** | offered at checkout only when on |
| `standard_taxi_class` / `express_taxi_class` | `courier` / `express` | owner decision: Standard = `courier`, Express = `express` |
| `standard_eta_min/max_minutes` / `express_eta_min/max_minutes` | 60/180 · 30/90 | the promised window after preparation |
| `default_item_weight_g` | 300 | product weights are not modelled: parcel weight = this × quantity |
| `markup_percent` | 10.00 | §8 |
| `estimate_vat_percent` | 0.00 | §8 (VAT uplift for an estimate that may exclude VAT) |
| `max_auto_increase` | 10 000 | §8 |
| `max_loss_per_shipment` | 15 000 | §8 |
| `prep_deadline_minutes` | 120 | working minutes after payment before `preparation_overdue` |

Code constants: rounding step 500 so'm, quote TTL 15 min, courier search 30 min, calendar horizon 14 days.

## 8. Pricing and dispatch policy

- **Fee at checkout** (`pricing.customer_fee`): `check-price` estimate × (1 + `estimate_vat_percent`) × (1 +
  `markup_percent`), rounded **up** to 500 so'm, in integer basis points. The fee the customer paid **never
  changes** afterwards (no surcharge, no partial refund).
- **At dispatch** the claim's binding offer (`price_with_vat`) is compared by `pricing.dispatch_decision` with the
  quote's carrier estimate on the offer's basis (`CourierClaim.estimate_price` = estimate × (1 +
  `estimate_vat_percent`), rounded up to the so'm, no markup — so an unchanged price is no increase; the paid fee
  when the method was switched) and the paid fee: accepted while `increase = offer − estimate ≤ max_auto_increase` **and** `loss = offer − paid fee ≤
  max_loss_per_shipment` (bounds inclusive); otherwise `dispatch_review` with code `increase_over_limit` /
  `loss_over_limit`. A claim with warnings (`warnings`) or without an offer (`offer_missing`) always goes to
  review. Staff approve by dispatching again with `approved_price` (accepted up to that amount, `approved`;
  above → `above_approved`), switch the method, or cancel.
- A review closes the claim (never accepted: settled at once, cost 0) and parks the stage at
  `awaiting_dispatch` (`returned` for a parcel that came back); event `price_review` with the amounts.

## 9. Dispatch (`yandex.dispatch` → `commerce.yandex_flow.dispatch`)

Every step commits before the next call; no transaction is open during a Yandex call. The handler holds the session
advisory lock `yandex:{order_id}` (`commerce.locks.yandex_lock`, shared with the carrier cancel; busy →
`RetryLater` 15 s).

1. **Plan** under the Order lock: the claim of the message's `request_id`; an ended claim → nothing; an order no
   longer open, or a cancel requested → best-effort free cancel of an unaccepted claim, closed with code
   `order_closed` / `cancel_requested` (no issue; the review beat raises `cancel_incomplete` when a claim withdrawn
   for a cancel leaves the order open, §11); a
   missing warehouse or pin → `dispatch_failed` (`not_configured` / `coordinates_required`).
2. A claim with `accept_sent_at` is only re-read and confirmed (step 7; never a second accept).
3. **Previous claim** (number − 1): unless Yandex already said finished, a fresh `claims/info`: finished or
   `returned` → go on; never accepted → best-effort free cancel, go on; alive → the new claim is closed, the
   previous one is followed again, urgent `dispatch_failed` `previous_claim_active` (decision 7).
4. **Create** with the claim's `request_id`: refusal (a documented 403 code such as `forbidden_by_antifake`
   included) → `dispatch_failed <Yandex code>`; outcome unknown → raise (outbox backoff, same `request_id`); not
   processed (429, 401, any other 403, …) → `RetryLater` 60 s (give up after 30 min). **Adopted-claim guard** — a
   mismatch check: the claim is foreign only when an order reference Yandex echoes (point B's `external_order_id`,
   an item's `extra_id`; we send both as our order id) is not our order id → closed, stage `failed`,
   `dispatch_unknown` `foreign_claim` (ERROR `yandex.foreign_claim`). The echo is optional in the documentation:
   a claim that echoes neither came back for our own `request_id` and is followed (WARNING
   `yandex.claim_ref_missing`). The claim id is committed at once.
5. **Estimate**: up to 3 reads 1 s apart, then `RetryLater` 10 s; more than 5 minutes since the claim row →
   best-effort cancel, `dispatch_failed` `estimating_timeout`; `estimating_failed` → `dispatch_failed` with
   Yandex's reason.
6. **Policy** (§8) on `ready_for_approval`.
7. **Accept**: re-check under the Order lock (order open, no cancel requested, `dispatch_refusal(now)` is none,
   offer not expired) — else best-effort cancel and `dispatch_failed` with the refusal (`order_closed` /
   `cancel_requested`: no issue) —, stamp `accept_sent_at`, commit, `accept(version)` **at most once**:
   - 409 `offer_expired` → `dispatch_failed` `offer_expired`; 404 (another 4xx) → `dispatch_failed` with its code;
   - **provably not taken** — not processed (never sent, a redirect, 401/403/408/425/429) or 409 `old_version` →
     `accept_sent_at` is released (`delivery.services.release_accept_sent`, INFO `yandex.accept_released`) and the
     message deferred (`RetryLater` 60 s / 10 s, given up after 30 min): the next run re-reads the claim, repeats
     steps 5–6 and this re-check (an expired offer → `dispatch_failed` `offer_expired`) and sends one accept with
     the fresh version;
   - **any other 409** (`inappropriate_status`, `state_mismatch`, …: Yandex answered, nothing was accepted) → one
     fresh read: still before acceptance → the dispatch ends (below) with the 409's code; moved on → recorded by
     the writer (+ tracking link); `claims/info` refuses it → closed, `dispatch_failed` with the 409's code; no
     answer → retried, the next run only confirms (the stamp stays);
   - **outcome unknown** (timeout, 5xx, unreadable body) → never resent: the claim is re-read (`_confirm`, also
     every later run of a stamped claim): moved → acceptance recorded by the writer + tracking link; still before
     acceptance → `RetryLater` 10 s, until `ACCEPT_CONFIRM_WINDOW` (15 min, longer than the 10-minute offer) has
     passed since the accept, or the offer has expired and at least `INFO_LAG` (2 min, `claims/info` lags 5–30 s)
     has passed since the accept → the dispatch ends with `dispatch_failed` `accept_unconfirmed`;
   - a `claims/info` **4xx after the accept** is retried every 10 s within the 15-minute window; after it the
     claim is closed with stage `failed` but left **unsettled** (Yandex may have accepted it: the poll keeps
     reading it) and staff get an urgent `dispatch_unknown` `not_found`.

   **Ending a dispatch whose accept was not taken** (`_end_unaccepted`): a best-effort free cancel at Yandex, the
   claim closed, stage `failed` (`returned` for a re-dispatch after a return), `dispatch_failed` `<code>`, an open
   `dispatch_unknown` about this accept (`accept_unconfirmed` / `dead_letter`) resolved — staff may dispatch
   again. The claim is settled only when Yandex took the free cancel; terms that are not free or a refused cancel
   mean the read was probably stale, so one fresh read follows (past acceptance → the writer records it and
   nothing ends); otherwise, or with no answer, the claim is closed **unsettled**. While a carrier cancel is under
   way (`cancel_requested_at` or `cancel_sent_at` set) nothing is sent and no issue opens: the claim is only
   closed unsettled as `cancel_requested` (the cancel's next attempt finds no open claim and finishes the order
   cancel; the review beat raises `cancel_incomplete` if nobody repeats it). A claim a poll or callback saw
   accepted or ended meanwhile is left as its writer recorded it (`_close` re-checks under the Order lock).

   An **unsettled closed claim** stays in the poll: a late acceptance raises urgent `dispatch_unknown`
   `unexpected_acceptance`, any other new status settles it through the writer, and one whose status never moves is
   settled after 24 h (`UNANSWERED_AFTER`, §10) with cost 0 (WARNING `yandex.claim_unanswered`). The code
   `no_answer` only fills a blank code, and every path that closes a claim unsettled sets one
   (`accept_unconfirmed`, `not_found`, the 409's code, `dead_letter`, `cancel_requested`), so in practice such a
   claim keeps its closing code.

**Dead letter** (ten transport failures, or a "not processed" answer once the message has been deferring for 30
minutes, `NOT_PROCESSED_GIVE_UP`, counted from its first deferral; `DISPATCH_DEFERRAL_WINDOW`, 45 min, is K6's
reporting window), `on_dispatch_dead` under the Order lock (stage `failed` is parked as `returned` for a
re-dispatch after a return):

| The claim | Outcome |
|---|---|
| no Yandex id (`claims/create` never answered) | closed and settled (no accept can exist without the id), stage `failed`, `dispatch_failed` `dead_letter` (not urgent); staff may dispatch again |
| a Yandex id, no `accept_sent_at` (none sent, or released after a "not processed" accept) | closed **unsettled** (still polled), stage `failed`, `dispatch_failed` `dead_letter`; the next dispatch's step 3 reads it again and cancels it |
| `accept_sent_at` set (or accepted) | stays open and polled, **urgent** `dispatch_unknown` `dead_letter` (a courier may come); requeueing the message (`requeue_outbox --topic yandex.dispatch --dead`) resumes it — never a second claim, never a second accept |

A dispatch is refused while **any** claim of the order is open.

**Parcel** of every claim: one item `GlowDerma №<order id>`, quantity 1, the weighed `weight_g` (else default
weight), `cost_value` = the items subtotal (declared value), `optional_return=false` (the courier must bring the
item back on cancellation), the method's class, the callback URL when configured.

## 10. Callback and poll

- **Callback** `POST /api/v1/webhooks/yandex-delivery/?ref=<order>-<hmac>&updated_ts=…&claim_id=…` (customer
  contract §2.13.4). Yandex signs nothing and sends no status (documented as deprecated, "a push ahead of
  polling"), so the call is an unauthenticated **hint**: the `ref` (16 hex of an HMAC with a key derived from
  `PAYMENT_REF_KEY`, domain `glowderma.yandex.callback.v1`) is verified first; `claim_id` must be one of that
  order's claims; `(claim_id, updated_ts)` is deduplicated in `WebhookInbox`; then `yandex.sync_claim` re-reads
  `claims/info` and applies it (a lagging read is retried for 2 minutes). No Yandex call runs in the request.
- **Poll** — beat `commerce-poll-yandex` (`commerce.poll_yandex`, every 60 s, single flight under the cache lock
  `gd:lock:poll_yandex`, 300 s): `claims/bulk_info` over every claim with a Yandex id whose cost is not settled
  (open claims, finished ones waiting for `final_price`, and claims a dispatch closed unsettled, §9), each snapshot
  through the writer (one failing claim never stops the others), then the two 24 h settlements: `cost_unknown`
  (§13) and `no_answer` (`UNANSWERED_AFTER`: a closed, never-accepted claim whose status has not moved since it
  ended → settled at cost 0, keeping its closing code — `no_answer` only fills a blank one, which in practice no
  such claim has —, WARNING `yandex.claim_unanswered`; the poll stops reading it). A failed `bulk_info` skips both settlements for that run. A no-op while the fake runs under
  the production settings; no candidate → nothing written.
  Health: an answered `bulk_info` → `record_sync_success("yandex")` (`last_sync_at`); a failed one →
  `record_yandex_failure("poll", code)` (§11).

## 11. Failure handling, issues and alerts

**Exception queue** (`DeliveryIssue`, one open row per order and kind, mostly self-closing; admin contract §2.18):

| Kind | Opened when (codes) | Closed by |
|---|---|---|
| `dispatch_failed` | a dispatch that summoned no courier: Yandex's refusal code (a documented `claims/create` 403 code included), `not_configured`, `coordinates_required`, `estimating_timeout`, `offer_expired`, `previous_claim_active` (urgent), `not_found`, the refusal of the accept re-check, the code of a 409 refusing the accept (`inappropriate_status`, `state_mismatch`, …), `accept_unconfirmed` (the accept still not visible 15 min after it was sent, or once the offer has expired and at least 2 min, `INFO_LAG`, after it was sent), `dead_letter` (a dead dispatch with no accept outstanding); a claim that failed before pickup (`performer_not_found`, `cancelled_by_taxi`, `failed`, Yandex's estimating code); `courier_search_slow` (urgent, review beat) | a new dispatch; cancel |
| `dispatch_unknown` | Yandex may have taken our accept: `not_found` (urgent: `claims/info` refused the claim 15 min after the accept; closed unsettled, still polled), `dead_letter` (urgent: the dispatch died after the accept was sent; the claim stays open and polled), `unexpected_acceptance` (urgent: a claim we no longer follow shows accepted), `foreign_claim` (`claims/create` returned another order's claim) | the current, open claim seen accepted or finished by the writer (for `dead_letter` that is the claim itself, which stays open; it also closes when the requeued dispatch ends unaccepted); delivery (Yandex's `delivered*` of the current claim, or staff's manual «delivered»); staff. An issue about a claim the writer no longer follows (`not_found`, `unexpected_acceptance`, `foreign_claim`: that claim is closed) is not closed by that claim's own later end or 24 h settlement: it stays open — the urgent ones reminded every 30 min — until staff resolve it, a re-dispatched claim is accepted or finishes, or the order is delivered (intended: staff must look) |
| `dispatch_review` | the policy held the offer: `warnings`, `offer_missing`, `increase_over_limit`, `loss_over_limit`, `above_approved` | a new dispatch; cancel |
| `delivery_failed` | Yandex started a return we did not ask for: the return reason or the status | delivery; cancel |
| `return_pending` | a parcel is coming / came back: `returning`, `returned`, `return_requested`, `warehouse_closed` (urgent while the warehouse is closed or refuses returns) | `POST orders/{id}/confirm-return/`; delivery |
| `preparation_overdue` | a paid order still `awaiting_dispatch` after `prep_deadline_minutes` working minutes: `overdue`, `suspended`, `closed`, `not_configured`, `returns_unavailable` | dispatch; the order closed |
| `cancel_incomplete` (urgent) | a carrier cancel stopped between its phases for 10 min (the stamp's first time) with the order still open: `carrier_cancelled` (Yandex shows the claim cancelled, or the claim was withdrawn for the cancel — error code `cancel_requested`, by the cancel or by a dispatch run that found the stamp), `returning` | repeating the cancel (no second carrier call); delivery |
| `parcel_unaccounted` (urgent) | a cancel / failure after pickup, or `delivered_after_cancel` | staff (manual «delivered» resolves it) |
| `incident` | staff report (`staff_report`, `POST orders/{id}/issues/`) | staff |

Alerts (`core.alerts`, through the outbox; ids and codes only — never the staff note or `details`):
`delivery.issue_opened {order_id, kind, code, urgent, issue_id}` (scope: the issue id) when an issue becomes
urgent; `delivery.issue_reminder` (same fields; scope `"<issue id>:<reminder no>"`) for every open urgent issue
untouched for 30 minutes (review beat, every 5 minutes); `delivery.provider_failing {provider: "yandex", code}`
(scope: the Tashkent date) at the **third** account failure in a row — a failed poll or a check-price 401
(`unauthorized`), 403 (`forbidden`) or payment refusal (`estimating.cargocorp_payment_failure`,
`estimating.payment_method_cant_order`, `estimating.payment_method_zone_unavailable`). `IntegrationState(yandex)`
counts the run (`consecutive_failures`, `last_error` = `"poll: <code>"` / `"check_price: <code>"`); the admin
dashboard shows `attention.yandex_failing` while it is above 0 in live mode; any answered price check or poll ends
the run. Receiver: the Telegram bot (repo `bot`), `docs/ops/monitoring.md` §1. Log keys: monitoring §2.1.

## 12. Cancellation, failed delivery and returns

The admin cancel stays one endpoint (`POST admin/orders/{id}/cancel/`). For a Yandex order its view first runs
`commerce.services.cancel_at_carrier` **outside** the idempotent transaction (skipped on a replay), holding
`yandex:{order_id}` (busy → 409 `conflict`): txn A checks every refusal and stamps `Shipment.cancel_requested_at`
(from then on no dispatch sends an accept and the writer reads a coming `cancelled*` / `returning` as ours; a
repeated cancel keeps the stamp's first time, which the review beat counts from), the Yandex calls run with no
transaction, txn B records the outcome (audit `order.carrier_cancel`). Then the unchanged `cancel_order` runs (full
refund when paid).

**Never sent twice.** Right after `claims/cancel` or `claims/return` answered 2xx — or once its outcome is
unknown — and before any re-read, the call is marked sent in its own short transaction:
`CourierClaim.cancel_sent_at` + `cancel_state` (the terms it was sent with) + `cancel_price` (a paid cancel's
`cancel-info` price), or a `return_requested` shipment event of that claim. A claim with a marker never gets a second `cancel-info` /
`claims/cancel` / `claims/return`: every later attempt only re-reads it (shown → it proceeds; not yet, or the read
fails → the same 409 again). The one further call: a sent cancel whose claim the courier has since picked up takes
the return path (one `claims/return`, itself never resent once marked). A refusal (409 / another 4xx) or a "not processed" answer writes no marker, so a later
attempt may send the call again. The cancel clears the stamp only when Yandex provably changed nothing: nothing was
sent (the first read or `cancel-info` failed, the call not processed), terms `unavailable`, a definitive refusal of
the call that the re-read confirms, delivered, no point B id, a parcel gone after pickup. Outside the cancel, staff
confirming the returned parcel of an order that stays open (`confirm-return`, below) clear it too — after a return
Yandex did carry out.

**Busy order.** The marker's transaction waits at most `CARRIER_RECORD_WAIT` (15 s) for the Order row; txn A and
txn B wait `ADMIN_LOCK_WAIT` (5 s). Past it the marker alone is written without the Order lock — the claim's own
columns under its row lock, or the insert-only event, waiting at most `CARRIER_MARK_WAIT` (5 s) more — with WARNING
`yandex.carrier_record_busy` (no audit row), and the answer is 409 `conflict` (the pending-cancel or the
requested-return message), the stamp kept; the next attempt finds the marker, only re-reads, counts a fee left
uncounted (once Yandex shows `cancelled_with_payment`) and opens `return_pending`. Both waits running out → ERROR
`yandex.carrier_marker_lost`: no marker, as after a crash (below). These bounds cover the marker path only: the
snapshots the cancel applies (its first `claims/info` and every re-read) go through `apply_yandex_claim`, whose
Order-row wait has no limit inside the request, as in every call of that writer.

**Paid fee.** Recorded as soon as Yandex answered the paid `claims/cancel` 2xx (`record_cancel_fee` in the marker's
transaction: `cancel_fee` and the carrier cost), whatever the re-reads show. After a lost answer (or a busy order)
the stored `cancel_price` is counted once Yandex shows `cancelled_with_payment` — by the single writer (poll,
callback, re-read), by the marker's own transaction should a poll have shown it first, by a retry's txn B, at the
latest by the 24 h `cost_unknown` settlement (§13); a refused cancel whose re-read shows the claim cancelled with
payment counts this attempt's quoted price. Counted once; a later `final_price` replaces it.

| Situation | Behaviour |
|---|---|
| unpaid; paid with no open claim | as before: no carrier call, cancel (+ full refund) |
| open claim whose accept was never sent | the claim is closed here (summons nobody; best-effort free cancel at Yandex when it has an id) → cancel + refund |
| claim open, before pickup | fresh `claims/info` → `cancel-info` → `cancel(version, free\|paid)` → marker → re-read (≤ 3 reads, 1 s apart) shows `cancelled*` → cancel + refund; a paid fee goes on `CourierClaim.cancel_fee` and into the carrier cost as soon as Yandex answered; event `cancelled.bts_cancelled = true` |
| cancel answered, or its outcome unknown, not visible yet | 409 `conflict`, stamp and marker kept; the retry only re-reads (the review beat opens `cancel_incomplete` if Yandex then shows it cancelled and nobody finishes the cancel) |
| `cancel-info` refused, terms `unavailable` | 409 `not_cancellable`, stamp cleared, order unchanged |
| a 409 or refusal of `claims/cancel` | the same — unless the re-read shows the claim `cancelled` / `cancelled_with_payment` all the same: it proceeds (`already_cancelled`, this attempt's quoted fee counted) |
| Yandex not answering before anything was sent (the first read, `cancel-info`, or `claims/cancel` not processed) | 503 `provider_unavailable`, stamp cleared |
| picked up (in transit / at destination) | `claims/return(point B, need_return_items)` → marker → re-read: `returning…` → cancel + refund, `return_pending` open until the parcel is back; still in transit, an unknown outcome included → 409 `return_requested` (order unchanged, `return_pending` open, stamp kept; the retry only re-reads); delivered meanwhile → 409 `not_cancellable`; refused → 409 `not_cancellable`, stamp cleared (unless the re-read shows it `returning…`: it proceeds); not processed → 503 |
| a sent cancel, then the courier picked up | the return path (another call) |
| the order busy > 15 s right after Yandex answered 2xx (or the outcome turned unknown) | 409 `conflict`, marker written without the Order lock (≤ 5 s more; else `carrier_marker_lost`, no marker), stamp kept, no audit row (above) |
| stage `returning` / `returned` | no carrier call: cancel + refund, `return_pending` kept / opened for the claim that carried the parcel unless its return was confirmed |
| delivered | 409 `not_cancellable` |
| picked up, claim ended without a return | 409 `not_cancellable` (refund only once Yandex reports the parcel coming back; staff may mark it delivered while `parcel_unaccounted` is open) |
| Yandex already shows the claim cancelled (a cancel that stopped between its phases), or the claim was withdrawn for it | no second call: cancel + refund, `cancel_incomplete` resolved (a paid fee nobody counted yet is counted here while the claim is open, else by the 24 h settlement) |

**Residual window (a known limit, not closed).** The marker is committed only after Yandex answered. A crash or a
worker kill between that answer and the commit (a deploy's restart waits gunicorn's `graceful_timeout`, 30 s, for a
running request) — or both bounded waits running out — leaves no marker: the next attempt sends `cancel-info` /
`claims/cancel` (or `claims/return`) again, and if Yandex refuses that duplicate while `claims/info` still lags, the
stamp is cleared and the coming `cancelled*` / `returning` is read as not ours (stage `failed` + `dispatch_failed`,
or `delivery_failed`). Closing it would mean writing the marker before the call (open questions, known limits).

**Failed delivery.** The documented rule: recipient unreachable for 10 minutes at the door → the parcel returns to
point A (`returning … returned_finish`). We record `delivery_failed` and open the issue; the order stays as it is
until staff decide. **Refund after pickup only when Yandex itself reports the parcel coming back** (decision 11).
Staff confirm the physical return with `POST orders/{id}/confirm-return/` (stage `returned`, `returned_at`,
event `shipment_returned` with source `admin`) and then cancel (refund) or dispatch again. The return confirmed is
the one of the claim that **carried the parcel** (`delivery.selectors.return_claim`: the highest-numbered claim that
picked it up, by its status or a `claim_status` event), not of the current claim: after a confirmed return, a
re-dispatch that fails or goes to price review leaves the stage `returned` with that new claim current, yet
`confirm_return` is not offered again, a second confirm answers 409 `conflict`, and a cancel opens no new
`return_pending`; `shipment_returned` carries the carrying claim's id (deduplicated with Yandex's own `returned*`).

## 13. Costs

| Field | Meaning |
|---|---|
| `DeliveryQuote.carrier_price` / `customer_fee` | Yandex's estimate / what the order charged (`Order.delivery_fee`) |
| `CourierClaim.offer_price` | the binding offer (VAT-inclusive) at dispatch |
| `CourierClaim.final_price` | Yandex's final price ("filled in after the delivery/return is completed", VAT-inclusive); added **once** to `Shipment.carrier_cost_total` (minus a cancel fee counted before) |
| `CourierClaim.cancel_fee` | a paid cancellation's fee (counted until a `final_price` replaces it) |
| `CourierClaim.cancel_sent_at` / `cancel_state` / `cancel_price` | our `claims/cancel` was sent (answered 2xx or outcome unknown; never sent again), the terms it was sent with (`free` / `paid`) and a paid one's `cancel-info` price — it becomes `cancel_fee` once Yandex shows `cancelled_with_payment` when nobody counted the fee yet (§12) |
| `Shipment.cost_difference` | paid fee − `carrier_cost_total` (negative = the store paid more than it charged) |
| `cost_unknown` | a finished, accepted claim still without `final_price` 24 h after it ended is settled with this code (WARNING `yandex.cost_unknown`); a paid cancel whose quoted `cancel_price` no writer counted (Yandex showed `cancelled_with_payment` before our cancel was marked sent) gets that price counted as its fee then and keeps its own error code |
| `no_answer` | a claim the dispatch closed unsettled without any acceptance (§9) whose status has not moved 24 h after it ended: settled at cost 0 (`cost_difference` recomputed), this code only when it had none (WARNING `yandex.claim_unanswered`) — in practice never stored: every path that closes a claim unsettled sets a code, which the claim keeps |

Claims that end in `estimating_failed`, `performer_not_found`, `cancelled` or `cancelled_by_taxi`, and any claim
never accepted, are settled at once with no cost (our reading: no courier took the parcel) — except a claim the
dispatch closed unsettled because Yandex might still take its accept (polled until a status settles it, or the
24 h `no_answer` settlement, which keeps its closing code). Billing is a prepaid
balance on the Uzbek site; an exhausted balance is expected to surface as a payment refusal (§11, YD-10).

## 14. Personal data sent to Yandex

| Call | Fields |
|---|---|
| `check-price` | warehouse pin + address text; the customer's **pin and address text** (the saved address); parcel weight; class. No name, no phone |
| `claims/create` | point A: warehouse address, pin, contact name, phone, e-mail, courier comment; point B: the order's **address text, pin, recipient name and phone, address comment**, `external_order_id` = our order id (also the item's `extra_id`); item title `GlowDerma №<id>`, the items subtotal as declared value, weight; the callback URL (our HMAC order reference, no personal data) |
| `tariffs` | warehouse pin + address text |
| others | claim ids, versions, `point_id`, the fixed return comment |

Received and stored: claim ids, statuses, prices, the destination point id, `return_reasons`, the courier's
name and vehicle (`car_model car_color car_number`; never a phone) and the tracking link — the last three blanked
7 days after the claim ended (beat `delivery-blank-courier-details`). Shipment events keep an allow-list (claim
id, number, status, version, prices). Compliance: `docs/ops/compliance.md`.

## 15. Capabilities

| Capability | Documented for non-Russian accounts | Verified for this account |
|---|---|---|
| Standard (`courier` class, ≤ 10 kg) / Express (`express` class, ≤ 20 kg) | yes ("Other countries" flow `check-price` → `claims/create` → `claims/info` → `claims/accept`; "use `tariffs` for the exact list") | no — no account; `manage.py yandex_delivery_check` is the first check |
| Scheduled pickup (`due`), same-day (`same_day_data`) | yes, limits undocumented outside Russia (`due` ≤ 3 days per the errors page) | no — not built |
| Returns (`claims/return`, automatic return of an unreachable recipient), cancellation (`cancel-info`, `cancel`) | yes; fees, waiting minutes and `return_reasons` values undocumented | no |
| Billing | not in the API docs; the Uzbek site describes a prepaid balance (contract with a legal entity) | no |
| Webhooks / callbacks (`callback_properties`) | yes — deprecated, unsigned, no status, retries unspecified | no (the poll is the reliable path) |
| Test environment | none documented for the Express API | — |

## 16. Before the first real order

- [ ] Contract and cabinet for the Uzbek entity; OAuth token from the cabinet's *Integration* tab; prepaid
      balance topped up; `auto_accept` **stays off** (never sent; it needs a manager's approval).
- [ ] Staging: `YANDEX_DELIVERY_MODE=live`, `YANDEX_DELIVERY_TOKEN`; `health/?ready=1` shows `yandex` configured.
- [ ] `manage.py yandex_delivery_check` at the warehouse pin: `courier` and `express` listed, prices in `UZS`
      (a currency mismatch is an ERROR and stops everything: YD-5).
- [ ] Ops console: warehouse name, address, pin, contact, hours; «Yandex bilan tekshirish»; switch on one method.
- [ ] `YANDEX_DELIVERY_PILOT_CUSTOMER_IDS` = the pilot customers; alert webhook → Telegram bot tested.
- [ ] Pilot order (deploy.md §13): quote → pay → dispatch → watch `claims/info` statuses → delivered; note every
      status, price, VAT and currency observed; then a cancel before pickup on a second order.
- [ ] Optional: `YANDEX_DELIVERY_CALLBACK_URL` once the edge route exists (the poll works without it).
