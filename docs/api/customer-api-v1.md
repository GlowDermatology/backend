# Glow Derma customer API v1 (M2 contract)

| | |
|---|---|
| Version | 1.0.0-draft (2026-10-26), milestone M2: the iOS/Android customer app, provider callbacks and the cashier QR endpoint |
| Implemented by | the Django 6.1 / DRF 3.18 backend (Codex, packages P0–P3 of `docs/plans/2026-10-26-m2-customer-api.md`); consumed by the native iOS and Android apps (not in this repo) |
| Source of truth | `docs/specs/2026-10-24-backend-foundation-design.md` (Auth and request security → Customer (M2); Financial integrity; Milestones → M2; Provisional business rules). If this file and the spec disagree, the spec wins and this file gets fixed. |
| Shared rules | `docs/api/admin-api-v1.md` §1 (envelope, pagination, idempotency, values), §3 (enums), §4 (OrderEvent types), §5.8 (Uzbek labels). This file **references** them and lists only what is different for the customer audience. |
| Open business rules | `docs/decisions/open-questions.md` (A1–A8, O1–O8, L1–L8, P1–P4, C1–C4, T1–T19) plus the new M2 points in §7 (`M2-*`) |
| Enforced by | drf-spectacular (`openapi/schema.yaml`, `--validate --fail-on-warn`), the URL-walk auth matrix, contract tests per endpoint, the offline Click/Payme sandbox suites (plan §5) |

Examples use `jsonc`: `//` comments give types and nullability and are not part of the payload. Example values are
illustrative unless a test vector says so. Provider protocol details marked **(verify M3)** come from the public
provider documentation and must be confirmed against the provider sandboxes in M3 before going live; the fake
adapters implement exactly what this file says.

---

## 1. Conventions (customer audience)

Everything in admin §1 applies unless a row below says otherwise.

### 1.1 Base, routing, network

| Prefix | Audience | Auth | Notes |
|---|---|---|---|
| `/api/v1/auth/…` | app, before sign-in | none (refresh token in the body where needed) | device headers required (§1.2) |
| `/api/v1/me/…`, `/api/v1/cart/…`, `/api/v1/orders/…`, `/api/v1/appointments/…`, `/api/v1/delivery/calculate/` | signed-in app | **C**: customer access JWT required | device headers required |
| `/api/v1/catalog/…`, `/api/v1/settings/public/`, `/api/v1/delivery/regions/`, `/api/v1/specialists/…`, `/api/v1/services/` | anyone | **O**: optional; a present bearer must be valid | device headers optional |
| `/api/v1/webhooks/{click,payme,bts}/…` | payment and delivery providers | provider signature / Basic auth / shared secret (§2.13) | **not** the error envelope: provider-native bodies |
| `/api/v1/pos/…` | in-store cashier terminals | per-device API key (§2.10) | envelope; Uzbek messages only |
| `/api/v1/dev/…` | developers, fake mode only | **C** | mounted only when `FAKE_PAYMENTS_ENABLED=true`; production settings refuse that flag |

- Every path ends with `/`. Paths in §2 are relative to `/api/v1/`.
- The customer API is reachable from the internet through the edge proxy (TLS terminates there). `AdminNetworkMiddleware`
  does not apply to these prefixes. There is no CORS, no cookie auth and no CSRF: the clients are native apps.
- OpenAPI: one schema; operationIds are `customer_<resource>_<action>`, `webhook_<provider>_<action>`,
  `pos_<resource>_<action>`, `dev_<resource>_<action>`.
- Base views (P0): `CustomerAPIView` (`CustomerJWTAuthentication` + `IsActiveCustomer` + `CustomerRateThrottle`,
  JSON parser), `PublicAPIView` (`OptionalCustomerAuthentication` + `AllowAny` + `PublicIPThrottle`),
  `WebhookAPIView` (no DRF auth; form + JSON parsers; provider-native responses), `PosAPIView`
  (`PosKeyAuthentication` + `IsActivePosDevice` + `PosRateThrottle`). The default stays `DenyAll`.

### 1.2 Request headers

| Header | Required | Rule and what the server does with it |
|---|---|---|
| `Authorization: Bearer <access JWT>` | **C** routes; optional on **O** routes; absent on `auth/*` (except `auth/logout/`, where it is optional) | HS256, `kid` header, claims `iss, aud="customer", sub (customer id), sid, iat, exp, jti`, all required. TTL `CUSTOMER_ACCESS_TTL` (900 s), capped at the session's `absolute_expires_at`. A staff token fails decoding (`aud`) → 401 `not_authenticated`. On an **O** route a present but invalid header is a 401, never silently anonymous. |
| `X-GD-Device-Id` | `auth/*` and **C** routes | The Firebase installation id (`fid`) of this app install, `^[A-Za-z0-9_-]{8,128}$`. Missing or malformed → 400 `device_required`. `auth/*`: the session is bound to `Device(customer, fid)` (created or reactivated on success). **C** routes: must equal the fid of the session's device, else 401 `not_authenticated` and audit `auth.device_mismatch` (at most one row per session per hour). |
| `X-GD-Platform` | with `X-GD-Device-Id` | `ios` \| `android` (else 400 `device_required`). Stored on `Device.platform`; `Order.channel` is taken from the **session's device**, never from a body field. A different platform for an existing `(customer, fid)` → 401 `not_authenticated`. |
| `X-GD-App-Version` | with `X-GD-Device-Id` | `^\d{1,3}\.\d{1,3}\.\d{1,3}$`. Below `MIN_APP_VERSION_IOS` / `MIN_APP_VERSION_ANDROID` (default `0.0.0` = no floor) → 426 `app_update_required` with `details: {"min_version", "store_url"}`. Written to `Device.app_version` together with the `last_active_at` bump (at most every 5 minutes). |
| `Accept-Language` | optional | First supported tag in q-order among `uz`, `ru`, `en` (region subtags ignored: `ru-RU` → `ru`). Absent or unsupported → the signed-in customer's `language`, else `uz`. Selects every `message`, every server-rendered label and every i18n data field (§1.4). Response header `Content-Language`. |
| `Idempotency-Key` | on the **I** routes (§1.7) | UUID, 36 chars, as admin §1.7. |
| `Content-Type` | on bodies | `application/json`; `multipart/form-data` only on `PUT me/image/`; webhooks also accept `application/x-www-form-urlencoded` (Click). Anything else → 415. |
| `X-Request-ID` | ignored from the app | Accepted only with the internal secret (admin §1.2), which the app never has. The server always generates one; the app shows it on error screens ("Xato kodi"). |
| `X-GD-Fake-Scenario` | fake mode only | JSON object copied into `Order.metadata["fake"]` at checkout (BTS/Click/Payme switches of plan §4). Honoured only when `INTEGRATIONS_MODE=fake` **and** `FAKE_SCENARIO_HEADER_ENABLED=true`; otherwise ignored. Production settings refuse the flag. |

**401 codes and the app's reaction** (customer refresh tokens rotate, so a refresh failure always ends the session):

| `code` | Cause | App reaction |
|---|---|---|
| `not_authenticated` | Header missing or malformed; bad signature, `aud` or `iss`; access JWT expired; device id/platform mismatch; unknown refresh token on `auth/refresh/` | Call `auth/refresh/` once and retry. Any 401 from `auth/refresh/` → delete tokens, go to sign-in. |
| `SESSION_EXPIRED` | Session revoked (`logout`, `password_changed`, `deactivated`) or past `absolute_expires_at` (60 days idle); customer deleted; unknown/revoked refresh on `auth/logout/` | Delete tokens; sign-in screen with the phone pre-filled; no message. |
| `SESSION_REPLACED` | Session revoked with reason `replaced`: the account signed in on another device (single-device rule, §1.9), or this refresh token was reused after the grace window (§2.1.6) | Delete tokens; show "Hisobingizga boshqa qurilmadan kirildi." and the sign-in screen. |
| `invalid_credentials` | `auth/login/` only | Form error. |

### 1.3 Response headers
As admin §1.3, plus `Content-Language` (always) and `Retry-After` on every 429 (including the OTP codes) and on 409
`request_in_progress`. `Cache-Control: no-store` on every response except **O** catalog/settings GETs, which send
`Cache-Control: public, max-age=60` (banners, categories, settings) or `max-age=30` (products) and vary on
`Accept-Language` and `Authorization`.

### 1.4 Values
Admin §1.4 applies (snake_case, integer ids, money as integer so'm, ISO 8601 `+05:00`, percent decimal strings with
2 dp, phone E.164 `^\+998\d{9}$`, nulls never omitted, absolute media URLs). Differences:

| Kind | Customer rule | Example |
|---|---|---|
| i18n data fields | Rendered as **one string** in the resolved language (§1.2), not `{uz, ru, en}`. Fallback chain: requested language → `uz` → `""`. A product `name` falls back further to `billz_name`. Admin objects keep the three-language shape. | `"name": "Сыворотка с гиалуроновой кислотой"` |
| Labels | Every enum that the app shows comes with a server-rendered `<field>_label` in the resolved language (§5). The enum literal stays authoritative; the label is presentation. | `"status": "processing", "status_label": "Tayyorlanmoqda"` |
| Phone mask | `+998 XX *** ** NN` | `"+998 90 *** ** 67"` |
| Email mask | first character, `***`, domain | `"m***@gmail.com"` |
| Stock | `available` = max(0, Billz quantity − active reservations − converted-since-sync), the B5 `commerce.selectors.available_stock` | `7` |
| Coordinates | JSON numbers, ≤6 dp | `41.311081` |
| Time of day | `"HH:MM"` Tashkent | `"09:30"` |

### 1.5 Pagination
Envelope, clamping and the `page_size` error as admin §1.5.

| Endpoint | `page_size` allow-list | Default |
|---|---|---|
| `catalog/products/` | 20, 50 | 20 |
| `me/orders/` | 10, 20 | 10 |
| `me/cashback/ledger/`, `me/notifications/`, `me/appointments/` | 20, 50 | 20 |

`me/notifications/` adds one top-level key, `unread_count`, next to the envelope keys (§2.12).

### 1.6 Errors
The admin §1.6 envelope with **one extra key for this audience**: `details`, always present, `null` unless the code's
row in §2 defines an object.
```jsonc
{"code": "OUT_OF_STOCK",
 "message": "Ba’zi mahsulotlar yetarli emas.",          // localized (§1.2), catalog key error.OUT_OF_STOCK
 "errors": [],
 "details": {"lines": [{"product_id": 4, "name": "Seramidli namlantiruvchi krem",
                        "requested": 3, "available": 1, "status": "insufficient"}]},
 "request_id": "6f1c2a0e9b8d4c37"}
```
- Messages are localized (uz default; ru/en from the catalog in §5). Field `message`s too.
- Resources of another customer always answer **404 `not_found`** (never 403), so ids cannot be probed.
- Top-level code casing: snake_case, except the PDF literals `SESSION_EXPIRED`, `SESSION_REPLACED` and `OUT_OF_STOCK`.

Codes added for this audience (admin §1.6 codes keep their meaning):

| HTTP | `code` | When | `details` |
|---|---|---|---|
| 400 | `device_required` | Device headers missing or malformed (§1.2) | null |
| 400 | `otp_invalid` | Wrong SMS code (§2.1.2) | `{"attempts_left": int}` |
| 400 | `otp_expired` | No live code for (phone, purpose) | null |
| 400 | `verification_invalid` | `verification_token` unknown, expired, used, or bound to another phone/purpose | null |
| 400 | `social_token_invalid` | Provider token rejected (§2.1.8) | null |
| 400 | `social_ticket_invalid` | `social_ticket` unknown, expired or used | null |
| 409 | `phone_registered` · `phone_not_registered` | Purpose does not fit the phone (§2.1.1) | null |
| 409 | `social_provider_linked` · `social_identity_linked` | Link refused (§2.1.9) | `{"provider": str}` |
| 409 | `OUT_OF_STOCK` | Cart add / checkout stock refusal | cart add: `{"product_id", "available": 0}`; checkout: `{"lines": [...]}` |
| 409 | `product_unavailable` · `cart_full` · `cart_empty` | Cart and checkout (§2.5, §2.7) | `cart_full`: `{"max_lines": int}` |
| 409 | `price_changed` | `expected_total` differs (§2.7.1) | `{"total", "items_subtotal", "delivery_fee", "cashback_used", "cart": Cart}` |
| 409 | `open_order_exists` | An unpaid order already exists (§2.7.1) | `{"order_id": int, "pay_by": datetime}` |
| 409 | `delivery_unavailable` | BTS does not deliver to the address (§2.6) | null |
| 409 | `not_payable` · `order_expired` · `already_paid` · `payment_in_progress` | `orders/{id}/pay/` refusals (§2.7.2) | `payment_in_progress`: `{"provider": str}` |
| 409 | `not_cancellable` | Order or appointment cancel refused | null |
| 409 | `conflict` | Booking race lost, other state conflicts | null |
| 409 | `booking_limit` · `address_limit` | Count limits | `{"limit": int}` |
| 409 | `qr_invalid` | POS: token unknown, expired, used or of a non-active customer (one code for all, no oracle) | null |
| 426 | `app_update_required` | App below the minimum version | `{"min_version": str, "store_url": str}` |
| 429 | `otp_resend_too_soon` · `otp_daily_limit` · `otp_locked` | OTP limits (§2.1.1) | `{"retry_after": int}` (seconds; also `Retry-After`) |

Generic field codes (admin §1.6 table) render their messages in the resolved language (catalog keys `field.<code>`).
Endpoint-specific field codes are listed in each endpoint table and registered in
`core.api.exceptions.ENDPOINT_FIELD_CODES` by P0.

### 1.7 Idempotency
Mechanism, fingerprint, storage and responses as admin §1.7 (`core.idempotency.idempotent`). Differences:
- **Scope:** `(audience "customer", actor = customer id, method, resolved path, key)`.
- **Required** (400 `idempotency_key_required` without it): `POST orders/checkout/`, `POST orders/{id}/pay/`,
  `POST orders/{id}/cancel/`, `POST appointments/`, `POST appointments/{id}/cancel/`.
- **Optional** on every other **C** POST/PUT/PATCH/DELETE. `auth/*` ignore the header (responses carry credentials).
- Webhooks do not use it: provider deliveries are de-duplicated by `integrations.WebhookInbox` (§2.13.0). The POS
  resolve is not idempotent by design (the QR token is single-use).
- App rule: one key per user intent (one tap of "Buyurtma berish", "To‘lash", "Yozilish", "Bekor qilish"), reused on
  network retries, renewed after any 2xx or after 422 `idempotency_key_reused`. Errors are never stored, so a retry
  after 409/503 runs again.

### 1.8 Throttles (Redis; defaults from `core/throttling.py`, overridable by `THROTTLE_RATES`)

| Scope (existing unless marked new) | Key | Default | Applies to |
|---|---|---|---|
| `sms_phone` | normalized phone | 5/min | `auth/send-code/` |
| `sms_ip` | client IP | 20/min | `auth/send-code/` |
| OTP rules (not a DRF throttle; derived from `SmsCode` rows so they survive a cache flush) | phone + purpose | resend ≥ 60 s; ≤ 10 codes per phone per Tashkent day; 15 min lockout after 5 wrong codes | `auth/send-code/`, `auth/verify-code/` |
| `login` | client IP | 30/min | `auth/verify-code/`, `auth/set-password/`, `auth/login/`, `auth/reset-password/`, `auth/social/complete/` |
| customer login guard (`LoginGuard`, scope `customer_login`) | SHA-256 of the phone | 5 failures/min, then lockout 1 → 15 min exponential | `auth/login/` |
| `oauth` | client IP | 30/min | `auth/social/` |
| `refresh` | SHA-256 of the presented refresh token | 120/min | `auth/refresh/` |
| `auth_client_ip` | client IP | 600/min | `auth/refresh/`, `auth/logout/` |
| `customer` | customer id | 600/min | every **C** route |
| `checkout` | customer id | 30/min | `orders/checkout/` (in addition to `customer`) |
| `pay` | customer id | 30/min | `orders/{id}/pay/` (in addition to `customer`) |
| `pos` | POS device id; client IP before the key is known | 120/min | `pos/*` |
| **`public`** (new, P0) | client IP | 300/min | every **O** route when anonymous (a signed-in caller counts against `customer`) |

Webhooks are not throttled (providers retry on 429 and would amplify); they are protected by signature checks and
the optional IP allow-lists `CLICK_ALLOWED_IPS` / `PAYME_ALLOWED_IPS` (defence in depth only).

### 1.9 Sessions, devices and the single-device rule
- A customer has at most **one active `AuthSession` and one active `Device`**. Every successful sign-in
  (`auth/set-password/`, `auth/login/`, `auth/reset-password/`, `auth/social/`, `auth/social/complete/`) runs
  `accounts.services.start_customer_session(customer, fid, platform, app_version)` under `select_for_update` of the
  customer row:
  1. upsert `Device(customer, fid)` → `is_active=True`, `platform`, `app_version`, `last_active_at=now`;
  2. every other device of the customer → `is_active=False` (its `fcm_token` is kept so the farewell push can reach
     it; it is no longer pushable because `is_active` is false);
  3. every other open session → `revoked_at=now`, `revoke_reason="replaced"`, `auth:sid:{sid}` tombstone
     `revoked:replaced` written on commit (admin spec: never a plain delete);
  4. outbox `push.session_replaced {device_id}` per replaced device that has a token (data-only FCM message
     `{"type": "session_replaced"}`, no notification text, no PII), so the old app signs out immediately instead of
     at its next request;
  5. a new session: `refresh_hash = SHA-256(refresh)`, `absolute_expires_at = now + CUSTOMER_REFRESH_TTL_DAYS`,
     `device = the device`, `device_label = "<platform> <model or fid[:8]>"`; `Customer.last_login_at = now`.
- What the old device sees: the data push (best effort), then **401 `SESSION_REPLACED`** on its next request
  (the tombstone is read before the database, across all gunicorn workers) and on its next `auth/refresh/`.
- A **known device** is a `(customer, fid)` row that already exists (it can only have been created by a verified
  sign-in). Password sign-in on an unknown device needs the SMS code (A2, §2.1.4).
- `auth/logout/` revokes the session (`logout`) and sets the device `is_active=False` (it stays known).
- `DELETE me/` revokes with `deactivated` and clears every device token (A1, §2.2.3).

---
## 2. Endpoints

### 2.0 Index
**Auth:** `–` none · **C** customer JWT · **O** optional JWT · **K** POS key · **S** provider signature.
**I** = `Idempotency-Key` required · **P** = paginated. Package = owner in the M2 plan.

| # | Method | Path | Auth | Flags | Success | Package |
|---|---|---|---|---|---|---|
| 1 | POST | `auth/send-code/` | – | | 200 | P1 |
| 2 | POST | `auth/verify-code/` | – | | 200 | P1 |
| 3 | POST | `auth/set-password/` | – | | 201 | P1 |
| 4 | POST | `auth/login/` | – | | 200 | P1 |
| 5 | POST | `auth/reset-password/` | – | | 200 | P1 |
| 6 | POST | `auth/refresh/` | – | | 200 | P1 |
| 7 | POST | `auth/logout/` | – (bearer optional) | | 204 | P1 |
| 8 | POST | `auth/social/` | – | | 200 | P1 |
| 9 | POST | `auth/social/complete/` | – | | 200 / 201 | P1 |
| 10 | GET / PATCH / DELETE | `me/` | C | | 200 / 200 / 204 | P1 |
| 11 | POST | `me/consent/` | C | | 200 | P1 |
| 12 | PUT / DELETE | `me/image/` | C | multipart / – | 200 / 204 | P1 |
| 13 | PUT | `me/device/` | C | | 200 | P1 |
| 14 | GET / POST | `me/addresses/` | C | | 200 / 201 | P1 |
| 15 | PATCH / DELETE | `me/addresses/{id}/` | C | | 200 / 204 | P1 |
| 16 | POST | `me/addresses/{id}/default/` | C | | 200 | P1 |
| 17 | GET | `catalog/banners/`, `catalog/categories/` | O | | 200 | P1 |
| 18 | GET | `catalog/products/` | O | P | 200 | P1 |
| 19 | GET | `catalog/products/{id}/` | O | | 200 | P1 |
| 20 | GET | `settings/public/` | O | | 200 | P1 |
| 21 | GET / DELETE | `cart/` | C | | 200 | P2 |
| 22 | POST | `cart/items/` | C | | 200 | P2 |
| 23 | PATCH / DELETE | `cart/items/{product_id}/` | C | | 200 | P2 |
| 24 | DELETE | `cart/items/?product_id=…` (bulk) | C | | 200 | P2 |
| 25 | GET | `delivery/regions/` | O | | 200 | P2 |
| 26 | POST | `delivery/calculate/` | C | | 200 | P2 |
| 27 | POST | `orders/checkout/` | C | I | 201 | P2 |
| 28 | POST | `orders/{id}/pay/` | C | I | 200 | P2 |
| 29 | POST | `orders/{id}/cancel/` | C | I | 200 | P2 |
| 30 | GET | `me/orders/` | C | P | 200 | P2 |
| 31 | GET | `me/orders/{id}/` | C | | 200 | P2 |
| 32 | GET | `me/cashback/` | C | | 200 | P3 |
| 33 | GET | `me/cashback/ledger/` | C | P | 200 | P3 |
| 34 | GET | `me/qr/` | C | | 200 | P3 |
| 35 | POST | `pos/cashback/resolve-qr/` | K | | 200 | P3 |
| 36 | GET | `specialists/`, `specialists/{id}/`, `services/` | O | | 200 | P3 |
| 37 | GET | `specialists/{id}/slots/?date=&service_id=` | O | | 200 | P3 |
| 38 | POST | `appointments/` | C | I | 201 | P3 |
| 39 | GET | `me/appointments/` | C | P | 200 | P3 |
| 40 | GET | `me/appointments/{id}/` | C | | 200 | P3 |
| 41 | POST | `appointments/{id}/cancel/` | C | I | 200 | P3 |
| 42 | GET | `me/notifications/` | C | P | 200 | P3 |
| 43 | GET | `me/notifications/unread-count/` | C | | 200 | P3 |
| 44 | POST | `me/notifications/{id}/read/`, `me/notifications/read-all/` | C | | 200 | P3 |
| 45 | POST | `me/notifications/ack/` | C | | 200 | P3 |
| 46 | POST | `webhooks/click/prepare/`, `webhooks/click/complete/` | S | | 200 | P2 |
| 47 | POST | `webhooks/payme/` | S | | 200 | P2 |
| 48 | POST | `webhooks/bts/` | S | | 200 | P2 |
| 49 | POST | `dev/payments/{order_id}/simulate/` | C | fake only | 200 | P2 |

**Shared shapes.**
```jsonc
// AuthTokens — every successful sign-in
{"access": "eyJhbGciOiJIUzI1NiIsImtpZCI6...",   // JWT aud=customer, 15 min
 "access_expires_at": "2026-10-26T12:15:00+05:00",
 "refresh": "Qm9n…43 chars",                     // opaque 256-bit base64url; stored as SHA-256; ROTATES on every refresh
 "refresh_expires_at": "2026-12-25T12:00:00+05:00",   // now + 60 days, sliding
 "customer": { /* Me, §2.2.1 */ },
 "is_new_customer": false,                       // true when this call created the Customer row
 "reactivated": false}                           // true when this call reactivated a deleted Customer (A1)

// NextStep — a sign-in that needs one more factor (200, no tokens)
{"next_step": "verify_device",                  // NextStep: verify_device | verify_phone
 "phone_mask": "+998 90 *** ** 67",              // verify_device only, else null
 "otp": {"expires_in": 120, "resend_in": 60, "length": 6},   // verify_device: the code just sent (or the live one); null for verify_phone
 "social_ticket": null,                          // verify_phone only: opaque signed ticket, 10 min, single use
 "provider": null,                               // verify_phone only: "google" | "facebook"
 "email_mask": null,                             // verify_phone only: masked provider email | null
 "expires_in": 600}                              // validity of the ticket / of this step in seconds
```

### 2.1 Auth

Common to every `auth/*` endpoint: device headers required (§1.2); phone fields are E.164 (`invalid` "Telefon raqamini
to‘g‘ri kiriting."; catalog `field.phone_invalid`); bodies are JSON; audit rows use `actor_type="customer"`
(`actor_id` = customer id when known, `entity_type="customer"`), never the raw code, password or token.

OTP purposes (`OtpPurpose`) map to `SmsCode.Purpose`: `register` → `signup`, `login_new_device` → `new_device`,
`reset_password` → `reset`, `link_social` → `social_link`.

#### 2.1.1 `POST auth/send-code/`
Body `{"phone": str, "purpose": OtpPurpose, "social_ticket": str | null}`.

| Field / check (in order) | Code | Message (uz) · catalog key |
|---|---|---|
| `phone` | `required`, `invalid` | "Telefon raqamini to‘g‘ri kiriting." · `field.phone_invalid` |
| `purpose` | `required`, `invalid_choice` | generic |
| `social_ticket` required iff `purpose = link_social` | `required` | "Ijtimoiy tarmoq orqali kirishni qaytadan boshlang." · `field.social_ticket_required` |
| `social_ticket` valid (signature, ≤10 min, unused) | 400 `social_ticket_invalid` | "Ijtimoiy tarmoq orqali kirish muddati tugadi. Qaytadan boshlang." · `error.social_ticket_invalid` |
| `register` and an **active** customer with this phone exists | 409 `phone_registered` | "Bu raqam ro‘yxatdan o‘tgan. Hisobingizga kiring." · `error.phone_registered` |
| `login_new_device` / `reset_password` and no active customer (deleted counts as none) | 409 `phone_not_registered` | "Bu raqam bilan hisob topilmadi. Ro‘yxatdan o‘ting." · `error.phone_not_registered` |
| `sms_phone` / `sms_ip` throttle | 429 `throttled` | admin message, localized · `error.throttled` |
| A row for (phone, `exhausted_at` > now − 15 min) | 429 `otp_locked` | "Urinishlar soni tugadi. {m} daqiqadan so‘ng qayta urinib ko‘ring." · `error.otp_locked` |
| The newest row for (phone, purpose) is younger than 60 s | 429 `otp_resend_too_soon` | "Yangi kodni {n} soniyadan so‘ng so‘rashingiz mumkin." · `error.otp_resend_too_soon` |
| ≥ `CUSTOMER_OTP_DAILY_LIMIT` (10) rows for the phone in the current Tashkent day | 429 `otp_daily_limit` | "Bugungi SMS kodlar limiti tugadi. Ertaga qayta urinib ko‘ring." · `error.otp_daily_limit` |

```jsonc
// 200
{"phone_mask": "+998 90 *** ** 67", "purpose": "register",
 "expires_in": 120, "resend_in": 60, "attempts": 5, "length": 6}
```
**Side effects** (`accounts.services.send_code`, one transaction): previous unconsumed code of (phone, purpose) gets
`expires_at=now`; new `SmsCode` row (`code_hmac = HMAC-SHA256(code, SMS_CODE_HMAC_KEY)` hex, 6 random digits from
`secrets`, `expires_at = now + 120 s`, `max_attempts = 5`, `request_ip`); outbox `sms.send {sms_code_id, phone, text,
purpose}` (dedupe `sms.send:{id}`; text = catalog `sms.otp` in the resolved language, §2.14.1). No audit (volume);
structured log `otp.issued {purpose, phone_hash}`.
Phone enumeration through `register`/`reset_password` answers is an accepted UX trade-off, bounded by the throttles
(§7 `M2-A9`).

#### 2.1.2 `POST auth/verify-code/`
Body `{"phone": str, "purpose": OtpPurpose, "code": str}`.

| Field / check | Code | Message (uz) · key |
|---|---|---|
| `code` `^\d{6}$` | `invalid` | "6 xonali kodni kiriting." · `field.otp_format` |
| No live code (none, expired, consumed, superseded) | 400 `otp_expired` | "Kod muddati tugagan. Yangi kod oling." · `error.otp_expired` |
| Row exhausted, or lockout active | 429 `otp_locked` | `error.otp_locked` |
| HMAC mismatch (`hmac.compare_digest`) | 400 `otp_invalid`, `details.attempts_left`; field `code` `otp_invalid` | "Kod noto‘g‘ri. Yana {n} marta urinishingiz mumkin." · `error.otp_invalid`; at 0 left → 429 `otp_locked` |

```jsonc
// 200
{"verification_token": "vt_Zx…43 chars",   // 256-bit, returned once; SHA-256 stored on the SmsCode row
 "expires_in": 600, "purpose": "register",
 "customer_exists": false}                 // an active customer has this phone (lets register/link screens branch)
```
Side effects (`select_for_update` on the row): on mismatch `attempts += 1` (and `exhausted_at` at the max); on match
`consumed_at`, `verification_token_hash`, `verification_expires_at = now + 600 s`. The token is single-use and bound
to (phone, purpose): every consumer sets `verification_used_at` under lock and refuses a used, expired or
mismatching token with 400 `verification_invalid` "Tasdiqlash muddati tugagan. Kodni qaytadan oling."
(`error.verification_invalid`).

#### 2.1.3 `POST auth/set-password/` (registration)
Body `{"verification_token": str (purpose register), "password": str, "name": str, "terms_version": str, "language": ContentLanguage | null}`.

| Field / check | Code | Message (uz) · key |
|---|---|---|
| `verification_token` | 400 `verification_invalid` | `error.verification_invalid` |
| `name` trimmed 2..150 | `min_length` / `max_length` | "Ismingizni kiriting (kamida 2 belgi)." · `field.name_min` / generic |
| `password` 8..128; Django validators with `CUSTOMER_PASSWORD_MIN_LENGTH` (8), common, numeric, similarity to the phone | `password_too_short` · `password_too_common` · `password_entirely_numeric` · `password_too_similar` · `max_length` | "Parol kamida 8 ta belgidan iborat bo‘lsin." · "Bu parol juda oddiy." · "Parol faqat raqamlardan iborat bo‘lmasin." · "Parol telefon raqamingizga juda o‘xshash." (`field.password_*`) |
| `terms_version` = current `SiteSettings.terms_version` (`"0"` when the setting is empty) | `terms_outdated` | "Foydalanish shartlarining yangi tahririni qabul qiling." · `field.terms_outdated` |
| `language` | `invalid_choice` | generic; default = resolved request language |
| An **active** customer with the phone exists (race with another registration) | 409 `phone_registered` | `error.phone_registered` |

→ **201 `AuthTokens`**. Side effects (one transaction, `customers.services.register`):
- new `Customer(phone, name, language)` or, for a deleted row, `accounts.services.reactivate_customer(name=…)`
  (same id; `registered_at` kept, `reactivated_at=now`; `LoyaltyAccount`, `Purchase`, `Order` history intact, A1);
- `set_customer_password`; consent `terms_version`, `consent_at=now`, `consent_ip=client IP` (C3);
- `start_customer_session` (§1.9); verification token marked used;
- audit `customer.registered {reactivated: bool, terms_version}`.

#### 2.1.4 `POST auth/login/`
Body `{"phone": str, "password": str 1..128, "verification_token": str | null}`.
1. Customer login guard (per phone) → 429 `throttled`.
2. Unknown phone, deleted customer, no password set (social-only account) or wrong password → **401
   `invalid_credentials`** "Telefon raqami yoki parol noto‘g‘ri." (same body and timing: the hasher runs for unknown
   phones too). Audit `customer.login_failed {method: "password"}` with `actor_id` when the customer exists.
3. **Known device** (§1.9) → **200 `AuthTokens`** (single-device rule applies). Audit `customer.login_succeeded
   {method: "password", new_device: false}`.
4. **Unknown device, no `verification_token`** → **200 `NextStep`** `verify_device`. The server issues a
   `login_new_device` code exactly as `send-code` would; when a live code younger than 60 s exists it is reused and
   `otp.resend_in` is the remaining wait; when the daily limit or lockout applies → 429 `otp_daily_limit` /
   `otp_locked`. No session is created.
5. **Unknown device with `verification_token`** (purpose `login_new_device`, same phone) → password re-checked, token
   consumed → **200 `AuthTokens`**; audit `customer.login_succeeded {method: "password", new_device: true}`.
   A bad token → 400 `verification_invalid`.

The app keeps the typed password in memory only until step 5 (≤10 minutes) and never persists it.

#### 2.1.5 `POST auth/reset-password/`
Body `{"verification_token": str (purpose reset_password), "password": str}`. Password rules as §2.1.3, plus
`password_unchanged` "Yangi parol eskisidan farq qilsin." (`field.password_unchanged`).
→ **200 `AuthTokens`**. Side effects: password set (`password_changed_at`); **every** session of the customer revoked
with `password_changed` (tombstones on commit); then `start_customer_session` on this device (it is now known);
audit `customer.password_reset`. 400 `verification_invalid`; 409 `phone_not_registered` if the customer was deleted in
between.

#### 2.1.6 `POST auth/refresh/`
Body `{"refresh": str}`. Device headers must match the session's device (else 401 `not_authenticated`).

| Case | Result |
|---|---|
| Hash matches `refresh_hash` of an open session within `absolute_expires_at`, active customer | **Rotate:** new 256-bit refresh; `previous_refresh_hash = old`, `refresh_hash = new`, `rotated_at = now`, `last_used_at = now`, `absolute_expires_at = now + 60 days` (sliding). Cache `auth:refresh_grace:{old_hash}` = the full response body, TTL `CUSTOMER_REFRESH_GRACE_SECONDS` (30). → 200 |
| Hash matches `previous_refresh_hash` and `rotated_at` is within the grace window | The cached response of that rotation (identical tokens), so two concurrent refreshes from one app both succeed → 200 |
| Hash matches `previous_refresh_hash` after the grace window (**reuse**) | Session revoked `replaced`; audit `customer.refresh_reused {sid}` → 401 `SESSION_REPLACED` |
| Session revoked / expired / customer deleted | 401 `SESSION_EXPIRED` (or `SESSION_REPLACED` for reason `replaced`) |
| Unknown hash | 401 `not_authenticated` |

```jsonc
// 200
{"access": "eyJ…", "access_expires_at": "2026-10-26T12:15:00+05:00",
 "refresh": "new…43 chars", "refresh_expires_at": "2026-12-25T12:00:00+05:00"}
```
The raw rotated token lives in Redis only for the grace window (internal network, TTL 30 s); this is the only place a
raw refresh token is stored (§6).

#### 2.1.7 `POST auth/logout/`
Body `{"refresh": str}` → **204**. Revokes the session owning the token (`logout`), tombstone on commit, device
`is_active=False`. Unknown, revoked or expired token → 401 `SESSION_EXPIRED` (the app treats it as logged out).
Audit `customer.logout`.

#### 2.1.8 `POST auth/social/`
Body `{"provider": "google" | "facebook", "token": str 1..4096, "token_type": "id_token" | "access_token" | null, "nonce": str | null}`.
`token_type` defaults to `id_token` for Google and `access_token` for Facebook.

| Provider | Server-side verification (P1 extends the OAuth port with `verify_app_token(token, token_type, nonce)`) |
|---|---|
| `google` (`id_token`) | RS256 against Google's JWKS (cached by `kid`); `iss` ∈ {`https://accounts.google.com`, `accounts.google.com`}; `aud` ∈ `GOOGLE_APP_CLIENT_IDS` (the iOS client id and the web/server client id the Android SDK requests tokens for); `exp`; `nonce` equal when sent. `email_verified` is **not** required (the phone is the identity). |
| `facebook` (`access_token`) | `GET https://graph.facebook.com/{FACEBOOK_GRAPH_VERSION}/debug_token?input_token=…` with the app token `app_id|app_secret` (POST body / header, never in a logged URL): `is_valid`, `app_id == FACEBOOK_APP_ID`; then `GET /me?fields=id,email` with `appsecret_proof`. |
| `facebook` (`id_token`, iOS Limited Login) | RS256 against Facebook's OIDC JWKS; `iss = https://www.facebook.com`, `aud = FACEBOOK_APP_ID`, `exp`, `nonce` **(verify M3)** |

- Linked identity `(provider, subject)` whose customer is active → **200 `AuthTokens`**; `last_login_at` on the
  identity; audit `customer.login_succeeded {method: provider}`. Social sign-in does **not** require the new-device
  SMS code (the provider authentication is the second factor; §7 `M2-A10`).
- Unknown identity → **200 `NextStep`** `verify_phone` with a `social_ticket` = `TimestampSigner(salt="gd.social")`
  over `{provider, subject, email, jti}`, 10 min, single use (`jti` claimed with `cache.add` when consumed).
- 400 `social_token_invalid` "Kirish amalga oshmadi. Qayta urinib ko‘ring." (`exchange_failed`, `invalid_token`);
  503 `provider_unavailable` (`unavailable`); 404 for an unknown provider; 429 `throttled` (`oauth`).

#### 2.1.9 `POST auth/social/complete/`
Body `{"social_ticket": str, "verification_token": str (purpose link_social), "name": str | null, "terms_version": str | null, "language": ContentLanguage | null}`.
The phone is the one the verification token is bound to (the app sent it to `send-code` with the same ticket).

| Phone state | Requirements | Result |
|---|---|---|
| Active customer | — | Identity linked → **200 `AuthTokens`** (`is_new_customer: false`) |
| No customer | `name` (2..150) and `terms_version` required (`required`, `terms_outdated`) | Customer created without a password (`has_password: false`; a password can be added with `reset_password`) → **201** |
| Deleted customer | as above | Reactivated (A1) → **201** with `reactivated: true` |

Errors: 400 `social_ticket_invalid`, 400 `verification_invalid`; 409 `social_provider_linked` "Bu hisobga boshqa
{provider} profili bog‘langan." when the customer already has an identity for that provider; 409
`social_identity_linked` "Bu {provider} profili boshqa hisobga bog‘langan." when the subject was linked meanwhile
(unique-violation race too). Side effects: `CustomerSocialIdentity(provider, subject, email_at_link)`; consent when
creating/reactivating; `start_customer_session`; audit `customer.social_linked {provider}` (+ `customer.registered`).

### 2.2 Me (profile, consent, image, device, addresses)

#### 2.2.1 `GET me/` → `Me`
```jsonc
{"id": 5, "phone": "+998901234567", "name": "Malika Karimova",
 "language": "uz",                                  // ContentLanguage: push texts and the default Accept-Language
 "image_url": null,                                  // absolute URL | null
 "registered_at": "2025-03-12T10:20:00+05:00",
 "has_password": true, "linked_providers": ["google"],
 "push_enabled": true,                               // of the CURRENT device
 "consent": {"terms_version": "2026-10-01", "accepted_at": "2025-03-12T10:20:00+05:00",
             "current_version": "2026-10-01", "update_required": false},
 "default_address": { /* Address | null */ }}
```

#### 2.2.2 `PATCH me/`
Body (every key optional) `{"name": str, "language": ContentLanguage, "push_enabled": bool}` → 200 `Me`.

| Field | Rule | Code · message (uz) · key |
|---|---|---|
| `name` | trimmed 2..150 | `min_length` "Ismingizni kiriting (kamida 2 belgi)." · `field.name_min` |
| `language` | `uz` \| `ru` \| `en` | `invalid_choice` |
| `push_enabled` | applies to the current device; `true` requires a stored FCM token | `push_token_missing` "Bildirishnomalarni yoqish uchun ilovaga ruxsat bering." · `field.push_token_missing` |

Side effects: `Customer.name/language`; `Device.push_enabled`. No audit (own profile), log only.

#### 2.2.3 `DELETE me/`
Body `{"password": str | null}`: required when `has_password` (`required`; wrong → `password_incorrect` "Parol
noto‘g‘ri." · `field.password_incorrect`). → **204**. Orchestrated by `customers.services.delete_account(customer)`
(top layer, one transaction, lock order Customer → Order → Payment → LoyaltyAccount):
1. every `awaiting_payment` order → `commerce.services.cancel_by_customer` (P2; reason `customer_request`;
   processing/shipped orders continue: BTS uses the order's recipient snapshot);
2. every open future appointment → `appointments.services.cancel_by_customer` (P3; `reason = "account_deleted"`);
3. cart lines deleted;
4. `accounts.services.delete_customer` (A1): `status=deleted`, `deleted_at`, name/image/address/password scrubbed,
   addresses and social identities deleted, devices `is_active=False, push_enabled=False, fcm_token=""`, every session
   revoked `deactivated` with tombstones;
5. audit `customer.deleted {orders_cancelled, appointments_cancelled}`.
**Kept:** the row and phone (unique), `LoyaltyAccount` balance and ledger, `Purchase` rows, orders, appointments,
inbox rows (they are deleted by the M3 retention job, N-9). **Re-registration** with the same phone goes through
`register` and reactivates the same id (§2.1.3); social identities must be linked again.

#### 2.2.4 `POST me/consent/`
Body `{"terms_version": str}` equal to the current version (else `terms_outdated`) → 200 `Me`. Stamps
`terms_version`, `consent_at`, `consent_ip`; audit `customer.consent_accepted {terms_version}`. When
`consent.update_required` is true the app blocks checkout and booking screens until this call succeeds; the server
enforces it on `orders/checkout/` and `appointments/` (409 `conflict` "Foydalanish shartlarining yangi tahririni qabul
qiling." · `error.terms_update_required`).

#### 2.2.5 `PUT me/image/` · `DELETE me/image/`
Multipart part `file` (≤5 MiB, JPEG/PNG/WebP, re-encoded and EXIF-stripped by `core.images.process_image`;
`file_too_large`, `unsupported_image`, `required`) → 200 `{"image_url": str}`; the previous file is deleted.
`DELETE` → 204 (idempotent).

#### 2.2.6 `PUT me/device/`
Registers or refreshes the current device's facts and FCM token. Call it after every sign-in, on FCM token refresh,
on notification-permission changes and on app start when anything changed.
Body `{"fcm_token": str 0..512 | null, "push_enabled": bool, "model": str ≤80, "os": str ≤40, "locale": str ≤8}`.
- `push_enabled: true` with an empty token → field `push_enabled` `push_token_missing`.
- A non-empty token held by another **active** device row (the phone changed accounts) → that row is deactivated
  first (`device_active_fcm_token_unique` backstop).
- `fcm_invalidated_at` is cleared when a new token arrives.
→ 200 `{"fid", "platform", "model", "os", "app_version", "push_enabled", "last_active_at"}`.
Service: `accounts.services.register_device` (P0).

#### 2.2.7 Addresses
```jsonc
// Address
{"id": 12, "label": "Uy", "text": "Toshkent, Chilonzor tumani, 9-kvartal, 12-uy, 45-xonadon",
 "region_code": "10", "city_code": "1001",           // BTS reference codes (§2.6); "" until chosen
 "comment": "3-podyezd, domofon 45", "latitude": 41.285431, "longitude": 69.203512,
 "is_default": true, "created_at": "…", "updated_at": "…"}
```
- `GET me/addresses/` → `{"results": [Address…]}` (default first, then `updated_at` desc; not paginated, ≤10).
- `POST me/addresses/` → 201 `Address` via `accounts.services.add_address` (the first address becomes the default).
- `PATCH me/addresses/{id}/` → 200 via `accounts.services.update_address` (only the given keys change).
- `DELETE me/addresses/{id}/` → 204 via `remove_address`. `POST me/addresses/{id}/default/` → 200 via
  `set_default_address`.
- Every write recomputes `Customer.address_text` (design 01 §1.3). Another customer's id → 404.

| Field | Rule | Code · message (uz) · key |
|---|---|---|
| `label` | ≤60, default `""` | `max_length` |
| `text` | trimmed 5..300, required on create | `min_length` "Manzilni to‘liq kiriting." · `field.address_text_min` |
| `region_code`, `city_code` | `^\d{1,16}$` or `""`; existence is checked by `delivery/calculate/` and checkout (§2.6) | `invalid` |
| `comment` | ≤200 | `max_length` |
| `latitude`, `longitude` | −90..90 / −180..180, both or neither | `latlng_pair` "Joylashuvni qayta tanlang." · `field.latlng_pair` |
| `is_default` | bool | — |
| count | ≤ `ADDRESS_MAX_PER_CUSTOMER` (10) on create | 409 `address_limit` "Ko‘pi bilan {limit} ta manzil saqlash mumkin." · `error.address_limit` |

---
### 2.3 Catalog (public, **O**)

**Visibility** (`catalog.selectors.customer_visible_products()`, P0): `Product.status = active` ∧ `price ≥ 1000` ∧
category set and `Category.is_active` (a hidden category hides its products, design 01 open point 12). Anything else
is 404 here and `unavailable` in the cart. Availability (`available`) comes from `commerce.selectors.available_stock`
— which is why these views live in the top-layer `customers` module (catalog may not import commerce).

```jsonc
// ProductCard (list item)
{"id": 1, "name": "Gialuron kislotali serum",       // resolved language → uz → billz_name
 "volume": "30 ml", "price": 185000, "promo": false,
 "category": {"slug": "serums", "name": "Serumlar"},
 "main_image_url": "https://media.example.uz/products/1/9f2c.webp",   // | null
 "available": 12, "stock_status": "in_stock",       // StockStatus: in_stock (available > 0) | out_of_stock
 "in_cart_quantity": 0}                              // 0 when anonymous
// ProductDetail = ProductCard + 
{"description": "…", "ingredients": "…", "usage": "…",             // resolved language, "" when empty
 "images": [{"id": 31, "url": "https://…/9f2c.webp", "position": 0}],
 "max_quantity": 10}                                 // min(available, CART_MAX_QTY_PER_LINE); 0 = cannot be added
```

**`GET catalog/banners/`** → `{"results": [Banner…]}` (not paginated, ≤20): `is_active` ∧ (`starts_at` null or ≤ now) ∧
(`ends_at` null or > now), ordered by `position`, id.
```jsonc
{"id": 3, "title": "Kuzgi chegirmalar", "image_url": "https://…/77aa.webp", "image_width": 1200, "image_height": 600,
 "link": {"kind": "product", "product_id": 4, "category": null}}   // a link to an invisible product/inactive category renders kind "none"
```
**`GET catalog/categories/`** → `{"results": [{"slug", "name", "image_url" | null, "product_count"}]}`: active categories by
`position`; `product_count` = visible products.

**`GET catalog/products/`** → `Page<ProductCard>`

| Param | Values | Rule |
|---|---|---|
| `search` | str ≤64 (trimmed; longer input cut to 64) | Case-insensitive partial match over the uz/ru/en names, `billz_name`, `billz_id` (`catalog.selectors.filter_products_search`, pg_trgm). Fewer than 2 characters is ignored. |
| `category` | slug | unknown or inactive slug → 400 `invalid_choice` |
| `in_stock` | bool | `available > 0` |
| `promo` | bool | |
| `ordering` | `default` (newest `created_at` first), `price`, `-price`, `name` | ties by `id` desc; `name` uses the resolved-language name under `glowderma_ci` |
| `page`, `page_size` | §1.5 | |

**`GET catalog/products/{id}/`** → `ProductDetail`; 404 when not visible.

### 2.4 Settings
**`GET settings/public/`** (**O**, cache 60 s, `site_config.selectors` + env):
```jsonc
{"support_phone": "+998712000000",        // seller ("Sotuvchi bilan bog‘lanish", max_reached screen) | null
 "store_phone": "+998712000001", "cosmetologist_phone": null,
 "telegram_url": "https://t.me/glowderma", "instagram_url": null, "website_url": "https://glowderma.uz",
 "pickup_address": "Toshkent, Yunusobod, …",          // resolved language
 "privacy_policy": "…", "terms": "…", "booking_policy": "…",   // resolved language; "" when not filled
 "terms_version": "2026-10-01",                       // "0" when SiteSettings.terms_version is empty
 "terms_updated_at": "2026-10-01T00:00:00+05:00",     // | null
 "reservation_minutes": 15,
 "payment_providers": ["click", "payme"],             // enabled providers (configured adapters)
 "min_app_version": {"ios": "1.0.0", "android": "1.0.0"},
 "store_urls": {"ios": "https://apps.apple.com/…", "android": "https://play.google.com/…"}}
```

### 2.5 Cart (server cart, **C**)

```jsonc
// Cart
{"items": [                                          // CartLine[], cart order (added_at, id)
   {"product_id": 4, "name": "Seramidli namlantiruvchi krem", "volume": "50 ml",
    "main_image_url": "https://…/a1b2.webp",          // | null
    "unit_price": 312000, "promo": false,             // current Product.price / promo (no snapshot)
    "quantity": 2, "line_total": 624000,
    "available": 7,                                   // §1.4
    "max_quantity": 7,                                // max(0, min(available, CART_MAX_QTY_PER_LINE))
    "max_reached": false,                             // quantity ≥ max_quantity > 0 → app shows "Ko‘proq kerak bo‘lsa, sotuvchi bilan bog‘laning"
    "status": "ok",                                   // CartLineStatus: ok | insufficient | out_of_stock | unavailable
    "price_changed": false, "previous_price": null}],   // price_at_add ≠ unit_price → true and the old price
 "item_count": 1, "quantity_total": 2,
 "items_subtotal": 624000, "promo_subtotal": 0,     // over lines with status ok only
 "has_problems": false,                              // any line not ok → checkout answers 409 OUT_OF_STOCK
 "cashback": {"available": 48600,                     // loyalty available (may be ≤ 0)
              "max_usable": 48600},                   // max(0, min(available, items_subtotal − CHECKOUT_MIN_PAYABLE)); checkout re-caps with the delivery fee
 "updated_at": "2026-10-26T11:58:00+05:00",
 "last_change": null}                                // set on POST/PATCH responses: {"product_id", "requested_quantity", "quantity", "max_reached"}
```
Line status: `unavailable` = not visible (§2.3); `out_of_stock` = `available ≤ 0`; `insufficient` = `quantity >
max_quantity > 0`; else `ok`. A GET never changes stored quantities. Every write locks the `Cart` row
(`select_for_update`; created lazily with `get_or_create`) and stores `price_at_add = current price` on the touched
line (model change `commerce.0004_cartitem_price_at_add`, §7 `M2-C12`).

| Endpoint | Body / params | Rules and errors |
|---|---|---|
| **`GET cart/`** | – | 200 `Cart` |
| **`POST cart/items/`** | `{"product_id": int, "quantity": int 1..99 = 1}` | Adds to an existing line. New quantity = min(old + quantity, `max_quantity`); clamped → `last_change.max_reached = true` (200, not an error). Not visible → 409 `product_unavailable` "Mahsulot hozir sotuvda yo‘q." (`error.product_unavailable`); `max_quantity = 0` → 409 `OUT_OF_STOCK` "Bu mahsulot qolmagan." (`error.out_of_stock_one`, `details {"product_id", "available": 0}`); a new line when the cart has `CART_MAX_LINES` (30) lines → 409 `cart_full` "Savatga ko‘pi bilan {limit} xil mahsulot qo‘shish mumkin." (`error.cart_full`). Unknown id → `product_id` `does_not_exist`. |
| **`PATCH cart/items/{product_id}/`** | `{"quantity": int 1..99}` | Sets min(quantity, `max_quantity`). Line absent → 404. `max_quantity = 0` → 409 `OUT_OF_STOCK` (line unchanged). Not visible → 409 `product_unavailable`. |
| **`DELETE cart/items/{product_id}/`** | – | Removes the line; idempotent (absent line → 200). |
| **`DELETE cart/items/?product_id=4&product_id=9`** | 1..50 ids | Removes those lines; unknown ids ignored. None or >50 → 400 `product_id` `required` / `max_items`. |
| **`DELETE cart/`** | – | Empties the cart. |

All writes answer 200 `Cart`. No audit, no events (the cart is ephemeral).

### 2.6 Delivery

**`GET delivery/regions/`** (**O**) — the BTS reference list for the address form, from the BTS port
`regions()` cached 24 h under `gd:bts:regions` (fake: 4 regions below).
```jsonc
{"regions": [{"code": "10", "name": "Toshkent shahri",
              "cities": [{"code": "1001", "name": "Chilonzor tumani"}, {"code": "1002", "name": "Yunusobod tumani"}]},
             {"code": "11", "name": "Toshkent viloyati", "cities": [ /* … */ ]}],
 "updated_at": "2026-10-26T03:00:00+05:00"}
```
Names in the resolved language when BTS provides it, else as BTS sends them **(verify M3)**.

**`POST delivery/calculate/`** (**C**) — price shown before checkout.
Body `{"fulfilment": "delivery" | "pickup", "address_id": int | null, "region_code": str | null, "city_code": str | null}`.

| Check | Code · message (uz) · key |
|---|---|
| `fulfilment` | `required`, `invalid_choice` |
| delivery: exactly one of `address_id` or (`region_code` + `city_code`) | `address_id` `required` "Yetkazib berish manzilini tanlang." · `field.address_required` |
| `address_id` of this customer | `does_not_exist` "Manzil topilmadi." · `field.address_not_found` |
| region/city present and in the BTS reference list | `address_incomplete` "Manzilda viloyat va tumanni tanlang." · `field.address_incomplete` |
| BTS refuses the route | 409 `delivery_unavailable` "Bu hududga yetkazib berish mavjud emas." · `error.delivery_unavailable` |
| BTS transport failure | 503 `provider_unavailable` |

```jsonc
// 200 delivery
{"fulfilment": "delivery", "delivery_fee": 25000, "region_code": "10", "city_code": "1001",
 "weight_g": 1000,                                   // BTS_DEFAULT_PARCEL_WEIGHT_G (product weights are not modelled, §7 M2-O9)
 "pickup_address": null, "quoted_at": "2026-10-26T12:00:00+05:00"}
// 200 pickup
{"fulfilment": "pickup", "delivery_fee": 0, "region_code": null, "city_code": null, "weight_g": null,
 "pickup_address": "Toshkent, Yunusobod, …", "quoted_at": "2026-10-26T12:00:00+05:00"}
```
**BTS tariff call (verify M3):** the port's `quote(region_code, city_code)` asks BTS's tariff calculation with the
sender city `BTS_SENDER_CITY_CODE`, the receiver region/city, weight `BTS_DEFAULT_PARCEL_WEIGHT_G` and one parcel; the
answer's price in so'm is the fee. The quote is informative: checkout quotes again (§2.7.1) and the checkout quote is
what the order stores.

### 2.7 Checkout, pay, cancel

#### 2.7.1 `POST orders/checkout/` (**C**, **I**, throttle `checkout`)
Body:
```jsonc
{"fulfilment": "delivery",            // FulfilmentMethod
 "address_id": 12,                    // delivery: required, own address; pickup: must be null
 "payment_provider": "click",         // PaymentProvider (enabled in settings/public)
 "cashback_to_use": 48600,            // int ≥ 0, default 0
 "expected_total": 784400,            // the total the app showed; required
 "customer_note": "Soat 18:00 dan keyin"}   // ≤300, default ""
```
Checks, in this order (the first failure answers):

| # | Check | Result · message (uz) · key |
|---|---|---|
| 1 | Body fields | 400: `fulfilment`/`payment_provider` `invalid_choice`; `address_id` `required` (`field.address_required`) / `invalid` (pickup with an address); `cashback_to_use` `min_value`; `expected_total` `required`/`min_value`; `customer_note` `max_length` |
| 2 | Consent current (§2.2.4) | 409 `conflict` · `error.terms_update_required` |
| 3 | No other `awaiting_payment` order (`CHECKOUT_MAX_OPEN_ORDERS`, default 1) | 409 `open_order_exists` "Sizda to‘lanmagan buyurtma bor (№{order_id}). Avval uni to‘lang yoki bekor qiling." · `error.open_order_exists`, `details {order_id, pay_by}` |
| 4 | Cart has lines | 409 `cart_empty` "Savat bo‘sh." · `error.cart_empty` |
| 5 | Delivery: address own (`does_not_exist` `field.address_not_found`), region/city known (`address_incomplete`) | 400 |
| 6 | Every line visible and `available ≥ quantity` (re-checked **under the `ProductStock` row locks** inside `create_order`) | 409 `OUT_OF_STOCK` "Ba’zi mahsulotlar yetarli emas." · `error.OUT_OF_STOCK`, `details.lines[] {product_id, name, requested, available, status}` (every problem line) |
| 7 | Delivery: BTS quote (taken **before** the transaction, passed in as `delivery_fee`) | 409 `delivery_unavailable` / 503 `provider_unavailable` |
| 8 | `cashback_to_use ≤ available` (checked under the LoyaltyAccount lock by `loyalty.services.create_hold`) | 400 `cashback_to_use` `insufficient_balance` "Cashback balansi yetarli emas (mavjud: {available} so‘m)." · `field.cashback_insufficient` |
| 9 | `cashback_to_use ≤ min(items_subtotal, items_subtotal + delivery_fee − CHECKOUT_MIN_PAYABLE)` (C-4: never pays delivery; T-rule: a payable remainder ≥ 1 000 so'm for the providers) | 400 `cashback_to_use` `max_value` "Ko‘pi bilan {max} so‘m cashback ishlatish mumkin." · `field.cashback_max` |
| 10 | `expected_total == items_subtotal + delivery_fee − cashback_to_use` (computed inside the transaction from live prices) | 409 `price_changed` "Narxlar yangilandi. Buyurtmani qayta tekshiring." · `error.price_changed`, `details {total, items_subtotal, delivery_fee, cashback_used, cart}` |

→ **201 `OrderDetail`** (§2.8, `status: awaiting_payment`, `actions.pay: true`, `pay_by` set).

**Side effects** — one transaction owned by `commerce` (the B5 service, not a re-implementation):
`commerce.services.create_order(customer_id, items=<cart lines>, fulfilment, payment_provider, channel=<session
device platform>, address_id, cashback_used, delivery_fee=<quote>, expected_total, metadata={"checkout":
{"app_version", "device_id"}, "fake": <X-GD-Fake-Scenario>})`, which writes (B5, design 03 §4.1 row 1): `Order`
(`awaiting_payment`, address + recipient snapshot, amounts, `reservation_minutes = SiteSettings.reservation_minutes`,
`reservation_expires_at`), `OrderItem` snapshots (name, billz id, image URL, unit price, promo), one active
`StockReservation` per line, the `CashbackHold` (if `cashback_to_use > 0`, `expires_at = reservation_expires_at`), the
`pending` `Payment` (`provider = payment_provider`, `amount = total`; one active payment per order), the delivery
`Shipment` (`not_created`), events `order_created {channel, item_count, total}` + `reservation_created {item_count,
minutes, expires_at}`. Then, still in the transaction: cart lines deleted, `Cart.last_checkout_order`. No outbox, no
audit (system row; the customer is the actor of the idempotency record). Replay with the same key → the stored 201
even though the cart is now empty.

#### 2.7.2 `POST orders/{id}/pay/` (**C**, **I**, throttle `pay`)
Body `{"provider": PaymentProvider | null}` (default: the order's `payment_provider`). Returns where to pay; it never
moves money and never creates order events.

| Check | Result · message (uz) · key |
|---|---|
| Order of another customer | 404 |
| Status `processing`/`handed_to_courier`/`delivered`, or a `paid` payment exists | 409 `already_paid` "Buyurtma allaqachon to‘langan." · `error.already_paid` |
| Status `cancelled` | 409 `not_payable` "Bu buyurtmani to‘lab bo‘lmaydi." · `error.not_payable` |
| `reservation_expires_at ≤ now` (expiry job not run yet) | the order is expired on the spot (`commerce.services.expire_reservation`) → 409 `order_expired` "To‘lov muddati tugadi. Buyurtmani qaytadan rasmiylashtiring." · `error.order_expired` |
| Provider switch while the active payment is a Payme transaction in state 1 | 409 `payment_in_progress` "To‘lov {provider} ilovasida jarayonda. Uni yakunlang yoki biroz kuting." · `error.payment_in_progress`, `details {provider}` |
| `provider` not enabled | 400 `provider` `invalid_choice` |

**Side effects** (`commerce.services.ensure_payable_payment(order_id, provider)`, P2, under Order → Payment locks):
the active `pending` payment of the same provider is reused; a pending payment of the other provider is failed with
`error_code="abandoned"` (`payments.services.fail_payment(unilateral=True)`; a Payme state-1 transaction refuses, see
above) and a new `pending` payment is created with `amount = order.total`, `Order.payment_provider` updated; a failed
previous attempt (provider callback failure) gets a new `pending` row. One active payment per order is guaranteed by
the partial unique index.
```jsonc
// 200
{"order_id": 10248, "payment_id": 301, "provider": "click", "amount": 784400,
 "pay_by": "2026-10-23T14:17:00+05:00",
 "web_url": "https://my.click.uz/services/pay?service_id=…&merchant_id=…&amount=784400&transaction_param=10248&return_url=glowderma%3A%2F%2Fpayment%2Freturn%3Forder_id%3D10248",
 "app_url": null,                                   // provider app deep link when known (verify M3); open web_url otherwise
 "return_url": "glowderma://payment/return?order_id=10248"}
```

| Provider | `web_url` (verify M3) |
|---|---|
| Click | `{CLICK_PAY_URL}?service_id={CLICK_SERVICE_ID}&merchant_id={CLICK_MERCHANT_ID}&amount={total}&transaction_param={order_id}&return_url={urlencode(return_url)}` — `amount` in so'm without decimals; `transaction_param` comes back as `merchant_trans_id` |
| Payme | `{PAYME_CHECKOUT_URL}/{base64("m={PAYME_MERCHANT_ID};ac.{PAYME_ACCOUNT_FIELD}={order_id};a={total×100};l={lang};c={urlencode(return_url)};ct=15000")}` — `a` in tiyin; `l` = resolved language |

`return_url` is `PAYMENT_RETURN_URL` with `order_id` appended; it is server-configured (never taken from the client,
no open redirect). After the provider returns, the app polls `GET me/orders/{id}/` every 2 s for up to 60 s until
`payment.status` is `paid` (or shows "To‘lov tekshirilmoqda" and lets the push finish the story).

#### 2.7.3 `POST orders/{id}/cancel/` (**C**, **I**)
Empty body. Allowed only from `awaiting_payment` (C-6; design 03 §4.1 row 5) → 200 `OrderDetail`.
Else 409 `not_cancellable` "Bu buyurtmani ilovadan bekor qilib bo‘lmaydi. Sotuvchi bilan bog‘laning."
(`error.order_not_cancellable`). 404 for another customer's order.
Side effects (`commerce.services.cancel_by_customer(order_id, customer_id)`, P2, same transaction rules as the
admin cancel row 4): the pending payment is failed unilaterally (`cancelled`), except a Payme state-1 transaction,
which stays `pending` until Payme's `CancelTransaction` (P-2); reservations released (`cancelled`); hold released
(`order_cancelled`); `status = cancelled`, `cancellation_reason = customer_request`, `cancelled_by = null`; event
`cancelled {reason: "customer_request", note: null, refund_id: null, cashback_reversed: false, stock_released,
bts_cancelled: false, push_sent}` with `source = system`, `actor = null`; outbox `push.order_status cancelled`
(state.py row 5). No audit (the event and idempotency record name the actor).

### 2.8 Customer orders

**`GET me/orders/`** → `Page<OrderSummary>`; filter `scope` = `active` (`awaiting_payment`, `processing`,
`handed_to_courier`) | `completed` (`delivered`) | `cancelled` | `all` (default); newest first (`created_at`, id).
```jsonc
// OrderSummary
{"id": 10248, "created_at": "2026-10-23T14:02:00+05:00",
 "status": "handed_to_courier", "status_label": "Kuryerga topshirildi",
 "fulfilment": "delivery", "total": 784400, "item_count": 3,
 "preview_images": ["https://…/a1b2.webp"],          // ≤3 item image URLs (snapshots), nulls skipped
 "payment": {"status": "paid", "status_label": "To‘langan", "provider": "click"},
 "pay_by": null,                                    // reservation_expires_at while awaiting_payment, else null
 "tracking_number": "48213907"}                     // | null
```
**`GET me/orders/{id}/`** → `OrderDetail` (404 for another customer's order):
```jsonc
{ /* …OrderSummary… */
 "fulfilment_label": "Yetkazib berish",
 "items": [{"product_id": 4, "name": "Seramidli namlantiruvchi krem", "image_url": "…" /* | null */,
            "quantity": 1, "unit_price": 312000, "promo": false, "line_total": 312000}],
 "items_subtotal": 808000, "promo_subtotal": 240000, "delivery_fee": 25000, "cashback_used": 48600,
 "address": "Toshkent, Chilonzor tumani, 9-kvartal, 12-uy, 45-xonadon",   // delivery snapshot | null
 "address_comment": "3-podyezd",                                          // | null
 "pickup_address": null,                                                  // pickup: SiteSettings pickup address (resolved language)
 "customer_note": null,
 "payment": {"status": "paid", "status_label": "To‘langan", "provider": "click", "amount": 784400,
             "confirmed_at": "2026-10-23T14:05:00+05:00"},   // display payment (B5 payments.selectors.display_payment)
 "refund": null,                    // | {"state": "in_progress" | "completed", "state_label", "amount", "requested_at", "completed_at"}
 "shipment": {"carrier": "BTS", "tracking_number": "48213907", "tracking_url": null},   // null for pickup or before creation
 "cashback": {"used": 48600, "earned": 5680,
              "earned_state": "credited"},  // none | credited | reversed (purchase reversed)
 "cancellation": null,              // | {"reason": CancellationReason, "label": "…"}  (staff notes are never shown)
 "progress": [ /* ProgressStep[], §4.2 */ ],
 "timeline": [ /* TimelineEntry[], newest first, §4.1 */ ],
 "actions": {"pay": false, "cancel": false}}   // pay = awaiting_payment ∧ pay_by > now; cancel = awaiting_payment
```
Refund mapping: `requested`, `submitting`, `manual_required`, `unknown`, `failed` → `in_progress` "To‘lov
qaytarilmoqda" (staff resolve the last three); `succeeded` → `completed` "To‘lov qaytarildi".
`tracking_url` stays null until BTS confirms a public tracking URL format (verify M3).

---
### 2.9 Loyalty (**C**)

#### 2.9.1 `GET me/cashback/`
Reads `loyalty.selectors.account_summary`, `customer_totals`, `tier_for`, the active policy and the active holds.
```jsonc
{"balance": 48600, "held": 20000, "available": 28600,   // available = balance − active holds; may be negative (L5)
 "negative": false,                                      // balance < 0 (a reversal larger than the balance)
 "lifetime_total": 13580000,                             // Σ non-reversed purchases, store + online
 "tier": {"percent": "1.00", "from": 0, "to": 20000000}, // band covering lifetime_total | null
 "next_tier": {"percent": "2.00", "from": 20000000, "remaining": 6420000, "progress": 0.679},   // | null
 "tiers": [{"from": 0, "to": 20000000, "percent": "1.00"},          // active policy, for the "how it works" screen
           {"from": 20000000, "to": 30000000, "percent": "2.00"},
           {"from": 30000000, "to": 50000000, "percent": "3.00"}],
 "earn_on_cashback_paid": false,                         // active policy flag (L2)
 "holds": [{"order_id": 10250, "amount": 20000, "expires_at": "2026-10-26T12:15:00+05:00"}],
 "qr_ttl_seconds": 60}
```
Promotional products never earn cashback (shown as a fixed sentence in the app, catalog key `label.cashback.promo_rule`).

#### 2.9.2 `GET me/cashback/ledger/` → `Page<LedgerItem>`
Newest first (`at` desc, `id` desc), the customer's own entries only.
```jsonc
// LedgerItem
{"id": 121, "at": "2026-10-23T14:05:00+05:00", "kind": "credit", "amount": 5680, "balance_after": 48600,
 "title": "Cashback hisoblandi", "subtitle": "Buyurtma №10248 · 1%",
 "order_id": 10248,                     // int | null
 "source": "online"}                    // LedgerSource: online | store | admin
```

| `kind` / source | `title` (uz) · key | `subtitle` (uz) · key |
|---|---|---|
| credit, order | "Cashback hisoblandi" · `label.ledger.credit` | "Buyurtma №{order_id} · {percent}%" · `label.ledger.sub_order_percent` |
| credit, billz_sale | "Cashback hisoblandi" · `label.ledger.credit` | "Do‘kondagi xarid · {percent}%" · `label.ledger.sub_store_percent` |
| debit | "Cashback ishlatildi" · `label.ledger.debit` | "Buyurtma №{order_id}" · `label.ledger.sub_order` |
| reversal, amount > 0 (a debit given back) | "Cashback qaytarildi" · `label.ledger.reversal_back` | "Buyurtma №{order_id} bekor qilindi" · `label.ledger.sub_order_cancelled` |
| reversal, amount < 0 (a credit taken back) | "Cashback bekor qilindi" · `label.ledger.reversal_taken` | as above (store: "Do‘kondagi xarid qaytarildi" · `label.ledger.sub_store_returned`) |
| adjustment, amount > 0 / < 0 | "Bonus qo‘shildi" · `label.ledger.adjust_add` / "Balans tuzatildi" · `label.ledger.adjust_subtract` | null — the staff `reason` is internal and never shown |

`{percent}` is rendered like the admin `PCT()` helper ("1", "1,5", "0,57"; ru/en use the same comma rule for uz/ru and
a dot for en).

#### 2.9.3 `GET me/qr/` — the personal QR token
```jsonc
// 200
{"token": "GD1:3q2-7wEjRZ8nYlZTS3l6qg",   // the QR payload: "GD1:" + base64url(16 random bytes), 26 chars
 "expires_at": "2026-10-26T12:01:00+05:00",
 "ttl_seconds": 60, "refresh_in_seconds": 50}
```
- 128-bit token from `secrets.token_bytes(16)`. Redis key `gd:loyalty:qr:{hex(HMAC-SHA256(QR_HMAC_KEY, token))}` →
  `"{customer_id}:{issued_at_epoch}"`, `SET NX EX QR_TOKEN_TTL_SECONDS` (60). The raw token is never stored server-side
  and never logged.
- Previous tokens stay valid until their own TTL (the app refreshes every 50 s, so a token being scanned does not die
  under the cashier's scanner). Single use is enforced at resolve time (§2.10).
- No database write, no audit (one call per minute per open screen). Throttle `customer`. The app keeps the token in
  memory only and hides the QR when the screen is backgrounded.

### 2.10 POS: `POST pos/cashback/resolve-qr/` (**K**)
Called by the in-store integration (the Billz POS or a small cashier app) when the cashier scans the customer's QR.

**Auth:** header `Authorization: Api-Key <key>`; key format `gdpos_<8-char prefix>_<32 random base64url chars>`,
created by `manage.py createposkey --name "Kassa 1" [--location "Yunusobod"]` (shown once). Lookup: SHA-256 of the
presented key against `PosDevice.key_hash` (unique), compared with `compare_digest`; `is_active` required;
`last_used_at` updated at most once per minute. Missing, unknown or inactive key → 401 `not_authenticated`
(`WWW-Authenticate: Api-Key`). In fake mode `POS_API_KEYS` (env, comma separated `name:key`) seeds rows.

Body `{"token": str 1..64, "sale_ref": str ≤64 | null}` (`sale_ref` = the POS receipt number, audit only).

| Check | Result |
|---|---|
| `token` not matching `^GD1:[A-Za-z0-9_-]{22}$` | 409 `qr_invalid` |
| `GETDEL gd:loyalty:qr:{hmac}` returns nothing (unknown, expired **or already used**) | 409 `qr_invalid` "QR kod yaroqsiz yoki muddati o‘tgan. Mijozdan ilovada QR kodni yangilashni so‘rang." |
| Customer no longer `active` | 409 `qr_invalid` (same body) |
| Throttle `pos` | 429 `throttled` |

```jsonc
// 200
{"customer_id": 5,
 "phone": "+998901234567",               // what Billz needs to attach the sale to the customer (PDF §9.1)
 "phone_mask": "+998 90 *** ** 67",      // what the cashier screen should display
 "available": 48600,                      // cashback the customer could use (display only; POS redemption is L6, M3)
 "percent": "1.00",                       // current tier percent | null
 "resolved_at": "2026-10-26T12:00:41+05:00"}
```
- **Must not reveal:** the customer's name, addresses, email, social identities, orders, appointments, ledger lines,
  device data, registration dates, the balance split (held/negative) or anything about other customers. The response
  is identical in shape for every customer; failures never say whether the token existed (one code, one message).
- **Atomic single use:** `GETDEL` (Redis ≥ 6.2, redis-py 6.4 `getdel`) is the only read; two scans of one token race
  in Redis and exactly one gets the customer. Tests with LocMem use a lock-protected get+delete in the cache helper.
- **Audit:** `pos.qr_resolved {pos_device_id, customer_id, sale_ref}` (actor_type `integration`, actor_id = device
  id); failures `pos.qr_rejected {pos_device_id, reason: "format" | "unknown"}` (one row per failure; throttled).
- **Open (M3):** how the sale is then discovered and credited (L7) and whether cashiers redeem (L6). M2 stores nothing
  else; `loyalty.services.record_store_purchase` (B4) is the M3 entry point.
- Uzbek messages only on this route (cashier software); `Accept-Language` is honoured if sent.

### 2.11 Appointments

**`GET specialists/`** (**O**) → `{"results": [Specialist…]}`: active professionals by position, name, with active
links to active services (`professionals.selectors.booking_catalogue`).
```jsonc
// Specialist
{"id": 1, "name": "Nodira Rahimova", "role": "dermatologist", "role_label": "Dermatolog",
 "photo_url": null, "bio": "…", "experience_years": 8,
 "location": {"id": 1, "name": "Glow Derma Yunusobod", "address": "…", "phone": "+998712000000",
              "latitude": 41.36, "longitude": 69.28},
 "services": [{"id": 1, "name": "Dermatolog konsultatsiyasi", "short_name": "Konsultatsiya",
               "duration_min": 30, "price": 150000}],
 "slot_step_min": 30}
```
**`GET specialists/{id}/`** → `Specialist` (404 when inactive). **`GET services/`** (**O**) →
`{"results": [{"id", "name", "short_name", "description", "duration_min" /* default */, "price_from", "price_to",
"specialist_ids": [int]}]}` (active services offered by ≥1 active specialist).

**`GET specialists/{id}/slots/?date=YYYY-MM-DD&service_id=`** (**O**) — free start times.

| Param | Rule · code · message (uz) · key |
|---|---|
| `date` | required, ISO date; `today ≤ date ≤ today + BOOKING_HORIZON_DAYS` (30) · `invalid` / `out_of_range` "Faqat {n} kun ichidagi sanani tanlash mumkin." · `field.booking_horizon` |
| `service_id` | required; an active service the specialist offers · `does_not_exist` "Mutaxassis bu xizmatni ko‘rsatmaydi." · `field.service_not_offered` |

Algorithm (`appointments.selectors.free_slots`, P3): hours = that day's `WorkingHoursOverride` else `WorkingHours`
(none → no slots); step = `Professional.slot_step_min`; duration = the link's `duration_min`; candidates
`t = hours.start + k·step` with `t + duration ≤ hours.end`; drop `t < now + BOOKING_MIN_LEAD_MINUTES` (60); drop any
overlap of `[t, t + duration)` with blocked periods, the recurring break and `requested`/`confirmed` appointments of
the specialist (pending requests hold the slot, P1).
```jsonc
{"date": "2026-10-28", "specialist_id": 1, "service_id": 1, "duration_min": 30, "price": 150000, "slot_step_min": 30,
 "working_hours": {"start": "09:00", "end": "18:00"},    // | null (not working)
 "slots": [{"start": "09:00", "end": "09:30", "start_at": "2026-10-28T09:00:00+05:00"}]}
```

**`POST appointments/`** (**C**, **I**). Body `{"specialist_id": int, "service_id": int, "start": datetime, "note": str ≤300 | null}`.

| Check (in order) | Code · message (uz) · key |
|---|---|
| `specialist_id` active | `does_not_exist` "Mutaxassis topilmadi." · `field.specialist_not_found` |
| `service_id` offered (active link, active service) | `does_not_exist` · `field.service_not_offered` |
| consent current | 409 `conflict` · `error.terms_update_required` |
| open future appointments of the customer < `BOOKING_MAX_OPEN_PER_CUSTOMER` (3) | 409 `booking_limit` "Sizda {limit} ta faol qabul bor. Yangisini olishdan oldin birini bekor qiling." · `error.booking_limit` |
| `start` aware | `invalid` (naive) |
| `start ≥ now + BOOKING_MIN_LEAD_MINUTES` | `in_past` "Kamida {n} daqiqa oldin yoziling." · `field.booking_too_soon` |
| within the horizon | `out_of_range` · `field.booking_horizon` |
| on the specialist's grid (`(start − hours.start) mod slot_step_min = 0`, seconds 0) | `invalid` "Vaqt {n} daqiqalik qadamda bo‘lsin." · `field.booking_grid` |
| inside the day's hours, `end` fits | `outside_working_hours` "Mutaxassis bu vaqtda ishlamaydi." · `field.outside_working_hours` |
| not overlapping blocks, the break or an open appointment of the specialist | `slot_unavailable` "Bu vaqt band." · `field.slot_unavailable` |
| not overlapping another open appointment **of this customer** | `slot_unavailable` "Sizda bu vaqtda boshqa qabul bor." · `field.own_overlap` |
| exclusion-constraint race (`appointment_no_overlap`, or the 40P01 deadlock it can raise) | 409 `conflict` "Bu vaqt band bo‘lib qoldi. Boshqa vaqtni tanlang." · `error.slot_taken` (nothing written) |

→ **201 `Appointment`** with `status: requested`. Side effects (`appointments.services.request_by_customer`, P3):
`Appointment(requested_via="app", created_by=null, duration_min/price snapshotted from the link, customer_note)`;
event `requested {via: "app", start, end, service_id, professional_id, price, duration_min}` with `source =
customer`; audit `appointment.requested` (actor_type `customer`). No push (the customer did it); staff see it in
`appointments/pending-summary/`.

```jsonc
// Appointment
{"id": 41, "status": "requested", "status_label": "Tasdiqlanishi kutilmoqda",
 "specialist": {"id": 1, "name": "Nodira Rahimova", "role": "dermatologist", "role_label": "Dermatolog", "photo_url": null},
 "service": {"id": 1, "name": "Dermatolog konsultatsiyasi", "short_name": "Konsultatsiya"},
 "location": {"name": "Glow Derma Yunusobod", "address": "…", "phone": "+998712000000"},
 "start": "2026-10-28T09:00:00+05:00", "end": "2026-10-28T09:30:00+05:00",
 "duration_min": 30, "price": 150000,                  // snapshots
 "note": null,                                          // the customer's own note | null
 "requested_at": "2026-10-26T12:00:00+05:00", "confirmed_at": null, "cancelled_at": null,
 "cancelled_by": null,                                  // "customer" | "admin" | null
 "can_cancel": true}                                    // open ∧ start − now ≥ BOOKING_CANCEL_MIN_LEAD_MINUTES
```
**`GET me/appointments/?scope=upcoming|past`** → `Page<Appointment>`: `upcoming` (default) = `start ≥ now` ∧ status
`requested`/`confirmed`, ascending by start; `past` = the rest, descending. **`GET me/appointments/{id}/`** →
`Appointment` (404 for another customer's).

**`POST appointments/{id}/cancel/`** (**C**, **I**). Body `{"reason": str ≤300 | null}`.
Allowed for `requested`/`confirmed` with `start − now ≥ BOOKING_CANCEL_MIN_LEAD_MINUTES` (default 0; P3 terms are
not enforced beyond that) → 200 `Appointment` (`status: cancelled`). Else 409 `not_cancellable` "Bu qabulni bekor
qilib bo‘lmaydi." (`error.appointment_not_cancellable`). Side effects (`appointments.services.cancel_by_customer`,
P3, under the row lock): `status=cancelled`, `cancelled_at`, `cancelled_by="customer"`; event `cancelled {by:
"customer", reason, push_sent: false}` (`source = customer`); the slot is free again; no push to the customer; audit
`appointment.cancelled` (actor_type `customer`). Rescheduling from the app = cancel + book (the `rescheduled` event is
admin-only by DB constraint).

### 2.12 Notifications inbox (**C**)

```jsonc
// InboxItem
{"id": 901, "kind": "order_status",          // InboxKind: promo | general | order_status | appointment
 "title": "Kuryerga topshirildi", "body": "Buyurtma №10248 kuryerga topshirildi.",   // rendered at creation in the customer's language
 "image_url": null,
 "link": {"kind": "order", "product_id": null, "order_id": 10248, "appointment_id": null},   // InboxLinkKind: none | product | order | appointment
 "created_at": "2026-10-23T16:40:00+05:00", "read_at": null, "delivered_at": "2026-10-23T16:40:05+05:00"}
```
- **`GET me/notifications/`** → `Page<InboxItem>` + top-level `"unread_count": int`; newest first. Rows exist also for
  pushes that could not be sent (`push_state = no_device`, N-10), so the feed is complete after a reinstall.
- **`GET me/notifications/unread-count/`** → `{"unread_count": 3}` (badge polling; `inbox_customer_unread_idx`).
- **`POST me/notifications/{id}/read/`** → 200 `{"id", "read_at", "unread_count"}`; idempotent; also sets
  `delivered_at` when null. 404 for another customer's row.
- **`POST me/notifications/read-all/`** → 200 `{"updated": int, "unread_count": 0}`.
- **`POST me/notifications/ack/`** — delivery acknowledgement from the app's push handler (foreground, background or
  notification-service extension; the app batches and retries on next start). Body `{"inbox_ids": [int] 1..50}`
  (`required` / `max_items` "Ko‘pi bilan 50 ta."). → 200 `{"acknowledged": int}`. Ids of other customers or unknown ids
  are ignored silently (no enumeration). Effects (`notifications.services.acknowledge`, P3), per row whose
  `delivered_at` is null: `InboxNotification.delivered_at = now`; the linked `CampaignRecipient.delivered_at = now`
  (only if null); then `Campaign.delivered = COALESCE(delivered, 0) + 1` by a conditional `UPDATE` that runs only when
  the recipient row actually changed (so retries never double count). This fills the admin `delivered` counter that
  was `null` in M1.
- Push payloads carry `inbox_id` (P3 changes the three senders), §2.14.2.

---
### 2.13 Provider callbacks (webhooks, **S**)

#### 2.13.0 Common rules
- Views inherit `WebhookAPIView` and live in `commerce/api/webhooks/` (the order orchestrator; protocol-pure helpers
  — signature, amount parsing, error tables — live in `integrations/{click,payme,bts}/protocol.py` without DB access).
- **HTTP status:** always 200 with the provider-native body for protocol outcomes (including auth failures for
  Click and Payme, as their protocols require). An unexpected exception rolls everything back and answers 500 so the
  provider retries; nothing is half-applied.
- **WebhookInbox (T5):** inside one `transaction.atomic()` the view takes `pg_advisory_xact_lock(hashtext(
  "<provider>:<lock key>"))`, then processes, then inserts the `WebhookInbox` row with `result = {"status": 200,
  "body": <response>, "outcome": "accepted" | "duplicate" | "rejected" | "error", "error_code": str | null}`,
  `entity_ref` (`"order:10248"`, `"payment:301"`), allow-listed headers (`Content-Type`, `User-Agent`,
  `X-Forwarded-For`; never `Authorization` or tokens), `raw_body` ≤64 KiB, `processing_ms`.

  | Provider | `event_key` | Duplicate handling |
  |---|---|---|
  | Click | `prepare:{click_trans_id}:{merchant_trans_id}` / `complete:{click_trans_id}:{merchant_trans_id}` | an existing key with the **same payload hash** replays `result.body` and changes nothing; a different payload under the same key → error `-8` and a `rejected:` row |
  | Payme | `{method}:{params.id or "-"}:{uuid4}` — every delivery is its own row | never replayed: the Payme state machine below is idempotent on `params.id` (= `Payment.provider_txn_id`) and must answer from the **current** state (a replay of an old state-1 answer to a repeated `CreateTransaction` would be wrong) |
  | BTS | `{tracking_number or clientId}:{status}:{event_time}` from the payload, else `{uuid4}` | replay `{"ok": true}` |
  | any | **auth/signature failure** → `rejected:{uuid4}` | a forged request can never occupy the key of a real delivery |
- Lock keys: Click `click:{click_trans_id}`; Payme `payme:{params.id}` (`payme:order:{order_id}` for
  `CheckPerformTransaction`); BTS `bts:{tracking_number}`. The commerce services then take Order → Payment →
  LoyaltyAccount locks as usual.
- **Payable** (one definition for every provider; `commerce.services.is_payable(order, payment, now)`, P2):
  `order.status = awaiting_payment` ∧ (`now < reservation_expires_at` ∨ (the payment has a provider transaction
  attached ∧ `now < attached_at + PAYMENT_IN_FLIGHT_GRACE_SECONDS` (300))). `attached_at` = `provider_state.prepare_at`
  (Click) or `provider_state.create_time` (Payme). The expiry job applies the same grace: it skips an order whose
  pending payment is in flight and expires it after the grace (P2 change to `commerce.services.expire_reservation`).
- **Late capture** (spec Financial integrity: captures on non-awaiting orders are refused; one that still lands is
  refunded and alerted): see Click complete step 7 and `commerce.services.record_late_capture` (P2).

#### 2.13.1 Click SHOP API
Configured in the Click merchant cabinet: Prepare URL `…/api/v1/webhooks/click/prepare/`, Complete URL
`…/api/v1/webhooks/click/complete/`. Requests are `application/x-www-form-urlencoded` POSTs **(verify M3)**; JSON is
accepted too. Responses are `application/json`.

**Request parameters**

| Param | Type | Prepare | Complete | Meaning |
|---|---|---|---|---|
| `click_trans_id` | int (bigint) | ✓ | ✓ | Click's payment id → `Payment.provider_txn_id` |
| `service_id` | int | ✓ | ✓ | must equal `CLICK_SERVICE_ID` |
| `click_paydoc_id` | int | ✓ | ✓ | Click payment document id (stored) |
| `merchant_trans_id` | str | ✓ | ✓ | our order id (the `transaction_param` of the pay URL) |
| `merchant_prepare_id` | int | – | ✓ | the id we returned from prepare (= `Payment.id`) |
| `amount` | decimal string | ✓ | ✓ | so'm, e.g. `"784400"` or `"784400.00"` |
| `action` | int | `0` | `1` | |
| `error` | int | ✓ | ✓ | Click's status: `0` = ok, `< 0` = the payment failed at Click (e.g. `-5017` insufficient funds) |
| `error_note` | str | ✓ | ✓ | |
| `sign_time` | str | ✓ | ✓ | `YYYY-MM-DD HH:MM:SS` |
| `sign_string` | str | ✓ | ✓ | lower-case hex MD5, below |

**Signature** (over the **raw received strings**, concatenated without separators; compared with `hmac.compare_digest`):
- Prepare: `MD5(click_trans_id + service_id + CLICK_SECRET_KEY + merchant_trans_id + amount + action + sign_time)`
- Complete: `MD5(click_trans_id + service_id + CLICK_SECRET_KEY + merchant_trans_id + merchant_prepare_id + amount + action + sign_time)`

**Response**
```jsonc
// prepare
{"click_trans_id": 2875510123, "merchant_trans_id": "10248", "merchant_prepare_id": 301, "error": 0, "error_note": "Success"}
// complete
{"click_trans_id": 2875510123, "merchant_trans_id": "10248", "merchant_confirm_id": 301, "error": 0, "error_note": "Success"}
// errors: merchant_prepare_id / merchant_confirm_id are null
```

**Error codes** (`error_note` texts as Click documents them)

| `error` | `error_note` | We answer it when |
|---|---|---|
| 0 | `Success` | accepted |
| -1 | `SIGN CHECK FAILED!` | signature mismatch |
| -2 | `Incorrect parameter amount` | `amount` not a non-negative decimal with ≤2 dp, not integral so'm, or ≠ `order.total` (= `payment.amount`) |
| -3 | `Action not found` | `action` ≠ 0 on prepare / ≠ 1 on complete |
| -4 | `Already paid` | the order is already paid (`processing`+ or a `paid` payment by another transaction) |
| -5 | `User does not exist` | `merchant_trans_id` is not an existing order id |
| -6 | `Transaction does not exist` | complete: `merchant_prepare_id` is not a Click payment of this order bound to this `click_trans_id` |
| -7 | `Failed to update user` | reserved (never sent: a DB failure answers 500 so Click retries) |
| -8 | `Error in request from click` | missing/malformed parameters; `service_id` ≠ ours; IP outside `CLICK_ALLOWED_IPS` (when set); same key with a different payload |
| -9 | `Transaction cancelled` | the order is cancelled or not payable; the payment was failed; Click reported `error < 0` |

**Prepare (action 0)** — checks in order: params (-8) → IP (-8) → signature (-1) → action (-3) → `service_id` (-8)
→ inbox replay → order exists (-5) → already paid (-4) → order cancelled or not payable (-9) → Click `error < 0`
(-9, no effect) → amount (-2) → `ensure_payable_payment(order, "click")`: an active Payme state-1 transaction → -9
(`error_note` `Transaction cancelled`; the customer is paying with Payme). Then bind: a pending Click payment already
bound to **another** `click_trans_id` is failed (`abandoned`) and a new pending payment is created; the payment gets
`provider_txn_id = click_trans_id`, `provider_state += {merchant_trans_id, merchant_prepare_id: payment.id,
click_trans_id, click_paydoc_id, service_id, prepare_at, sign_time}` (`payments.services.attach_transaction`, P2). →
`0` with `merchant_prepare_id = payment.id`. No order event (a prepare is not a state change).

**Complete (action 1)** — params → IP → signature → action → `service_id` → inbox replay, then:
1. order exists (-5);
2. `Payment(id = merchant_prepare_id, order, provider click, provider_txn_id = click_trans_id)` exists (-6);
3. payment `paid` → -4; payment `failed` → -9;
4. Click `error < 0` → `commerce.services.fail_payment_effects(order_id, error_code=str(error), provider_txn_id)`
   (payment `failed`, event `payment_failed {provider, transaction_id, error_code}`; the order stays payable until
   expiry and the app may pay again) → -9;
5. amount ≠ `payment.amount` → -2 (payment stays pending);
6. order already paid by another payment → -4;
7. **not payable** (cancelled, or expired beyond the in-flight grace):
   - `CLICK_REVERSES_ON_COMPLETE_ERROR=true` (default; Click cancels a payment whose Complete is answered with an
     error **(verify M3)**) → payment failed (`late_complete`), no event → -9;
   - `false` → **late capture**: `commerce.services.record_late_capture(order_id, payment_id, click_trans_id)`:
     payment `paid` (`confirmed_at`), `Refund(origin=late_capture, amount=payment.amount)` `requested` + outbox
     `refund.submit` (Click reversal), event `refund_requested {refund_id, amount, provider, manual: false}` (source
     system), audit `payment.late_capture {order_id, payment_id, provider}`, ERROR log for alerting; the refund then
     shows in `attention.refunds_needing_action` if it fails → `0` with `merchant_confirm_id`;
8. otherwise `commerce.services.mark_paid(order_id, click_trans_id)` (B5: `confirm_payment` + `confirm_payment_effects`
   — reservations converted, hold captured, purchase + cashback credit, `processing`, events `payment_confirmed`
   (+ `cashback_credited`), outbox `billz.push_sale`, `bts.create_shipment` (delivery), `push.order_status
   processing`) → `0` with `merchant_confirm_id = payment.id`; `provider_state += {complete_at, error, error_note}`.

#### 2.13.2 Payme Merchant API (JSON-RPC 2.0)
Endpoint `POST …/api/v1/webhooks/payme/`, `Content-Type: application/json`. Every answer is HTTP 200 with a JSON-RPC
body; amounts are **tiyin** (so'm × 100) and exist only in this adapter; times are epoch **milliseconds**.

**Auth:** `Authorization: Basic base64("Paycom:" + key)` where key = `PAYME_KEY` (the test key in the sandbox).
Missing or wrong → error `-32504`. The optional `PAYME_ALLOWED_IPS` check runs after auth and answers `-32504` too
(defence in depth only).

```jsonc
// request
{"jsonrpc": "2.0", "id": 12345, "method": "CreateTransaction",
 "params": {"id": "5305e3bab097f420a62ced0b", "time": 1729673880000, "amount": 78440000,
            "account": {"order_id": "10248"}}}
// success
{"jsonrpc": "2.0", "id": 12345, "result": {"create_time": 1729673881000, "transaction": "301", "state": 1}}
// error
{"jsonrpc": "2.0", "id": 12345,
 "error": {"code": -31008, "message": {"uz": "Amalni bajarib bo‘lmaydi.", "ru": "Невозможно выполнить операцию.",
                                        "en": "Unable to perform the operation."}, "data": null}}
```

**Error codes**

| Code | `message.uz` / `ru` / `en` | When |
|---|---|---|
| -32300 | "So‘rov POST bo‘lishi kerak." / "Метод запроса должен быть POST." / "Request method must be POST." | non-POST |
| -32700 | "JSON xato." / "Ошибка парсинга JSON." / "Parse error." | body is not JSON |
| -32600 | "So‘rov noto‘g‘ri." / "Неверный запрос." / "Invalid request." | missing `method`/`params`/`id`, wrong types |
| -32601 | "Metod topilmadi." / "Метод не найден." / "Method not found." | unknown method; also `ChangePassword` (keys rotate through env, §7 `M2-P6`) |
| -32504 | "Ruxsat yetarli emas." / "Недостаточно привилегий для выполнения метода." / "Insufficient privileges." | auth / IP |
| -31001 | "Noto‘g‘ri summa." / "Неверная сумма." / "Wrong amount." | `amount ≠ order.total × 100` |
| -31003 | "Tranzaksiya topilmadi." / "Транзакция не найдена." / "Transaction not found." | unknown `params.id` |
| -31007 | "Buyurtma bajarilgan, tranzaksiyani bekor qilib bo‘lmaydi." / "Заказ выполнен. Невозможно отменить транзакцию." / "The order is fulfilled; the transaction cannot be cancelled." | cancel of a performed transaction without an open refund |
| -31008 | "Amalni bajarib bo‘lmaydi." / "Невозможно выполнить операцию." / "Unable to perform the operation." | order not payable; another active transaction for the order; transaction not in state 1; 12-hour timeout |
| -31050 | "Buyurtma topilmadi." / "Заказ не найден." / "Order not found." (`data: "order_id"`) | missing/unknown `account.order_id` (range -31050…-31099 is reserved for account errors) |

**States** (`provider_state.state`): `1` created, `2` performed, `-1` cancelled before perform, `-2` cancelled after
perform. **Reasons** (`CancelTransaction.reason`, stored): 1 recipient not found/inactive, 2 debit error at the
processing centre, 3 transaction execution error, 4 cancelled by timeout, 5 refund, 10 unknown **(verify M3)**.
**Timeout:** a state-1 transaction older than 12 h (`now − create_time > 43 200 000 ms`) is cancelled by us on the
next touch (state −1, reason 4, payment failed, `payment_failed` event) and the call answers -31008.

| Method | Params | Logic (in order) | `result` |
|---|---|---|---|
| `CheckPerformTransaction` | `amount`, `account` | order exists (-31050) → payable and `ensure_payable_payment` would succeed (dry run: Payme state-1 transaction with another id → -31008) (-31008) → amount (-31001) | `{"allow": true}` (fiscal `detail` only when `PAYME_FISCAL_DETAIL_ENABLED`, §7 `M2-P5`) |
| `CreateTransaction` | `id`, `time`, `amount`, `account` | **existing** `id`: state ≠ 1 → -31008; timed out → cancel (reason 4) and -31008; else the stored answer. **new** `id`: as CheckPerform (-31050/-31008/-31001) → `time` older than 12 h → -31008 → the order has another Payme transaction in state 1 → -31008 → `ensure_payable_payment(order, "payme")` → `attach_transaction(provider_txn_id = id, provider_state = {transaction: id, time, create_time: now_ms, state: 1, account})` | `{"create_time", "transaction": "<payment id>", "state": 1}` |
| `PerformTransaction` | `id` | unknown (-31003) → state 2: the stored answer → state ≠ 1: -31008 → timed out: cancel (reason 4), -31008 → not payable (cancelled/expired beyond grace): -31008 (the transaction stays in state 1; Payme cancels it) → `commerce.services.mark_paid(order_id, id)`; `provider_state += {state: 2, perform_time: now_ms}` | `{"transaction", "perform_time", "state": 2}` |
| `CancelTransaction` | `id`, `reason` | unknown (-31003) → state 1: state −1, `cancel_time`, `reason`; `commerce.services.fail_payment_effects(order_id, str(reason), provider_txn_id=id)` (payment failed, `payment_failed` event; also on an already cancelled/expired order) → state 2 **with an open `manual_required` refund** (staff refunded in the Payme cabinet after an admin cancel, O5): state −2, `cancel_time`, `reason`; `commerce.services.resolve_refund(refund_id, "succeeded", actor_id=None)` (refund succeeded, payment `refunded`, event `refund_succeeded`) → state 2 without an open refund: -31007 → state −1/−2: the stored answer | `{"transaction", "cancel_time", "state"}` |
| `CheckTransaction` | `id` | unknown (-31003) | `{"create_time", "perform_time" (0 if none), "cancel_time" (0 if none), "transaction", "state", "reason" (null if none)}` |
| `GetStatement` | `from`, `to` (ms) | Payme payments with `provider_state.create_time ∈ [from, to]`, ascending | `{"transactions": [{"id", "time", "amount", "account": {"order_id"}, "create_time", "perform_time", "cancel_time", "transaction", "state", "reason", "receivers": null}]}` |

Every method writes a `WebhookInbox` row (§2.13.0). The `transaction` value is our `Payment.id` as a string; the
Payme id is `Payment.provider_txn_id`.

#### 2.13.3 `POST webhooks/bts/` and the polling job
- **Auth:** shared secret `BTS_WEBHOOK_SECRET` in header `X-GD-Webhook-Token` or, if BTS cannot set headers, query
  `?token=` **(verify M3)**; `compare_digest`; mismatch → 401 `{"ok": false}` + `rejected:` inbox row.
- **Body:** BTS's status notification (JSON). We read only the parcel identity — `clientId` (= our order id) and/or
  the tracking number / BTS order id — and **never trust the status in the payload**: the view **re-tracks** the
  parcel with the BTS port `track(tracking_number)` (P2 adds it) and applies that authoritative status through
  `commerce.services.apply_bts_status(order_id, status, via="webhook", payload=<tracked parcel>)` (B5: shipment
  event; `200` → `handed_to_courier`, `300` → `delivered`, others recorded only; events with source `bts`;
  `push.order_status`). A forged webhook can therefore only trigger a re-track.
- → 200 `{"ok": true}`; unknown parcel → 200 `{"ok": true, "ignored": true}` + WARNING log (BTS must not retry
  forever); BTS transport failure during the re-track → 503 `{"ok": false}`, nothing stored (BTS retries; polling
  catches up).
- **Polling** (beat `commerce.poll_bts`, every 600 s, P2): shipments `created` whose order is `processing` or
  `handed_to_courier` and whose `last_status_at` is older than `BTS_POLL_STALE_MINUTES` (30), oldest first, at most
  100 per run → `track()` → `apply_bts_status(…, via="polling")`; `IntegrationState(bts).last_sync_at`; transport
  failures counted in `consecutive_failures`.

### 2.14 Outbound provider protocols (what the adapters send)

#### 2.14.1 Eskiz SMS (`integrations/eskiz/`, P1)
- **Auth (verify M3):** `POST {ESKIZ_BASE_URL}/auth/login` (form `email`, `password`) → bearer token, cached in Redis
  `gd:eskiz:token` for 29 days; a 401 on send → one re-login and retry; `PATCH /auth/refresh` when supported.
- **Send:** `POST {ESKIZ_BASE_URL}/message/sms/send` form `mobile_phone` (digits without `+`, `998901234567`),
  `message`, `from` = `ESKIZ_SENDER`, `callback_url` = `ESKIZ_CALLBACK_URL` (empty → omitted) → `{"id": "…",
  "status": "waiting", …}`; the id is stored in `SmsCode.provider_message_id`.
- **Texts** must match Eskiz-approved templates character for character: catalog `sms.otp` (uz) "Glow Derma: tasdiqlash
  kodingiz {code}. Kodni hech kimga bermang." · (ru) "Glow Derma: ваш код подтверждения {code}. Никому его не
  сообщайте." · (en) "Glow Derma: your verification code is {code}. Do not share it with anyone." Language = resolved
  request language. The Eskiz test account only delivers its own test text (use fake mode until production).
- **Handler** `sms.send` (`accounts/tasks.py`): claim `sent_at` in its own transaction **before** the call; a
  connection error raised before the request left (connect failure) un-claims and re-raises (outbox retry); a timeout
  or 5xx after sending keeps the claim and is never resent (the user can resend after 60 s); 4xx (invalid number, no
  balance) is logged with `IntegrationState(eskiz)` error and not retried.
- **Delivery callback:** optional; M2 sets no `callback_url` and exposes no endpoint (M3 may add
  `webhooks/eskiz/` to record `DELIVRD`/`UNDELIV`).
- **Fake (`ESKIZ_MODE=fake`):** phone ending `0000` → rejected (definitive); ending `9999` → transport down; otherwise
  accepted with id `fake-<sms_code_id>`; every message is kept in `FakeEskiz.sent`; the generated code is always
  `ESKIZ_FAKE_CODE` (default `123456`) so black-box e2e can sign in. Production settings refuse `ESKIZ_MODE=fake`.

#### 2.14.2 FCM push (`integrations/fcm/`; live adapter M3, payload contract now)
HTTP v1 `messages:send` through firebase-admin (`send_each`, ≤500 per call) **(verify M3)**.
```jsonc
{"token": "<device fcm_token>",
 "notification": {"title": "Kuryerga topshirildi", "body": "Buyurtma №10248 kuryerga topshirildi."},  // omitted for data-only
 "data": {"type": "order_status", "inbox_id": "901", "order_id": "10248", "status": "handed_to_courier"},  // all values strings
 "android": {"priority": "high", "notification": {"channel_id": "orders"}},
 "apns": {"headers": {"apns-priority": "10", "apns-push-type": "alert"},
          "payload": {"aps": {"sound": "default", "mutable-content": 1}}}}
```

| `data.type` | Other `data` keys | Notification block | Android channel / priority |
|---|---|---|---|
| `order_status` | `inbox_id`, `order_id`, `status` | template text (`{raqam}` only) | `orders` / high |
| `appointment` | `inbox_id`, `appointment_id`, `action` | fixed texts (`notifications/texts.py`) | `appointments` / high |
| `campaign` | `inbox_id`, `campaign_id`, `product_id` (or `""`) | as authored; `notification.image` = campaign image URL | `promo` or `general` / normal |
| `session_replaced` | – | none (data-only; APNs `content-available: 1`, `apns-push-type: background`, priority 5) | – / high |

- **C1:** no names, phones, addresses or amounts in `title`, `body` or `data` — ids and the order number only.
- `UNREGISTERED` / token `INVALID_ARGUMENT` → result error `unregistered` →
  `accounts.services.invalidate_fcm_token(device_id)` (`fcm_token=""`, `push_enabled=False`, `fcm_invalidated_at=now`)
  in the same handler; the app re-registers through `PUT me/device/`. `UNAVAILABLE`, `INTERNAL`, `QUOTA_EXCEEDED` →
  `FcmUnavailable` (outbox retry).

#### 2.14.3 BTS (`integrations/bts/`; B5 port + P2 additions; live adapter M3)
| Port method | Use | Notes (verify M3 against `apitest`) |
|---|---|---|
| `regions()` | `delivery/regions/`, address validation | BTS region/city reference; cached 24 h |
| `quote(region_code, city_code)` | `delivery/calculate/`, checkout | tariff calculation with `BTS_SENDER_CITY_CODE`, `BTS_DEFAULT_PARCEL_WEIGHT_G`, one parcel; `BtsRejected` → 409 `delivery_unavailable` |
| `find_by_client_id(client_id)` | before every create (B5) | `clientId` = order id |
| `create(request)` | outbox `bts.create_shipment` (B5) | recipient = the order's recipient snapshot |
| `cancel(parcel)` | admin cancel (B5) | succeeds only at status `100` (O1) |
| `track(tracking_number)` | webhook re-track, polling (P2) | returns the parcel with its current status |

Fake additions (P2): `regions()` returns the four regions of the fake tariff table (`10` Toshkent shahri, `11`
Toshkent viloyati, `12` Sirdaryo, `13` Samarqand) with two cities each; `track()` returns `scenario["track_status"]`
or the parcel's last status; `scenario["quote"] == "rejected"` → `BtsRejected`, `"down"` → `BtsUnavailable`.

### 2.15 `POST dev/payments/{order_id}/simulate/` (fake mode only)
Mounted only when `FAKE_PAYMENTS_ENABLED=true` (refused by production settings). **C**, own order. Body
`{"outcome": "success" | "fail", "provider": PaymentProvider | null}`. It builds a correctly signed Click
prepare + complete (or Payme `CheckPerformTransaction` + `CreateTransaction` + `PerformTransaction` / `CancelTransaction`)
sequence and runs it through the **real webhook views** in-process, so the mobile team can finish a purchase without
provider accounts. → 200 `{"order_id", "provider", "steps": [{"call": "prepare", "response": {…}}, …]}`.

---
## 3. Enumerations (customer-facing subsets)

Literals live in `glowderma/core/enums.py` (P0 adds the new classes); labels in §5.

| Enum | Literals | Used in |
|---|---|---|
| OrderStatus | `awaiting_payment`, `processing`, `handed_to_courier`, `delivered`, `cancelled` (admin §3) | orders |
| PaymentStatus | `pending`, `paid`, `failed`, `refunded` | order payment |
| PaymentProvider | `click`, `payme` | checkout, pay |
| FulfilmentMethod | `delivery`, `pickup` | checkout, orders, delivery |
| CancellationReason | `reservation_expired`, `admin`, `customer_request` | `cancellation.reason` |
| LedgerKind | `credit`, `debit`, `adjustment`, `reversal` | ledger |
| ContentLanguage | `uz`, `ru`, `en` | `me.language`, `Accept-Language` |
| SpecialistRole | `dermatologist`, `cosmetologist` | specialists |
| AppointmentStatus | `requested`, `confirmed`, `rejected`, `attended`, `cancelled` (the `cancelled` literal reserved by T3 is now public) | appointments |
| BannerLinkKind | `none`, `product`, `category` | banners |
| **OtpPurpose** (new) | `register`, `login_new_device`, `reset_password`, `link_social` | `auth/send-code/`, `auth/verify-code/` |
| **NextStep** (new) | `verify_device`, `verify_phone` | `auth/login/`, `auth/social/` |
| **SocialProvider** (new) | `google`, `facebook` | `auth/social/` |
| **SocialTokenType** (new) | `id_token`, `access_token` | `auth/social/` |
| **DevicePlatform** (new; same literals as OrderChannel) | `ios`, `android` | `X-GD-Platform`, `me/device/` |
| **StockStatus** (new) | `in_stock`, `out_of_stock` | products |
| **CustomerProductOrdering** (new) | `default`, `price`, `-price`, `name` | `catalog/products/` |
| **CartLineStatus** (new) | `ok`, `insufficient`, `out_of_stock`, `unavailable` | cart, `OUT_OF_STOCK.details.lines[].status` |
| **OrderScope** (new) | `active`, `completed`, `cancelled`, `all` | `me/orders/` |
| **CustomerRefundState** (new) | `in_progress`, `completed` | `OrderDetail.refund` |
| **CashbackEarnedState** (new) | `none`, `credited`, `reversed` | `OrderDetail.cashback` |
| **TimelineType** (new) | §4.1 first column (customer-visible OrderEvent types plus `refund_in_progress`) | `OrderDetail.timeline` |
| **ProgressState** (new) | `done`, `current`, `upcoming`, `skipped` | `OrderDetail.progress` |
| **LedgerSource** (new) | `online`, `store`, `admin` | ledger |
| **AppointmentScope** (new) | `upcoming`, `past` | `me/appointments/` |
| **AppointmentCancelledBy** (new; = `Appointment.CancelledBy`) | `customer`, `admin` | appointments |
| **InboxKind** (new; = `InboxNotification.Kind`) | `promo`, `general`, `order_status`, `appointment` | inbox |
| **InboxLinkKind** (new) | `none`, `product`, `order`, `appointment` | inbox |

---

## 4. Order timeline for customers

### 4.1 `timeline[]` — from OrderEvent (admin §4) to customer entries
`OrderDetail.timeline` is built by `commerce.selectors.customer_timeline(order)` (P2) from the order's events, newest
first (`at` desc, `id` desc). Admin-only facts (staff names and notes, Billz ids, internal BTS failures, refund
failures) never reach the customer.
```jsonc
// TimelineEntry
{"type": "payment_confirmed", "at": "2026-10-23T14:05:00+05:00",
 "title": "To‘lov qabul qilindi", "subtitle": "Click orqali · cashback +5 680 so‘m"}   // subtitle | null; resolved language
```

| OrderEvent `type` | Customer entry | `title` (uz) · key | `subtitle` (uz) · key |
|---|---|---|---|
| `order_created` | shown | "Buyurtma berildi" · `label.timeline.order_created` | "{item_count} ta mahsulot · {total} so‘m" · `label.timeline.sub_items_total` |
| `reservation_created` | folded (feeds `pay_by`) | – | – |
| `payment_confirmed` (+ folded `cashback_credited`) | shown | "To‘lov qabul qilindi" · `label.timeline.payment_confirmed` | "{provider} orqali" + (credit ? " · cashback +{amount} so‘m") · `label.timeline.sub_paid_via`, `label.timeline.sub_cashback` |
| `cashback_credited` | folded into `payment_confirmed` | – | – |
| `billz_sale_recorded` | hidden | – | – |
| `payment_failed` | shown | "To‘lov amalga oshmadi" · `label.timeline.payment_failed` | "{provider} · qayta urinib ko‘ring" while `awaiting_payment`, else "{provider}" · `label.timeline.sub_payment_failed_retry` |
| `shipment_created` | shown | "Yetkazib berishga topshirildi" · `label.timeline.shipment_created` | "BTS kuzatuv raqami: {tracking_number}" · `label.timeline.sub_tracking` |
| `shipment_failed` | hidden (staff retry; the order stays "Tayyorlanmoqda") | – | – |
| `status_changed` → `handed_to_courier` | shown as `handed_to_courier` | "Kuryerga topshirildi" · `label.timeline.handed_to_courier` | null |
| `status_changed` → `delivered` | shown as `delivered` | delivery "Yetkazildi" · `label.timeline.delivered`; pickup "Olib ketildi" · `label.timeline.picked_up` | null |
| `handed_to_courier` (BTS) | shown | "Kuryerga topshirildi" | "BTS" · `label.timeline.sub_bts` |
| `delivered` (BTS) | shown | "Yetkazildi" | "BTS" |
| `cancelled` (+ folded `refund_requested` with the same `refund_id`) | shown | "Buyurtma bekor qilindi" · `label.timeline.cancelled` | by reason: `admin` "Do‘kon tomonidan bekor qilindi" · `label.timeline.sub_cancelled_admin`; `customer_request` "Siz bekor qildingiz" · `label.timeline.sub_cancelled_customer`; + (refund ? " · to‘lov qaytariladi" · `label.timeline.sub_refund_follows`). The staff `note` is never shown. |
| `reservation_expired` | shown as `cancelled` | "Buyurtma bekor qilindi" | "To‘lov muddati tugadi" · `label.timeline.sub_expired` |
| `refund_requested` (not folded: late capture, staff retry) | shown as `refund_in_progress` | "To‘lov qaytarilmoqda" · `label.timeline.refund_in_progress` | "{amount} so‘m" |
| `refund_succeeded` | shown | "To‘lov qaytarildi" · `label.timeline.refund_succeeded` | "{amount} so‘m · {provider}" |
| `refund_failed` | hidden (the order still reads "To‘lov qaytarilmoqda" through `refund.state`) | – | – |

Order №10248 test vector (admin §4 events) → customer timeline, newest first:
1. `handed_to_courier` "Kuryerga topshirildi" — "BTS"
2. `shipment_created` "Yetkazib berishga topshirildi" — "BTS kuzatuv raqami: 48213907"
3. `payment_confirmed` "To‘lov qabul qilindi" — "Click orqali · cashback +5 680 so‘m"
4. `order_created` "Buyurtma berildi" — "3 ta mahsulot · 784 400 so‘m"

Numbers use the admin `N()` rule (NBSP thousands separator) for uz/ru and a comma separator for en.

### 4.2 `progress[]` — the step bar
```jsonc
// ProgressStep
{"step": "paid", "label": "To‘lov qabul qilindi", "state": "done", "at": "2026-10-23T14:05:00+05:00"}   // at | null
```

| Fulfilment | Steps (`step` · label uz · key) | Reached when |
|---|---|---|
| delivery | `placed` "Buyurtma berildi" · `label.progress.placed` → `paid` "To‘lov qabul qilindi" · `label.progress.paid` → `with_courier` "Kuryerda" · `label.progress.with_courier` → `delivered` "Yetkazildi" · `label.progress.delivered` | `created_at` → `paid_at` → `handed_to_courier_at` → `delivered_at` |
| pickup | `placed` → `paid` → `picked_up` "Olib ketildi" · `label.progress.picked_up` | `created_at` → `paid_at` → `delivered_at` (O4: no separate "ready for pickup" status, §7 `M2-O10`) |

`state`: reached steps `done`; the first unreached step `current` while the order is open; later ones `upcoming`. A
cancelled order marks the unreached steps `skipped` and appends `{"step": "cancelled", "label": "Bekor qilindi",
"state": "done", "at": cancelled_at}`. A pickup order delivered by a manual `status_changed` has no
`handed_to_courier_at`; a delivery order delivered by BTS without a `200` first marks `with_courier` `done` with `at`
null.

### 4.3 Customer-facing status labels

| OrderStatus | delivery (uz · key `label.order_status.<s>`) | pickup override |
|---|---|---|
| `awaiting_payment` | "To‘lov kutilmoqda" | – |
| `processing` | "Tayyorlanmoqda" | – |
| `handed_to_courier` | "Kuryerga topshirildi" | – (cannot happen) |
| `delivered` | "Yetkazildi" | "Olib ketildi" · `label.order_status_pickup.delivered` |
| `cancelled` | "Bekor qilindi" | – |

Admin §5.8 labels stay unchanged for the admin UI and exports; push templates (StatusTemplate) are admin-edited and
independent of these labels.

---

## 5. i18n message catalog

`glowderma/core/i18n.py` (P0) holds `CATALOG: dict[key, {"uz", "ru", "en"}]` and `t(key, lang, **params)`; a unit test
asserts every key has three non-empty texts with the same placeholders, and a contract test asserts every key named
in §2–§4 exists. The admin audience keeps its Uzbek strings (`core.api.exceptions.MESSAGES`); the customer exception
handler renders through the catalog. Placeholders: `{n}`, `{m}`, `{limit}`, `{max}`, `{available}`, `{order_id}`,
`{provider}`, `{amount}`, `{percent}`, `{item_count}`, `{total}`, `{tracking_number}`, `{code}` (SMS only).

### 5.1 Top-level codes (`error.*`)

| Key | uz | ru | en |
|---|---|---|---|
| `error.validation_error` | Maydonlarni tekshiring. | Проверьте поля. | Please check the fields. |
| `error.bad_request` | So‘rov noto‘g‘ri. | Некорректный запрос. | Malformed request. |
| `error.parse_error` | So‘rovni o‘qib bo‘lmadi. | Не удалось разобрать запрос. | The request could not be parsed. |
| `error.not_authenticated` | Avtorizatsiya talab qilinadi. | Требуется авторизация. | Please sign in. |
| `error.SESSION_EXPIRED` | Sessiya tugadi. Qayta kiring. | Сессия истекла. Войдите снова. | Your session has ended. Please sign in again. |
| `error.SESSION_REPLACED` | Hisobingizga boshqa qurilmadan kirildi. | В ваш аккаунт вошли с другого устройства. | Your account was signed in on another device. |
| `error.invalid_credentials` | Telefon raqami yoki parol noto‘g‘ri. | Неверный номер телефона или пароль. | Wrong phone number or password. |
| `error.permission_denied` | Ruxsat yo‘q. | Доступ запрещён. | Access denied. |
| `error.not_found` | Topilmadi. | Не найдено. | Not found. |
| `error.method_not_allowed` | Bu amal qo‘llab-quvvatlanmaydi. | Действие не поддерживается. | This action is not supported. |
| `error.not_acceptable` | Javob formatini berib bo‘lmaydi. | Невозможно вернуть ответ в этом формате. | The response format is not available. |
| `error.unsupported_media_type` | Ma’lumot formati noto‘g‘ri. | Неверный формат данных. | Unsupported data format. |
| `error.payload_too_large` | Fayl juda katta. | Файл слишком большой. | The file is too large. |
| `error.device_required` | Qurilma ma’lumotlari yuborilmadi. Ilovani yangilang. | Не переданы данные устройства. Обновите приложение. | Device information is missing. Please update the app. |
| `error.app_update_required` | Ilovaning yangi versiyasini o‘rnating. | Установите новую версию приложения. | Please install the latest version of the app. |
| `error.idempotency_key_required` | Idempotency-Key sarlavhasi kerak. | Нужен заголовок Idempotency-Key. | The Idempotency-Key header is required. |
| `error.idempotency_key_reused` | Bu kalit boshqa so‘rov uchun ishlatilgan. | Этот ключ уже использован для другого запроса. | This key was used for a different request. |
| `error.request_in_progress` | So‘rov bajarilmoqda. Birozdan so‘ng qayta urinib ko‘ring. | Запрос выполняется. Повторите чуть позже. | The request is in progress. Please try again shortly. |
| `error.throttled` | Juda ko‘p urinish. {n} soniyadan so‘ng qayta urinib ko‘ring. | Слишком много попыток. Повторите через {n} с. | Too many attempts. Try again in {n} s. |
| `error.server_error` | Kutilmagan xato. | Непредвиденная ошибка. | Unexpected error. |
| `error.provider_unavailable` | Tashqi xizmat javob bermadi. Qayta urinib ko‘ring. | Внешний сервис не ответил. Повторите попытку. | An external service did not respond. Please try again. |
| `error.conflict` | Amalni bajarib bo‘lmadi. Ma’lumotlarni yangilang. | Не удалось выполнить действие. Обновите данные. | The action could not be completed. Please refresh. |
| `error.otp_resend_too_soon` | Yangi kodni {n} soniyadan so‘ng so‘rashingiz mumkin. | Новый код можно запросить через {n} с. | You can request a new code in {n} s. |
| `error.otp_daily_limit` | Bugungi SMS kodlar limiti tugadi. Ertaga qayta urinib ko‘ring. | Лимит SMS-кодов на сегодня исчерпан. Попробуйте завтра. | Today's SMS code limit is reached. Please try tomorrow. |
| `error.otp_locked` | Urinishlar soni tugadi. {m} daqiqadan so‘ng qayta urinib ko‘ring. | Попытки закончились. Повторите через {m} мин. | No attempts left. Try again in {m} min. |
| `error.otp_invalid` | Kod noto‘g‘ri. Yana {n} marta urinishingiz mumkin. | Неверный код. Осталось попыток: {n}. | Wrong code. {n} attempts left. |
| `error.otp_expired` | Kod muddati tugagan. Yangi kod oling. | Срок действия кода истёк. Получите новый код. | The code has expired. Please request a new one. |
| `error.verification_invalid` | Tasdiqlash muddati tugagan. Kodni qaytadan oling. | Подтверждение истекло. Получите код заново. | Verification has expired. Please request a new code. |
| `error.phone_registered` | Bu raqam ro‘yxatdan o‘tgan. Hisobingizga kiring. | Этот номер уже зарегистрирован. Войдите в аккаунт. | This number is already registered. Please sign in. |
| `error.phone_not_registered` | Bu raqam bilan hisob topilmadi. Ro‘yxatdan o‘ting. | Аккаунт с этим номером не найден. Зарегистрируйтесь. | No account uses this number. Please sign up. |
| `error.social_token_invalid` | Kirish amalga oshmadi. Qayta urinib ko‘ring. | Не удалось войти. Попробуйте ещё раз. | Sign-in failed. Please try again. |
| `error.social_ticket_invalid` | Ijtimoiy tarmoq orqali kirish muddati tugadi. Qaytadan boshlang. | Время входа через соцсеть истекло. Начните заново. | Social sign-in has expired. Please start again. |
| `error.social_provider_linked` | Bu hisobga boshqa {provider} profili bog‘langan. | К этому аккаунту уже привязан другой профиль {provider}. | Another {provider} profile is already linked to this account. |
| `error.social_identity_linked` | Bu {provider} profili boshqa hisobga bog‘langan. | Этот профиль {provider} привязан к другому аккаунту. | This {provider} profile is linked to another account. |
| `error.terms_update_required` | Foydalanish shartlarining yangi tahririni qabul qiling. | Примите новую редакцию условий использования. | Please accept the updated terms of use. |
| `error.address_limit` | Ko‘pi bilan {limit} ta manzil saqlash mumkin. | Можно сохранить не более {limit} адресов. | You can save up to {limit} addresses. |
| `error.product_unavailable` | Mahsulot hozir sotuvda yo‘q. | Товар сейчас недоступен. | This product is not available right now. |
| `error.out_of_stock_one` | Bu mahsulot qolmagan. | Этот товар закончился. | This product is out of stock. |
| `error.OUT_OF_STOCK` | Ba’zi mahsulotlar yetarli emas. | Некоторых товаров недостаточно. | Some products are not available in the requested quantity. |
| `error.cart_full` | Savatga ko‘pi bilan {limit} xil mahsulot qo‘shish mumkin. | В корзину можно добавить не более {limit} разных товаров. | You can add up to {limit} different products to the cart. |
| `error.cart_empty` | Savat bo‘sh. | Корзина пуста. | Your cart is empty. |
| `error.delivery_unavailable` | Bu hududga yetkazib berish mavjud emas. | Доставка в этот регион недоступна. | Delivery to this area is not available. |
| `error.open_order_exists` | Sizda to‘lanmagan buyurtma bor (№{order_id}). Avval uni to‘lang yoki bekor qiling. | У вас есть неоплаченный заказ (№{order_id}). Оплатите или отмените его. | You have an unpaid order (№{order_id}). Please pay or cancel it first. |
| `error.price_changed` | Narxlar yangilandi. Buyurtmani qayta tekshiring. | Цены обновились. Проверьте заказ ещё раз. | Prices have changed. Please review your order. |
| `error.already_paid` | Buyurtma allaqachon to‘langan. | Заказ уже оплачен. | This order is already paid. |
| `error.not_payable` | Bu buyurtmani to‘lab bo‘lmaydi. | Этот заказ нельзя оплатить. | This order cannot be paid. |
| `error.order_expired` | To‘lov muddati tugadi. Buyurtmani qaytadan rasmiylashtiring. | Время на оплату истекло. Оформите заказ заново. | The payment time has expired. Please place the order again. |
| `error.payment_in_progress` | To‘lov {provider} ilovasida jarayonda. Uni yakunlang yoki biroz kuting. | Оплата в {provider} уже идёт. Завершите её или подождите. | A {provider} payment is in progress. Please finish it or wait. |
| `error.order_not_cancellable` | Bu buyurtmani ilovadan bekor qilib bo‘lmaydi. Sotuvchi bilan bog‘laning. | Этот заказ нельзя отменить в приложении. Свяжитесь с продавцом. | This order cannot be cancelled in the app. Please contact the seller. |
| `error.booking_limit` | Sizda {limit} ta faol qabul bor. Yangisini olishdan oldin birini bekor qiling. | У вас {limit} активных записи. Отмените одну, чтобы записаться снова. | You have {limit} active appointments. Cancel one to book another. |
| `error.slot_taken` | Bu vaqt band bo‘lib qoldi. Boshqa vaqtni tanlang. | Это время уже занято. Выберите другое. | This time has just been taken. Please choose another. |
| `error.appointment_not_cancellable` | Bu qabulni bekor qilib bo‘lmaydi. | Эту запись нельзя отменить. | This appointment cannot be cancelled. |
| `error.qr_invalid` | QR kod yaroqsiz yoki muddati o‘tgan. Mijozdan ilovada QR kodni yangilashni so‘rang. | QR-код недействителен или устарел. Попросите клиента обновить его в приложении. | The QR code is invalid or expired. Ask the customer to refresh it in the app. |

### 5.2 Field codes (`field.*`)

| Key | uz | ru | en |
|---|---|---|---|
| `field.required` / `field.null` | Majburiy maydon. | Обязательное поле. | This field is required. |
| `field.blank` | Maydonni to‘ldiring. | Заполните поле. | Please fill in this field. |
| `field.invalid` | Noto‘g‘ri qiymat. | Неверное значение. | Invalid value. |
| `field.invalid_choice` | Ruxsat etilmagan qiymat. | Недопустимое значение. | This value is not allowed. |
| `field.min_length` / `field.max_length` | Kamida {limit} ta belgi kiriting. / {limit} belgidan oshmasin. | Минимум {limit} символов. / Не более {limit} символов. | At least {limit} characters. / At most {limit} characters. |
| `field.min_value` / `field.max_value` | Qiymat {limit} dan kam bo‘lmasin. / Qiymat {limit} dan oshmasin. | Не меньше {limit}. / Не больше {limit}. | At least {limit}. / At most {limit}. |
| `field.max_decimal_places` | Ko‘pi bilan 2 ta kasr raqam. | Не более 2 знаков после запятой. | At most 2 decimal places. |
| `field.unique` | Bu qiymat band. | Это значение уже занято. | This value is already taken. |
| `field.does_not_exist` | Topilmadi. | Не найдено. | Not found. |
| `field.read_only` | Bu maydonni o‘zgartirib bo‘lmaydi. | Это поле нельзя изменить. | This field cannot be changed. |
| `field.file_too_large` | Rasm 5 MB dan oshmasin. | Изображение не больше 5 МБ. | The image must be at most 5 MB. |
| `field.unsupported_image` | Faqat JPEG, PNG yoki WebP rasm. | Только изображения JPEG, PNG или WebP. | JPEG, PNG or WebP images only. |
| `field.max_items` | Ko‘pi bilan {limit} ta. | Не более {limit}. | At most {limit}. |
| `field.mismatch` | Ro‘yxat joriy elementlarga mos emas. | Список не совпадает с текущими элементами. | The list does not match the current items. |
| `field.phone_invalid` | Telefon raqamini to‘g‘ri kiriting. | Введите корректный номер телефона. | Please enter a valid phone number. |
| `field.otp_format` | 6 xonali kodni kiriting. | Введите 6-значный код. | Enter the 6-digit code. |
| `field.social_ticket_required` | Ijtimoiy tarmoq orqali kirishni qaytadan boshlang. | Начните вход через соцсеть заново. | Please start social sign-in again. |
| `field.name_min` | Ismingizni kiriting (kamida 2 belgi). | Введите имя (минимум 2 символа). | Enter your name (at least 2 characters). |
| `field.password_too_short` | Parol kamida {limit} ta belgidan iborat bo‘lsin. | Пароль должен быть не короче {limit} символов. | The password must be at least {limit} characters. |
| `field.password_too_common` | Bu parol juda oddiy. | Этот пароль слишком простой. | This password is too common. |
| `field.password_entirely_numeric` | Parol faqat raqamlardan iborat bo‘lmasin. | Пароль не должен состоять только из цифр. | The password cannot be only digits. |
| `field.password_too_similar` | Parol telefon raqamingizga juda o‘xshash. | Пароль слишком похож на номер телефона. | The password is too similar to your phone number. |
| `field.password_unchanged` | Yangi parol eskisidan farq qilsin. | Новый пароль должен отличаться от старого. | The new password must differ from the old one. |
| `field.password_incorrect` | Parol noto‘g‘ri. | Неверный пароль. | Wrong password. |
| `field.terms_outdated` | Foydalanish shartlarining yangi tahririni qabul qiling. | Примите новую редакцию условий использования. | Please accept the updated terms of use. |
| `field.push_token_missing` | Bildirishnomalarni yoqish uchun ilovaga ruxsat bering. | Разрешите приложению уведомления, чтобы их включить. | Allow notifications for the app to turn them on. |
| `field.address_text_min` | Manzilni to‘liq kiriting. | Введите полный адрес. | Please enter the full address. |
| `field.latlng_pair` | Joylashuvni qayta tanlang. | Выберите местоположение заново. | Please choose the location again. |
| `field.address_required` | Yetkazib berish manzilini tanlang. | Выберите адрес доставки. | Choose a delivery address. |
| `field.address_not_found` | Manzil topilmadi. | Адрес не найден. | Address not found. |
| `field.address_incomplete` | Manzilda viloyat va tumanni tanlang. | Укажите в адресе область и район. | Choose the region and district in the address. |
| `field.cashback_insufficient` | Cashback balansi yetarli emas (mavjud: {available} so‘m). | Недостаточно кешбэка (доступно: {available} сум). | Not enough cashback (available: {available} so'm). |
| `field.cashback_max` | Ko‘pi bilan {max} so‘m cashback ishlatish mumkin. | Можно использовать не более {max} сум кешбэка. | You can use at most {max} so'm of cashback. |
| `field.specialist_not_found` | Mutaxassis topilmadi. | Специалист не найден. | Specialist not found. |
| `field.service_not_offered` | Mutaxassis bu xizmatni ko‘rsatmaydi. | Специалист не оказывает эту услугу. | The specialist does not offer this service. |
| `field.booking_too_soon` | Kamida {n} daqiqa oldin yoziling. | Записывайтесь не позднее чем за {n} мин. | Book at least {n} minutes in advance. |
| `field.booking_horizon` | Faqat {n} kun ichidagi sanani tanlash mumkin. | Можно выбрать дату не далее {n} дней. | You can book up to {n} days ahead. |
| `field.booking_grid` | Vaqt {n} daqiqalik qadamda bo‘lsin. | Время должно быть кратно {n} минутам. | The time must be on a {n}-minute step. |
| `field.outside_working_hours` | Mutaxassis bu vaqtda ishlamaydi. | Специалист в это время не работает. | The specialist does not work at this time. |
| `field.slot_unavailable` | Bu vaqt band. | Это время занято. | This time is taken. |
| `field.own_overlap` | Sizda bu vaqtda boshqa qabul bor. | У вас уже есть запись на это время. | You already have an appointment at this time. |

### 5.3 Labels (`label.*`) and SMS (`sms.*`)

| Key | uz | ru | en |
|---|---|---|---|
| `label.order_status.awaiting_payment` | To‘lov kutilmoqda | Ожидает оплаты | Awaiting payment |
| `label.order_status.processing` | Tayyorlanmoqda | Готовится | Being prepared |
| `label.order_status.handed_to_courier` | Kuryerga topshirildi | Передан курьеру | With the courier |
| `label.order_status.delivered` | Yetkazildi | Доставлен | Delivered |
| `label.order_status_pickup.delivered` | Olib ketildi | Получен | Picked up |
| `label.order_status.cancelled` | Bekor qilindi | Отменён | Cancelled |
| `label.payment_status.pending` / `.paid` / `.failed` / `.refunded` | To‘lov kutilmoqda / To‘langan / To‘lov o‘tmadi / Qaytarilgan | Ожидает оплаты / Оплачен / Оплата не прошла / Возвращён | Awaiting payment / Paid / Payment failed / Refunded |
| `label.fulfilment.delivery` / `.pickup` | Yetkazib berish / Olib ketish | Доставка / Самовывоз | Delivery / Pickup |
| `label.cancellation.reservation_expired` / `.admin` / `.customer_request` | To‘lov muddati tugadi / Do‘kon bekor qildi / Siz bekor qildingiz | Истекло время оплаты / Отменён магазином / Отменён вами | Payment time expired / Cancelled by the shop / Cancelled by you |
| `label.refund.in_progress` / `.completed` | To‘lov qaytarilmoqda / To‘lov qaytarildi | Возврат в процессе / Деньги возвращены | Refund in progress / Refunded |
| `label.timeline.order_created` | Buyurtma berildi | Заказ оформлен | Order placed |
| `label.timeline.sub_items_total` | {item_count} ta mahsulot · {total} so‘m | {item_count} товар(ов) · {total} сум | {item_count} item(s) · {total} so'm |
| `label.timeline.payment_confirmed` | To‘lov qabul qilindi | Оплата получена | Payment received |
| `label.timeline.sub_paid_via` | {provider} orqali | через {provider} | via {provider} |
| `label.timeline.sub_cashback` | cashback +{amount} so‘m | кешбэк +{amount} сум | cashback +{amount} so'm |
| `label.timeline.payment_failed` | To‘lov amalga oshmadi | Оплата не прошла | Payment failed |
| `label.timeline.sub_payment_failed_retry` | {provider} · qayta urinib ko‘ring | {provider} · попробуйте ещё раз | {provider} · please try again |
| `label.timeline.shipment_created` | Yetkazib berishga topshirildi | Передан в доставку | Handed over for delivery |
| `label.timeline.sub_tracking` | BTS kuzatuv raqami: {tracking_number} | Трек-номер BTS: {tracking_number} | BTS tracking number: {tracking_number} |
| `label.timeline.handed_to_courier` | Kuryerga topshirildi | Передан курьеру | With the courier |
| `label.timeline.delivered` | Yetkazildi | Доставлен | Delivered |
| `label.timeline.picked_up` | Olib ketildi | Получен | Picked up |
| `label.timeline.sub_bts` | BTS | BTS | BTS |
| `label.timeline.cancelled` | Buyurtma bekor qilindi | Заказ отменён | Order cancelled |
| `label.timeline.sub_cancelled_admin` | Do‘kon tomonidan bekor qilindi | Отменён магазином | Cancelled by the shop |
| `label.timeline.sub_cancelled_customer` | Siz bekor qildingiz | Вы отменили заказ | You cancelled the order |
| `label.timeline.sub_refund_follows` | to‘lov qaytariladi | деньги будут возвращены | the payment will be refunded |
| `label.timeline.sub_expired` | To‘lov muddati tugadi | Истекло время оплаты | The payment time expired |
| `label.timeline.refund_in_progress` | To‘lov qaytarilmoqda | Возврат в процессе | Refund in progress |
| `label.timeline.refund_succeeded` | To‘lov qaytarildi | Деньги возвращены | Payment refunded |
| `label.progress.placed` / `.paid` / `.with_courier` / `.delivered` / `.picked_up` / `.cancelled` | Buyurtma berildi / To‘lov qabul qilindi / Kuryerda / Yetkazildi / Olib ketildi / Bekor qilindi | Оформлен / Оплачен / У курьера / Доставлен / Получен / Отменён | Placed / Paid / With courier / Delivered / Picked up / Cancelled |
| `label.ledger.credit` / `.debit` | Cashback hisoblandi / Cashback ishlatildi | Начислен кешбэк / Списан кешбэк | Cashback earned / Cashback used |
| `label.ledger.reversal_back` / `.reversal_taken` | Cashback qaytarildi / Cashback bekor qilindi | Кешбэк возвращён / Кешбэк отменён | Cashback returned / Cashback reversed |
| `label.ledger.adjust_add` / `.adjust_subtract` | Bonus qo‘shildi / Balans tuzatildi | Начислен бонус / Баланс скорректирован | Bonus added / Balance corrected |
| `label.ledger.sub_order_percent` | Buyurtma №{order_id} · {percent}% | Заказ №{order_id} · {percent}% | Order №{order_id} · {percent}% |
| `label.ledger.sub_store_percent` | Do‘kondagi xarid · {percent}% | Покупка в магазине · {percent}% | In-store purchase · {percent}% |
| `label.ledger.sub_order` | Buyurtma №{order_id} | Заказ №{order_id} | Order №{order_id} |
| `label.ledger.sub_order_cancelled` | Buyurtma №{order_id} bekor qilindi | Заказ №{order_id} отменён | Order №{order_id} cancelled |
| `label.ledger.sub_store_returned` | Do‘kondagi xarid qaytarildi | Покупка в магазине возвращена | In-store purchase returned |
| `label.cashback.promo_rule` | Aksiyadagi mahsulotlarga cashback berilmaydi. | На акционные товары кешбэк не начисляется. | Promotional products do not earn cashback. |
| `label.appointment_status.requested` / `.confirmed` / `.rejected` / `.attended` / `.cancelled` | Tasdiqlanishi kutilmoqda / Tasdiqlandi / Rad etildi / Qabul bo‘lib o‘tdi / Bekor qilindi | Ожидает подтверждения / Подтверждена / Отклонена / Состоялась / Отменена | Awaiting confirmation / Confirmed / Declined / Completed / Cancelled |
| `label.role.dermatologist` / `.cosmetologist` | Dermatolog / Kosmetolog | Дерматолог / Косметолог | Dermatologist / Cosmetologist |
| `sms.otp` | Glow Derma: tasdiqlash kodingiz {code}. Kodni hech kimga bermang. | Glow Derma: ваш код подтверждения {code}. Никому его не сообщайте. | Glow Derma: your verification code is {code}. Do not share it with anyone. |

---

## 6. Security notes

1. **Ownership on every resource.** Every **C** query starts from `request.user` (the Customer): addresses, cart,
   orders, payments, appointments, inbox, ledger, holds. Ids of other customers answer 404 (never 403). A contract test
   walks every **C** route with a second customer's ids. Webhooks bind each callback to the order/payment it names
   (`merchant_trans_id` + `merchant_prepare_id` + `click_trans_id`; Payme `account.order_id` + `params.id`).
2. **Audience separation.** Customer tokens carry `aud="customer"`; staff tokens `aud="staff"`; each authentication
   class pins its audience, so neither token works on the other API. The URL-walk test (spec) covers every customer
   route with anonymous, staff-token, Basic and Django-session requests; POS routes with a customer token; webhook
   routes with tokens (ignored) and bad signatures.
3. **Rate limits** (§1.8) on SMS, verification, login, refresh, social, checkout, pay, POS and anonymous reads; OTP day
   caps and lockouts are derived from `SmsCode` rows so a cache flush does not reset them.
4. **OTP storage.** Codes only as `HMAC-SHA256(code, SMS_CODE_HMAC_KEY)` (key ≥32 bytes, required in production),
   compared with `compare_digest`; 120 s TTL; 5 attempts per code; resend ≥60 s; verification tokens are 256-bit,
   stored as SHA-256, single use, bound to (phone, purpose), 10 min. Codes and tokens are never logged (logs carry
   `phone_hash` = SHA-256 of the phone). `SmsCode` rows are deleted after 24 h (`accounts.cleanup_auth`).
5. **Passwords:** Argon2 (`PASSWORD_HASHERS`), checked for unknown phones too (timing), min 8 + Django validators,
   never returned or logged; reset revokes all sessions.
6. **Token storage on the device.** Refresh token: iOS Keychain (`kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly`,
   no iCloud sync) / Android Keystore-backed EncryptedSharedPreferences; access token in memory; both deleted on any
   `SESSION_*`, on logout and on a 401 from `auth/refresh/`. The QR token is never persisted and the QR screen is
   excluded from screenshots/recents where the OS allows (`FLAG_SECURE` on Android). Server side the only raw refresh
   token ever stored is the 30-second rotation-grace cache entry in Redis.
7. **Refresh rotation** with reuse detection (§2.1.6): a stolen refresh token used after the legitimate app rotated it
   kills the session for both parties.
8. **Device binding.** `X-GD-Device-Id`/`X-GD-Platform` must match the session's device; a token copied to another
   install is refused (§1.2). The fid is not a secret; the binding raises the bar, the bearer token remains the secret.
9. **PII in pushes (C1).** FCM is a foreign service: titles/bodies/data carry no names, phones, addresses or amounts —
   ids and order numbers only (§2.14.2). The same holds for logs shipped off-host and error tracking (scrubbed).
10. **Consent (C3).** `terms_version`, `consent_at`, `consent_ip` at registration, social completion and every
    re-acceptance; checkout and booking refuse an outdated consent (§2.2.4). The biometric consent of the Phase-2 AI
    feature is separate and not part of M2.
11. **Payments.** Card data never reaches the backend (provider pages). Click MD5 over raw strings + `service_id`
    binding + amount equality + `merchant_prepare_id` binding; Payme Basic auth per mode, amount equality in tiyin, one
    active transaction per order (partial unique index + -31008), 12-hour timeout; IP allow-lists are defence in depth
    only. Captures on non-payable orders are refused; a late capture that lands is refunded automatically and alerted.
    Every callback is stored (WebhookInbox, append-only, 180-day retention, C4) with credentials stripped.
12. **Financial invariants** (spec): money as integer so'm (tiyin only inside the Payme adapter); lock order Order →
    Payment → LoyaltyAccount; the balance changes only in `loyalty.services.post_entry`; cashback holds are checked
    under the account lock; stock is re-checked under `ProductStock` row locks; checkout, pay, cancel and booking are
    idempotent; outbox handlers are idempotent at the provider boundary.
13. **POS minimisation** (§2.10): hashed per-device keys, single-use QR via `GETDEL`, one error for every failure, no
    name or history in the response, every resolve audited.
14. **Fake/dev switches** (`ESKIZ_MODE=fake`, `FAKE_SCENARIO_HEADER_ENABLED`, `FAKE_PAYMENTS_ENABLED`, `*_MODE=fake`)
    are refused by `config/settings/production.py`.
15. **Enumeration.** Customer ids in URLs are sequential but always ownership-checked; phone existence is revealed only
    by the register/reset branches of `send-code` under SMS throttles (§7 `M2-A9`); POS failures reveal nothing.
16. **Uploads** (`me/image/`) go through `core.images.process_image` (re-encode, EXIF strip, 5 MiB, type sniffing).

---

## 7. Cross-references: open questions and provisional defaults

Existing points this contract implements (`docs/decisions/open-questions.md`):

| # | Provisional default used here |
|---|---|
| A1 | Delete scrubs and deactivates; the phone stays unique; re-registration reactivates the same row with balance and purchases (§2.2.3, §2.1.3) |
| A2 | Password login; SMS code for sign-up, reset and **password** login on an unknown device (§2.1.4) |
| A3 | 5 attempts per code, 15-minute lockout, 10 codes per phone per day (`CUSTOMER_OTP_*`) |
| A6 | Social sign-in allowed; the first sign-in verifies a phone by SMS and links to that phone's customer (§2.1.8–9) |
| A7 | Google iOS/Android client ids and the Facebook app are needed from the client; fake providers until then (`GOOGLE_APP_CLIENT_IDS`) |
| O1 / O5 | Admin cancel rules and refund display unchanged; the customer sees "To‘lov qaytarilmoqda" until `succeeded` |
| O3 | Reservation 15 minutes (`SiteSettings.reservation_minutes`), extended only by the in-flight payment grace (`M2-P8`) |
| O4 | Pickup reuses `delivered`, labelled "Olib ketildi" for customers |
| O7 | Only Click and Payme; `payment_providers` in `settings/public/` keeps the app generic |
| L1–L5 | Engine defaults unchanged (rate before purchase, cashback-paid part does not earn, seed tiers, spendable immediately, negative balance allowed and shown as `negative`) |
| L6 / L7 | No POS redemption; how a resolved in-store sale is discovered stays open (M3); M2 only resolves and audits |
| P1 | A `requested` appointment holds the slot (exclusion constraint) |
| P2 / P3 | No deposits; cancellation terms are free text (`booking_policy`), enforced only by `BOOKING_CANCEL_MIN_LEAD_MINUTES` (0) |
| C-4 | Cashback never pays the delivery fee |
| C-6 | Customer cancel only from `awaiting_payment` |
| T5 | WebhookInbox written after processing in the same transaction; replay rules per provider in §2.13.0 |

New M2 points (the plan adds them to `open-questions.md`; each default is implement-ready):

| # | Question for the client | Provisional default |
|---|---|---|
| M2-A9 | May the app reveal whether a phone is registered (register / reset flows)? | Yes, throttled (`phone_registered` / `phone_not_registered`) |
| M2-A10 | Does a social sign-in on a new phone also need the SMS code? | No; only password sign-in on an unknown device does |
| M2-A11 | Customer password rule | ≥8 characters, not only digits, not common, not similar to the phone |
| M2-A12 | Deleting an account: confirmation and open items | Password required when one is set; unpaid orders and open appointments are cancelled; paid orders continue |
| M2-A13 | Minimum app version enforcement | Off (`MIN_APP_VERSION_*=0.0.0`) until the first store release |
| M2-C12 | Show "price changed since you added it" in the cart | Yes — needs `CartItem.price_at_add` (the only migration of M2) |
| M2-C13 | Several unpaid orders at once? | No: one `awaiting_payment` order per customer (`CHECKOUT_MAX_OPEN_ORDERS=1`) |
| M2-C14 | Minimum amount paid by card when cashback is used | 1 000 so'm (`CHECKOUT_MIN_PAYABLE`), so cashback can never make an order free |
| M2-C15 | Per-line quantity ceiling (C-2) and distinct lines | 10 per line ("contact the seller" above it), 30 lines |
| M2-O9 | Parcel weight for the BTS tariff | 1 000 g per order (`BTS_DEFAULT_PARCEL_WEIGHT_G`); product weights not modelled |
| M2-O10 | Pickup "ready to collect" notification | None in M2 (no status for it); staff mark pickup orders delivered |
| M2-P5 | Payme fiscal receipt data (IKPU / package codes per product) | Not sent (`PAYME_FISCAL_DETAIL_ENABLED=false`); enabling needs product fiscal codes (a catalog migration) |
| M2-P6 | Payme `ChangePassword` | Not supported (-32601); keys rotate through env |
| M2-P7 | Click Complete for an expired order | Answer -9 and rely on Click cancelling the payment (`CLICK_REVERSES_ON_COMPLETE_ERROR=true`); the late-capture refund path exists for `false` |
| M2-P8 | Grace for a payment already started at the provider when the reservation ends | 5 minutes (`PAYMENT_IN_FLIGHT_GRACE_SECONDS=300`) |
| M2-L9 | What the cashier sees after scanning | Phone (full for Billz, masked for display), available cashback, tier percent; no name |
| M2-L10 | QR refresh | New token every 50 s; older tokens stay valid until their 60 s TTL; each token works once |
| M2-B1 | Booking limits | ≥60 min ahead, ≤30 days ahead, ≤3 open appointments, cancellation any time before the start |
| M2-N1 | Show the admin's adjustment reason to the customer? | No (generic "Bonus qo‘shildi" / "Balans tuzatildi") |
| M2-N2 | Push for a customer's own order cancellation | Yes (state machine row 5 pushes `cancelled`) |
| M2-T1 | Terms not yet versioned in settings | Current version reads `"0"` until staff set `terms_version` |
