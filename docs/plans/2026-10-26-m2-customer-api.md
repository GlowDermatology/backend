# M2 customer API — implementation plan (slice B7)

**Contract:** `docs/api/customer-api-v1.md` (this plan names its sections as "C§"). **Spec:**
`docs/specs/2026-10-24-backend-foundation-design.md` (Customer (M2), Financial integrity, Milestones → M2). **Shared
rules:** `docs/api/admin-api-v1.md` §1/§3/§4, `AGENTS.md`. Where this plan and the contract disagree, the contract
wins and this file is fixed.

**Goal.** The iOS/Android app can sign up, sign in on one device, browse, fill a server cart, check out with stock
reservation and a cashback hold, pay with Click or Payme (callbacks verified and idempotent), track BTS delivery, see
cashback and show the personal QR, book specialists, and read its inbox — on the built M1 models with **one**
migration, reusing the B4/B5/B6 services instead of redefining them.

---

## 0. Branching, packages and order

```
feat/m1c-orders (B5) ─┐
                      ├─ feat/m2-base (lead: merge, gate green)
feat/m1-followups ────┘        │
                               └─ feat/m2-p0-seams (P0, sequential, small)
                                     ├─ feat/m2-p1-accounts    (P1)  ┐
                                     ├─ feat/m2-p2-commerce    (P2)  ├ parallel, disjoint files
                                     └─ feat/m2-p3-engagement  (P3)  ┘
merge order into feat/m2-integration: P2 → P3 → P1 → lead e2e + schema regeneration
```

| Package | Scope | Worktree / branch | Test DB |
|---|---|---|---|
| **P0 seams** | Everything two packages would otherwise both edit: customer auth class + base views, device headers, i18n catalog, error codes + `details`, throttle scope, enums, settings/env/beat, URL mounts, fixtures, the two cross-package service stubs | `/home/user/wt/m2-p0`, `feat/m2-p0-seams` | `test_gd_m2_p0` |
| **P1 accounts** | Customer auth (OTP, register, login, reset, refresh, logout, social), `me/*` incl. delete + addresses + device, public catalog, `settings/public/`, Eskiz port | `/home/user/wt/m2-p1`, `feat/m2-p1-accounts` | `test_gd_m2_p1` |
| **P2 commerce** | Cart, delivery regions/calculate, checkout, pay, customer cancel, Click/Payme callbacks, late capture, expiry grace, customer orders + timeline, BTS webhook + polling, dev simulate | `/home/user/wt/m2-p2`, `feat/m2-p2-commerce` | `test_gd_m2_p2` |
| **P3 engagement** | Loyalty summary/ledger, QR + POS resolve + `createposkey`, specialists/services/slots, booking + customer cancel, inbox list/read/ack, FCM payload changes + token pruning | `/home/user/wt/m2-p3`, `feat/m2-p3-engagement` | `test_gd_m2_p3` |

Why this split (instead of the suggested three): the customer authentication class, the error-envelope extension,
the i18n catalog, settings, URL mounts and fixtures are needed by **every** package; building them once in P0 removes
every shared-file conflict, so P1–P3 own disjoint files. The public catalog moves into P1's `customers` module
because availability (`commerce.selectors.available_stock`) may not be imported by `catalog` (layering), and `DELETE
me/` lives in `customers` (top layer) because it orchestrates commerce and appointments.

### 0.1 Rules for every package
- Dev loop of `AGENTS.md` with the package's `TEST_DB_NAME`; coverage ≥85 %; `makemigrations --check`; mypy;
  lint-imports (the layer contract is unchanged).
- **Do not commit `openapi/schema.yaml`.** Run `spectacular --validate --fail-on-warn` locally; the lead regenerates
  and commits the schema once on `feat/m2-integration` (no hand-merging a generated file).
- Only the files listed under "Owns" (plus new test files in the module's `tests/`). Anything else → stop and report.
- Every handler: `@extend_schema` with the operationId of C§1.1, `CUSTOMER_ERRORS`/`PUBLIC_ERRORS`, the device-header
  parameters (P0 helper), `IDEMPOTENCY_KEY_HEADER` on **I** routes, `@idempotent()` on **I** routes.
- Messages only through `core.i18n.t(key, lang, …)` with the keys of C§5 (P0 ships the whole catalog; a package that
  needs a new key stops and reports — the contract changes first).
- Services take ids and scalars, return dataclasses; cross-module writes only through the lower module's services; lock
  order Order → Payment → LoyaltyAccount; time via `timeutils.now()`.
- Commits: small, imperative (`feat(customers): add the send-code endpoint`), never pushed.

---

## 1. P0 — seams (sequential, before P1–P3)

### Owns
| File | Change |
|---|---|
| `glowderma/core/enums.py` | the new enums of C§3 (`OtpPurpose`, `NextStep`, `SocialProvider`, `SocialTokenType`, `DevicePlatform`, `StockStatus`, `CustomerProductOrdering`, `CartLineStatus`, `OrderScope`, `CustomerRefundState`, `CashbackEarnedState`, `TimelineType`, `ProgressState`, `LedgerSource`, `AppointmentScope`, `AppointmentCancelledBy`, `InboxKind`, `InboxLinkKind`) |
| `glowderma/core/i18n.py` (new) | `CATALOG` with every key of C§5, `t(key, lang, **params)`, `resolve_language(request, customer=None)` (Accept-Language q-parsing, fallback customer → `uz`), `format_number(n, lang)`, `format_percent(p, lang)` |
| `glowderma/core/api/exceptions.py` | customer codes (C§1.6) as `CustomerError(code, status, *, details=None, params=None)` subclasses; `ENDPOINT_FIELD_CODES` += `otp_invalid`, `password_too_short`, `password_too_common`, `password_entirely_numeric`, `password_too_similar`, `password_unchanged`, `password_incorrect`, `terms_outdated`, `push_token_missing`, `latlng_pair`, `address_incomplete`; `AUTH_FAILURE_CODES` unchanged; the handler adds `details` (null by default) and renders `message`/field messages through `core.i18n` **only** when the resolved view is a customer/public/POS view (`view.audience` attribute); admin bodies stay byte-identical (regression test) |
| `glowderma/core/api/schema.py` | `CUSTOMER_ERRORS`, `PUBLIC_CUSTOMER_ERRORS`, `DEVICE_HEADERS` (`X-GD-Device-Id`, `X-GD-Platform`, `X-GD-App-Version`), `ACCEPT_LANGUAGE_HEADER` |
| `glowderma/core/throttling.py` | scope `public: "300/min"` |
| `glowderma/core/audit.py` | `record()` accepts a `Customer` or `PosDevice` actor: `actor_type` `customer`/`integration`, `actor_label` = masked phone / device name (no `get_username` on non-staff) |
| `glowderma/accounts/models.py` | `Customer.is_authenticated = True`, `is_anonymous = False` (properties, **no migration**) so `core.idempotency` and DRF treat it as a principal |
| `glowderma/accounts/tokens.py` | access TTL by audience (`STAFF_ACCESS_TTL` / `CUSTOMER_ACCESS_TTL`) |
| `glowderma/accounts/authentication.py` | `CustomerJWTAuthentication` (aud `customer`; `auth:sid:{sid}` cache value `active:{abs_expiry}:{fid_sha256[:16]}` so the device check needs no query; tombstones `revoked:<reason>`; device/platform header match; `last_active_at`/`app_version` bump ≤ every 5 min); `OptionalCustomerAuthentication` |
| `glowderma/accounts/permissions.py` | `IsActiveCustomer` |
| `glowderma/accounts/api/customer_base.py` (new) | `CustomerAPIView`, `CustomerGenericViewSet`, `PublicAPIView`, `WebhookAPIView` (CSRF-exempt, form+JSON parsers, no auth, provider-native responses), device-header parsing + 400 `device_required` + 426 `app_update_required` gate (a mixin used by the auth views too) |
| `glowderma/accounts/api/throttles.py` | `CustomerRateThrottle` (`customer`), `PublicIPThrottle` (`public`), `CheckoutThrottle`, `PayThrottle`, `SmsPhoneThrottle`, `SmsIPThrottle`, `PosRateThrottle` |
| `glowderma/accounts/services.py` | `start_customer_session(customer_id, *, fid, platform, app_version) -> (AuthSession, raw_refresh)` (C§1.9 steps 1–5), `revoke_customer_sessions(customer_id, reason, except_sid=None)`, `register_device(customer_id, fid, …)`, `invalidate_fcm_token(device_id)`, tombstone TTL = max(staff, customer access TTL) + 60 |
| `glowderma/accounts/topics.py` (new) | `SMS_SEND = "sms.send"`, `PUSH_SESSION_REPLACED = "push.session_replaced"` + dedupe-key builders (`sms.send:{sms_code_id}`, `push.session_replaced:{session_id}`) |
| `glowderma/accounts/tasks.py` (new) | handler `push.session_replaced` (FCM data-only message through `integrations.fcm`; an `unregistered` result → `invalidate_fcm_token`); beat task `accounts.cleanup_auth` (delete `SmsCode` older than 24 h; delete customer `AuthSession` rows revoked or expired for >90 days) |
| `glowderma/catalog/selectors.py` | `customer_visible_products()` (C§2.3 visibility) |
| `glowderma/commerce/services.py` | stub `cancel_by_customer(order_id: int, customer_id: int, *, at=None) -> int` raising `NotImplementedError("P2")` |
| `glowderma/appointments/services.py` | stub `cancel_by_customer(appointment_id: int, customer_id: int, *, reason: str = "", at=None) -> Appointment` raising `NotImplementedError("P3")` |
| URL stubs | `glowderma/{customers,site_config,delivery,commerce,loyalty,appointments,notifications}/api/customer/{__init__,urls}.py`, `glowderma/commerce/api/webhooks/{__init__,urls}.py`, `glowderma/commerce/api/dev/{__init__,urls}.py`, `glowderma/loyalty/api/pos/{__init__,urls}.py` — each `urlpatterns = []` |
| `config/urls.py` | `CUSTOMER_API_MODULES = ("customers", "site_config", "delivery", "commerce", "loyalty", "appointments", "notifications")` mounted at `api/v1/`; `api/v1/webhooks/` → commerce webhooks; `api/v1/pos/` → loyalty pos; `api/v1/dev/` only when `FAKE_PAYMENTS_ENABLED` |
| `config/settings/base.py`, `test.py`, `production.py` | every setting of §7 with validation; production refuses the fake switches and requires the secrets |
| `config/celery.py` | beat entries of §8 (`commerce.poll_bts` points at a P0 stub task `return []`, implemented by P2) |
| `.env.example` | the lines of §7 |
| `conftest.py`, `glowderma/accounts/tests/factories.py` | fixtures `customer`, `customer_session`, `customer_token`, `customer_client` (bearer + device headers `fid-test-0001`, `ios`, `1.0.0`), `public_client`, `pos_device` + `pos_key`, `pos_client`; helper `device_headers(fid=…, platform=…, version=…)` |

### Tests (P0)
- Audience separation both ways; customer token on admin routes → 401; staff token on a dummy customer route → 401.
- Revocation codes through the cache (`replaced` → `SESSION_REPLACED`, others `SESSION_EXPIRED`) without a DB hit;
  deleted customer → `SESSION_EXPIRED`.
- Device headers: missing → 400 `device_required`; fid mismatch → 401 + one audit row per hour; platform mismatch;
  `X-GD-App-Version` below the floor → 426 with `details`.
- `start_customer_session`: second device revokes the first (`replaced`), deactivates its device, enqueues
  `push.session_replaced`; **race:** two concurrent sign-ins of one customer on two devices (threads, `transaction=True`)
  end with exactly one open session and one active device.
- `register_device`: a token held by another active row deactivates that row; `push_enabled` without a token → error.
- i18n: every catalog key has uz/ru/en with identical placeholder sets; `resolve_language` matrix (`ru-RU`, `en;q=0.5,
  uz`, unsupported, customer fallback).
- Error envelope: customer views get `details` and localized messages; admin responses unchanged (snapshot test of
  one validation error and one 409).
- `core.idempotency` with a customer principal (replay / 422 / 409) and audience scoping (same key, staff vs customer).
- `accounts.cleanup_auth` purges only the right rows; the throttle scope validation knows `public`.
- The automatic URL-walk auth matrix is extended: every route under `api/v1/` that is not admin/health/schema is
  classified **C**/**O**/**S**/**K** from its base class and probed accordingly.

### Depends on
`feat/m2-base` green.

---

## 2. P1 — accounts, profile, public catalog, settings

### Owns
`glowderma/customers/{services.py, selectors.py, api/customer/*}` (new views: auth, me, catalog, delete),
`glowderma/accounts/services.py` (OTP and credential flows only — below the P0 block), `glowderma/accounts/tasks.py`
(the `sms.send` handler, appended), `glowderma/integrations/eskiz/{__init__,ports,fake,registry}.py` (new),
`glowderma/integrations/oauth/{ports,fake,google,facebook}.py` (`verify_app_token`), `glowderma/site_config/{selectors.py,
api/customer/*}`, `glowderma/catalog/selectors.py` (customer list helpers, after P0's function), tests in
`customers/tests/`, `accounts/tests/`, `integrations/tests/`, `site_config/tests/`.

### Endpoints
C§2.0 rows 1–20: `auth/send-code/`, `auth/verify-code/`, `auth/set-password/`, `auth/login/`, `auth/reset-password/`,
`auth/refresh/`, `auth/logout/`, `auth/social/`, `auth/social/complete/`, `me/` (GET/PATCH/DELETE), `me/consent/`,
`me/image/`, `me/device/`, `me/addresses/*`, `catalog/banners/`, `catalog/categories/`, `catalog/products/`,
`catalog/products/{id}/`, `settings/public/`.

### Services
| New | Where | Notes |
|---|---|---|
| `send_code(phone, purpose, *, ip, lang) -> CodeIssue` | `accounts.services` | resend/day/lockout rules from `SmsCode` rows; HMAC; outbox `sms.send` |
| `verify_code(phone, purpose, code) -> raw verification token` | `accounts.services` | `select_for_update`; attempts/exhaustion |
| `consume_verification(token, *, purpose, phone=None) -> phone` | `accounts.services` | single use under lock |
| `rotate_customer_refresh(raw, *, fid) -> RefreshResult` | `accounts.services` | rotation, grace cache `auth:refresh_grace:{old_hash}`, reuse → revoke `replaced` + audit |
| `logout_customer(raw)` | `accounts.services` | revoke + device inactive |
| `register(...)`, `login(...)`, `reset_password(...)`, `social_sign_in(...)`, `complete_social(...)`, `accept_consent(...)`, `update_profile(...)`, `set_image(...)`, `delete_account(customer_id, password)` | `customers.services` | orchestrators (need `site_config` for the terms version and, for delete, `commerce`/`appointments`) |
| `public_settings()` | `site_config.selectors` | cached 60 s, invalidated on `update_settings` commit |
| `product_cards(qs, lang, customer_id=None)` | `customers.selectors` | one query for products + `available_stock` + main image + cart quantities |

**Reused (do not redefine):** `accounts.services.{add_address, update_address, set_default_address, remove_address,
set_customer_password, check_customer_password, delete_customer, reactivate_customer, start_customer_session (P0),
register_device (P0), revoke_customer_sessions (P0)}`, `accounts.api.guard.LoginGuard` (scope `customer_login`),
`catalog.selectors.{filter_products_search, customer_visible_products, category_queryset, banner_queryset}`,
`commerce.selectors.available_stock` (B5), `site_config.models.SiteSettings.load`, `core.images.process_image`,
`commerce.services.cancel_by_customer` (P2) and `appointments.services.cancel_by_customer` (P3) from `delete_account`
(called only when the customer has open items; P1 tests cover the no-open-items path, the lead's integration test the
rest).

### Models / migrations
None.

### Fakes
- **FakeEskiz** (C§2.14.1): phone ending `0000` → `EskizRejected`, `9999` → `EskizUnavailable`, else accepted
  `fake-<sms_code_id>`; `FakeEskiz.sent` list; the code generator returns `ESKIZ_FAKE_CODE` in fake mode.
- **OAuth `verify_app_token` fakes**: `fake:<subject>:<email>` valid (Google: nonce must be `nonce-<subject>` or `*`
  when sent), `fake-unverified:…` valid without verified email, `fake-fail` → `exchange_failed`, `fake-down` →
  `unavailable`, `fake-expired` → `invalid_token`.

### Tests (P1)
- Every validation row of C§2.1–§2.4 (one test per code, uz message; one ru and one en rendering per endpoint group).
- OTP: resend window (61 s ok, 59 s 429 with `Retry-After`), daily cap 10 (Tashkent day boundary), lockout after 5
  wrong codes, superseded code, expired code, `compare_digest` path, verification token single use / purpose / phone
  binding / expiry; **race:** two concurrent `verify-code` with the right code → one token issued, the other
  `otp_expired`.
- Register: new customer; deleted customer reactivated with the same id, ledger and purchases intact (spec test
  "delete → reactivate keeps balance and purchases"); consent stamped; outdated terms.
- Login: known device → tokens; unknown device → `verify_device` + SMS issued; second call with the token → tokens and
  the old session `SESSION_REPLACED`; wrong password timing parity (hasher called for unknown phones); lockout.
- Refresh: rotation; grace replay returns identical tokens (concurrent refresh threads); reuse after grace → 401
  `SESSION_REPLACED` and the session is dead for the new token too; sliding expiry; device mismatch.
- Social: linked → tokens; unknown → ticket; send-code with ticket; complete for active/no/deleted customer;
  `social_provider_linked`, `social_identity_linked` (unique-race); ticket reuse refused.
- `DELETE me/`: password check; scrub (name, image file, addresses, identities, device tokens), sessions revoked with
  tombstones, audit; re-registration afterwards.
- Addresses through the services (default handling, `address_text` recomputed, limit 10, foreign id 404).
- Catalog: visibility (hidden product, inactive category, price 0), availability = stock − active reservations,
  search in ru/en, `in_stock`, ordering, language fallback chain to `billz_name`, banner window and link downgrade,
  page-size allow-list, optional auth (invalid token → 401, absent → anonymous), cache headers.
- `sms.send` handler: claim before call, no resend after claim, connect-error un-claim, 4xx not retried.

### Depends on
P0. For the delete orchestration at integration time: P2 (`commerce.services.cancel_by_customer`) and P3
(`appointments.services.cancel_by_customer`).

---

## 3. P2 — cart, delivery, checkout, payments, orders, BTS

### Owns
`glowderma/commerce/{models.py, migrations/0004_cartitem_price_at_add.py, services.py, selectors.py, tasks.py,
api/customer/*, api/webhooks/*, api/dev/*}`, `glowderma/payments/{services.py, selectors.py}`,
`glowderma/delivery/{services.py, selectors.py, api/customer/*}`, `glowderma/integrations/click/*`,
`glowderma/integrations/payme/*`, `glowderma/integrations/bts/{ports,fake,registry}.py`, tests in those modules.

### Endpoints
C§2.0 rows 21–31 and 46–49: `cart/*`, `delivery/regions/`, `delivery/calculate/`, `orders/checkout/`,
`orders/{id}/pay/`, `orders/{id}/cancel/`, `me/orders/`, `me/orders/{id}/`, `webhooks/click/prepare/`,
`webhooks/click/complete/`, `webhooks/payme/`, `webhooks/bts/`, `dev/payments/{order_id}/simulate/`.

### Services
| New / changed | Module | Notes |
|---|---|---|
| `cart_view(customer_id, lang)`, `add_to_cart`, `set_cart_quantity`, `remove_from_cart`, `clear_cart` | `commerce.services` / `selectors` | Cart row lock; `price_at_add`; clamping to `max_quantity`; C§2.5 errors |
| `create_order(…, delivery_fee: int \| None = None, expected_total: int \| None = None)` | `commerce.services` (B5, extended) | the BTS quote moves **before** the transaction (no HTTP call under `ProductStock` locks); `expected_total` mismatch raises `PriceChanged` inside the transaction (rollback); `OutOfStock` gains per-line `requested`/`available` |
| `checkout(customer_id, request) -> order_id` | `commerce.services` | open-order rule, consent check (from the view), cart → `create_order`, cart emptied, `Cart.last_checkout_order` |
| `is_payable(order, payment, now)` | `commerce.services` | C§2.13.0 definition incl. in-flight grace |
| `ensure_payable_payment(order_id, provider) -> PaymentRow` | `commerce.services` | reuse / switch (`abandoned`) / Payme state-1 refusal / new row after a failure; updates `Order.payment_provider` |
| `cancel_by_customer(order_id, customer_id, *, at=None)` | `commerce.services` | replaces the P0 stub; state.py row 5 |
| `record_late_capture(order_id, payment_id, provider_txn_id, *, at=None)` | `commerce.services` | paid + `Refund(origin=late_capture)` + `refund_requested` event + audit `payment.late_capture` + ERROR log |
| `expire_reservation` / `expire_due_reservations` | `commerce.services` (B5, changed) | skip an order whose pending payment is in flight (C§2.13.0), expire after the grace |
| `customer_orders(customer_id, scope)`, `customer_order_detail(order_id, customer_id, lang)`, `customer_timeline(order, lang)`, `progress(order, lang)` | `commerce.selectors` | C§2.8, C§4 |
| `poll_bts()` | `commerce.tasks` | replaces the P0 stub; C§2.13.3 |
| `attach_transaction(payment_id, provider_txn_id, provider_state)`, `merge_provider_state(payment_id, patch)` | `payments.services` | under the Payment lock; `(provider, provider_txn_id)` unique violation → typed `TransactionTaken` |
| `payme_statement(from_ms, to_ms)`, `payment_by_txn(provider, txn_id)` | `payments.selectors` | |
| `region_catalog()` | `delivery.selectors` | BTS `regions()` cached 24 h (`gd:bts:regions`) |
| Click protocol (`sign_ok`, `parse_amount`, `ERRORS`), Payme protocol (`auth_ok`, `rpc_error`, `MESSAGES`, `TIMEOUT_MS`) | `integrations.{click,payme}.protocol` | pure functions, no DB |
| `ClickPort.pay_url(order_id, amount, return_url)`, `PaymePort.checkout_url(order_id, amount_tiyin, lang, return_url)` | `integrations.{click,payme}` ports + fakes | |
| `BtsPort.track(tracking_number)`, `BtsPort.regions()`; `quote` may raise `BtsRejected` | `integrations.bts` | fakes per §6 |

**Reused (do not redefine):** `commerce.services.{create_order, mark_paid, confirm_payment_effects,
fail_payment_effects, expire_reservation, apply_bts_status, resolve_refund, record_event, lock_order}` (B5),
`commerce.selectors.available_stock`, `payments.services.{create_payment, confirm_payment, fail_payment,
create_refund}`, `payments.selectors.{active_payment, display_payment, refunds_for_order}`,
`delivery.services.{ensure_shipment, request_bts_create, apply_status}`, `delivery.selectors.shipment_info`,
`loyalty.selectors.account_summary` (cart `cashback`), `loyalty.services.create_hold` (inside `create_order`),
`catalog.selectors.customer_visible_products` (P0), `site_config.SiteSettings` (pickup address).

### Models / migrations
`commerce.0004_cartitem_price_at_add`: `CartItem.price_at_add = BigIntegerField(null=True)` (null for lines created
before the migration → `price_changed: false`). Nothing else: `Order.metadata` carries `checkout.*`/`fake.*`,
`Payment.provider_state` carries the protocol fields (design 02 §2.1), `Refund.Origin.LATE_CAPTURE` exists.

### Tests (P2)
- Cart: every C§2.5 rule (clamp, `max_reached`, `OUT_OF_STOCK` on zero, `product_unavailable`, `cart_full`, bulk
  delete limits, `price_changed` after an admin price edit, statuses after a Billz stock drop).
- Checkout: every row of C§2.7.1 in order; snapshots and events equal to B5's admin expectations; cart emptied; replay
  returns the stored 201 with an empty cart; consent outdated.
- **Race — two checkouts for the last unit** (threads, `transaction=True`): exactly one order, one reservation; the
  other gets `OUT_OF_STOCK` with `available: 0`.
- **Race — two checkouts of one customer with different keys**: one order, the other `open_order_exists`.
- Pay: reuse, switch Click↔Payme (`abandoned`), Payme state-1 refusal, expired-on-the-spot, already paid, URL format
  (Click query, Payme base64 with tiyin and language).
- Customer cancel: from `awaiting_payment` (Click pending failed; Payme state-1 left pending), refused otherwise.
- Customer orders: scope filters, ownership 404, №10248 timeline vector (C§4.1) in uz/ru/en, progress for delivery,
  pickup and cancelled orders, refund mapping.
- **Click sandbox suite (offline, §5.1)** and **Payme sandbox suite (offline, §5.2)** — every row.
- **Race — expiry vs late callback:** (a) Click complete holding the Order lock while the expiry job runs → the job
  skips (`skip_locked`), the payment wins; (b) the job wins first → complete answers -9 (default) or, with
  `CLICK_REVERSES_ON_COMPLETE_ERROR=false`, a late-capture refund; (c) prepare at T−30 s, expiry at T, complete at
  T+60 s → still paid (in-flight grace); (d) complete after the grace → not payable.
- **Race — duplicate concurrent Click complete** (two threads, same payload): one effect, both answers `0`.
- **Race — two Payme `CreateTransaction` with different ids for one order**: one state-1 transaction, the other -31008.
- Webhook inbox: forged signature never occupies the real key (`rejected:` rows); same key + different payload → -8;
  credentials absent from stored headers; `entity_ref` set.
- Outbox "crash after provider success": Click complete commits, the response is lost, Click retries → replay `0`, one
  purchase, one credit.
- BTS: webhook bad token 401; re-track is authoritative (payload says `300`, track says `200` → `handed_to_courier`);
  unknown parcel ignored; transport failure 503 and nothing stored; polling picks stale shipments only; regions cache.
- Delivery calculate: own address, region unknown, BTS rejected → 409, BTS down → 503, pickup → 0.
- Dev simulate: mounted only with the flag; runs the real views; refused in production settings.

### Depends on
P0; B5 merged (`feat/m2-base`).

---

## 4. P3 — loyalty, QR/POS, appointments, inbox

### Owns
`glowderma/loyalty/{qr.py (new), selectors.py, api/customer/*, api/pos/*, authentication.py (new: PosKeyAuthentication,
IsActivePosDevice), services.py (PosDevice key creation), management/commands/createposkey.py}`,
`glowderma/appointments/{services.py, selectors.py, api/customer/*}`, `glowderma/notifications/{services.py,
selectors.py, api/customer/*}`, `glowderma/integrations/fcm/{ports,fake}.py` (`PushMessage` gains `kind`/`priority`
for the Android channel and APNs headers; the fake records them), tests in those modules.

### Endpoints
C§2.0 rows 32–45: `me/cashback/`, `me/cashback/ledger/`, `me/qr/`, `pos/cashback/resolve-qr/`, `specialists/`,
`specialists/{id}/`, `services/`, `specialists/{id}/slots/`, `appointments/`, `me/appointments/`,
`me/appointments/{id}/`, `appointments/{id}/cancel/`, `me/notifications/`, `me/notifications/unread-count/`,
`me/notifications/{id}/read/`, `me/notifications/read-all/`, `me/notifications/ack/`.

### Services
| New / changed | Module | Notes |
|---|---|---|
| `issue_qr(customer_id) -> QrToken`, `resolve_qr(token) -> customer_id \| None` | `loyalty.qr` | 128-bit token, HMAC key in Redis, `SET NX EX 60`, `GETDEL` through the raw Redis client (LocMem: lock + get + delete) |
| `create_pos_device(name, location_label, actor) -> (PosDevice, raw_key)`, `authenticate_pos_key(raw)` | `loyalty.services` / `loyalty.authentication` | SHA-256, `compare_digest`, `last_used_at` ≤ once per minute; `POS_API_KEYS` seeding in fake mode (seeder) |
| `customer_ledger_page(customer_id)`, `ledger_item_view(row, lang)` | `loyalty.selectors` | over `ledger_for_customer` rows, paginated in SQL |
| `free_slots(professional_id, service_id, date, now)`, `check_customer_slot(...)` | `appointments.selectors` / `services` | C§2.11 algorithm (grid relative to the hours start, lead time, horizon, blocks, break, open appointments) |
| `request_by_customer(customer_id, professional_id, service_id, start, note)` | `appointments.services` | exclusion-race → 409 `conflict` (reuse `_lost_the_slot`); event `requested` source `customer`; audit |
| `cancel_by_customer(appointment_id, customer_id, *, reason="", at=None)` | `appointments.services` | replaces the P0 stub |
| `customer_appointments(customer_id, scope)` | `appointments.selectors` | |
| `inbox_page(customer_id)`, `unread_count(customer_id)` | `notifications.selectors` | `inbox_customer_unread_idx` |
| `mark_read(customer_id, inbox_id)`, `mark_all_read(customer_id)`, `acknowledge(customer_id, inbox_ids)` | `notifications.services` | conditional updates; campaign `delivered` increment only on a real transition |
| push payloads with `inbox_id`; `unregistered` → `accounts.services.invalidate_fcm_token` | `notifications.services` (`_push_messages`, `_deliver`, `_settle`) | C§2.14.2 |

**Reused (do not redefine):** `loyalty.selectors.{account_summary, customer_totals, tier_for, active_policy,
rules_of, ledger_for_customer, held_amount}`, `professionals.selectors.{day_schedules, booking_catalogue}`,
`appointments.services.{active_link, record_event, _lost_the_slot}`, `appointments.selectors.get_appointment`,
`notifications.services.{_deliver, deliver_order_status, deliver_appointment, send_batch}`,
`notifications.selectors.customer_push_device`, `accounts.services.invalidate_fcm_token` (P0).

### Models / migrations
None (`PosDevice`, `InboxNotification.delivered_at`, `CampaignRecipient.delivered_at`, `Campaign.delivered`,
`Appointment.CancelledBy.CUSTOMER`, `AppointmentEvent.Source.CUSTOMER` exist).

### Tests (P3)
- Cashback summary numbers equal the admin card for Malika (seed vectors); negative balance flag; holds listed;
  ledger titles/subtitles for every kind in uz/ru/en; adjustment reason never leaked.
- QR: token format; TTL 60 s (pinned clock + cache expiry); **race — double resolve** (two threads, one token): exactly
  one 200, one `qr_invalid`; expired token; deleted customer; malformed token; audit rows; response field allow-list
  (snapshot: no name/ledger keys).
- POS auth: unknown/inactive key 401, key never logged, throttle `pos`, `createposkey` prints the key once and stores
  only the hash.
- Slots: hours override vs weekly, day off, break, blocked period across midnight, open appointments (requested holds,
  rejected frees), lead time, horizon, slot step alignment; booking validation rows of C§2.11; **race — two customers
  book the same slot** → one 201, one 409 `conflict` (including the 40P01 deadlock branch); own-overlap; booking
  limit; customer cancel and the slot freed; ownership 404.
- Inbox: pagination + `unread_count`; read one/all; ack idempotent (a retried ack does not double-count
  `Campaign.delivered`); foreign ids ignored; read implies delivered.
- FCM payloads: `inbox_id` present for the three senders; channel/priority mapping; no PII (assert title/body/data
  contain no customer name or phone for seeded customers); `unregistered` prunes the device.

### Depends on
P0.

---

## 5. Offline sandbox suites (P2)

Run with the real webhook views and a pinned clock; signatures built by `integrations/click/sandbox.py` /
`integrations/payme/sandbox.py` (test helpers, also used by `dev/payments/…/simulate/`).

### 5.1 Click (order total 784 400; payment id 301)

| # | Call | Input variation | Expected `error` | Expected state |
|---|---|---|---|---|
| 1 | prepare | valid | 0, `merchant_prepare_id` 301 | payment pending, txn attached, `prepare_at` |
| 2 | prepare | same payload again | 0 (replayed) | unchanged; one inbox row |
| 3 | prepare | wrong `sign_string` | -1 | nothing; `rejected:` row |
| 4 | prepare | `action=1` on prepare URL | -3 | nothing |
| 5 | prepare | unknown `merchant_trans_id` | -5 | nothing |
| 6 | prepare | `amount=784401` / `"784400.50"` / `"abc"` | -2 | nothing |
| 7 | prepare | `service_id` other / missing param | -8 | nothing |
| 8 | prepare | order cancelled or expired | -9 | nothing |
| 9 | prepare | order paid | -4 | nothing |
| 10 | prepare | new `click_trans_id` while 301 is prepared | 0, new prepare id | 301 failed `abandoned`, new pending |
| 11 | complete | valid | 0, `merchant_confirm_id` 301 | paid, order `processing`, events, outbox |
| 12 | complete | same payload again | 0 (replayed) | one purchase, one credit |
| 13 | complete | unknown `merchant_prepare_id` | -6 | nothing |
| 14 | complete | `error=-5017` | -9 | payment failed, `payment_failed` event, order still payable |
| 15 | complete | amount differs | -2 | pending |
| 16 | complete | after expiry, beyond grace, reverses=true | -9 | payment failed `late_complete` |
| 17 | complete | after expiry, beyond grace, reverses=false | 0 | paid + `Refund(late_capture)` requested + `refund.submit` + audit |
| 18 | complete | after expiry, within grace | 0 | paid normally |
| 19 | complete | bad signature (incl. `merchant_prepare_id` tampered) | -1 | nothing |
| 20 | complete | payment already failed | -9 | nothing |

### 5.2 Payme (mirrors the `test.paycom.uz` checklist; amounts in tiyin 78 440 000)

| # | Method | Variation | Expected |
|---|---|---|---|
| 1 | any | no/wrong Basic auth | -32504 |
| 2 | – | body not JSON / no `method` / unknown method / `ChangePassword` / GET | -32700 / -32600 / -32601 / -32601 / -32300 |
| 3 | CheckPerformTransaction | unknown order | -31050 (`data: "order_id"`) |
| 4 | CheckPerformTransaction | wrong amount | -31001 |
| 5 | CheckPerformTransaction | cancelled / paid / expired order | -31008 |
| 6 | CheckPerformTransaction | valid | `{"allow": true}` |
| 7 | CreateTransaction | valid new id | state 1, `transaction` "301" |
| 8 | CreateTransaction | same id again | same `create_time`, state 1 |
| 9 | CreateTransaction | other id while 7 is state 1 | -31008 |
| 10 | CreateTransaction | `time` older than 12 h | -31008 |
| 11 | PerformTransaction | unknown id | -31003 |
| 12 | PerformTransaction | valid | state 2, order `processing` |
| 13 | PerformTransaction | again | same `perform_time`, state 2, one purchase |
| 14 | CancelTransaction | state 2, no refund open | -31007 |
| 15 | CancelTransaction | state 2 after an admin cancel (`manual_required`) | state -2, refund `succeeded`, payment `refunded`, `refund_succeeded` event |
| 16 | CancelTransaction | state 1 (reason 3) | state -1, payment failed, `payment_failed` event |
| 17 | CancelTransaction | again | same `cancel_time`, state -1 |
| 18 | PerformTransaction | state -1 | -31008 |
| 19 | CreateTransaction / Perform | state-1 transaction older than 12 h | state -1 reason 4, -31008 |
| 20 | PerformTransaction | order expired beyond grace while state 1 | -31008, payment stays pending |
| 21 | CheckTransaction | each state | fields per C§2.13.2 (0 for missing times, `reason` null) |
| 22 | GetStatement | window covering 7 and 16 | both, ascending by `create_time` |

---

## 6. Fake provider behaviour (deterministic switches for e2e)

Black-box e2e drives every branch with `X-GD-Fake-Scenario` (copied into `Order.metadata["fake"]` at checkout; read
directly by `delivery/calculate/`, which has no order yet; fake mode only) or with the phone/token grammar below.

| Provider | Switch | Effect |
|---|---|---|
| Eskiz | phone `…0000` / `…9999` / other | rejected / transport down / accepted; code = `ESKIZ_FAKE_CODE` |
| OAuth | token `fake:<sub>:<email>`, `fake-unverified:…`, `fake-fail`, `fake-down`, `fake-expired` | valid / valid unverified / `social_token_invalid` / 503 / `social_token_invalid` |
| BTS | `bts.quote`: `"rejected"` / `"down"`; `bts.tariff`: int | 409 `delivery_unavailable` / 503 / fixed fee |
| BTS | `bts.fail_attempts`, `bts.down`, `bts.existing`, `bts.tracking_number`, `bts.status`, `bts.cancel` | B5 rules (create/cancel) |
| BTS | `bts.track_status`: `"100"`/`"200"`/`"300"`/other | what `track()` answers (webhook re-track, polling) |
| Click | `click.refund`: `succeeded`/`failed`/`unknown`/`down` | B5 refund rules (also used by the late-capture refund) |
| Click | `click.transaction_id` | fixed `click_trans_id` for `dev/…/simulate/` |
| Payme | `payme.transaction_id` | fixed Payme id for `dev/…/simulate/` |
| FCM | token `…-bad` / `…-down` | `unregistered` (device pruned) / `FcmUnavailable` (retry) |
| POS | `POS_API_KEYS=name:key,…` | seeded `PosDevice` rows (fake mode) |
| Clock | `FAKE_NOW` (local/test) | pinned `timeutils.now()` for expiry/grace/slot tests |

---

## 7. Settings and environment (P0 adds all; `.env.example` lines)

```dotenv
# ---- Customer API (M2) ---------------------------------------------------------
# Access JWT lifetime (seconds) and the sliding refresh window (days); grace for concurrent refreshes (seconds).
#CUSTOMER_ACCESS_TTL=900
#CUSTOMER_REFRESH_TTL_DAYS=60
#CUSTOMER_REFRESH_GRACE_SECONDS=30
# OTP policy (A3): code TTL, resend wait, attempts, lockout, daily cap per phone; verification token TTL.
#CUSTOMER_OTP_TTL_SECONDS=120
#CUSTOMER_OTP_RESEND_SECONDS=60
#CUSTOMER_OTP_MAX_ATTEMPTS=5
#CUSTOMER_OTP_LOCKOUT_MINUTES=15
#CUSTOMER_OTP_DAILY_LIMIT=10
#CUSTOMER_VERIFICATION_TTL_SECONDS=600
#CUSTOMER_PASSWORD_MIN_LENGTH=8
# HMAC key for stored SMS codes (32+ random bytes, base64url). Required in production.
SMS_CODE_HMAC_KEY=change-me-sms-code-hmac-key-at-least-32-bytes
#SOCIAL_TICKET_TTL_SECONDS=600
# Google client ids the app's ID tokens are issued for (iOS client id, web/server client id used by Android), comma separated.
#GOOGLE_APP_CLIENT_IDS=change-me-ios.apps.googleusercontent.com,change-me-web.apps.googleusercontent.com
# Minimum app versions (426 app_update_required below them) and store links.
#MIN_APP_VERSION_IOS=0.0.0
#MIN_APP_VERSION_ANDROID=0.0.0
#APP_STORE_URL=https://apps.apple.com/app/id0000000000
#PLAY_STORE_URL=https://play.google.com/store/apps/details?id=uz.glowderma.app
#ADDRESS_MAX_PER_CUSTOMER=10
# ---- Eskiz SMS ----------------------------------------------------------------
# fake = in-process (phone ...0000 rejected, ...9999 down; the code is always ESKIZ_FAKE_CODE); live = notify.eskiz.uz (M3).
#ESKIZ_MODE=fake
#ESKIZ_BASE_URL=https://notify.eskiz.uz/api
#ESKIZ_EMAIL=change-me@example.uz
#ESKIZ_PASSWORD=change-me-eskiz-password
#ESKIZ_SENDER=4546
#ESKIZ_CALLBACK_URL=
#ESKIZ_FAKE_CODE=123456
# ---- Cart and checkout ---------------------------------------------------------
#CART_MAX_LINES=30
#CART_MAX_QTY_PER_LINE=10
#CHECKOUT_MAX_OPEN_ORDERS=1
#CHECKOUT_MIN_PAYABLE=1000
# Seconds a payment already started at the provider keeps an expiring order payable.
#PAYMENT_IN_FLIGHT_GRACE_SECONDS=300
# Where the provider page sends the customer back (app deep link; order_id is appended).
#PAYMENT_RETURN_URL=glowderma://payment/return
# ---- Click (SHOP API) ----------------------------------------------------------
#CLICK_SERVICE_ID=00000
#CLICK_MERCHANT_ID=00000
#CLICK_MERCHANT_USER_ID=00000
CLICK_SECRET_KEY=change-me-click-secret-key
#CLICK_PAY_URL=https://my.click.uz/services/pay
#CLICK_ALLOWED_IPS=
# true: an error answered to Complete makes Click cancel the payment (verify in M3); false: accept and auto-refund late captures.
#CLICK_REVERSES_ON_COMPLETE_ERROR=true
# ---- Payme (Merchant API) ------------------------------------------------------
#PAYME_MERCHANT_ID=change-me-payme-merchant-id
PAYME_KEY=change-me-payme-key
#PAYME_CHECKOUT_URL=https://checkout.paycom.uz
#PAYME_ACCOUNT_FIELD=order_id
#PAYME_ALLOWED_IPS=
#PAYME_FISCAL_DETAIL_ENABLED=false
# ---- BTS ---------------------------------------------------------------------
#BTS_BASE_URL=https://api.bts.uz
#BTS_LOGIN=change-me
#BTS_PASSWORD=change-me
#BTS_SENDER_CITY_CODE=1001
#BTS_DEFAULT_PARCEL_WEIGHT_G=1000
BTS_WEBHOOK_SECRET=change-me-bts-webhook-secret
#BTS_POLL_STALE_MINUTES=30
# ---- FCM (live adapter M3) -------------------------------------------------------
#FCM_PROJECT_ID=change-me-firebase-project
#FCM_CREDENTIALS_FILE=/run/secrets/fcm.json
# ---- Loyalty QR and POS ----------------------------------------------------------
QR_HMAC_KEY=change-me-qr-hmac-key-at-least-32-bytes
#QR_TOKEN_TTL_SECONDS=60
# Fake mode only: POS devices seeded as name:key pairs (production keys come from `manage.py createposkey`).
#POS_API_KEYS=Kassa 1:gdpos_demo0001_change-me-32-characters-key
# ---- Appointments ------------------------------------------------------------------
#BOOKING_MIN_LEAD_MINUTES=60
#BOOKING_HORIZON_DAYS=30
#BOOKING_MAX_OPEN_PER_CUSTOMER=3
#BOOKING_CANCEL_MIN_LEAD_MINUTES=0
# ---- Development switches (refused in production) -----------------------------------
#FAKE_SCENARIO_HEADER_ENABLED=false
#FAKE_PAYMENTS_ENABLED=false
```
Validation (P0): positive integers where numeric; `ESKIZ_MODE`/`CLICK_MODE`/`PAYME_MODE`/`BTS_MODE` ∈ {fake, live};
production requires `SMS_CODE_HMAC_KEY`, `QR_HMAC_KEY` (≥32 bytes), `CLICK_SECRET_KEY`, `PAYME_KEY`,
`BTS_WEBHOOK_SECRET` when the matching mode is live, and refuses `ESKIZ_MODE=fake`, `FAKE_SCENARIO_HEADER_ENABLED`,
`FAKE_PAYMENTS_ENABLED`; `THROTTLE_RATES` knows `public`.

## 8. Beat entries (`config/celery.py`, P0)

| Entry | Task | Schedule | Owner of the body |
|---|---|---|---|
| `accounts-cleanup-auth` | `accounts.cleanup_auth` | every 3600 s | P0 |
| `commerce-poll-bts` | `commerce.poll_bts` | every 600 s | P2 (P0 stub returns `[]`) |
| (exists, B5) `commerce-expire-reservations` | `commerce.expire_reservations` | every 60 s | B5; grace change by P2 |

---

## 9. Integration (lead) and cross-package e2e

1. Merge P2, P3, P1 into `feat/m2-integration`; run the full gate; regenerate and commit `openapi/schema.yaml`.
2. Integration tests (lead, `glowderma/customers/tests/test_e2e_m2.py`):
   - register → login on a second device (`verify_device`) → first device gets `SESSION_REPLACED` and a
     `session_replaced` push → refresh rotation;
   - catalog → cart (clamp) → calculate → checkout with cashback → pay (Click) → sandbox prepare/complete → order
     `processing`, hold captured, credit posted, push inbox row, Billz push + BTS create outbox → BTS webhook `200` →
     `handed_to_courier` push → polling `300` → `delivered`;
   - checkout → expiry (pinned clock) → hold released, stock available again, Payme Perform -31008;
   - admin cancel of a Payme-paid order → Payme `CancelTransaction` → refund succeeded → customer timeline "To‘lov
     qaytarildi";
   - `DELETE me/` with an unpaid order and an open appointment → both cancelled → re-register → same id, balance kept;
   - QR issue → POS resolve → second resolve `qr_invalid`;
   - booking → admin confirm (B6 endpoint) → push with `inbox_id` → ack → admin `delivered` counter.
3. Update `AGENTS.md` (customer base views, fixtures, i18n rule, fake switches), `docs/decisions/open-questions.md`
   (the `M2-*` rows of C§7) and the spec's M2 section if a decision changed it.
4. Mobile handover: contract link, `openapi/schema.yaml`, the fake-mode recipe (`ESKIZ_FAKE_CODE`, dev simulate).

## 10. Dependencies at a glance

| From | Needs | Why |
|---|---|---|
| P0 | `feat/m2-base` (B5 + followups merged) | commerce services/selectors, Click/Payme/BTS ports |
| P1 | P0 | auth class, catalog, i18n, settings, `start_customer_session` |
| P1 (`DELETE me/` integration) | P2 `cancel_by_customer`, P3 `appointments.cancel_by_customer` | stubs in P0 keep P1 independent until merge |
| P2 | P0, B5 | `create_order`, `mark_paid`, `fail_payment_effects`, `apply_bts_status`, `resolve_refund` |
| P3 | P0 | auth class, `invalidate_fcm_token`, i18n |
| Lead e2e | P1 + P2 + P3 | full flows |
