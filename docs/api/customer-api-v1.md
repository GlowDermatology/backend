# Glow Derma customer API v1 (M2 contract)

| | |
|---|---|
| Version | 1.5.0-draft (2026-10-27, B-W1 web client: `X-GD-Platform: web`, several sessions at once and the session list §1.9/§2.2.10, `PAYMENT_WEB_RETURN_URL`, website OAuth client ids, slugs and SEO texts §2.3/§2.11) on top of 1.4.0-draft (2026-10-26, review revision 4 on top of revisions 2–3 and the product-owner decisions of 2026-10-26), milestone M2: the iOS/Android customer app, provider callbacks and the cashier QR endpoint |
| Implemented by | the Django 6.1 / DRF 3.18 backend (Codex, packages P0–P3 of `docs/plans/2026-10-26-m2-customer-api.md`); consumed by the native iOS and Android apps (not in this repo) |
| Source of truth | `docs/specs/2026-10-24-backend-foundation-design.md` (Auth and request security → Customer (M2); Financial integrity; Milestones → M2; Provisional business rules). If this file and the spec disagree, the spec wins and this file gets fixed. |
| Shared rules | `docs/api/admin-api-v1.md` §1 (envelope, pagination, idempotency, values), §3 (enums), §4 (OrderEvent types), §5.8 (Uzbek labels). This file **references** them and lists only what is different for the customer audience. |
| Open business rules | `docs/decisions/open-questions.md` (A1–A8, O1–O8, L1–L8, P1–P4, C1–C4, T1–T19) plus the new M2 points in §7 (`M2-*`) |
| Out of scope (Phase 2) | The AI features of Backend PDF §12 (`POST /ai/analyze`, `POST /ai/chat`), their biometric consent and error codes are **not** part of v1; no route is mounted. Live Eskiz/Click/Payme/BTS/FCM adapters and reconciliation are M3; the Billz live adapter (M3) is built from docs.billz.io (`openapi.yaml` is the source of truth), and what it does not cover stays an explicit stub (§2.10). Fake adapters may run under the production settings (product-owner decision 2026-10-26); what that posture allows is §6.14. |
| Enforced by | drf-spectacular (`openapi/schema.yaml`, `--validate --fail-on-warn`), the URL-walk auth matrix, contract tests per endpoint, the offline Click/Payme sandbox suites (plan §5) |

Examples use `jsonc`: `//` comments give types and nullability and are not part of the payload. Example values are
illustrative unless a test vector says so. Provider protocol details marked **(verify M3)** come from the public
provider documentation and must be confirmed against the provider sandboxes in M3 before going live; the fake
adapters implement exactly what this file says, and every such choice sits behind a named protocol constant so M3
can switch it without a contract rewrite.

---

## 1. Conventions (customer audience)

Everything in admin §1 applies unless a row below says otherwise.

### 1.1 Base, routing, network

| Prefix | Audience | Auth | Notes |
|---|---|---|---|
| `/api/v1/auth/…` | app, before sign-in | none (refresh token in the body where needed) | device headers required (§1.2) |
| `/api/v1/me/…`, `/api/v1/cart/…`, `/api/v1/orders/…`, `/api/v1/appointments/…`, `/api/v1/delivery/calculate/` | signed-in app | **C**: customer access JWT required | device headers required |
| `/api/v1/catalog/…`, `/api/v1/settings/public/`, `/api/v1/delivery/regions/`, `/api/v1/specialists/…`, `/api/v1/services/` | anyone | **O**: optional; a present bearer must be valid | device headers optional (the app always sends them) |
| `/api/v1/webhooks/{click,payme,bts}/…` | payment and delivery providers | provider signature / Basic auth / shared secret (§2.13); a provider whose mode is `fake` under the production settings starts no transaction and honours no capture (§2.13.0) | **not** the error envelope: provider-native bodies |
| `/api/v1/pos/…` | in-store cashier terminals | per-device API key (§2.10) | envelope; Uzbek messages only |
| `/api/v1/dev/…` | developers, local/test settings only | **C** | mounted only when `DEV_FEATURES_ALLOWED` (local/test settings), `FAKE_PAYMENTS_ENABLED=true` **and** `CLICK_MODE` = `PAYME_MODE` = `fake` (§2.15); settings validation refuses the flag with any live payment mode and the production settings refuse it outright |

- Every path ends with `/`. Paths in §2 are relative to `/api/v1/`.
- The customer API is reachable from the internet through the edge proxy (TLS terminates there). `AdminNetworkMiddleware`
  does not apply to these prefixes. There is no CORS, no cookie auth and no CSRF: the clients are the native apps and
  (B-W1) the **customer website**, which calls the API **server-side only** from its BFF (Next.js route handlers and
  Server Components): it sends `X-GD-Platform: web`, a long-lived browser id from an HttpOnly cookie as
  `X-GD-Device-Id`, and — like the admin BFF — `X-GD-Internal-Secret` together with `X-GD-Client-IP` (the browser's
  IP) and `X-GD-Client-UA` (the browser's `User-Agent`, B-W1); the last two count only with a valid secret
  (`core.clientip`), otherwise the request's own peer IP and `User-Agent` are used. The browser never holds a token:
  the BFF keeps the customer's access/refresh tokens and the device key in HttpOnly cookies.
- OpenAPI: one schema; operationIds are `customer_<resource>_<action>`, `webhook_<provider>_<action>`,
  `pos_<resource>_<action>`, `dev_<resource>_<action>`.
- Base views (P0, `accounts/api/customer_base.py`): `CustomerAPIView` (`CustomerJWTAuthentication` + `IsActiveCustomer`
  + `CustomerRateThrottle`, JSON parser), `PublicAPIView` (`OptionalCustomerAuthentication` + `AllowAny` +
  `PublicDeviceThrottle` + `PublicIPThrottle`), `WebhookAPIView` (no DRF auth; parsers per provider; provider-native
  responses). `PosAPIView` (`PosKeyAuthentication` + `IsActivePosDevice` + `PosRateThrottle`) lives in
  `loyalty/api/pos/base.py` (P3), because its authentication reads `loyalty.PosDevice`, which `accounts` may not
  import. The default stays `DenyAll`.

### 1.2 Request headers

| Header | Required | Rule and what the server does with it |
|---|---|---|
| `Authorization: Bearer <access JWT>` | **C** routes; optional on **O** routes; absent on `auth/*` (except `auth/logout/`, where it is optional) | HS256, `kid` header, claims `iss, aud="customer", sub (customer id), sid, rot, iat, exp, jti`, all required. `rot` = the session's rotation counter `AuthSession.rotation` (integer, `0` before the first rotation) when the token was issued (§2.1.6). TTL `CUSTOMER_ACCESS_TTL` (900 s), capped at the session's `absolute_expires_at`. A staff token fails decoding (`aud`) → 401 `not_authenticated`. On an **O** route a present but invalid header is a 401, never silently anonymous. |
| `X-GD-Device-Id` | `auth/*` and **C** routes | The Firebase installation id (`fid`) of this app install — for the website (B-W1) the BFF's random browser id kept in a long-lived HttpOnly cookie —, `^[A-Za-z0-9_-]{8,128}$`. Missing or malformed → 400 `device_required`. `auth/*`: the session is bound to `Device(customer, fid)` (created or reactivated on success). **C** routes: must equal the fid of the session's device, else 401 `not_authenticated` and audit `auth.device_mismatch` (at most one row per session per hour). The fid is an identifier, **not** a secret (§6.8). |
| `X-GD-Device-Key` | `auth/login/` when the app holds one (social sign-in never reads it: `M2-A10`) | The device secret issued once in `AuthTokens.device_key` (§1.9): exactly 43 base64url characters, `^[A-Za-z0-9_-]{43}$`, no prefix (`secrets.token_urlsafe(32)`). Proves that this install completed a verified sign-in before; it is what makes a device **known** for A2 (§2.1.4). Never logged; compared as SHA-256 with `compare_digest`. Absent, malformed, over-long or wrong → the device is treated as unknown (never a 400). |
| `X-GD-Platform` | with `X-GD-Device-Id` | `ios` \| `android` \| `web` (B-W1: the website's BFF; else 400 `device_required`). Stored on `Device.platform`; `Order.channel` is taken from the **session's device**, never from a body field. A different platform for an existing `(customer, fid)` → 401 `not_authenticated`. |
| `X-GD-App-Version` | with `X-GD-Device-Id` and `ios`/`android`; optional with `web` | `^\d{1,3}\.\d{1,3}\.\d{1,3}$`. Below `MIN_APP_VERSION_IOS` / `MIN_APP_VERSION_ANDROID` (default `0.0.0` = no floor) → 426 `app_update_required` with `details: {"min_version", "store_url"}`. **`web` (B-W1):** optional — absent is fine (stored as `""`), a present value must still match the pattern (else 400 `device_required`), and no minimum version, store URL or 426 ever applies (the website deploys with the API). Written to `Device.app_version` together with the `last_active_at` bump (at most every 5 minutes). |
| `Accept-Language` | optional | First supported tag in q-order among `uz`, `ru`, `en` (region subtags ignored: `ru-RU` → `ru`). Absent or unsupported → the signed-in customer's `language`, else `uz`. Selects every `message`, every server-rendered label and every i18n data field (§1.4). Response header `Content-Language`. |
| `Idempotency-Key` | on the **I** routes (§1.7) | UUID, 36 chars, as admin §1.7. |
| `Content-Type` | on bodies | `application/json`; `multipart/form-data` only on `PUT me/image/`; Click webhooks `application/x-www-form-urlencoded` only (§2.13.1). Anything else → 415 (Click: error `-8`). |
| `X-Request-ID` | ignored from the app | Accepted only with the internal secret (admin §1.2), which the app never has. The server always generates one; the app shows it on error screens ("Xato kodi"). |
| `X-GD-Client-UA` | website BFF only (B-W1) | The browser's `User-Agent`, honoured only with a valid `X-GD-Internal-Secret` (like `X-GD-Client-IP`); printable characters, ≤512. Names the session in the session list (§2.2.10); never used for any decision. |
| `X-GD-Fake-Scenario` | local/test settings only | JSON object copied into `Order.metadata["fake"]` at checkout (BTS/Click/Payme switches of plan §6). Honoured only when `DEV_FEATURES_ALLOWED` (local/test settings), `FAKE_SCENARIO_HEADER_ENABLED=true` **and** every provider mode (`ESKIZ_MODE`, `CLICK_MODE`, `PAYME_MODE`, `BTS_MODE`, `FCM_MODE`) is `fake`; settings validation refuses the flag otherwise and the production settings refuse it outright (fake providers in production never read a scenario, §6.14). |

**401 codes and the app's reaction** (customer refresh tokens rotate, so a refresh failure always ends the session):

| `code` | Cause | App reaction |
|---|---|---|
| `not_authenticated` | Header missing or malformed; bad signature, `aud` or `iss`; access JWT expired; device id/platform mismatch; unknown refresh token on `auth/refresh/` | Refresh once and retry, **single-flight**: the app runs at most one `auth/refresh/` at a time; every request that failed with an access token older than the stored one, or while a refresh is running, waits for it and retries with the newer stored tokens instead of refreshing again; the new refresh token is persisted (Keychain/Keystore, atomically replacing the old one) before the old one is discarded and before any retry. Any 401 from `auth/refresh/` → delete tokens, go to sign-in. |
| `SESSION_EXPIRED` | Session revoked (`logout` — also signed out from the session list of another device, `deactivated`, `refresh_reused`, `expired`, `admin_revoked`, `replaced` — the same install signed in again, or another account took its push token) or past `absolute_expires_at` (60 days idle); customer deleted; unknown/revoked refresh on `auth/logout/` | Delete tokens; sign-in screen with the phone pre-filled; no message. |
| `password_changed` | Session revoked with reason `password_changed`: the password was reset (`auth/reset-password/`) or changed (`me/password/`) on another device. **Only** this cause. | Delete tokens; show "Parolingiz o‘zgartirildi. Qayta kiring. Bu siz bo‘lmasangiz, sotuvchi bilan bog‘laning." (`error.password_changed`) and the sign-in screen. |
| ~~`SESSION_REPLACED`~~ | **Retired (B-W1).** A sign-in on another device no longer ends a session (§1.9), so the customer audience never answers it; the code and its catalog text stay reserved (old builds may still branch on it). | — |
| `invalid_credentials` | `auth/login/` only | Form error. |

### 1.3 Response headers
As admin §1.3, plus `Content-Language` (always) and `Retry-After` on every 429 (including the OTP codes), on 503
`sms_unavailable` and on 409 `request_in_progress`. `Cache-Control: no-store` on every response except **O**
catalog/settings GETs:
- **anonymous** request (no `Authorization` header): `Cache-Control: public, max-age=60` (banners, categories,
  settings, regions) or `public, max-age=30` (products), `Vary: Accept-Language`;
- request **with** `Authorization`: `Cache-Control: private, no-cache` and `Vary: Accept-Language, Authorization`
  (product responses carry the personalised `in_cart_quantity` and live stock, so no shared cache and no unvalidated
  reuse by URLCache/OkHttp may serve them).

### 1.4 Values
Admin §1.4 applies (snake_case, integer ids, money as integer so'm, ISO 8601 `+05:00`, percent decimal strings with
2 dp, phone E.164 `^\+998\d{9}$`, nulls never omitted, absolute media URLs). Differences:

| Kind | Customer rule | Example |
|---|---|---|
| i18n data fields | Rendered as **one string** in the resolved language (§1.2), not `{uz, ru, en}`. Fallback chain: requested language → `uz` → `""`. A product `name` falls back further to `billz_name`. Admin objects keep the three-language shape. | `"name": "Сыворотка с гиалуроновой кислотой"` |
| Labels | Every enum that the app shows comes with a server-rendered `<field>_label` in the resolved language (§5). The enum literal stays authoritative; the label is presentation. | `"status": "processing", "status_label": "Tayyorlanmoqda"` |
| Phone mask | `+998 XX *** ** NN` | `"+998 90 *** ** 67"` |
| Email mask | first character, `***`, domain | `"m***@gmail.com"` |
| Stock | `available` = max(0, Billz quantity − active reservations − converted-since-sync), the B5 `commerce.selectors.available_stock`, read through `commerce.selectors.customer_available_stock`; **0 for every product while Billz is fake under the production settings** (online selling needs live Billz stock, §2.3) | `7` |
| Coordinates | JSON numbers, ≤6 dp | `41.311081` |
| Time of day | `"HH:MM"` Tashkent | `"09:30"` |
| Payment account reference | `"<order_id>-<ref>"`, `ref` = first 10 lower-case hex chars of `HMAC-SHA256(PAYMENT_REF_KEY, str(order_id))` (§2.7.2) | `"10248-3f9a0c1b2d"` |

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
- Top-level code casing: snake_case, except the PDF literals `SESSION_EXPIRED`, `SESSION_REPLACED` (retired for
  customers, B-W1) and `OUT_OF_STOCK`.
- The app branches on `code` only, never on `message` (each situation the app must react to differently has its own
  code).

Codes added for this audience (admin §1.6 codes keep their meaning):

| HTTP | `code` | When | `details` |
|---|---|---|---|
| 400 | `device_required` | Device headers missing or malformed (§1.2) | null |
| 400 | `otp_invalid` | Wrong SMS code (§2.1.2) | `{"attempts_left": int}` |
| 400 | `otp_expired` | No live code for (phone, purpose) | null |
| 400 | `verification_invalid` | `verification_token` unknown, expired, used, or bound to another phone/purpose/ticket | null |
| 400 | `social_token_invalid` | Provider token rejected, nonce missing/unknown/used, or token replayed (§2.1.8) | null |
| 400 | `social_ticket_invalid` | `social_ticket` unknown, expired, used or issued to another device | null |
| 401 | `password_changed` | The session was revoked because the password was reset or changed on another device (§1.2, §2.1.5) | null |
| 409 | `phone_registered` · `phone_not_registered` | Purpose does not fit the phone (§2.1.1) | null |
| 409 | `social_provider_linked` · `social_identity_linked` | Link refused (§2.1.9, §2.2.8) | `{"provider": str}` |
| 409 | `last_sign_in_method` | Unlinking the only way to sign in (§2.2.8) | `{"provider": str}` |
| 409 | `terms_update_required` | Consent older than the current terms (§2.2.4) on checkout and booking | `{"current_version": str}` |
| 409 | `OUT_OF_STOCK` | Cart add / checkout stock refusal | cart add: `{"product_id", "available": 0}`; checkout: `{"lines": [...]}` |
| 409 | `product_unavailable` · `cart_full` · `cart_empty` | Cart and checkout (§2.5, §2.7) | `cart_full`: `{"max_lines": int}` |
| 409 | `price_changed` | `expected_total` differs (§2.7.1) | `{"total", "items_subtotal", "delivery_fee", "cashback_used", "cart": Cart}` |
| 409 | `open_order_exists` | An unpaid order already exists (§2.7.1) | `{"order_id": int, "pay_by": datetime}` |
| 409 | `delivery_unavailable` | BTS does not deliver to the address, or delivery is not offered at all (§2.6: fake BTS under the production settings) | null |
| 409 | `online_sales_unavailable` | Online selling is off because the stock source is not live (§2.3: fake Billz under the production settings): cart add/patch and checkout | null |
| 409 | `not_payable` · `order_expired` · `already_paid` | `orders/{id}/pay/` refusals (§2.7.2) | null |
| 409 | `payment_in_progress` | A provider transaction of this order is in flight (§2.7.2): pay with the other provider, customer cancel, `DELETE me/` | `{"provider": str, "order_id": int}` |
| 409 | `not_cancellable` | Order or appointment cancel refused (catalog key per resource, §5.0) | null |
| 409 | `invalid_transition` | A state machine refuses a transition that a reused B5/B6 service checks itself (admin §1.6 code; rare for customers, e.g. a concurrent admin change) | null |
| 409 | `slot_taken` | Booking race lost (§2.11) | null |
| 409 | `conflict` | Other state conflicts (generic) | null |
| 409 | `booking_limit` · `address_limit` | Count limits | `{"limit": int}` |
| 409 | `qr_invalid` | POS: token unknown, expired, used or of a non-active customer (one code for all, no oracle) | null |
| 426 | `app_update_required` | App below the minimum version | `{"min_version": str, "store_url": str}` |
| 429 | `otp_resend_too_soon` · `otp_daily_limit` · `otp_network_limit` · `otp_locked` | OTP limits (§2.1.1) | `{"retry_after": int}` (seconds; also `Retry-After`) |
| 503 | `sms_unavailable` | The global SMS budget tripped (§2.1.1) | `{"retry_after": int}` |
| 503 | `terms_not_published` | Registration while the terms are not published (§2.1.3) | null |

Generic field codes (admin §1.6 table) render their messages in the resolved language (catalog keys `field.<code>`).
Endpoint-specific field codes are listed in each endpoint table and registered in
`core.api.exceptions.ENDPOINT_FIELD_CODES` by P0. Which catalog key each code renders is §5.0.

### 1.7 Idempotency
Mechanism, fingerprint, storage and responses as admin §1.7 (`core.idempotency.idempotent`). Differences:
- **Scope:** `(audience "customer", actor = customer id, method, resolved path, key)`.
- **Required** (400 `idempotency_key_required` without it): `POST orders/checkout/`, `POST orders/{id}/pay/`,
  `POST orders/{id}/cancel/`, `POST orders/{id}/reorder/`, `POST appointments/`, `POST appointments/{id}/cancel/`,
  `POST cart/items/` (an increment: a retried "add 1" must not add 2), `POST me/addresses/` (a retried create must not
  duplicate), `POST me/social/`, `POST me/password/` (a retried change would answer `password_unchanged` or find its
  verification token used).
- **Optional** on every other **C** POST/PUT/PATCH/DELETE (they are absolute writes: a retry converges), documented
  as the optional `Idempotency-Key` header of those operations. `auth/*` ignore the header (responses carry
  credentials). The fingerprint is an HMAC (keyed by the server secret) over the body **and the query string**.
- Webhooks do not use it: provider deliveries are de-duplicated by `integrations.WebhookInbox` and by provider state
  (§2.13.0). The POS resolve is not idempotent by design (the QR token is single-use).
- **No side effects on refusal:** under `@idempotent` every non-2xx answer rolls the whole transaction back
  (`transaction.set_rollback(True)`). A 409 therefore never carries a write; a refusal that must change state (for
  example an expiry) is left to the job that owns it (§2.7.2). State a refusal keeps on purpose lives outside that
  transaction: the sign-in guard counters (Redis) and the revocation of a refused flow's Apple token (§2.1.8).
- App rule: one key per user intent (one tap of "Savatga", "Saqlash", "Buyurtma berish", "To‘lash", "Qayta
  buyurtma", "Yozilish", "Bekor qilish"), reused on network retries, renewed after any 2xx or after 422
  `idempotency_key_reused`. Errors are never stored, so a retry after 409/503 runs again.

### 1.8 Throttles (Redis; defaults from `core/throttling.py`, overridable by `THROTTLE_RATES`)

Uzbek mobile operators put most subscribers behind carrier-grade NAT, so thousands of app users share a few public
IPv4 addresses. Customer throttles therefore key on the **device** (`X-GD-Device-Id`, hashed) first and keep only a
high pure-IP ceiling as a backstop; the per-phone rules are the main protection of the auth flows. All rates are
**provisional** and are tuned from production metrics (plan §9).

| Scope (existing unless marked new) | Key | Default | Applies to |
|---|---|---|---|
| `sms_phone` | `pii_ref(phone)` (§6.4) | 5/min | `auth/send-code/` |
| **`sms_device`** (new) | fid | 10/min | `auth/send-code/` |
| `sms_ip` | client IP (backstop) | 600/min (was 20/min; the scope is used only by this route) | `auth/send-code/` |
| OTP rules (not a DRF throttle; §2.1.1) | phone (DB-derived, survive a cache flush); fid and IP daily counters in Redis | resend ≥ 60 s per (phone, purpose); ≤ 10 codes per phone per Tashkent day; ≤ 10 wrong codes per phone per day; 15 min lockout after 5 wrong codes on one code; ≤ 10 codes per fid and ≤ 1000 per IP per day (`otp_network_limit`); global ≤ `SMS_GLOBAL_HOURLY_LIMIT` per hour | `auth/send-code/`, `auth/verify-code/`, `auth/login/` |
| **`customer_auth_device`** (new) | fid | 30/min | `auth/verify-code/`, `auth/set-password/`, `auth/login/`, `auth/reset-password/`, `auth/social/`, `auth/social/nonce/`, `auth/social/complete/` |
| **`customer_auth_ip`** (new) | client IP (backstop) | 1200/min | the same routes |
| customer login guard (`LoginGuard`, scope `customer_login`) | `pii_ref(phone)` | 5 failures/min, then lockout 1 → 15 min exponential | checked and counted by `auth/login/` on a device without a valid device key and by the password step of `auth/social/complete/` (both unauthenticated); it also **receives** (counts, never checks) every failure of the signed-in re-authentication below, so a stolen access token adds no unchecked guessing path |
| **signed-in re-authentication guard** (new; `LoginGuard`, scope `customer_reauth`) | customer id | 5 failures/min, then lockout 1 → 15 min exponential | the password checks of a signed-in customer — `me/social/` (POST and DELETE), `me/password/`, `DELETE me/`: they check **only** this lock, which only a holder of the customer's session can fill, so someone who merely knows the phone (and fills `customer_login` with `auth/login/` calls) can never block the owner's password change, account deletion or social link/unlink; a success resets only this scope |
| **known-device login guard** (new; `LoginGuard`, scope `customer_login_device`) | device row id | 5 failures/min, then lockout 1 → 15 min exponential | `auth/login/` with a valid `X-GD-Device-Key` (§2.1.4): someone who only knows the phone cannot lock the owner out of their own known device |
| `refresh` | SHA-256 of the presented refresh token | 120/min | `auth/refresh/` |
| **`refresh_unknown`** (new) | client IP, counted only when the presented token matches no session | 60/min | `auth/refresh/` (junk-token rotation cannot bypass the per-token key) |
| `auth_client_ip` | client IP | 600/min | `auth/refresh/`, `auth/logout/` |
| `customer` | customer id | 600/min | every **C** route |
| **`qr`** (new) | customer id | 6/min | `me/qr/` (in addition to `customer`) |
| `checkout` | customer id | 30/min | `orders/checkout/` (in addition to `customer`) |
| `pay` | customer id | 30/min | `orders/{id}/pay/` (in addition to `customer`) |
| `pos` | POS device id (after successful key auth) | 120/min | `pos/*` |
| POS failed auth (not a DRF throttle; inside `PosKeyAuthentication`, because DRF authenticates before throttling) | client IP, Redis counter | `POS_FAILED_AUTH_PER_IP_PER_MIN` (20); beyond it → 429 `throttled` without a key lookup | `pos/*` |
| **`public`** (new) | fid when the device header is present, else client IP | 300/min | every **O** route when anonymous (a signed-in caller counts against `customer`) |
| **`public_ip`** (new) | client IP (backstop) | 3000/min | every **O** route when anonymous |
| **`public_bff`** (B-W1) | one bucket (`bff`) | 6000/min | every **O** route when anonymous **and** called by the website's BFF on its own behalf: a valid `X-GD-Internal-Secret` without a well-formed `X-GD-Client-IP` (cache revalidation, sitemap builds); such calls skip `public` (unless they carry a device id) and `public_ip`. With a forwarded `X-GD-Client-IP` (the `core.clientip` trust rule: only with the secret) `public` and `public_ip` key on the **browser's** IP, so website visitors never share the web server's bucket; a forged or missing secret leaves the peer IP in charge |

Webhooks are not throttled when they authenticate (providers retry on 429 and would amplify); they are protected by
signature checks and the optional IP allow-lists `CLICK_ALLOWED_IPS` / `PAYME_ALLOWED_IPS` (defence in depth only).
Requests that **fail** authentication are bounded instead (§2.13.0: ≤ `WEBHOOK_REJECTED_PER_IP_PER_MIN` stored rows per
IP per minute, the rest only counted and sampled in the log).

### 1.9 Sessions and devices (B-W1: any number at once)
- A customer may be signed in on **any number of phones and browsers at the same time** (the app and the website, two
  phones, a laptop): one `Device(customer, fid)` per install or browser and **one open `AuthSession` per device**. The
  single-device rule of M2 is retired (B-W1): no sign-in ends another device's session, no `push.session_replaced` is
  queued any more (the handler stays registered only to drain messages queued before the release) and the customer
  audience never answers `SESSION_REPLACED`. The customer manages the sessions in the account's «Faol seanslar» list
  (§2.2.10). Every successful sign-in (`auth/set-password/`, `auth/login/`, `auth/reset-password/`, `auth/social/`,
  `auth/social/complete/`) runs `accounts.services.start_customer_session(customer_id, fid, platform, app_version, *,
  issue_device_key)` under the customer-level lock (`select_for_update(no_key=True)` on the customer row, §2.7.1):
  1. upsert `Device(customer, fid)` → `is_active=True`, `platform`, `app_version`, `last_active_at=now`;
  2. an open session **of this device** (an earlier sign-in of the same install or browser) → `revoke_reason="replaced"`
     (tombstone `revoked:replaced` and its QR keys on commit; the client that signs in again holds the new tokens);
     every **other** device keeps its session;
  3. the customer's devices left **without** an open session (e.g. a reset revoked them all) → `is_active=False`,
     `push_enabled=False`, `fcm_token=""` (key hash kept); their tokens are returned as `SessionStart.farewell_tokens`
     (normally `[]`);
  4. when `issue_device_key` (every sign-in verified by SMS, by a social provider or by registration, and every
     reset): a new 256-bit `device_key` (`secrets.token_urlsafe(32)`: 43 base64url chars, no prefix) is generated, `Device.key_hash = SHA-256(device_key)`
     replaces the old hash, and the raw key is returned **once** in `AuthTokens.device_key`; a known-device password
     sign-in keeps the existing key (`device_key: null`). The website's BFF keeps it in an HttpOnly cookie;
  5. a new session: `refresh_hash = SHA-256(refresh)`, `absolute_expires_at = now + CUSTOMER_REFRESH_TTL_DAYS`,
     `device = the device`, `device_label = "<platform> <model or 'app'>"` (the fid never appears in labels);
     `Customer.last_login_at = now`. The sign-in response then records the session's client for the session list
     (`user_agent`, `last_ip`, `location`, §2.2.10).
- **Ending sessions.** A session ends by `auth/logout/` (`scope` `current` | `others` | `all`, §2.1.7), by `DELETE
  me/sessions/{id}/` and `POST me/sessions/revoke-others/` (§2.2.10), by staff (`admin_revoked`, admin §2.6), by a
  password reset or change (every other session, `password_changed`), by `DELETE me/` (`deactivated`) or by expiry. Each
  revocation writes the `revoked:<reason>` tombstone on commit (read before the database by every gunicorn worker) and
  deletes the QR keys **that session** issued (§2.9.3); a device left without an open session is retired at once
  (inactive, no push, no token; key hash kept, so signing in there again skips the SMS). The revoked client gets 401
  `SESSION_EXPIRED` (`password_changed` for that reason) on its next request and on its next `auth/refresh/`.
- **Pushes** (§2.14.2) go to **every** active, push-enabled device of the customer (one FCM batch per notification,
  newest first, at most 10); the website registers no FCM token (no web push in v1), so it simply has no push. The FCM
  token stays unique among active devices (`device_active_fcm_token_unique`): `PUT me/device/` moves a token another
  row holds to this device and ends that row's session (`replaced`; an install that changed accounts).
  Campaigns keep one device (the newest pushable one) per customer.
- A **known device** for A2 is a `Device(customer, fid)` row whose `key_hash` equals SHA-256 of the presented
  `X-GD-Device-Key`. The fid alone never makes a device known: it is a non-secret identifier (Firebase knows it, it may
  appear in support screenshots), whereas the device key exists only in this install's Keychain/Keystore (or the
  website's HttpOnly cookie) (§6.6). A reinstall (or cleared cookies) loses the key, so the next password sign-in asks
  for the SMS code once.
- `DELETE me/` revokes with `deactivated` and clears every device token and key hash (A1, §2.2.3).
- `auth/reset-password/` and `me/password/` capture the FCM tokens of the devices they sign out
  (`accounts.services.sign_out_customer`, before the tokens are cleared) and pass them, with
  `SessionStart.farewell_tokens`, to their `push.account_security` notice, so a phone the legitimate owner may still
  hold after a takeover gets a visible notice too (§2.14.2).

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
| 8 | GET | `auth/social/nonce/` | – | | 200 | P1 |
| 9 | POST | `auth/social/` | – | | 200 | P1 |
| 10 | POST | `auth/social/complete/` | – | | 200 / 201 | P1 |
| 11 | GET / PATCH / DELETE | `me/` | C | | 200 / 200 / 204 | P1 |
| 12 | POST | `me/consent/` | C | | 200 | P1 |
| 13 | PUT / DELETE | `me/image/` | C | multipart / – | 200 / 204 | P1 |
| 14 | PUT | `me/device/` | C | | 200 | P1 |
| 15 | POST | `me/password/` | C | I | 200 | P1 |
| 16 | POST / DELETE | `me/social/`, `me/social/{provider}/` (DELETE with a JSON body) | C | I (POST) | 200 / 200 | P1 |
| 17 | GET / POST | `me/addresses/` | C | I (POST) | 200 / 201 | P1 |
| 18 | PATCH / DELETE | `me/addresses/{id}/` | C | | 200 / 204 | P1 |
| 19 | POST | `me/addresses/{id}/default/` | C | | 200 | P1 |
| 20 | GET | `catalog/banners/`, `catalog/categories/` | O | | 200 | P1 |
| 21 | GET | `catalog/products/` | O | P | 200 | P1 |
| 22 | GET | `catalog/products/{id}/` | O | | 200 | P1 |
| 23 | GET | `settings/public/` | O | | 200 | P1 |
| 24 | GET / DELETE | `cart/` | C | | 200 | P2 |
| 25 | POST | `cart/items/` | C | I | 200 | P2 |
| 26 | PATCH / DELETE | `cart/items/{product_id}/` | C | | 200 | P2 |
| 27 | DELETE | `cart/items/?product_id=…` (bulk) | C | | 200 | P2 |
| 28 | GET | `delivery/regions/` | O | | 200 | P2 |
| 29 | POST | `delivery/calculate/` | C | | 200 | P2 |
| 30 | POST | `orders/checkout/` | C | I | 201 | P2 |
| 31 | POST | `orders/{id}/pay/` | C | I | 200 | P2 |
| 32 | POST | `orders/{id}/cancel/` | C | I | 200 | P2 |
| 33 | POST | `orders/{id}/reorder/` | C | I | 200 | P2 |
| 34 | GET | `me/orders/` | C | P | 200 | P2 |
| 35 | GET | `me/orders/{id}/` | C | | 200 | P2 |
| 36 | GET | `me/cashback/` | C | | 200 | P3 |
| 37 | GET | `me/cashback/ledger/` | C | P | 200 | P3 |
| 38 | GET | `me/qr/` | C | | 200 | P3 |
| 39 | POST | `pos/cashback/resolve-qr/` | K | | 200 | P3 |
| 40 | GET | `specialists/`, `specialists/{id}/`, `services/` | O | | 200 | P3 |
| 41 | GET | `specialists/{id}/slots/?date=&service_id=` | O | | 200 | P3 |
| 42 | POST | `appointments/` | C | I | 201 | P3 |
| 43 | GET | `me/appointments/` | C | P | 200 | P3 |
| 44 | GET | `me/appointments/{id}/` | C | | 200 | P3 |
| 45 | POST | `appointments/{id}/cancel/` | C | I | 200 | P3 |
| 46 | GET | `me/notifications/` | C | P | 200 | P3 |
| 47 | GET | `me/notifications/unread-count/` | C | | 200 | P3 |
| 48 | POST | `me/notifications/{id}/read/`, `me/notifications/read-all/` | C | | 200 | P3 |
| 49 | POST | `me/notifications/ack/` | C | | 200 | P3 |
| 50 | POST | `webhooks/click/prepare/`, `webhooks/click/complete/` | S | | 200 | P2 |
| 51 | POST | `webhooks/payme/` | S | | 200 | P2 |
| 52 | POST | `webhooks/bts/` | S | | 200 | P2 |
| 53 | POST | `dev/payments/{order_id}/simulate/` | C | local/test settings only | 200 | P2 |
| 54 | GET | `me/sessions/` | C | P | 200 | B-W1 |
| 55 | DELETE | `me/sessions/{id}/` | C | | 204 | B-W1 |
| 56 | POST | `me/sessions/revoke-others/` | C | | 200 | B-W1 |
| 57 | GET | `catalog/products/{slug}/` | O | | 200 | B-W1 |
| 58 | GET | `specialists/{slug}/` | O | | 200 | B-W1 |

**Shared shapes.**
```jsonc
// AuthTokens — every successful sign-in
{"access": "eyJhbGciOiJIUzI1NiIsImtpZCI6...",   // JWT aud=customer, 15 min
 "access_expires_at": "2026-10-26T12:15:00+05:00",
 "refresh": "Qm9n…43 chars",                     // opaque 256-bit base64url; stored as SHA-256; ROTATES on every refresh
 "refresh_expires_at": "2026-12-25T12:00:00+05:00",   // now + 60 days, sliding
 "device_key": "7HqZr2…43 base64url chars",      // | null (no prefix): issued once on a verified sign-in (§1.9 step 5); store it in the Keychain/Keystore, replacing the old one; null = keep the stored key
 "customer": { /* Me, §2.2.1 */ },
 "is_new_customer": false,                       // true when this call created the Customer row
 "reactivated": false}                           // true when this call reactivated a deleted Customer (A1)

// NextStep — a sign-in that needs one more factor (200, no tokens)
{"next_step": "verify_device",                  // NextStep: verify_device | verify_phone
 "phone_mask": "+998 90 *** ** 67",              // verify_device only, else null
 "otp": {"expires_in": 120, "resend_in": 60, "length": 6},   // verify_device: the code just sent (or the live one); null for verify_phone
 "social_ticket": null,                          // verify_phone only: opaque signed ticket, 10 min, single use, bound to this fid
 "provider": null,                               // verify_phone only: CustomerSocialProvider
 "email_mask": null,                             // verify_phone only: masked provider email | null
 "expires_in": 600}                              // validity of the ticket / of this step in seconds
```

### 2.1 Auth

Common to every `auth/*` endpoint: device headers required (§1.2); phone fields are E.164 (`invalid` "Telefon raqamini
to‘g‘ri kiriting."; catalog `field.phone_invalid`); bodies are JSON; audit rows use `actor_type="customer"`
(`actor_id` = customer id when known, `entity_type="customer"`), never the raw code, password, token or device key.

OTP purposes (`OtpPurpose`) map to `SmsCode.Purpose`: `register` → `signup`, `login_new_device` → `new_device`,
`reset_password` → `reset`, `link_social` → `social_link`. `login_new_device` codes are issued **only** by
`auth/login/` after the password check (§2.1.4); `send-code` refuses that purpose, so nobody can make the server text
a registered phone without its password.

**Set or change the password while signed in:** `POST me/password/` (§2.2.9) — with the current password, or with an
SMS verification token when the account has none (social-only). It keeps the current session, device key and social
identities. `auth/reset-password/` (§2.1.5) is the **unauthenticated recovery** path only (forgotten password, account
takeover): it revokes every session and unlinks every social identity.

#### 2.1.1 `POST auth/send-code/`
Body `{"phone": str, "purpose": "register" | "reset_password" | "link_social", "social_ticket": str | null}`.

| Field / check (in order) | Code | Message (uz) · catalog key |
|---|---|---|
| `phone` | `required`, `invalid` | "Telefon raqamini to‘g‘ri kiriting." · `field.phone_invalid` |
| `purpose` (`login_new_device` included in the refusal) | `required`, `invalid_choice` | generic |
| `social_ticket` required iff `purpose = link_social` | `required` | "Ijtimoiy tarmoq orqali kirishni qaytadan boshlang." · `field.social_ticket_required` |
| `social_ticket` valid (signature, ≤10 min, unused, its `fid` = `X-GD-Device-Id`) | 400 `social_ticket_invalid` | "Ijtimoiy tarmoq orqali kirish muddati tugadi. Qaytadan boshlang." · `error.social_ticket_invalid` |
| `register` and an **active** customer with this phone exists | 409 `phone_registered` | "Bu raqam ro‘yxatdan o‘tgan. Hisobingizga kiring." · `error.phone_registered` |
| `reset_password` and no active customer (deleted counts as none) | 409 `phone_not_registered` | "Bu raqam bilan hisob topilmadi. Ro‘yxatdan o‘ting." · `error.phone_not_registered` |
| `sms_phone` / `sms_device` / `sms_ip` throttle | 429 `throttled` | admin message, localized · `error.throttled` |
| Global budget: the Redis counter `gd:otp:global:{YYYYMMDDHH}` ≥ `SMS_GLOBAL_HOURLY_LIMIT` (500) | 503 `sms_unavailable`, `Retry-After` = seconds to the next hour; ERROR log `otp.budget_tripped` (alert) | "SMS xizmati vaqtincha ishlamayapti. Birozdan so‘ng urinib ko‘ring." · `error.sms_unavailable` |
| — from here on under `pg_advisory_xact_lock(hashtext('otp:' + phone))`, so two concurrent calls for one phone are serialised — | | |
| A row for the phone with `exhausted_at` > now − 15 min | 429 `otp_locked` | "Urinishlar soni tugadi. {m} daqiqadan so‘ng qayta urinib ko‘ring." · `error.otp_locked` |
| Σ `attempts` (wrong codes) over the phone's rows of the current Tashkent day ≥ `CUSTOMER_OTP_DAILY_WRONG_LIMIT` (10) | 429 `otp_locked`, `retry_after` = seconds to Tashkent midnight | `error.otp_locked` |
| The newest row for (phone, purpose) is younger than 60 s | 429 `otp_resend_too_soon` | "Yangi kodni {n} soniyadan so‘ng so‘rashingiz mumkin." · `error.otp_resend_too_soon` |
| ≥ `CUSTOMER_OTP_DAILY_LIMIT` (10) rows for the phone in the current Tashkent day, or ≥ `CUSTOMER_OTP_DEVICE_DAILY_LIMIT` (10) codes for this fid (Redis day counter keyed by `pii_ref(fid)`; the fid is self-asserted, so this cap only stops a looping or buggy client, not an attacker) | 429 `otp_daily_limit` | "Bugungi SMS kodlar limiti tugadi. Ertaga qayta urinib ko‘ring." · `error.otp_daily_limit` |
| ≥ `CUSTOMER_OTP_IP_DAILY_LIMIT` (1000; provisional, tuned from metrics) codes for this client IP today (Redis day counter) — a cost control that, behind carrier-grade NAT, can also hit legitimate users, so it has its own code and message | 429 `otp_network_limit` | "Bu tarmoqdan bugun juda ko‘p kod so‘raldi. Boshqa tarmoq (Wi-Fi yoki mobil internet) orqali urinib ko‘ring." · `error.otp_network_limit` |

```jsonc
// 200
{"phone_mask": "+998 90 *** ** 67", "purpose": "register",
 "expires_in": 120, "resend_in": 60, "attempts": 5, "length": 6}
```
**Side effects** (`accounts.services.send_code`, one transaction): previous unconsumed code of (phone, purpose) gets
`expires_at=now`; new `SmsCode` row (`code_hmac = HMAC-SHA256(code, SMS_CODE_HMAC_KEY)` hex, 6 random digits from
`secrets`, `expires_at = now + 120 s`, `max_attempts = 5`, `request_ip`); the global, fid and IP counters are
incremented. The **plain code is never written to the database**: it is encrypted with AES-256-GCM under
`SECRETBOX_KEY` (`core.secretbox.seal`, random 96-bit nonce, associated data = `sms_code_id`) and stored in Redis as
`gd:otp:payload:{sms_code_id}` with the code TTL (120 s). The outbox message is `sms.send {sms_code_id, lang,
platform}` (dedupe `sms.send:{id}`) — no phone, no text, no code; the handler reads the phone from the row, opens the
ciphertext, renders `sms.otp` (§2.14.1), sends, and deletes the Redis key. For `link_social` the ticket's `jti` is
remembered as `gd:social:otp:{sms_code_id}` (15 min) so `auth/social/complete/` can require that the verification
token came from a code requested **with this ticket**. No audit (volume); structured log `otp.issued {purpose,
phone_ref}` (`pii_ref`, §6.4).
Phone enumeration through `register`/`reset_password` answers is an accepted UX trade-off, bounded by the throttles
(§7 `M2-A9`).

#### 2.1.2 `POST auth/verify-code/`
Body `{"phone": str, "purpose": OtpPurpose, "code": str}`.

| Field / check | Code | Message (uz) · key |
|---|---|---|
| `code` `^\d{6}$` | `invalid` | "6 xonali kodni kiriting." · `field.otp_format` |
| No live code (none, expired, consumed, superseded) | 400 `otp_expired` | "Kod muddati tugagan. Yangi kod oling." · `error.otp_expired` |
| Row exhausted, lockout active, or the daily wrong-code cap reached | 429 `otp_locked` | `error.otp_locked` |
| HMAC mismatch (`hmac.compare_digest`) | 400 `otp_invalid`, `details.attempts_left`; field `code` `otp_invalid` | "Kod noto‘g‘ri. Yana {n} marta urinishingiz mumkin." · `error.otp_invalid` (field message: `field.otp_invalid`, same text); at 0 left (on this code or for the day) → 429 `otp_locked` |

```jsonc
// 200
{"verification_token": "Zx3kq…43 chars",   // 256-bit base64url, no prefix, returned once; SHA-256 stored on the SmsCode row
 "expires_in": 600, "purpose": "register",
 "customer_exists": false}                 // an active customer has this phone (lets register/link screens branch)
```
Side effects (same per-phone advisory lock, then `select_for_update` on the row): on mismatch `attempts += 1` (and
`exhausted_at` at the max); on match `consumed_at`, `verification_token_hash`, `verification_expires_at = now + 600 s`.
`attempts_left` = min(this code's remaining attempts, the day's remaining wrong codes). The token is single-use and
bound to (phone, purpose): every consumer sets `verification_used_at` under lock and refuses a used, expired or
mismatching token with 400 `verification_invalid` "Tasdiqlash muddati tugagan. Kodni qaytadan oling."
(`error.verification_invalid`).

#### 2.1.3 `POST auth/set-password/` (registration)
Body `{"verification_token": str (purpose register), "password": str, "name": str, "terms_version": str, "language": ContentLanguage | null}`.

| Field / check | Code | Message (uz) · key |
|---|---|---|
| Terms published: `SiteSettings.terms_version` non-empty **and** the published uz texts of `terms` and `privacy_policy` hold a visible character (`terms_version` versions both documents; staff publish through admin §2.11 `POST settings/terms/publish/`) | 503 `terms_not_published` | "Ro‘yxatdan o‘tish vaqtincha mavjud emas. Keyinroq urinib ko‘ring." · `error.terms_not_published` |
| `verification_token` | 400 `verification_invalid` | `error.verification_invalid` |
| `name` trimmed 2..150 | `min_length` / `max_length` | "Ismingizni kiriting (kamida 2 belgi)." · `field.name_min` / generic |
| `password` 8..128; Django validators with `CUSTOMER_PASSWORD_MIN_LENGTH` (8), common, numeric, similarity to the phone | `password_too_short` · `password_too_common` · `password_entirely_numeric` · `password_too_similar` · `max_length` | "Parol kamida 8 ta belgidan iborat bo‘lsin." · "Bu parol juda oddiy." · "Parol faqat raqamlardan iborat bo‘lmasin." · "Parol telefon raqamingizga juda o‘xshash." (`field.password_*`) |
| `terms_version` = current `SiteSettings.terms_version` | `terms_outdated` | "Foydalanish shartlarining yangi tahririni qabul qiling." · `field.terms_outdated` |
| `language` | `invalid_choice` | generic; default = resolved request language |
| An **active** customer with the phone exists (race with another registration) | 409 `phone_registered` | `error.phone_registered` |

→ **201 `AuthTokens`** (with a `device_key`). Side effects (one transaction, `customers.services.register`):
- new `Customer(phone, name, language)` or, for a deleted row, `accounts.services.reactivate_customer(name=…)`
  (same id; `registered_at` kept, `reactivated_at=now`; `LoyaltyAccount`, `Purchase`, `Order` history intact, A1 —
  but the history shown to the new holder of the number starts at the **history horizon** `Customer.history_from`,
  which `accounts.services.move_history_horizon(customer_id, at=now)` sets to `reactivated_at`, §2.2.3);
- `set_customer_password`; consent `terms_version`, `consent_at=now`, `consent_ip=client IP` (C3);
- `start_customer_session(…, issue_device_key=True)` (§1.9); verification token marked used;
- audit `customer.registered {reactivated: bool, terms_version}`.

#### 2.1.4 `POST auth/login/`
Body `{"phone": str, "password": str 1..128, "verification_token": str | null}`; header `X-GD-Device-Key` when the app
holds one.
1. The device is **known** when the active customer of this phone has `Device(customer, fid)` with `key_hash =
   SHA-256(X-GD-Device-Key)` (§1.9); an unknown phone, a missing, malformed or wrong key = unknown device. This is
   evaluated first and reveals nothing by itself (the answer does not differ until the password is checked).
2. Login guard → 429 `throttled`: a **known** device counts its failures in scope `customer_login_device` keyed by the
   device row; an unknown device in scope `customer_login` keyed by `pii_ref(phone)` (§1.8). Someone who only knows the
   phone can lock the unknown-device path for that phone (residual risk, §6.3) but never the owner's known device, nor
   the password checks of the owner's signed-in session (scope `customer_reauth`, §1.8).
3. **Unknown device without `verification_token`:** if a code cannot be sent right now for **any** reason step 6 could
   meet — the phone's lockout, daily code cap or daily wrong-code cap, the fid day cap, the IP day cap or the global
   hourly budget (§2.1.1) — → 429 `otp_locked` / `otp_daily_limit` / `otp_network_limit` or 503 `sms_unavailable`
   (each with its own code and `Retry-After`) **before the password is checked**, with the same body for every password
   and for unknown phones, so no cap gives a password-correctness oracle. The check runs under the per-phone advisory
   lock of §2.1.1, which the request keeps until it commits, so the phone-derived caps cannot change before step 6.
4. Unknown phone, deleted customer, no password set (social-only account) or wrong password → **401
   `invalid_credentials`** "Telefon raqami yoki parol noto‘g‘ri." (same body and timing: whenever no stored hash is
   compared — unknown phone, deleted customer, empty password — the login service runs a dummy Argon2 check against a
   fixed hash, so all four cases cost one hash; `check_customer_password` itself is unchanged). Audit
   `customer.login_failed {method: "password"}` with `actor_id` when the customer exists.
5. **Known device** → **200 `AuthTokens`** (`device_key: null`; §1.9 applies: the other devices stay signed in). Audit
   `customer.login_succeeded {method: "password", new_device: false}`.
6. **Unknown device, no `verification_token`** → **200 `NextStep`** `verify_device`. The server issues a
   `login_new_device` code through the `send_code` internals (the advisory lock taken in step 3; a live code younger
   than 60 s is reused and `otp.resend_in` is the remaining wait). Step 6 never answers a cap error: the caps were
   checked in step 3, and the fid, IP and global Redis counters are incremented here without a second check (a
   concurrent request may overshoot one of those cost caps by one; answering it after the password check would be the
   oracle step 3 removes). Resending = calling `auth/login/` again with the
   password. No session is created.
7. **Unknown device with `verification_token`** (purpose `login_new_device`, same phone) → password re-checked, token
   consumed → **200 `AuthTokens`** with a new `device_key`; audit `customer.login_succeeded {method: "password",
   new_device: true}`. A bad token → 400 `verification_invalid`.

The app keeps the typed password in memory only until step 7 (≤10 minutes) and never persists it.

#### 2.1.5 `POST auth/reset-password/`
Body `{"verification_token": str (purpose reset_password), "password": str}`. Password rules as §2.1.3, plus
`password_unchanged` "Yangi parol eskisidan farq qilsin." (`field.password_unchanged`).
This is the **unauthenticated recovery** path (forgotten password, account takeover); a signed-in customer changes or
sets the password with `POST me/password/` (§2.2.9), which unlinks nothing.
→ **200 `AuthTokens`** (with a new `device_key`). Side effects: password set (`password_changed_at`); **every**
session of the customer revoked with `password_changed` and every other device retired (tombstones on commit; each
signed-out device's next request answers 401 `password_changed` with a message, §1.2;
`accounts.services.sign_out_customer(…, keep_fid=<this install>)` captures their FCM tokens first); **every social identity unlinked** (audit `customer.social_unlinked
{provider, reason: "password_reset"}` each; Apple identities are revoked at Apple, §2.1.8), because a reset is what a
victim does after an account takeover and a linked identity would otherwise keep the attacker in; every other
device's key hash cleared; the **dormant-account rule** (§2.2.3, `M2-A16`) applies; then `start_customer_session(…,
issue_device_key=True)` on this device; outbox `push.account_security` (payload §2.14.2: `event: "password_reset"`,
`provider: ""`, `farewell_tokens` = the captured tokens) — inbox row + push to the new device **and** a visible push to
every token captured from the devices this reset signed out (plus `SessionStart.farewell_tokens`, §1.9), so the previous
device learns of the reset even when the reset came from a stolen SIM (§2.14.2); audit `customer.password_reset`. 400
`verification_invalid`; 409 `phone_not_registered` if the customer was deleted in between.

#### 2.1.6 `POST auth/refresh/`
Body `{"refresh": str}`. Device headers must match the session's device for every answer that hands out tokens
(rotation, grace replay, re-rotation; else 401 `not_authenticated`). A **reuse** (step 6, including a dead token of
step 2) revokes the session whichever install presents it — a token from another install is the stronger signal.

`accounts.services.rotate_customer_refresh(raw, fid)` runs in one transaction:
1. `h = SHA-256(raw)`; `SELECT … FOR UPDATE` the session `WHERE audience='customer' AND (refresh_hash = h OR
   previous_refresh_hash = h)` (index `accounts_authsession_prev_refresh`, partial `WHERE previous_refresh_hash <>
   ''`, migration `accounts.0006`). Every check below runs on the locked row, so two concurrent refreshes serialise.
2. No row → if `auth:refresh_dead:{h}` names a session (a token replaced by a re-rotation, step 5b), that session is
   locked and treated as **reuse** (step 6); otherwise 401 `not_authenticated`, and the `refresh_unknown` per-IP
   throttle counts the miss. A token two or more rotations old is simply unknown: its holder never had the current one.
3. Revoked → 401 `password_changed` for reason `password_changed`, else `SESSION_EXPIRED` (B-W1: `replaced` too); past `absolute_expires_at` or customer not active → 401 `SESSION_EXPIRED`.
4. `refresh_hash = h` → **rotate**: new 256-bit refresh; `previous_refresh_hash = h`, `refresh_hash = new`, `rotation
   += 1`, `rotation_confirmed = false`, `rotated_at = last_used_at = now` (one instant), `absolute_expires_at = now + 60
   days` (sliding); the new access token carries `rot = rotation`. Before commit the full response body is written to
   `auth:refresh_grace:{h}` (TTL `CUSTOMER_REFRESH_GRACE_SECONDS`, 120 s; deleted again if the transaction rolls back)
   → 200.
5. `previous_refresh_hash = h`:
   - (a) **grace replay** — the grace body `auth:refresh_grace:{h}` exists (written by the rotation or re-rotation that
     replaced `h`, TTL `CUSTOMER_REFRESH_GRACE_SECONDS`, 120 s) → return it (identical tokens, nothing written),
     **whether or not the rotation is confirmed**. This is the common mobile case: when the access token expires, the
     app's parallel requests all get 401 and, without single flight, several refreshes carry the same old token; the
     first rotates and its retried request confirms the rotation within milliseconds, and the later ones must still get
     the same tokens, not a revocation;
   - (b) otherwise, the rotation is **unconfirmed** (`rotation_confirmed = false`: no access token carrying the current
     `rot` has been seen) **and** `now − rotated_at ≤ CUSTOMER_REFRESH_UNCONFIRMED_SECONDS` (600) → the response of that
     rotation was lost and the grace body has expired: **re-rotate**: the current `refresh_hash` (issued but never
     used) is remembered as dead (`auth:refresh_dead:{that hash}` = sid, TTL until `absolute_expires_at`), a fresh
     refresh replaces it, `previous_refresh_hash` stays `h`, `rotation += 1`, `rotation_confirmed = false`, `rotated_at`
     **unchanged** (the window stays anchored to the rotation that replaced `h`), and the new body is written to
     `auth:refresh_grace:{h}` (overwriting the expired entry) so a concurrent second retry gets the same tokens instead
     of forking again → 200.
6. `previous_refresh_hash = h`, no grace body (5a), and the rotation is **confirmed** (`rotation_confirmed = true`)
   **or** the unconfirmed window is over (`now − rotated_at > CUSTOMER_REFRESH_UNCONFIRMED_SECONDS`), or a dead token
   came back (step 2) → **reuse**: one of the holders is not the app. Session revoked with reason `refresh_reused`
   (tombstone on commit); audit `customer.refresh_reused {sid}`; WARNING log → 401 `SESSION_EXPIRED` (not
   `password_changed`). This is the spec's "reuse after a short grace window revokes the
   session": the grace window is the 120 s replay of 5a; the first use of the new tokens ends only the longer
   re-rotation window of 5b.

**Rotation counter and confirmation.** `rot` is the integer `AuthSession.rotation` (migration `accounts.0006`, `0`
before the first rotation), compared exactly — never a timestamp, so clock precision and a pinned `FAKE_NOW` cannot
break it. `CustomerJWTAuthentication` records the confirmation: when the add-only flag `auth:sidrot:{sid}:{rot}` is
absent, it runs one conditional `UPDATE accounts_authsession SET rotation_confirmed = true WHERE id = sid AND rotation
= <rot> AND NOT rotation_confirmed` (at most one write per rotation; an access token of an older rotation matches no
row and writes nothing), then `cache.add("auth:sidrot:{sid}:{rot}", 1, timeout=CUSTOMER_ACCESS_TTL)`. The active-sid
entry `auth:sid:{sid}` = `active:{abs_expiry}:{pii_ref(fid)}:{platform}` stays **immutable and add-only** (spec: filled
with `cache.add`, TTL ≤ 60 s; every revoke writes a `revoked:<reason>` tombstone on commit): no rotation or
confirmation ever writes it, so a confirmation can never overwrite a concurrent tombstone. Its `abs_expiry` may lag a
slide by at most its 60 s TTL, which matters only for a session that was within a minute of expiring. Confirmation
ends only the re-rotation window (step 5b); the grace replay (step 5a) still answers until its 120 s are over.
Presenting the **new refresh token** is not a confirmation step: it rotates again (step 4), after which the old token
is neither current nor previous and gets 401 `not_authenticated` (the app's single-flight rule of §1.2 means it never
presents that old token).

```jsonc
// 200
{"access": "eyJ…", "access_expires_at": "2026-10-26T12:15:00+05:00",
 "refresh": "new…43 chars", "refresh_expires_at": "2026-12-25T12:00:00+05:00"}
```
The raw rotated token lives in Redis only for the grace window (internal network, TTL 120 s); this is the only place a
raw refresh token is stored (§6). `auth:refresh_dead:*` holds hashes only.

#### 2.1.7 `POST auth/logout/`
Body `{"refresh": str, "scope": "current" | "others" | "all"}` (`scope` optional, default `current`; `LogoutScope`,
B-W1; another value → 400 `scope` `invalid_choice`) → **204**. `current` revokes the session owning the token
(`logout`) — "Chiqish"; `others` every other open session of the customer, this one stays — "Boshqa qurilmalardan
chiqish"; `all` every open session — "Hamma joydan chiqish". Tombstones and the revoked sessions' QR keys go on commit;
every device left without an open session is retired (`is_active=False`, `push_enabled=False`, `fcm_token=""`, key hash
kept). Unknown, revoked or expired token → 401 `SESSION_EXPIRED` (the app treats it as logged out). Audit
`customer.logout {sid, scope}`.

#### 2.1.8 `GET auth/social/nonce/` and `POST auth/social/`
**Availability.** Customer social sign-in (`auth/social/nonce/`, `auth/social/`, `auth/social/complete/`, `POST
me/social/`) answers **404** when `OAUTH_MODE=fake` under the production settings (`DEV_FEATURES_ALLOWED` false): the
fake verifier accepts made-up subjects, which would let anyone sign in to an account linked while it was on — the OAuth
equivalent of a fixed OTP (§6.14). `DELETE me/social/{provider}/` keeps working. The staff OAuth routes get the same
gate (`POST auth/oauth/{provider}/` and `…/link/` answer 404, admin §2.1), because the staff fake has the same exposure.
**Website (B-W1).** The same three routes serve the website: its BFF calls them with `X-GD-Platform: web` and its browser
id, so the nonce and the ticket are bound to that browser exactly as to an app install. Google Identity Services
issues ID tokens for a **web client id** (`GOOGLE_WEB_CLIENT_IDS`, accepted next to `GOOGLE_APP_CLIENT_IDS`); the
Facebook JS SDK issues an access token of the same app (`FACEBOOK_APP_ID`); Sign in with Apple JS uses a **Services ID**
(`APPLE_SERVICES_IDS`, accepted as `aud` next to `APPLE_BUNDLE_IDS`), and its authorization code is exchanged with
`client_id` = that Services ID **and** `redirect_uri` = `APPLE_WEB_REDIRECT_URI` (server-configured, the URI the
website's Apple authorization used; required, https, in production when `APPLE_SERVICES_IDS` is set). The identity
keeps the Services ID as `provider_client_id`, so its revocation uses the right client. Native flows are unchanged
(no `redirect_uri`).

Provider `apple` additionally needs `APPLE_BUNDLE_IDS` or `APPLE_SERVICES_IDS` (product-owner decision 2026-10-26; B-W1): both empty + `OAUTH_MODE=live` →
404 for `apple`; empty + `OAUTH_MODE=fake` (local/test) → the fake Apple adapter (`fake-apple:…`, plan §6) answers, so
the app can be built before the client provides the bundle id.

**Nonce.** `GET auth/social/nonce/` → 200 `{"nonce": "<43 base64url chars>", "expires_in": 600}`: 256-bit random,
stored as `gd:social:nonce:{sha256(nonce)}` = `pii_ref(fid)`, TTL `SOCIAL_NONCE_TTL_SECONDS` (600). The app passes it
to the provider SDK — Google `nonce` and Facebook Limited Login `nonce` as is; Apple: the request nonce is
`sha256(nonce.encode("utf-8")).hexdigest()` (lowercase hex), because Apple puts the request value into the token — and
then sends the raw nonce to `auth/social/`. Throttles `customer_auth_device` / `customer_auth_ip`.

**Sign-in.** Body `{"provider": "google" | "facebook" | "apple", "token": str 1..4096, "token_type": "id_token" |
"access_token" | null, "nonce": str | null, "authorization_code": str ≤1024 | null}`. `token_type` defaults to
`id_token` for Google and Apple and `access_token` for Facebook. **Only four (provider, token type) pairs exist**:
`google`/`id_token`, `apple`/`id_token`, `facebook`/`access_token`, `facebook`/`id_token`; any other pair (for example a
Google or Apple access token, which proves nothing about our audience) → 400 `social_token_invalid`. For every
`id_token` the `nonce` is **required**: it must exist in the nonce cache, be bound to this fid, and is deleted on use
(`cache.delete` result checked, so it is single-use); a missing, unknown or foreign nonce → 400 `social_token_invalid`.
The fakes' `*` nonce wildcard exists only under the local/test settings. `authorization_code` is **required for
`apple`** (`required`) and ignored otherwise (Apple token revocation, below).

| Provider | Server-side verification (P1 extends the OAuth port with `verify_app_token(token, token_type, nonce)`) |
|---|---|
| `google` (`id_token`) | RS256 against Google's JWKS (cached by `kid`); `iss` ∈ {`https://accounts.google.com`, `accounts.google.com`}; `aud` ∈ `GOOGLE_APP_CLIENT_IDS` (the iOS client id and the web/server client id the Android SDK requests tokens for) ∪ `GOOGLE_WEB_CLIENT_IDS` (the website, B-W1); required claims `iss, aud, exp, iat, sub, nonce`; `nonce` equal to the raw nonce (`compare_digest`). `email` is optional and `email_verified` ignored (the phone is the identity; the staff adapter's required `email` does not apply to customers). |
| `apple` (`id_token`) | RS256 against `https://appleid.apple.com/auth/keys` (cached by `kid`); `iss = https://appleid.apple.com`, `aud` ∈ `APPLE_BUNDLE_IDS` ∪ `APPLE_SERVICES_IDS` (the website, B-W1); required claims `iss, aud, exp, iat, sub, nonce`; `nonce == sha256(raw_nonce.encode("utf-8")).hexdigest()` (lowercase hex) **(verify M3)**; `sub` is the subject; `email` optional (may be a private relay address); `email_verified` ignored (Apple may send it as the string `"true"`). Apple's token has no `jti`. |
| `facebook` (`access_token`) | `debug_token` with the app token `app_id|app_secret` in the `Authorization` header and the user's `input_token` in a **form-encoded POST body** to `https://graph.facebook.com/{FACEBOOK_GRAPH_VERSION}/debug_token` **(verify M3** that Graph accepts the POST form; if it does not, the GET is the documented exception: the httpx/httpcore request loggers of the OAuth client are set to WARNING so no URL with `input_token=` is logged, and no egress proxy logs query strings**)**: `is_valid`, `app_id == FACEBOOK_APP_ID`; then `GET /me?fields=id,email` with the user token in the `Authorization` header and `appsecret_proof`. No nonce (the token is bound to our app id). P1 changes the staff adapter's `_debug_token` the same way (it passes `input_token` as a query parameter today). |
| `facebook` (`id_token`, iOS Limited Login) | RS256 against Facebook's OIDC JWKS; `iss = https://www.facebook.com`, `aud = FACEBOOK_APP_ID`; required claims `iss, aud, exp, iat, sub, nonce`; `nonce` equal to the raw nonce **(verify M3)**; `email` optional |

- **Replay cache:** every accepted ID token is remembered as `gd:social:seen:{sha256(provider + ":" + raw token)}` until
  its `exp`; a second use → 400 `social_token_invalid`. (A key built from claims would collide for two tokens issued in
  the same second, since Apple's token carries no `jti`.)
- **Apple token revocation** (App Store Review Guideline 5.1.1(v): apps offering Sign in with Apple revoke the user's
  tokens when the account is deleted). The app sends the single-use `authorizationCode` Apple returned with the
  identity token. Before any row lock the server exchanges it at `https://appleid.apple.com/auth/token` (`grant_type =
  authorization_code`, `client_id` = the token's `aud` bundle id, `client_secret` = an ES256 JWT signed with the key
  `APPLE_KEY_ID` of team `APPLE_TEAM_ID`, `APPLE_PRIVATE_KEY_FILE`) **(verify M3)** and keeps Apple's refresh token,
  sealed with `core.secretbox` (§2.1.1), on `CustomerSocialIdentity.provider_refresh_token` (migration
  `accounts.0006`). A linked identity whose stored token is empty gets one at its next sign-in; one that has a token
  does not exchange again. An exchange refused by Apple → 400 `social_token_invalid`; Apple unreachable → 503
  `provider_unavailable`; nothing is linked in either case. When an Apple identity is unlinked (`DELETE
  me/social/apple/`, the reset of §2.1.5, `DELETE me/` §2.2.3), the same transaction enqueues `apple.revoke
  {identity_id, customer_id, subject, subject_ref, client_id, sealed_token}` (dedupe `apple.revoke:{identity_id}`;
  `subject` is blanked once the message reaches a final outcome). Tokens exchanged for a refused `POST me/social/`, or
  for a completion refused with `social_provider_linked` / `social_identity_linked`, are revoked the same way unless
  their `sub` is linked to a customer (one revocation ends Apple's whole authorization of the app); a ticket that is
  never completed keeps its token until the ticket expires. Its handler (`accounts/tasks.py`)
  posts the token to `https://appleid.apple.com/auth/revoke` (`token_type_hint = refresh_token`); transport errors
  retry through the outbox, an Apple 400 `invalid_grant` or `invalid_request` (token already invalid) is final with a
  WARNING (every other 4xx — e.g. `invalid_client`, a misconfiguration — is retried and ends dead), a dead message logs ERROR
  `apple.revoke_dead` (alert; staff revoke in the Apple developer account). The fake records the revoke (local/test).
  Apple's server-to-server notifications (consent revoked, account deleted) are **not** handled in M2: the default is
  that the identity stays linked until the customer unlinks it or Apple refuses its next token; M3 adds the endpoint.
- **Apple's name.** Apple sends the user's name only on the first authorization and never in the token. The app keeps
  `fullName` locally from that first callback until `auth/social/complete/` succeeds and pre-fills it as `name`.
- Linked identity `(provider, subject)` whose customer is active → **200 `AuthTokens`**; `last_login_at` on the
  identity; `issue_device_key=True` (the provider authentication counts as a verified sign-in); audit
  `customer.login_succeeded {method: provider}`. Social sign-in does **not** require the new-device SMS code (§7
  `M2-A10`): an identity can only be linked by proving the account's strongest factor — at completion the SMS code
  **and** the password when the account has one (§2.1.9); from a signed-in session the password, or an SMS code when
  the account has no password (§2.2.8), never the bearer token alone — so it is at least as strong as the A2 path.
- Unknown identity → **200 `NextStep`** `verify_phone` with a `social_ticket` = `TimestampSigner(salt="gd.social")`
  over `{provider, subject, email, jti, fid: pii_ref(fid)}`, 10 min, where `jti` is a **server-generated** 128-bit id
  (`secrets.token_hex(16)`), never a token claim; it keys `gd:social:otp:{sms_code_id}` (§2.1.1) and, for Apple, the
  sealed refresh token waiting for completion (`gd:social:apple:{jti}`, 10 min; Apple's authorization code is valid
  for 5 minutes only, so it is exchanged now). Single use is durable: completing consumes a verification token
  (`verification_used_at`, DB) and creates a unique `(provider, subject)` row; the `jti` in the cache (`cache.add`)
  only stops a second concurrent completion early.
- 400 `social_token_invalid` "Kirish amalga oshmadi. Qayta urinib ko‘ring." (`exchange_failed`, `invalid_token`, nonce
  and replay failures, an unsupported (provider, token type) pair, a refused Apple code exchange); 400
  `authorization_code` `required` (Apple); 503 `provider_unavailable` (`unavailable`); 404 for an unknown provider,
  for `apple` while `APPLE_BUNDLE_IDS` and `APPLE_SERVICES_IDS` are empty in live mode, and for every provider while OAuth is fake under the
  production settings; 429 `throttled`.

#### 2.1.9 `POST auth/social/complete/`
Body `{"social_ticket": str, "verification_token": str (purpose link_social), "password": str | null, "name": str | null, "terms_version": str | null, "language": ContentLanguage | null}`.
The phone is the one the verification token is bound to. Checks: ticket valid and its `fid` = `X-GD-Device-Id` (400
`social_ticket_invalid`); the token's `SmsCode` row was requested with **this** ticket (`gd:social:otp:{sms_code_id}`
= ticket `jti`; missing or different → 400 `verification_invalid`).

| Phone state | Requirements | Result |
|---|---|---|
| Active customer **with** a password | `password` required and correct (`required` / `password_incorrect` "Parol noto‘g‘ri." · `field.password_incorrect`; LoginGuard scope `customer_login`) | Identity linked → **200 `AuthTokens`** (`is_new_customer: false`) |
| Active customer without a password (social-only) | — (the SMS code proves the phone; the account has no stronger factor). The dormant-account rule applies (§2.2.3, `M2-A16`): on a dormant account it first unlinks every existing identity, clears device keys and consent, then this identity is linked | Identity linked → **200** |
| No customer | `name` (2..150) and `terms_version` required (`required`, `terms_outdated`); terms published (503 `terms_not_published`) | Customer created without a password (`has_password: false`; a password can be added later with `POST me/password/` and an SMS code, §2.2.9) → **201** |
| Deleted customer | as above | Reactivated (A1) → **201** with `reactivated: true` |

Errors: 400 `social_ticket_invalid`, 400 `verification_invalid`; 409 `social_provider_linked` "Bu hisobga boshqa
{provider} profili bog‘langan." when the customer already has an identity for that provider; 409
`social_identity_linked` "Bu {provider} profili boshqa hisobga bog‘langan." when the subject was linked meanwhile
(unique-violation race too). Side effects: `CustomerSocialIdentity(provider, subject, email_at_link)` (Apple: the
sealed refresh token moved from `gd:social:apple:{jti}` onto the row; missing — a cache flush — → 400
`social_ticket_invalid`, the app restarts the sign-in, so no Apple identity is ever linked without a revocable token);
consent when
creating/reactivating; `start_customer_session(…, issue_device_key=True)`; outbox `push.account_security` (payload
§2.14.2: `event: "social_linked"`, `provider`, `farewell_tokens` = the captured tokens) when an existing account was
linked — the inbox row,
the push to every signed-in device of the customer (B-W1: the other devices stay signed in and see the notice;
`farewell_tokens` = `SessionStart.farewell_tokens`, normally `[]`, §1.9); audit `customer.social_linked {provider}` (+ `customer.registered`).

### 2.2 Me (profile, consent, image, device, addresses, social identities, password)

#### 2.2.1 `GET me/` → `Me`
```jsonc
{"id": 5, "phone": "+998901234567", "name": "Malika Karimova",
 "language": "uz",                                  // ContentLanguage: push texts and the default Accept-Language
 "image_url": null,                                  // absolute URL | null
 "registered_at": "2025-03-12T10:20:00+05:00",      // Customer.history_from when set (reactivated or dormant-reset account, §2.2.3), else registered_at
 "has_password": true, "linked_providers": ["google"],   // CustomerSocialProvider[]
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

Side effects: `Customer.name/language`; `Device.push_enabled`. No audit (own profile), log only. Inbox rows already
created keep the language they were rendered in (§2.12).

#### 2.2.3 `DELETE me/`
Body `{"password": str | null, "verification_token": str | null}`.
- `has_password` → `password` required (`required`; wrong → `password_incorrect` "Parol noto‘g‘ri." ·
  `field.password_incorrect`), guarded by `LoginGuard` scope `customer_reauth` keyed by the customer id (429
  `throttled` after 5 failures); every failure is also counted in the `customer_login` budget of `auth/login/`, so a
  stolen access token gets no unchecked guessing path, while someone who only knows the phone cannot lock the owner's
  deletion (§1.8; Apple guideline 5.1.1(v) requires deletion to work).
- No password (social-only) → `verification_token` of purpose `reset_password` for the customer's own phone required
  (`required` / 400 `verification_invalid`); the app asks for an SMS code first, so a stolen 15-minute access token
  alone cannot delete the account.

→ **204**. Orchestrated by `customers.services.delete_account(customer_id, …)` (top layer, one transaction, lock order
Customer → Order → Payment → LoyaltyAccount; the **customer-level lock comes first** — `FOR NO KEY UPDATE`, the same
lock checkout takes (§2.7.1) — so a concurrent checkout either finishes before the delete, whose step 1 then cancels
it, or runs after it and is refused because the customer is no longer active; a concurrent provider capture of one of
the customer's orders, which holds Order → Payment → LoyaltyAccount and inserts a `Purchase` row (its foreign key
takes `FOR KEY SHARE` on the customer row), is not blocked by that lock, finishes, and the delete then sees the order
paid):
1. an `awaiting_payment` order whose Click payment is **in flight** (§2.7.2) → 409 `payment_in_progress` (nothing
   written; the app asks the customer to finish or wait ≤2 minutes, `CLICK_INFLIGHT_SECONDS`);
2. every `awaiting_payment` order → `commerce.services.cancel_by_customer` (P2; reason `customer_request`;
   processing/shipped orders continue: BTS uses the order's recipient snapshot);
3. every open future appointment → `appointments.services.cancel_by_customer` (P3; `reason = "account_deleted"`,
   `account_deleted=True`: the lead-time rule of §2.11 is skipped on this path only). An appointment that stopped being
   cancellable between the selection and its row lock (staff cancelled, rejected or attended it, or it started) is
   skipped and not counted; it never aborts the delete (integration, 2026-10-26);
4. cart lines deleted (`commerce.services.delete_cart_lines`, P0);
5. `accounts.services.delete_customer` (A1; P0 extends the built function): under the customer-level lock
   (`no_key=True`): `status=deleted`, `deleted_at`, name/image/address/password scrubbed, `language` reset to `uz`,
   `terms_version=""`, `consent_at`/`consent_ip`/`last_login_at` cleared (the audit log keeps the consent history),
   addresses deleted, social identities deleted (an `apple.revoke` outbox message for each Apple identity holding a
   token, §2.1.8), devices `is_active=False, push_enabled=False, fcm_token="", key_hash=""`, every session revoked
   `deactivated` through `revoke_customer_sessions` (customer tombstone TTL), outstanding QR tokens deleted
   (`core.qr_store.revoke_all`), the customer's `IdempotencyRecord` rows deleted; key hashes and QR tokens are cleared
   in the same transaction, the Redis deletions on commit;
6. audit `customer.deleted {orders_cancelled, appointments_cancelled}`.

**Kept:** the row and phone (unique), `LoyaltyAccount` balance and ledger, `Purchase` rows, orders, appointments,
inbox rows (deleted by the M3 retention job, N-9). **Re-registration** with the same phone goes through `register`
(or social completion) and reactivates the same id (§2.1.3) with its balance and purchases (A1). Because Uzbek
operators recycle numbers, the person who reactivates may not be the person who deleted, so the **history horizon**
applies (§7 `M2-A15`): the horizon is `Customer.history_from` (migration `accounts.0006`; set to `reactivated_at` on
a reactivation, and by the dormant-account rule below; existing reactivated rows are back-filled from
`reactivated_at`). `me/orders/`, `me/orders/{id}/` (404 beyond it), `me/appointments/`, `me/appointments/{id}/`,
`me/notifications/` and the unread count show only rows created at or after it; ledger items before the horizon keep
their amounts (the balance must add up) but show `order_id: null` and the subtitle "Avvalgi hisob"
(`label.ledger.sub_previous`); order-status pushes for orders created before the horizon are not sent (P3,
`deliver_order_status`); `Me.registered_at` shows the horizon (§2.2.1). Social identities must be linked again.

**Dormant-account rule** (§7 `M2-A16`, provisional). Operators also recycle the numbers of customers who never deleted
their account. When `auth/reset-password/`, or an SMS-only link of a social identity to an account without a password
(`auth/social/complete/`), succeeds on an account whose **last activity** is older than `CUSTOMER_DORMANT_DAYS` (180),
the same transaction — before the new session starts and before the new identity is linked — moves the history horizon
to now (`move_history_horizon`) and resets the account as at deletion, so the previous holder keeps no way in:
- profile: `name` (the app asks for a name when `Me.name` is empty), image, saved addresses (`address_text`);
  `language` = the resolved request language; consent cleared (`terms_version = ""`, `consent_at`/`consent_ip` null),
  so `Me.consent.update_required` is true and checkout and booking ask the new holder to accept the terms (§2.2.4);
- sign-in methods: **every existing social identity unlinked** (audit `customer.social_unlinked {provider, reason:
  "dormant"}` each; Apple identities enqueue `apple.revoke`, §2.1.8) — otherwise the previous holder's Google, Facebook
  or Apple identity would still sign in on any device without SMS (`M2-A10`) — and every device's `key_hash` cleared
  (the new session's device then gets a fresh key, §1.9); every other session is revoked by the sign-in itself (§1.9).
  On `auth/social/complete/` the rule runs **before** the `social_provider_linked` check, so a provider the previous
  holder had linked neither blocks nor outlives the new holder's identity. (The reset path unlinks identities and
  rotates device keys anyway, §2.1.5.)
- the previous holder's **cart lines** are deleted (`commerce.services.delete_cart_lines`, as deletion step 4).

**Last activity** = the newest of `Customer.last_login_at`, the newest customer session `last_used_at`, the newest
`Purchase`, the newest order of the customer, the newest appointment's `created_at` (any status, booked in the app
**or by staff** on the phone) and the newest `start` of a `requested`, `confirmed` or `attended` appointment
(`customers.selectors.last_activity_at`, top layer, reading `appointments.selectors.last_activity_at`), so an in-store
customer who activates the app after recent purchases or visits, or someone staff booked for recently, is not affected.

**Why this is the deletion reset.** Deletion (above) also cancels open orders and open appointments, which the dormant
rule never has to: an `awaiting_payment` order lives `reservation_minutes` (its creation is activity), and an open
appointment — any `requested`/`confirmed` one, past or future — has its `start` counted, so an account with either is
never dormant. Everything else matches deletion step by step: profile and consent scrubbed, addresses and cart lines
deleted, social identities unlinked (Apple revoked), device keys cleared, every other session revoked and its device
token cleared and QR tokens deleted by the sign-in (§1.9), and the earlier history hidden by the same horizon
(`Customer.history_from`: orders, appointments, inbox, ledger order links, order pushes). Only the status stays
`active`, because the new holder is signing in. Balance, ledger and purchases are kept (A1). Audit
`customer.history_reset {reason: "dormant", last_activity_at}`.

#### 2.2.4 `POST me/consent/`
Body `{"terms_version": str}` equal to the current version (else `terms_outdated`) → 200 `Me`. Stamps
`terms_version`, `consent_at`, `consent_ip`; audit `customer.consent_accepted {terms_version}`. When
`consent.update_required` is true the app blocks checkout and booking screens until this call succeeds; the server
enforces it on `orders/checkout/` and `appointments/` with **409 `terms_update_required`** "Foydalanish shartlarining
yangi tahririni qabul qiling." (`error.terms_update_required`, `details {"current_version"}`), on which the app opens
the consent screen.

#### 2.2.5 `PUT me/image/` · `DELETE me/image/`
Multipart part `file` (≤5 MiB, JPEG/PNG/WebP, re-encoded and EXIF-stripped by `core.images.process_image`;
`file_too_large`, `unsupported_image`, `required`) → 200 `{"image_url": str}`; the previous file is deleted.
`DELETE` → 204 (idempotent).

#### 2.2.6 `PUT me/device/`
Registers or refreshes the current device's facts and FCM token. The app calls it **on every cold start** (cheap; it
heals a token that was pruned or never stored), after every sign-in, on FCM token refresh and on
notification-permission changes.
Body `{"fcm_token": str 0..512 | null, "push_enabled": bool, "model": str ≤80, "os": str ≤40, "locale": str ≤8}`.
- `push_enabled: true` with an empty token → field `push_enabled` `push_token_missing`.
- A non-empty token held by another device row (the phone changed accounts) → that row loses the token
  (`fcm_token=""`, `push_enabled=False`, and `is_active=False` if it was active) first
  (`device_active_fcm_token_unique` backstop).
- `fcm_invalidated_at` is cleared when a new token arrives.
- **Locks:** the caller's customer row and the customer row of every device holding the token, in ascending pk
  order, before any Device or AuthSession row; the holders are read without a lock first, and if the token moved in
  the meantime the attempt rolls back and starts again (up to `REGISTER_DEVICE_ATTEMPTS = 3`, then 409
  `request_in_progress` with `Retry-After: 1` — only reachable through a writer that bypasses this service). Each
  attempt first takes a transaction advisory lock keyed on a hash of the token (lock order token → Customer → Device →
  AuthSession), so two customers
  registering the same fresh token at once never answer 500: one keeps it, the other's device loses it.
→ 200 `{"fid", "platform", "model", "os", "app_version", "push_enabled", "last_active_at"}`.
Service: `accounts.services.register_device` (P0, lock order P1).

#### 2.2.7 Addresses
```jsonc
// Address
{"id": 12, "label": "Uy", "text": "Toshkent, Chilonzor tumani, 9-kvartal, 12-uy, 45-xonadon",
 "region_code": "10", "city_code": "1001",           // BTS reference codes (§2.6); "" until chosen
 "comment": "3-podyezd, domofon 45", "latitude": 41.285431, "longitude": 69.203512,
 "is_default": true, "created_at": "…", "updated_at": "…"}
```
- `GET me/addresses/` → `{"results": [Address…]}` (default first, then `updated_at` desc; not paginated, ≤10).
- `POST me/addresses/` (**I**) → 201 `Address` via `accounts.services.add_address` (the first address becomes the
  default).
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

#### 2.2.8 Social identities of a signed-in customer
**Re-authentication** (`POST` and `DELETE` alike): a bearer token alone never changes how the account signs in.
`has_password` → `password` required and checked (`required` / `password_incorrect` "Parol noto‘g‘ri." ·
`field.password_incorrect`; LoginGuard scope `customer_reauth`, each failure also counted in `customer_login`, §1.8); no
password (social-only) → `verification_token`
of purpose `reset_password` for the customer's **own** phone required (`required` / 400 `verification_invalid`),
consumed under the customer-level lock — the app asks for an SMS code first (`send-code` `reset_password` →
`verify-code`). Otherwise a stolen session (even a 15-minute access token) could attach the thief's own Google,
Facebook or Apple identity, which then signs in on any device without SMS (`M2-A10`).

- **`POST me/social/`** (**C**, **I**). Body `{"provider": CustomerSocialProvider, "token": str, "token_type": SocialTokenType |
  null, "nonce": str | null, "authorization_code": str | null, "password": str | null, "verification_token": str |
  null}`. The token is verified exactly as in §2.1.8 (allowed pairs, nonce, replay cache; Apple: the authorization code
  is required and exchanged, the refresh token stored sealed) and the caller re-authenticated as above. → 200 `Me`. Errors: 400
  `social_token_invalid`, 400 `verification_invalid`; 409 `social_provider_linked` / `social_identity_linked`; 429
  `throttled`; 503 `provider_unavailable`. Side effects: identity row; outbox `push.account_security` (payload §2.14.2:
  `event: "social_linked"`, `provider`, `farewell_tokens: []`); audit `customer.social_linked {provider, via:
  "settings"}`.
- **`DELETE me/social/{provider}/`** (**C**). Body `{"password": str | null, "verification_token": str | null}`,
  re-authenticated as above (checked first). → 200 `Me`; idempotent (not linked → 200). Refused with 409
  `last_sign_in_method` "Bu yagona kirish usuli. Avval parol o‘rnating." (`error.last_sign_in_method`, `details
  {provider}`) when the customer has no password and this is the only identity; the app then offers `POST me/password/`
  (§2.2.9), which keeps the identity. Side effects: identity deleted (Apple: `apple.revoke`, §2.1.8); audit
  `customer.social_unlinked {provider, reason: "customer"}`; outbox `push.account_security` (payload §2.14.2: `event:
  "social_unlinked"`, `provider`, `farewell_tokens: []`).

#### 2.2.9 `POST me/password/` (**C**, **I**) — set or change the password while signed in
Body `{"current_password": str | null, "verification_token": str | null, "password": str}`.

| Field / check | Code · message (uz) · key |
|---|---|
| `has_password` → `current_password` required and correct (LoginGuard scope `customer_reauth`, each failure also counted in `customer_login`, §1.8) | `required`; `password_incorrect` "Parol noto‘g‘ri." · `field.password_incorrect`; 429 `throttled` |
| no password (social-only) → `verification_token` of purpose `reset_password` for the customer's own phone, consumed under the customer-level lock | `required`; 400 `verification_invalid` · `error.verification_invalid` |
| `password` rules as §2.1.3, plus not equal to the current password | `password_too_short` · `password_too_common` · `password_entirely_numeric` · `password_too_similar` · `max_length` · `password_unchanged` "Yangi parol eskisidan farq qilsin." · `field.password_unchanged` |

→ **200 `Me`** (`has_password: true`). Side effects (`customers.services.change_password`, P1):
`set_customer_password` (`password_changed_at`); every **other** session of the customer revoked with
`password_changed` and their devices retired (B-W1: the phones and browsers signed in elsewhere are signed out); the
**current session, its device key and every social identity are kept**; outbox `push.account_security` (payload
§2.14.2: `event: "password_changed"`, `provider: ""`, `farewell_tokens` = the signed-out devices' FCM tokens; inbox +
push to this device and those tokens); audit
`customer.password_changed {first_password: bool}`. This is the route the `last_sign_in_method` message points to, and
the way a social-only customer adds a password; the unauthenticated `auth/reset-password/` stays the recovery path that
unlinks everything (§2.1.5).

---
#### 2.2.10 Sessions — «Faol seanslar» (B-W1)
Every open session of the customer is one row: each signed-in phone (the app) and browser (the website). Modelled on
an "Active sessions" table: **Device · Location · Created · Updated**, a «Joriy» badge on the caller's own row, a row
menu «Chiqish», and «Boshqa qurilmalardan chiqish» / «Hamma joydan chiqish» (`auth/logout/` `scope`, §2.1.7).

**`GET me/sessions/`** (**C**, **P**: `page_size` 20 | 50, default 20) → `Page<CustomerSession>`, most recently active
first (`updated_at` desc, then `created_at` desc, id).
```jsonc
// CustomerSession
{"id": "6f0c1d2e-…-uuid",                 // the session id (AuthSession.id)
 "device": "Chrome (Windows)",            // browser + OS from the user agent (the BFF's X-GD-Client-UA): "Safari (Mac OS X)",
                                          // "Yandex Browser (Android)"; app sessions: "Glow Derma iOS (iPhone 15)" from
                                          // PUT me/device/ (model, else OS; "Glow Derma Android" without either);
                                          // an unknown browser: label.session.browser ("Veb-brauzer" / "Веб-браузер" / "Web browser")
 "platform": "web",                       // OrderChannel: ios | android | web | null
 "location": "Tashkent, Tashkent, UZ",    // "City, Region, CC" from the local GeoIP City database; "—" when unknown
 "created_at": "2026-10-27T10:12:00+05:00",   // the sign-in
 "updated_at": "2026-10-27T18:40:00+05:00",   // the last activity (recorded at most every 5 minutes)
 "current": true}                         // the caller's own session
```
- **Device** comes from a small server-side parser (`core.useragent`: Edge, Opera, Yandex Browser, Samsung Internet,
  Telegram/Instagram/Facebook in-app, Firefox, Chrome, Safari × Windows, iOS, Android, Chrome OS, Mac OS X, Linux); the
  user agent is the website BFF's `X-GD-Client-UA` (only with the internal secret, §1.2), else the request's own
  `User-Agent`. It is recorded at sign-in and refreshed by the activity bump when it changes.
- **Location** is resolved **locally** from a GeoIP City `.mmdb` file (MaxMind GeoLite2-City or DB-IP City Lite) at
  `GEOIP_CITY_DB`, never by an online service (compliance); unset, unreadable, a private address or an address the
  database does not know → `"—"`. It is resolved at sign-in and again only when the session's client IP changes
  (`AuthSession.last_ip`, `location`; names in English).
- **Updated** = `AuthSession.last_used_at`, bumped by `CustomerJWTAuthentication` at most every
  `DEVICE_BUMP_SECONDS` (300) per session — the same throttle as `Device.last_active_at` — and by every refresh rotation.

**`DELETE me/sessions/{id}/`** (**C**; optional `Idempotency-Key`) → **204**: the row menu «Chiqish». Revokes that
session (`logout`; tombstone and its QR keys on commit; its device retired when no session is left on it). The
caller's own session may be named too (it is a logout: the next request answers 401 `SESSION_EXPIRED`). Unknown id,
not a UUID, already ended or another customer's session → **404 `not_found`**. Audit `customer.session_revoked {sid,
current}`.

**`POST me/sessions/revoke-others/`** (**C**; optional `Idempotency-Key`; no body) → **200 `{"revoked": int}`**: «Boshqa
qurilmalardan chiqish». Every open session but the caller's → `logout`, the devices left without one retired. The
caller's session is re-checked under the customer lock (401 if it ended meanwhile). Audit `customer.sessions_revoked
{count, scope: "others"}`.

The same list, without `current`, is on the admin customer card (admin §2.6: `GET customers/{id}/sessions/`, `DELETE
customers/{id}/sessions/{session_id}/` with reason `admin_revoked`).

### 2.3 Catalog (public, **O**)

**Visibility** (`catalog.selectors.customer_visible_products()`, P0): `Product.status = active` ∧ `price ≥ 1000` ∧
category set and `Category.is_active` (a hidden category hides its products, design 01 open point 12). Anything else
is 404 here and `unavailable` in the cart. Availability (`available`) comes from
`commerce.selectors.customer_available_stock` (P0: the B5 `available_stock`, or 0 for every product while online
selling is off, below) — which is why these views live in the top-layer `customers` module (catalog may not import
commerce).

**Online selling requires live Billz** (`core.integration_modes.online_sales_enabled()` = ¬`fake_in_production("billz")`, P0).
Billz owns the stock every reservation is checked against (§1.4); a fake Billz under the production settings knows no
real quantity, so the stock is **unknown** and nothing is sold online against it. In that posture: the catalog still
lists every visible product, but `available` is 0, `stock_status` `out_of_stock` and `max_quantity` 0 for all of them
(cart lines read `out_of_stock` too); `settings/public/` answers `online_sales_enabled: false` (§2.4) and the app
shows the products without "Savatga" and the cart without checkout; `POST cart/items/`, `PATCH cart/items/{id}/`
and `orders/checkout/` answer **409 `online_sales_unavailable`** "Onlayn buyurtma vaqtincha mavjud emas. Mahsulotlarni
do‘konimizdan xarid qilishingiz mumkin." (`error.online_sales_unavailable`) before anything else; the fake never
writes stock — the admin Billz import and every admin action that would read Billz answer 409 (admin §2.3) and
`catalog.services.sync_stock` is never reached through a fake. Orders placed while Billz was live keep their
reservations and can still be paid; their sales are entered in Billz by hand (§6.14). Under the local/test settings
the fake Billz serves stock normally.

```jsonc
// ProductCard (list item)
{"id": 1,
 "slug": "gialuron-kislotali-serum",                // B-W1: the website URL segment (unique, never only digits)
 "name": "Gialuron kislotali serum",       // resolved language → uz → billz_name
 "volume": "30 ml", "price": 185000, "promo": false,
 "category": {"slug": "serums", "name": "Serumlar"},
 "main_image_url": "https://media.example.uz/products/1/9f2c.webp",   // | null
 "available": 12, "stock_status": "in_stock",       // StockStatus: in_stock (available > 0) | out_of_stock
 "in_cart_quantity": 0,                              // 0 when anonymous
 "updated_at": "2026-10-22T18:04:00+05:00"}         // B-W1: last change of the product page (texts, price, status,
                                                    // gallery) — the sitemap lastmod
// ProductDetail = ProductCard + 
{"description": "…", "ingredients": "…", "usage": "…",             // resolved language, "" when empty
 "images": [{"id": 31, "url": "https://…/9f2c.webp", "position": 0}],
 "max_quantity": 10,                                 // min(available, CART_MAX_QTY_PER_LINE); 0 = cannot be added
 "seo_title": "",                                    // B-W1: staff-written page title in the resolved language ONLY
 "seo_description": ""}                              // (never another language's text); "" = the website builds one
```

**`GET catalog/banners/`** → `{"results": [Banner…]}` (not paginated, ≤20): `is_active` ∧ (`starts_at` null or ≤ now) ∧
(`ends_at` null or > now), ordered by `position`, id.
```jsonc
{"id": 3, "title": "Kuzgi chegirmalar", "image_url": "https://…/77aa.webp", "image_width": 1200, "image_height": 600,
 "link": {"kind": "product", "product_id": 4, "category": null, "url": null}}   // a link to an invisible product/inactive category renders kind "none"
// gap X-BANNER-URL: "link": {"kind": "url", "product_id": null, "category": null, "url": "https://glowderma.uz/aksiya"}
```
`link.kind` `url` (BannerLinkKind, admin §2.5): the app opens `link.url` — always an absolute `https` URL ≤500 (validated
when staff save it; open question GC-3) — in the system browser (or the in-app browser), never in a web view with the
session. Every key of `link` is always present (`null` when it does not apply).
**`GET catalog/categories/`** → `{"results": [{"slug", "name", "image_url" | null, "product_count", "updated_at",
"seo_title", "seo_description"}]}`: active categories by `position`; `product_count` = visible products; `image_url` is
the image staff upload in the admin (admin §2.4, gap ADM-5.1), `null` without one; B-W1: `updated_at` (sitemap) and
the staff-written `seo_title` / `seo_description` in the resolved language only (`""` = none).

**`GET catalog/products/`** → `Page<ProductCard>`

| Param | Values | Rule |
|---|---|---|
| `search` | str ≤64 (trimmed; longer input cut to 64) | Case-insensitive partial match over the uz/ru/en names, `billz_name`, `billz_id` (`catalog.selectors.filter_products_search`, pg_trgm), folded on both sides: the Uzbek o‘/g‘ mark typed as `'`, `` ` ``, `‘`, `’`, `ʻ` or `ʼ` is one character and `ё` = `е` (`ko'pik` finds "ko‘pik"; open question GC-1). Fewer than 2 characters is ignored. |
| `category` | slug | unknown or inactive slug → 400 `invalid_choice` |
| `in_stock` | bool | `available > 0` (no product while online selling is off) |
| `promo` | bool | |
| `ordering` | `default` (newest `created_at` first), `price`, `-price`, `name` | ties by `id` desc; `name` uses the resolved-language name under `glowderma_ci` |
| `page`, `page_size` | §1.5 | |

**`GET catalog/products/{id}/`** → `ProductDetail`; 404 when not visible.

**`GET catalog/products/{slug}/`** (B-W1) → the same `ProductDetail` by the product's slug (the website's readable URL);
404 when unknown or not visible. A digits-only segment is always the id route (a slug is never only digits). A slug is
derived once from the Uzbek name (else the Billz name) when the product is created — Uzbek apostrophe letters and
Cyrillic transliterated (`core.slugs`) — and changes only when staff edit it (admin §2.3), so the website keeps a
stable URL; it 301-redirects id URLs to the slug.

### 2.4 Settings
**`GET settings/public/`** (**O**, cache 60 s, `site_config.selectors` + env):
```jsonc
{"support_phone": "+998712000000",        // seller ("Sotuvchi bilan bog‘lanish", max_reached screen) | null
 "store_phone": "+998712000001", "cosmetologist_phone": null,
 "telegram_url": "https://t.me/glowderma", "instagram_url": null, "website_url": "https://glowderma.uz",
 "pickup_address": "Toshkent, Yunusobod, …",          // resolved language
 "privacy_policy": "…", "terms": "…", "booking_policy": "…",   // resolved language; "" when not filled; terms and privacy = the PUBLISHED copy
 "terms_version": "20261001-1",                       // | null while the terms are not published (registration answers 503 terms_not_published); versions terms AND privacy policy
 "terms_updated_at": "2026-10-01T10:15:00+05:00",     // | null; when that version was published (null whenever terms_version is null)
 "reservation_minutes": 15,
 "payment_providers": ["click", "payme"],             // providers that can take a payment now; [] = online payment unavailable (the app hides checkout)
 "online_sales_enabled": true,                        // false while Billz is fake in production: stock unknown, nothing can be added to the cart or checked out (§2.3)
 "fulfilment_methods": ["delivery", "pickup"],        // FulfilmentMethod[] offered at checkout; no "delivery" while BTS is fake in production (§2.6)
 "min_app_version": {"ios": "1.0.0", "android": "1.0.0"},
 "store_urls": {"ios": "https://apps.apple.com/…", "android": "https://play.google.com/…"}}
```
Staff set the phones, links and texts in admin §2.11 (`PATCH settings/`) and publish the terms there
(`POST settings/terms/publish/`, gap closure 2026-10-27). `terms` and `privacy_policy` are the **published** copies:
staff edit drafts, and the served texts change only together with `terms_version` (`YYYYMMDD-n`, the Tashkent day of
the publication and its count that day), so the text shown under a version is exactly the text a customer accepts
with it. `booking_policy` is not versioned and is served once saved (after the ≤60 s cache). A new version asks
every customer to accept again (§2.2.4) and is the only one registration accepts (§2.1.3). The links are absolute
`https` URLs on a public host (Telegram `t.me/<handle>`, Instagram `instagram.com/<handle>`) or `null`; they never
contain quotes, angle brackets, backticks, backslashes, whitespace or invisible control characters.

`payment_providers` lists Click and Payme in this order, each only when it can take a payment: a provider whose
mode is `fake` is listed under the local/test settings and **omitted under the production settings**, where its
callbacks are refused (§2.13.0, §6.14). Checkout and pay refuse a provider outside the list (400 `invalid_choice`,
§2.7.1 row 1, §2.7.2). With an empty list the app shows the cart without the checkout button.
`fulfilment_methods` lists `delivery` and `pickup` in this order; `delivery` is omitted while delivery is not offered
(§2.6: the BTS port is fake under the production settings), and then the app offers pickup only.
`online_sales_enabled` = `core.integration_modes.online_sales_enabled()`: false while the Billz port is fake under the
production settings (§2.3); the app then hides "Savatga" and checkout and shows the catalog for browsing only (the
server refuses those writes with `online_sales_unavailable` anyway). It does not hide the "To‘lash" button of an order
placed earlier.

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
              "max_usable_pickup": 48600},            // cashback_max(items_subtotal, 0, available) (§2.7); for delivery use delivery/calculate/ → cashback_max
 "updated_at": "2026-10-26T11:58:00+05:00",
 "last_change": null}                                // set on POST/PATCH responses: {"product_id", "requested_quantity", "quantity", "max_reached"}
```
Line status: `unavailable` = not visible (§2.3); `out_of_stock` = `available ≤ 0`; `insufficient` = `quantity >
max_quantity > 0`; else `ok`. A GET never changes stored quantities.

**Stock freshness** (gap X-STOCK-FRESHNESS, open questions GC-5/GC-6). Billz is polled (no webhooks): a delta sync every
`BILLZ_DELTA_SYNC_SECONDS` (300) and a daily full one. While Billz is **live**, `POST cart/items/` and
`PATCH cart/items/{id}/` first re-read from Billz the stock of their product when its `ProductStock.synced_at` is older
than `BILLZ_STOCK_MAX_AGE_SECONDS` (900; never read counts as older), through the Billz port with the interactive client
profile and **4 s** for the request, **before** the request's transaction and every row lock; the read is written at
once (`catalog.services.sync_stock`, never backwards) and the write then works on it (`available`, `max_quantity`,
`OUT_OF_STOCK`). A barcode Billz no longer knows reads as stock 0. When Billz cannot be reached the cart write carries on
from the cached stock — **cart writes never answer 503 for Billz**. Billz is not asked for a write that will be refused
or replayed anyway (GC-10): a body that does not validate, a missing or malformed `Idempotency-Key` on `POST`, a key
whose stored response is replayed, a product the customer cannot see, or (`PATCH`) a product that is not in the cart. A `GET` never calls Billz. With a fake Billz nothing
changes (no read, the cached stock as before). Every write locks the `Cart` row
(`select_for_update`; created lazily with `get_or_create`) and stores `price_at_add = current price` on the touched
line (model change `commerce.0004_cartitem_price_at_add`, §7 `M2-C12`).

| Endpoint | Body / params | Rules and errors |
|---|---|---|
| **`GET cart/`** | – | 200 `Cart` |
| **`POST cart/items/`** (**I**: an increment, so a network retry must replay, not add again) | `{"product_id": int, "quantity": int 1..99 = 1}` | Online selling off (§2.3) → 409 `online_sales_unavailable` (`error.online_sales_unavailable`), checked first. Adds to an existing line. New quantity = min(old + quantity, `max_quantity`); clamped → `last_change.max_reached = true` (200, not an error). Not visible → 409 `product_unavailable` "Mahsulot hozir sotuvda yo‘q." (`error.product_unavailable`); `max_quantity = 0` → 409 `OUT_OF_STOCK` "Bu mahsulot qolmagan." (`error.out_of_stock_one`, `details {"product_id", "available": 0}`); a new line when the cart has `CART_MAX_LINES` (30) lines → 409 `cart_full` "Savatga ko‘pi bilan {limit} xil mahsulot qo‘shish mumkin." (`error.cart_full`). Unknown id → `product_id` `does_not_exist`. |
| **`PATCH cart/items/{product_id}/`** | `{"quantity": int 1..99}` | Online selling off → 409 `online_sales_unavailable`, checked first. Absolute (retry-safe; the app's +/− steppers use it). Sets min(quantity, `max_quantity`). Line absent → 404. `max_quantity = 0` → 409 `OUT_OF_STOCK` (line unchanged). Not visible → 409 `product_unavailable`. |
| **`DELETE cart/items/{product_id}/`** | – | Removes the line; idempotent (absent line → 200). |
| **`DELETE cart/items/?product_id=4&product_id=9`** | 1..50 ids | Removes those lines; unknown ids ignored. None or >50 → 400 `product_id` `required` / `max_items`. |
| **`DELETE cart/`** | – | Empties the cart. |

All writes answer 200 `Cart`. No audit, no events (the cart is ephemeral).

### 2.6 Delivery

**Delivery availability** (`core.integration_modes.delivery_available()`, P0). Home delivery is offered only when a real
courier can carry the parcel: while `BTS_MODE=fake` under the production settings (`DEV_FEATURES_ALLOWED` false;
product-owner decision 4 allows that mode), a fake quote would charge a made-up tariff and a fake create would show a
made-up tracking number for a parcel nobody ships. In that posture `settings/public/` omits `delivery` from
`fulfilment_methods` (§2.4), `delivery/regions/` answers `{"regions": [], "updated_at": null}`, and
`delivery/calculate/` and checkout answer **409 `delivery_unavailable`** "Yetkazib berish hozircha mavjud emas. Olib
ketishni tanlang." (`error.delivery_not_offered`) for `fulfilment = delivery` before any BTS call; pickup works as
usual. What the fake port does with orders placed before the switch is §2.14.3. Under the local/test settings the
fake BTS serves delivery normally.

**`GET delivery/regions/`** (**O**) — the BTS reference list for the address form, from the BTS port
`regions()` cached 24 h under `gd:bts:regions` (fake: 4 regions below, local/test settings only; empty while delivery
is not offered).
```jsonc
{"regions": [{"code": "10", "name": "Toshkent shahri",
              "cities": [{"code": "1001", "name": "Chilonzor tumani"}, {"code": "1002", "name": "Yunusobod tumani"}]},
             {"code": "11", "name": "Toshkent viloyati", "cities": [ /* … */ ]}],
 "updated_at": "2026-10-26T03:00:00+05:00"}      // | null while delivery is not offered
```
Names in the resolved language when BTS provides it, else as BTS sends them **(verify M3)**.

**`POST delivery/calculate/`** (**C**) — price and cashback cap shown before checkout (computed over the customer's
current cart lines with status `ok`). Served by `commerce/api/customer/urls.py` (same path): it reads the cart,
`loyalty` and `commerce.selectors.cashback_max`, which the `delivery` layer may not import; it uses
`delivery.selectors.region_catalog` and the BTS port. `delivery/api/customer/` serves only `delivery/regions/`.
Body `{"fulfilment": "delivery" | "pickup", "address_id": int | null, "region_code": str | null, "city_code": str | null, "cashback_to_use": int ≥ 0 | null}`.

| Check | Code · message (uz) · key |
|---|---|
| `fulfilment` | `required`, `invalid_choice` |
| delivery offered (above; else no BTS call) | 409 `delivery_unavailable` "Yetkazib berish hozircha mavjud emas. Olib ketishni tanlang." · `error.delivery_not_offered` |
| delivery: exactly one of `address_id` or (`region_code` + `city_code`) | `address_id` `required` "Yetkazib berish manzilini tanlang." · `field.address_required` |
| `address_id` of this customer | `does_not_exist` "Manzil topilmadi." · `field.address_not_found` |
| region/city present and in the BTS reference list | `address_incomplete` "Manzilda viloyat va tumanni tanlang." · `field.address_incomplete` |
| BTS refuses the route | 409 `delivery_unavailable` "Bu hududga yetkazib berish mavjud emas." · `error.delivery_unavailable` |
| BTS transport failure | 503 `provider_unavailable` |

```jsonc
// 200 delivery
{"fulfilment": "delivery", "delivery_fee": 25000, "region_code": "10", "city_code": "1001",
 "weight_g": 1000,                                   // BTS_DEFAULT_PARCEL_WEIGHT_G (product weights are not modelled, §7 M2-O9)
 "pickup_address": null, "quoted_at": "2026-10-26T12:00:00+05:00",
 "items_subtotal": 808000,
 "cashback_max": 48600,                              // cashback_max(items_subtotal, delivery_fee, available), §2.7
 "cashback_to_use": 48600,                           // min(body value, cashback_max); 0 when the body sent none
 "expected_total": 784400}                           // items_subtotal + delivery_fee − cashback_to_use: send it to checkout
// 200 pickup
{"fulfilment": "pickup", "delivery_fee": 0, "region_code": null, "city_code": null, "weight_g": null,
 "pickup_address": "Toshkent, Yunusobod, …", "quoted_at": "2026-10-26T12:00:00+05:00",
 "items_subtotal": 808000, "cashback_max": 48600, "cashback_to_use": 0, "expected_total": 808000}
```
An empty cart answers 409 `cart_empty`. The app never re-implements the formula: it shows `expected_total` and sends
it unchanged to checkout.

**BTS tariff call (verify M3):** the port's `quote(region_code, city_code)` asks BTS's tariff calculation with the
sender city `BTS_SENDER_CITY_CODE`, the receiver region/city, weight `BTS_DEFAULT_PARCEL_WEIGHT_G` and one parcel; the
answer's price in so'm is the fee. The quote is informative: checkout quotes again (§2.7.1) and the checkout quote is
what the order stores.

### 2.7 Checkout, pay, cancel, reorder

**Cashback cap (one formula, used by the cart, `delivery/calculate/` and checkout):**
`cashback_max(items_subtotal, delivery_fee, available) = max(0, min(available, items_subtotal, items_subtotal +
delivery_fee − CHECKOUT_MIN_PAYABLE))`. Cashback never pays delivery (C-4) and always leaves a payable remainder of at
least `CHECKOUT_MIN_PAYABLE` (1 000 so'm) for the provider (§7 `M2-C14`).

#### 2.7.1 `POST orders/checkout/` (**C**, **I**, throttle `checkout`)
Body:
```jsonc
{"fulfilment": "delivery",            // FulfilmentMethod
 "address_id": 12,                    // delivery: required, own address; pickup: must be null
 "payment_provider": "click",         // PaymentProvider listed in settings/public payment_providers (§2.4)
 "cashback_to_use": 48600,            // int ≥ 0, default 0
 "expected_total": 784400,            // the total the app showed; required
 "customer_note": "Soat 18:00 dan keyin"}   // ≤300, default ""
```
**Stock freshness before the transaction** (gap X-STOCK-FRESHNESS, open questions GC-5/GC-6). While Billz is live, a
checkout whose body is valid first re-reads from Billz — **before** the `@idempotent` transaction, holding no lock — the
stock of every cart line whose `ProductStock.synced_at` is older than `BILLZ_STOCK_MAX_AGE_SECONDS` (900): never read
first, then the oldest, one lookup per barcode, the interactive profile, **4 s** for all of them; each read is committed
at once (`catalog.services.sync_stock`), so it is kept even when the checkout is then refused, and rows 7–9 run on it.
A line Billz could not read keeps its cached stock; row 4a decides. Only the lines Billz **failed** to answer count for
row 4a (transport error, 5xx, circuit open, or no answer at all within the 4 s); lines the 4 s budget, the rate limiter
or a 429 pause left unread after Billz had answered are *deferred* and go on from the cache (GC-10). Nothing is read for
a checkout without a well-formed `Idempotency-Key`, for a replayed key, or for lines whose product is not visible. With a
fake Billz nothing is read (§2.3).

**Serialisation.** Two checkouts of one customer (two taps, a retry with a renewed key, two app processes) must not
both pass the open-order check. Checkout therefore runs, inside the `@idempotent` transaction:
(a) body validation, consent, address checks and the BTS quote (an HTTP call while **no row lock** is held; the
idempotency advisory lock of this key is the only lock), then (b) the **customer-level lock** —
`SELECT … FOR NO KEY UPDATE` on the Customer row (`select_for_update(no_key=True)`) — then the open-order check and
the cart read **under that lock**, then (c) `create_order`. The lock order of the customer API is **Customer → Cart →
ProductStock → Order → Payment → LoyaltyAccount** (the global Order → Payment → LoyaltyAccount rule with the two
customer-level locks in front). Cart writes take only the Cart row lock, which checkout reaches after the Customer
lock. The customer-level lock is `FOR NO KEY UPDATE`, **never** `FOR UPDATE`: every insert of a row that references
the customer (`Purchase` on a capture, `Order`, `AuthSession`, `Appointment`) takes `FOR KEY SHARE` on the customer row
through its foreign key, which conflicts with `FOR UPDATE` but not with `FOR NO KEY UPDATE`. With `FOR UPDATE`, a
capture holding Order → Payment → LoyaltyAccount and inserting its `Purchase` would wait for the customer row while
`DELETE me/` (or a second checkout once `CHECKOUT_MAX_OPEN_ORDERS` > 1) holds it and waits for that Order or
LoyaltyAccount — a deadlock. `FOR NO KEY UPDATE` still serialises checkouts, bookings, sign-ins and `DELETE me/` of
one customer against each other, and no cycle exists. `DELETE me/` takes the same lock first (§2.2.3); updating the
customer's own non-key columns (status, name, consent) needs no stronger lock.

Checks, in this order (the first failure answers):

| # | Check | Result · message (uz) · key |
|---|---|---|
| 0 | Online selling on (§2.3: not while Billz is fake under the production settings); nothing is read or quoted | 409 `online_sales_unavailable` "Onlayn buyurtma vaqtincha mavjud emas. Mahsulotlarni do‘konimizdan xarid qilishingiz mumkin." · `error.online_sales_unavailable` |
| 1 | Body fields | 400: `fulfilment` `invalid_choice`; `payment_provider` `invalid_choice` (also when it is not in `payment_providers`, §2.4); `address_id` `required` (`field.address_required`) / `invalid` (pickup with an address); `cashback_to_use` `min_value`; `expected_total` `required`/`min_value`; `customer_note` `max_length` |
| 2 | Consent current (§2.2.4) | 409 `terms_update_required` · `error.terms_update_required`, `details {current_version}` |
| 3 | Delivery: offered (§2.6; else 409 `delivery_unavailable` · `error.delivery_not_offered`, no BTS call), address own (`does_not_exist` `field.address_not_found`), region/city known (`address_incomplete`) | 409 / 400 |
| 4 | Delivery: BTS quote (`quote(region_code, city_code)`, no row lock held; passed to `create_order` as `delivery_fee`) | 409 `delivery_unavailable` / 503 `provider_unavailable` |
| 4a | Live Billz: every line the re-read above could not confirm because Billz failed (not merely *deferred*, GC-10) has a cached stock read within `BILLZ_STOCK_STALE_REFUSE_SECONDS` (3600; a line never read from Billz fails). Otherwise the checkout proceeds on the cache | 503 `provider_unavailable` "Tashqi xizmat javob bermadi. Qayta urinib ko‘ring." · `error.provider_unavailable` (nothing written but the stock reads; a retry re-reads — the reads that succeeded are kept, the oldest lines are read first) |
| — | *Customer row locked (`select_for_update(no_key=True)`); customer still `active` (else 401 `SESSION_EXPIRED`)* | |
| 5 | No other `awaiting_payment` order (`CHECKOUT_MAX_OPEN_ORDERS`, default 1) | 409 `open_order_exists` "Sizda to‘lanmagan buyurtma bor (№{order_id}). Avval uni to‘lang yoki bekor qiling." · `error.open_order_exists`, `details {order_id, pay_by}` |
| 6 | Cart has lines (read under the lock) | 409 `cart_empty` "Savat bo‘sh." · `error.cart_empty` |
| — | *inside `create_order`: `ProductStock` rows locked* | |
| 7 | Every line customer-visible (`catalog.selectors.customer_visible_products()`: active, price ≥ 1000, active category) and `available ≥ quantity`, re-checked **under the `ProductStock` row locks** | 409 `OUT_OF_STOCK` "Ba’zi mahsulotlar yetarli emas." · `error.OUT_OF_STOCK`, `details.lines[] {product_id, name, requested, available, status}` (every problem line; `status` `unavailable` / `out_of_stock` / `insufficient`) |
| 8 | `cashback_to_use ≤ cashback_max(items_subtotal, delivery_fee, ∞)` (the subtotal/fee part of the formula, evaluated on the live subtotal inside the transaction) | 400 `cashback_to_use` `max_value` "Ko‘pi bilan {max} so‘m cashback ishlatish mumkin." · `field.cashback_max` |
| 9 | `expected_total == items_subtotal + delivery_fee − cashback_to_use` (live prices) | 409 `price_changed` "Narxlar yangilandi. Buyurtmani qayta tekshiring." · `error.price_changed`, `details {total, items_subtotal, delivery_fee, cashback_used, cart}` |
| 10 | `cashback_to_use ≤ available` (checked under the LoyaltyAccount lock by `loyalty.services.create_hold`) | 400 `cashback_to_use` `insufficient_balance` "Cashback balansi yetarli emas (mavjud: {available} so‘m)." · `field.cashback_insufficient` |

Rows 7–10 are raised by `create_order`, in this order, as typed errors that the view maps to the rows above:
`OutOfStock(lines=[…])`, `CashbackAboveMax(max)`, `PriceChanged(totals)` and `InsufficientCashback(available)` (the
last from `create_hold`, which runs after the order row exists; the whole transaction rolls back). Address problems
found inside `create_order` raise `AddressNotFound` / `AddressIncomplete` (rows 3). A generic `CheckoutError` reaching
the view is a bug (500).

→ **201 `OrderDetail`** (§2.8, `status: awaiting_payment`, `actions.pay: true`, `pay_by` set).

**Side effects** — one transaction owned by `commerce` (the B5 service, extended, not a re-implementation):
`commerce.services.create_order(customer_id, items=<cart lines>, fulfilment, payment_provider, channel=<session
device platform>, address_id, cashback_used, delivery_fee=<quote>, expected_total, min_payable=CHECKOUT_MIN_PAYABLE,
visible_only=True, customer_note=<body customer_note>, metadata={"checkout": {"app_version", "device_id"}, "fake":
<X-GD-Fake-Scenario>})`, which writes
(B5, design 03 §4.1 row 1): `Order` (`awaiting_payment`, address + recipient snapshot, amounts, `customer_note`,
`reservation_minutes =
SiteSettings.reservation_minutes`, `reservation_expires_at`), `OrderItem` snapshots (name, billz id, image URL, unit
price, promo), one active `StockReservation` per line, the `CashbackHold` (if `cashback_to_use > 0`, `expires_at =
reservation_expires_at`), the `pending` `Payment` (`provider = payment_provider`, `amount = total`; one active payment
per order), the delivery `Shipment` (`not_created`), events `order_created {channel, item_count, total}` +
`reservation_created {item_count, minutes, expires_at}`. Then, still in the transaction: cart lines deleted,
`Cart.last_checkout_order`. No outbox, no audit (system row; the customer is the actor of the idempotency record).
Replay with the same key → the stored 201 even though the cart is now empty.

#### 2.7.2 `POST orders/{id}/pay/` (**C**, **I**, throttle `pay`)
Body `{"provider": PaymentProvider | null}` (default: the order's `payment_provider`). Returns where to pay; it never
moves money, never creates order events and never expires an order.

**Definitions** (`commerce.services`, P2; one definition for the app, the jobs and every provider callback):
- **attached**: the payment carries a provider transaction — Click `provider_state.prepare_at` (a Prepare answered
  `0`), Payme `provider_state.create_time` (a `CreateTransaction` answered state 1). `attached_at` is that instant.
- **open transaction**: a `pending` payment that is attached and not completed — Click: Prepare answered `0`, no
  Complete yet; Payme: `provider_state.state = 1`.
- **in flight**: an open transaction the customer may be confirming right now — Click: open and `now < prepare_at +
  CLICK_INFLIGHT_SECONDS` (120; never longer than the grace, settings validation); Payme: open (state 1, until Payme
  performs or cancels it). In flight blocks only a switch to the **other** provider, the customer cancel and `DELETE
  me/`; it never blocks paying again with the same provider (below).
- **payable** — `is_payable(order, payment, now, *, attaching: bool)`: `order.status = awaiting_payment` ∧ (`now <
  reservation_expires_at` ∨ (¬`attaching` ∧ the payment was attached **before** `reservation_expires_at` ∧ `now <
  reservation_expires_at + PAYMENT_IN_FLIGHT_GRACE_SECONDS` (300))). Starting a transaction (Click Prepare, Payme
  `CreateTransaction` with a new id) always needs `now < reservation_expires_at`; only completing a transaction that
  was already attached in time (Click Complete, Payme `PerformTransaction`) gets the grace. The grace is anchored to
  `reservation_expires_at`, so repeated prepares cannot extend an order.
- **Click supersede rule.** Re-opening Click's pay page starts a **new** Click transaction (new `click_trans_id`). A
  Prepare with a new `click_trans_id` for an order that is still payable (`attaching=True`) therefore supersedes the
  order's open Click transaction, whatever its age: the old payment is failed `abandoned` and a new `pending` row is
  attached (§2.13.1 Prepare step 8). If the old transaction still completes with money taken, that is a refused
  capture (§2.13.0): accepted and reversed. So a customer who leaves Click's page after Prepare can pay again at once.
- The expiry job (`expire_due_reservations`) skips an order whose payment has an **open transaction attached before
  `reservation_expires_at`**, until `reservation_expires_at + PAYMENT_IN_FLIGHT_GRACE_SECONDS`; then it expires the
  order (a Click payment is failed `expired`; a Payme state-1 payment stays pending, P-2). A capture that still lands
  is a **refused capture** (§2.13.0) and is refunded.

Checked in this order (a cancelled order that is being refunded answers `not_payable`, never `already_paid`):

| Check | Result · message (uz) · key |
|---|---|
| Order of another customer | 404 |
| Status `cancelled` | 409 `not_payable` "Bu buyurtmani to‘lab bo‘lmaydi." · `error.not_payable` |
| Status `processing`/`handed_to_courier`/`delivered`, or a `paid` payment that is not a late capture (`error_code ≠ "late_capture"`) exists | 409 `already_paid` "Buyurtma allaqachon to‘langan." · `error.already_paid` |
| Not payable with `attaching=True` (`now ≥ reservation_expires_at`; the job has not run yet) | 409 `order_expired` "To‘lov muddati tugadi. Mahsulotlarni savatga qaytarib, qaytadan rasmiylashtiring." · `error.order_expired` — **nothing is written** (§1.7); the expiry job cancels the order |
| `provider` differs from the provider of an in-flight payment (Click prepared < `CLICK_INFLIGHT_SECONDS` ago without Complete, or Payme state 1) | 409 `payment_in_progress` "To‘lov {provider} ilovasida jarayonda. Uni yakunlang yoki biroz kuting." · `error.payment_in_progress`, `details {provider, order_id}` |
| `provider` not in `settings/public` `payment_providers` (§2.4) | 400 `provider` `invalid_choice` |

**Side effects** (`commerce.services.ensure_payable_payment(order_id, provider)`, P2, under `lock_order` → Payment
locks): the active `pending` payment of the **same** provider is **reused** as it is, attached or not (nothing is
written; the app opens the provider page again, and a Click Prepare with a new `click_trans_id` supersedes an open
Click transaction, above); a pending payment of the **other** provider that is not in flight is failed with
`error_code="abandoned"` (`payments.services.fail_payment(unilateral=True)`) and a new `pending` payment is created
with `amount = order.total`, `Order.payment_provider` updated; a failed previous attempt gets a new `pending` row. One
active payment per order is guaranteed by the partial unique index.
```jsonc
// 200
{"order_id": 10248, "payment_id": 301, "provider": "click", "amount": 784400,
 "pay_by": "2026-10-23T14:17:00+05:00",
 "account_ref": "10248-3f9a0c1b2d",                // §1.4; what the provider sends back
 "web_url": "https://my.click.uz/services/pay?service_id=…&merchant_id=…&amount=784400&transaction_param=10248-3f9a0c1b2d&return_url=https%3A%2F%2Fglowderma.uz%2Fpay%2Freturn%3Forder_id%3D10248",
 "app_url": null,                                   // always null in v1 (§7 M2-P9); open web_url in the system browser
 "return_url": "https://glowderma.uz/pay/return?order_id=10248"}
```

| Provider | `web_url` (verify M3) |
|---|---|
| Click | `{CLICK_PAY_URL}?service_id={CLICK_SERVICE_ID}&merchant_id={CLICK_MERCHANT_ID}&amount={total}&transaction_param={account_ref}&return_url={urlencode(return_url)}` — `amount` in so'm without decimals; `transaction_param` comes back as `merchant_trans_id` |
| Payme | `{PAYME_CHECKOUT_URL}/{base64("m={PAYME_MERCHANT_ID};ac.{PAYME_ACCOUNT_FIELD}={account_ref};a={total×100};l={lang};c={urlencode(return_url)};ct=15000")}` — `a` in tiyin; `l` = resolved language |

**Account reference.** The provider account value is never the bare sequential order id: `account_ref =
"<order_id>-<ref>"` with `ref` = the first 10 hex chars of `HMAC-SHA256(PAYMENT_REF_KEY, str(order_id))`
(`commerce.payment_ref.build/verify`, P2; no migration, the value is recomputed). Click Prepare, Payme
`CheckPerformTransaction` and `CreateTransaction` verify it with `hmac.compare_digest` before any lookup (§2.13), so a
third party cannot open or block a payment for someone else's order by guessing ids. Echoes (`merchant_trans_id`,
`account[PAYME_ACCOUNT_FIELD]`) repeat the full reference.

**Opening the provider.** The app opens `web_url` in the **system browser** (iOS `SFSafariViewController` is not
enough for the Click/Payme app hand-off: use `UIApplication.open`; Android a Custom Tab or the default browser), never
in an embedded WebView: the provider pages hand off to the installed Click/Payme app through their own universal/app
links when available. **Website (B-W1):** a pay request with `X-GD-Platform: web` gets `return_url` =
`PAYMENT_WEB_RETURN_URL`, a template with `{order_id}`, `{lang}` (the resolved language) and `{locale_prefix}` (`""` for
uz, `/ru`, `/en`) — default `https://glowderma.uz{locale_prefix}/buyurtmalar/{order_id}?paid=1` — the website's own order
page (no app link), where the website polls the order as below. For the app, `return_url` is the https universal/app link `PAYMENT_RETURN_URL` (`https://glowderma.uz/pay/return`,
served with `apple-app-site-association` / `assetlinks.json` by the website) with `order_id` appended; when the app
is not installed the page offers the custom-scheme fallback `PAYMENT_RETURN_FALLBACK_URL` (`glowderma://payment/return`).
Both are server-configured (never taken from the client, no open redirect). On return **and on every app resume**
while an order is `awaiting_payment`, the app polls `GET me/orders/{id}/` every 2 s for up to 60 s until
`payment.status` is `paid`; while `payment.in_progress` is true it shows "To‘lov tekshirilmoqda"
(`label.payment.in_progress`) instead of a countdown, and — for Click — keeps the "To‘lash" button (`actions.pay`): a
customer who left Click's page can pay again at once (the new Click transaction supersedes the old one, §2.7.2); a
customer who never returns is covered by the push of the `processing` status.

#### 2.7.3 `POST orders/{id}/cancel/` (**C**, **I**)
Empty body. Allowed only from `awaiting_payment` (C-6; design 03 §4.1 row 5) → 200 `OrderDetail`.
- Else 409 `not_cancellable` "Bu buyurtmani ilovadan bekor qilib bo‘lmaydi. Sotuvchi bilan bog‘laning."
  (`error.order_not_cancellable`). 404 for another customer's order.
- A **Click** payment in flight → 409 `payment_in_progress` `{provider: "click", order_id}` (the customer may be
  confirming on Click's page right now; a cancel would turn that capture into a refund). A Payme state-1 transaction
  does **not** block: Payme debits only on `PerformTransaction`, which a cancelled order refuses (P-2).

Side effects (`commerce.services.cancel_by_customer(order_id, customer_id)`, P2, same transaction rules as the
admin cancel row 4): the pending payment is failed unilaterally (`cancelled`), except a Payme state-1 transaction,
which stays `pending` until Payme's `CancelTransaction` (P-2); reservations released (`cancelled`); hold released
(`order_cancelled`); `status = cancelled`, `cancellation_reason = customer_request`, `cancelled_by = null`; event
`cancelled {reason: "customer_request", note: null, refund_id: null, cashback_reversed: false, stock_released,
bts_cancelled: false, push_sent}` with `source = system`, `actor = null`; outbox `push.order_status cancelled`
(state.py row 5). No audit (the event and idempotency record name the actor). The cart is **not** refilled
automatically (the customer may have built a new cart meanwhile); `actions.reorder` offers it.

#### 2.7.4 `POST orders/{id}/reorder/` (**C**, **I**)
Empty body. Copies the order's lines back into the server cart (any status; typically after `order_expired` or a
cancel) → 200 `Cart` (§2.5) with `last_change: null`. Per line, through the same rules as `POST cart/items/` (Cart row
lock): an existing cart line keeps the larger of its quantity and the order quantity; the quantity is clamped to
`max_quantity`; products that are no longer visible or are out of stock are added anyway so the customer sees them
with status `unavailable` / `out_of_stock` (they are skipped only when the cart would exceed `CART_MAX_LINES`, then
409 `cart_full` with nothing written). 404 for another customer's order or an order beyond the history horizon
(§2.2.3). No audit, no event.

### 2.8 Customer orders

**`GET me/orders/`** → `Page<OrderSummary>`; filter `scope` = `active` (`awaiting_payment`, `processing`,
`handed_to_courier`) | `completed` (`delivered`) | `cancelled` | `all` (default); newest first (`created_at`, id).
Only orders created at or after the history horizon (§2.2.3).
```jsonc
// OrderSummary
{"id": 10248, "created_at": "2026-10-23T14:02:00+05:00",
 "status": "handed_to_courier", "status_label": "Kuryerga topshirildi",
 "fulfilment": "delivery", "total": 784400, "item_count": 3,
 "preview_images": ["https://…/a1b2.webp"],          // ≤3 item image URLs (snapshots), nulls skipped
 "payment": {"status": "paid", "status_label": "To‘langan", "provider": "click",
             "in_progress": false},                  // true while a provider transaction is in flight (§2.7.2)
 "pay_by": null,                                    // reservation_expires_at while awaiting_payment, else null
 "tracking_number": "48213907"}                     // | null
```
**`GET me/orders/{id}/`** → `OrderDetail` (404 for another customer's order or beyond the horizon):
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
             "confirmed_at": "2026-10-23T14:05:00+05:00",
             "in_progress": false, "in_progress_label": null},   // label "To‘lov tekshirilmoqda" when in_progress
 "refund": null,                    // | {"state": "in_progress" | "completed", "state_label", "amount", "requested_at", "completed_at"}
 "shipment": {"carrier": "BTS", "tracking_number": "48213907", "tracking_url": null},   // null for pickup or before creation
 "cashback": {"used": 48600, "earned": 5680,
              "earned_state": "credited"},  // none | credited | reversed (purchase reversed)
 "cancellation": null,              // | {"reason": CancellationReason, "label": "…"}  (staff notes are never shown)
 "progress": [ /* ProgressStep[], §4.2 */ ],
 "timeline": [ /* TimelineEntry[], newest first, §4.1 */ ],
 "actions": {"pay": false, "cancel": false, "reorder": true}}
```
- **Display payment** (`payment` here and in `OrderSummary`, the admin order list, its `payment` filter and the admin
  order detail — one rule, P2 changes `payments.selectors.display_payment`, `pick_display` and
  `display_status_subquery` together): one ranking, newest id first within a rank — (1) a payment that is **not** a
  late capture (`error_code ≠ "late_capture"`) and is `paid` or `refunded`; (2) one that is not a late capture and is
  `pending`; (3) a **late capture** (§2.13.0; shown with its own status, `paid` until its refund succeeds, then
  `refunded` — never `pending`); (4) a `failed` payment. A double payment therefore always shows the payment that paid
  the order, in the list and in the detail alike; and an order whose only money-taking payment was captured too late
  shows that late capture — also when its other attempts failed (a superseded Click attachment captured late next to
  a newer attempt that then expired, sandbox rows 11–12; a declined first attempt) — so it never reads "To‘lov
  o‘tmadi" while taken money is being returned. While a newer attempt is still `pending` (or paid the order), that
  attempt is shown and the late capture's refund appears in the timeline (§4.1). `refund` is the newest refund of the
  **displayed** payment (null when it has none), so a displayed late capture always carries its refund `in_progress` /
  `completed`. `active_payment` ignores late captures, and dashboard revenue
  (`revenue_buckets`, O8) excludes them: they are never order revenue (admin §2.10). `actions.pay` = `awaiting_payment` ∧ `is_payable(…, attaching=True)` ∧ no Payme payment in state 1 (a Click
  payment in flight does not block it: paying again with Click supersedes, §2.7.2; a switch to Payme answers 409
  `payment_in_progress` for at most `CLICK_INFLIGHT_SECONDS`); `actions.cancel` = `awaiting_payment` ∧ no Click payment
  in flight (≤ 2 minutes after its Prepare); `actions.reorder` = not `awaiting_payment` (or
  `pay_by` passed).
- Item `name`: the live product name in the resolved language (fallback chain of §1.4) when the product still exists;
  otherwise the `OrderItem.name` snapshot, which B5 takes from `name_uz`. Prices, quantities and images are always the
  snapshots.
- Refund mapping: `requested`, `submitting`, `manual_required`, `unknown`, `failed` → `in_progress` "To‘lov
  qaytarilmoqda" (staff resolve the last three); `succeeded` → `completed` "To‘lov qaytarildi".
- `tracking_url` stays null until BTS confirms a public tracking URL format (verify M3).

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
 "holds": [{"order_id": 10250, "amount": 20000,
            "expires_at": "2026-10-26T12:15:00+05:00"}],   // EFFECTIVE expiry: reservation_expires_at, + PAYMENT_IN_FLIGHT_GRACE_SECONDS while a transaction attached in time is still open (§2.7.2)
 "qr_ttl_seconds": 60}
```
`CashbackHold.expires_at` itself stays at `reservation_expires_at`; holds are released only by the order transitions
(expiry, cancel, capture). `loyalty.services.expire_holds` exists but must **never** be scheduled while the in-flight
grace exists, because it would release a hold that a grace-period capture still needs (plan §8).
Promotional products never earn cashback (shown as a fixed sentence in the app, catalog key `label.cashback.promo_rule`).

#### 2.9.2 `GET me/cashback/ledger/` → `Page<LedgerItem>`
Newest first (`at` desc, `id` desc), the customer's own entries only. Entries before the history horizon of a
reactivated account (§2.2.3) keep `amount`/`balance_after` but show `order_id: null` and the subtitle "Avvalgi hisob"
(`label.ledger.sub_previous`).
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
  `"{customer_id}:{issued_at_epoch}:{session_id}"` (B-W1: bound to the session that issued it), `SET NX EX
  QR_TOKEN_TTL_SECONDS` (60). The raw token is never stored server-side
  and never logged.
- **Keys:** both the `SET` here and the `GETDEL` of §2.10 use the **raw** redis-py client of the cache connection
  (`django_redis`-independent helper `loyalty.qr._redis()`), with the literal key prefix `gd:loyalty:qr:` — never
  `cache.set` (Django's `KEY_PREFIX`/version would make the two keys differ). LocMem tests use the same helper's
  in-process fallback.
- **Live tokens:** at most `QR_MAX_LIVE_TOKENS` (2) **per session** (B-W1: two signed-in phones never evict each
  other's QR). The helper keeps a per-session list `gd:loyalty:qr:live:{customer_id}:s:{session_id}` (TTL 60 s) of the
  token keys it issued; issuing a third deletes the oldest key, so the screen that refreshes every 50 s always has the
  current token plus the one possibly under the scanner. Every key is also listed under the customer
  (`gd:loyalty:qr:live:{customer_id}`, capped at 64). Revocation of a session (logout, a session signed out from the
  list, the same install signing in again) deletes that session's keys; signing out everywhere, a password reset or
  `DELETE me/` deletes every key of the customer. The POS resolve (§2.10) also re-checks that the issuing session is
  still open, so a lost on-commit deletion never leaves a usable token.
- Throttle `qr` (6/min per customer) on top of `customer`. Single use is enforced at resolve time (§2.10).
- No database write, no audit (one call per minute per open screen). The app keeps the token in memory only, hides
  the QR when the screen is backgrounded, **requests a fresh token whenever the screen regains focus**, and offers a
  "Yangilash" button. Cashier re-scan (wrong customer selected, Billz hiccup): the first scan consumed the token, so the
  customer taps "Yangilash" and the cashier scans the new code.

### 2.10 POS: `POST pos/cashback/resolve-qr/` (**K**)
Called by the in-store integration (the Billz POS or a small cashier app) when the cashier scans the customer's QR.

**Auth:** header `Authorization: Api-Key <key>`; key format `gdpos_<8-char prefix>_<32 random base64url chars>`,
created by `manage.py createposkey --name "Kassa 1" [--location "Yunusobod"] --actor <staff email>` (shown once;
`--actor` is required and must be an active staff user: it fills `created_by` and the audit actor). Lookup: SHA-256 of
the presented key against `PosDevice.key_hash` (unique), compared with `compare_digest`; `is_active` required;
`last_used_at` updated at most once per minute. Missing, unknown or inactive key → 401 `not_authenticated`
(`WWW-Authenticate: Api-Key`). Failed key authentications are counted per client IP inside `PosKeyAuthentication`
(DRF authenticates before it throttles, so the `pos` scope cannot see them): beyond `POS_FAILED_AUTH_PER_IP_PER_MIN`
(20) the request is answered 429 `throttled` before any lookup. **Lifecycle:** `manage.py revokeposkey <prefix>
--actor <email>` (`is_active=False`, audit `pos.device_revoked`) and `manage.py rotateposkey <prefix> --actor <email>`
(new key shown once, old hash replaced, audit `pos.device_rotated`). Under the local/test settings only, `POS_API_KEYS`
(env, comma separated `name:key`) seeds rows through the loyalty seeder; the production settings refuse the variable,
whatever the provider modes, so a known demo key can never reach a deployed database. `PosAPIView` lives in
`loyalty/api/pos/base.py` (§1.1).

Body `{"token": str 1..64, "sale_ref": str ≤64 | null}` (`sale_ref` = the POS receipt number, audit only).

| Check | Result |
|---|---|
| `token` not matching `^GD1:[A-Za-z0-9_-]{22}$` | 409 `qr_invalid` |
| `GETDEL gd:loyalty:qr:{hmac}` returns nothing (unknown, expired **or already used**) | 409 `qr_invalid` "QR kod yaroqsiz yoki muddati o‘tgan. Mijozdan ilovada QR kodni yangilashni so‘rang." |
| Customer no longer `active` | 409 `qr_invalid` (same body) |
| Throttle `pos` | 429 `throttled` |

```jsonc
// 200
{"customer_ref": "c_4be19a0f7d2c13e8",   // opaque, stable per (POS device, customer): "c_" + 16 hex of HMAC-SHA256(QR_HMAC_KEY, "pos:{device_id}:{customer_id}"); lets the cashier software recognise a repeat scan without learning our ids
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
- The internal `customer_id` is **not** returned (sequential and useless to the cashier).
- **Audit:** `pos.qr_resolved {pos_device_id, customer_id, sale_ref}` (actor_type `integration`, actor_id = device
  id); failures `pos.qr_rejected {pos_device_id, reason: "format" | "unknown"}` (one row per failure; throttled).
- **Open (M3):** how the sale is then discovered and credited (L7) and whether cashiers redeem (L6). M2 stores nothing
  else; `loyalty.services.record_store_purchase` (B4) is the M3 entry point. The M3 Billz live adapter follows
  docs.billz.io (`openapi.yaml` is the source of truth); what those docs do not cover stays an explicit stub that
  raises a typed "not supported" error: refunds of paid sales, per-receipt discovery of in-store sales after a QR scan
  (L6/L7 stay open) and webhooks (Billz has none) (product-owner decision 2026-10-26).
- Uzbek messages only on this route (cashier software); `Accept-Language` is honoured if sent.

### 2.11 Appointments

**`GET specialists/`** (**O**) → `{"results": [Specialist…]}`: active professionals by position, name, with active
links to active services (`professionals.selectors.booking_catalogue`).
```jsonc
// Specialist
{"id": 1, "name": "Nodira Rahimova", "role": "dermatologist", "role_label": "Dermatolog",
 "photo_url": null, "bio": "…",
 "qualifications": "…",                                // FND-7: diplomas, certificates (localized like bio; "" = none)
 "experience_years": 8,
 "location": {"id": 1, "name": "Glow Derma Yunusobod", "address": "…", "phone": "+998712000000",
              "latitude": 41.36, "longitude": 69.28},
 "services": [{"id": 1, "name": "Dermatolog konsultatsiyasi", "short_name": "Konsultatsiya",
               "duration_min": 30, "price": 150000}],
 "slot_step_min": 30,
 "slug": "nodira-rahimova",                            // B-W1: the website URL segment (unique, never only digits)
 "updated_at": "2026-10-20T09:00:00+05:00"}            // B-W1: sitemap lastmod
```
**`GET specialists/{id}/`** → `Specialist` (404 when inactive). **`GET specialists/{slug}/`** (B-W1) → the same by slug
(404 when unknown or inactive); slots and booking keep using the id. **`GET services/`** (**O**) →
`{"results": [{"id", "name", "short_name", "description", "category" /* ServiceCategory, FND-7 */,
"category_label", "duration_min" /* default */, "price_from", "price_to", "specialist_ids": [int]}]}` (active services
offered by ≥1 active specialist). Staff manage all of it in the admin API (admin §2.17); the catalogue and the slots
are read from the database on every request (no cache), so a change is visible at once: a deactivated specialist
answers 404 here and on `slots/`, a deactivated service or offer disappears, a new price applies to the next
booking (booked appointments keep their snapshots).

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
| `specialist_id` active (re-read under a `FOR KEY SHARE` lock of the specialist after the customer lock: a deactivation committed meanwhile answers the same) | `does_not_exist` "Mutaxassis topilmadi." · `field.specialist_not_found` |
| `service_id` offered (active link, active service) | `does_not_exist` · `field.service_not_offered` |
| consent current | 409 `terms_update_required` · `error.terms_update_required`, `details {current_version}` |
| — *Customer row locked (`select_for_update(no_key=True)`, §2.7.1) before the next two checks, so concurrent bookings of one customer serialise* — | |
| open future appointments of the customer < `BOOKING_MAX_OPEN_PER_CUSTOMER` (3) | 409 `booking_limit` "Sizda {limit} ta faol qabul bor. Yangisini olishdan oldin birini bekor qiling." · `error.booking_limit` |
| `start` aware | `invalid` (naive) |
| `start ≥ now + BOOKING_MIN_LEAD_MINUTES` | `in_past` "Kamida {n} daqiqa oldin yoziling." · `field.booking_too_soon` |
| within the horizon | `out_of_range` · `field.booking_horizon` |
| on the specialist's grid (`(start − hours.start) mod slot_step_min = 0`, seconds 0) | `invalid` "Vaqt {n} daqiqalik qadamda bo‘lsin." · `field.booking_grid` |
| inside the day's hours, `end` fits | `outside_working_hours` "Mutaxassis bu vaqtda ishlamaydi." · `field.outside_working_hours` |
| not overlapping blocks, the break or an open appointment of the specialist | `slot_unavailable` "Bu vaqt band." · `field.slot_unavailable` |
| not overlapping another open appointment **of this customer** | `slot_unavailable` "Sizda bu vaqtda boshqa qabul bor." · `field.own_overlap` |
| exclusion-constraint race (`appointment_no_overlap`, or the 40P01 deadlock it can raise) | 409 `slot_taken` "Bu vaqt band bo‘lib qoldi. Boshqa vaqtni tanlang." · `error.slot_taken` (nothing written; the app reloads the slots) |

→ **201 `Appointment`** with `status: requested`. Side effects (`appointments.services.request_by_customer`, P3; it
locks the Customer row first, then counts open appointments and checks the own overlap):
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
`requested`/`confirmed`, ascending by start; `past` = the rest, descending; both limited to the history horizon
(§2.2.3). **`GET me/appointments/{id}/`** → `Appointment` (404 for another customer's or beyond the horizon).

**`POST appointments/{id}/cancel/`** (**C**, **I**). Body `{"reason": str ≤300 | null}`; the value reserved for the
account-deletion path, `"account_deleted"` (after trimming), is refused with 400 `reason` `invalid`, so the event and
the audit never show a false deletion.
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
 "title": "Kuryerga topshirildi", "body": "Buyurtma №10248 kuryerga topshirildi.",   // rendered at creation in the customer's language (title = §4.3 customer label); never re-rendered after a language switch
 "image_url": null,
 "link": {"kind": "order", "product_id": null, "order_id": 10248, "appointment_id": null, "url": null},   // InboxLinkKind: none | product | order | appointment | url
 // a campaign with an https destination (gap ADM-10.1): "link": {"kind": "url", "product_id": null, "order_id": null, "appointment_id": null, "url": "https://glowderma.uz/aksiya"}
 "created_at": "2026-10-23T16:40:00+05:00", "read_at": null, "delivered_at": "2026-10-23T16:40:05+05:00"}
```
- **`GET me/notifications/`** → `Page<InboxItem>` + top-level `"unread_count": int`; newest first; limited to the
  history horizon (§2.2.3). Rows exist also for pushes that could not be sent (`push_state = no_device`, N-10), so the
  feed is complete after a reinstall. `InboxKind` `general` also carries the `account_security` notices (§2.14.2):
  they are stored with `source_type = "account"` and `source_id` = the customer id (migration
  `notifications.0003_inbox_account_source`: the `account` source and the CHECK `kind = 'general' AND source_type =
  'account' AND template_status = ''`; the built model admits `general` only for campaigns), `link = {"kind":
  "none"}`, `dedupe_key = "account:{customer_id}:{event}:{event_id}"` with the payload's `event_id` (§2.14.2), so a
  redelivered message never adds a second row.
- **`GET me/notifications/unread-count/`** → `{"unread_count": 3}` (badge polling; `inbox_customer_unread_idx`).
- **`POST me/notifications/{id}/read/`** → 200 `{"id", "read_at", "unread_count"}`; idempotent. 404 for another
  customer's row. Reading implies delivery: the row goes through the **same** `notifications.services.acknowledge`
  transition as `ack/` (below) before `read_at` is set, so a message opened from the feed before its ack arrived is
  still counted in `Campaign.delivered`.
- **`POST me/notifications/read-all/`** → 200 `{"updated": int, "unread_count": 0}`; every row it marks read also goes
  through `acknowledge` (one set-based conditional update per table).
- **`POST me/notifications/ack/`** — delivery acknowledgement from the app's push handler (foreground, background or
  notification-service extension; the app batches and retries on next start). Body `{"inbox_ids": [int] 1..50}`
  (`required` / `max_items` "Ko‘pi bilan 50 ta."). → 200 `{"acknowledged": int}`. Ids of other customers or unknown ids
  are ignored silently (no enumeration). Effects (`notifications.services.acknowledge`, P3), per row whose
  `delivered_at` is null: `InboxNotification.delivered_at = now`; the linked `CampaignRecipient.delivered_at = now`
  (only if null); then `Campaign.delivered = COALESCE(delivered, 0) + 1` by a conditional `UPDATE` that runs only when
  the recipient row actually changed (so retries never double count). This fills the admin `delivered` counter that
  was `null` in M1.
- Push payloads carry `inbox_id` (P3 changes the three senders), §2.14.2.
- `link.url` (gap ADM-10.1, open question GC-3): set only for `kind: "url"` — a campaign whose staff picked a link; an
  absolute `https` URL ≤500 the app opens in the system browser; `null` for every other kind. Every key of `link` is
  always present.

---
### 2.13 Provider callbacks (webhooks, **S**)

#### 2.13.0 Common rules
- Views inherit `WebhookAPIView` and live in `commerce/api/webhooks/` (the order orchestrator; protocol-pure helpers
  — signature, amount parsing, error tables, the account-reference check — live in
  `integrations/{click,payme,bts}/protocol.py` without DB access).
- **Fake providers under the production settings** (product-owner decision 2026-10-26: production may run fake
  adapters, but fake mode can never hand out a free order). When `DEV_FEATURES_ALLOWED` is false (the production
  settings, which staging also runs) and the provider's mode is `fake`, its callback view starts **no** transaction and
  honours **no** capture, whatever the signature: Click answers `-1` (`SIGN CHECK FAILED!`) to every Prepare, Payme
  answers `-32504` to every method, BTS answers 401 `{"ok": false}`. Nothing is read or written except the bounded
  `rejected:` inbox row below (`error_code: "fake_mode"`). The check runs first, before parameter parsing, so a request
  signed with a leaked key cannot start or move an order.
  - **One exception: a Click Complete of a transaction attached while Click was live.** Switching `CLICK_MODE` from
    `live` to `fake` is a normal operation under decision 4 (for example as a kill switch), and a transaction prepared
    before the switch may complete after it with the customer's money already withdrawn — Click's protocol allows no
    error answer then (refused capture, below), and a `-1` would leave the money at Click with no refund on our side.
    So a Complete is parsed and its signature verified with `CLICK_SECRET_KEY` (a real key in every mode, plan §7);
    when it names a payment we attached — `merchant_prepare_id` is a Click payment of the order whose
    `provider_txn_id` equals `click_trans_id` and whose `provider_state.prepare_at` is set (under the production
    settings only a live Prepare can have set it) — the Complete steps of §2.13.1 run, except that the capture is
    **never honoured**: step 7 becomes a **refused capture** with `case: "fake_mode"` (unless an earlier case
    applies), its refund becomes `manual_required` because the Click port is fake (below), and the ERROR
    `payment.late_capture` alerts the on-call. Everything else — a bad signature, an unknown or unattached payment —
    answers `-1` with a bounded `rejected:` row (`fake_mode`).
  - Payme gets no exception, and fake Payme keeps answering `-32504` to every method. A state-1 transaction is safe:
    Payme debits only on `PerformTransaction`, so refusing it takes no money (Payme cancels it by its timeout; our
    payment stays `pending` and the order expires as usual). A **refund** is not: Payme returns money to the payer
    only through a successful `CancelTransaction` of the performed transaction, so while Payme is fake under the
    production settings a refund staff start in the Payme cabinet cannot complete — the money stays with Payme. That
    is why switching a payment provider from `live` to `fake` is an **operator procedure** (§6.14: not while the
    provider holds open money; the worker's startup ERROR lists what is open), and money held by a provider switched
    to fake is reconciled by hand in that provider's cabinet.

  Under the local/test settings fake callbacks are processed normally (that is what the dev simulate endpoint and the
  sandbox suites drive). Production also requires real values for `CLICK_SECRET_KEY`, `PAYME_KEY` and
  `BTS_WEBHOOK_SECRET` in every mode (plan §7), as a second layer.
- **HTTP status:** always 200 with the provider-native body for protocol outcomes (including auth failures for
  Click and Payme, as their protocols require). An unexpected exception rolls everything back; Click and BTS get
  HTTP 500 so they retry; Payme gets HTTP 200 with JSON-RPC error `-32400` (system error) — the choice per provider
  sits in the protocol constant `INTERNAL_ERROR_RESPONSE` **(verify M3)**. Nothing is ever half-applied.
- **Order of work in every handler** (one `transaction.atomic()`):
  1. protocol checks that need no database (parameters, IP allow-list, signature / Basic auth, action, service id,
     account-reference HMAC);
  2. `pg_advisory_xact_lock(hashtext("<provider>:<lock key>"))` (lock keys below) — this only serialises deliveries of
     **one provider transaction** for the inbox and replay logic;
  3. **`commerce.services.lock_order(order_id)`** — the Order row lock (then Payment, then LoyaltyAccount, as
     everywhere). Every check that reads order or payment state (exists, paid, payable, payment state, "another
     transaction", in flight) runs **after** this lock, so a concurrent `orders/{id}/pay/` switch, customer cancel,
     expiry or second provider callback is either fully before or fully after it;
  4. the state transition through a commerce service that names the **payment explicitly**:
     `mark_paid(order_id, payment_id, provider, provider_txn_id, at=None)`, `fail_payment_effects(order_id,
     payment_id, error_code, at=None, *, provider_txn_id=None)`, `record_late_capture(order_id, payment_id, provider,
     provider_txn_id, *, case, at=None)` (the signatures of plan §3; callers pass `provider_txn_id` and `case` as
     keywords).
     Each refuses (`InvalidTransition`) when that payment is not the order's payment for that provider in the state
     the call expects, so a capture can never confirm a different payment than the one the provider named (B5's
     `mark_paid(order_id, provider_txn_id)` picked "the pending payment of the order"; P2 changes the signatures);
     `payments.services.attach_transaction(payment_id, provider_txn_id, provider_state)` refuses with
     `TransactionTaken` a payment that already carries a **different** non-empty `provider_txn_id`;
  5. the `WebhookInbox` row (below), inserted last in the same transaction — under the delivery's own `event_key`
     for a first delivery, under `dup:{event_key}:{uuid4}` for a duplicate (a replay of a key that already has a row),
     never a second row under the same key (`webhookinbox_provider_event_unique`; a violation would answer 500 and make
     Click retry forever).
- **WebhookInbox (T5):** `result = {"status", "body": <response>, "outcome": "accepted" | "duplicate" | "rejected" |
  "error", "error_code": str | null}`, `entity_ref` (`"order:10248"`, `"payment:301"`), allow-listed headers
  (`Content-Type`, `User-Agent`, `X-Forwarded-For`; never `Authorization`, tokens or the BTS secret), `path` without its
  query string, `processing_ms`, `raw_body` ≤64 KiB — except:
  - **authentication/signature failures** (`rejected`): a bounded row only — headers allow-list, `payload` truncated
    to 1 KiB of its JSON text, `raw_body = ""`; at most `WEBHOOK_REJECTED_PER_IP_PER_MIN` (10) such rows per IP per
    minute (Redis counter `gd:webhook:rejected:{ip}:{minute}`); beyond that nothing is stored, the request is answered
    the same way, and one sampled WARNING per IP per minute carries the count. Requests that pass authentication are
    never capped;
  - **Payme read-only methods** (`CheckPerformTransaction`, `CheckTransaction`, `GetStatement`): `raw_body = ""`
    (the parsed `payload` is enough; these are frequent and change nothing);
  - **duplicates** (`dup:` rows, outcome `duplicate`): `raw_body = ""`; the answer is the stored body or the current
    state (table below). Authenticated duplicates are never capped.

  | Provider | `event_key` | Duplicate handling |
  |---|---|---|
  | Click | `prepare:{click_trans_id}` / `complete:{click_trans_id}` | When a row already exists under the key, the **semantic fingerprint** = SHA-256 over `click_trans_id, service_id, merchant_trans_id, merchant_prepare_id, amount, action, error` (excluding `sign_time`, `sign_string`, `error_note`, `click_paydoc_id`) is compared first: a conflicting fingerprint → `-8` and a `rejected:{uuid4}` row; the same fingerprint (a plain or re-signed retry) → the Prepare/Complete steps below answer **from state** (an applied call answers the same success) and a `dup:` row is written |
  | Payme | `{method}:{params.id or "-"}:{uuid4}` — every delivery is its own row | never replayed: the Payme state machine below is idempotent on `params.id` (= `Payment.provider_txn_id`) and answers from the **current** state |
  | BTS | `{our tracking number}:{status}:{event_time}` from the payload, else `{uuid4}` | replay `{"ok": true}` and a `dup:` row; no re-track |
  | any | **auth/signature failure** → `rejected:{uuid4}` | a forged request can never occupy the key of a real delivery |
  | Click | refusals after the signature but before any state is read (`action` `-3`, `service_id` `-8`, account reference `-5`) → `rejected:{uuid4}`, **uncapped**, payload and raw body kept | never stored under the delivery key, so the later correct delivery of the same transaction is not a fingerprint conflict (decided 2026-10-26, integration) |
- Lock keys: Click `click:{click_trans_id}`; Payme `payme:{params.id}` (`payme:order:{order_id}` for
  `CheckPerformTransaction`); BTS `bts:{shipment id}`.
- **Payable / open / in flight / attached / supersede:** exactly as defined in §2.7.2
  (`commerce.services.is_payable(order, payment, now, *, attaching)`, `is_open(payment)`, `in_flight(payment, now)`).
- **Refused capture** (spec Financial integrity: captures on non-awaiting orders are refused; one that still lands is
  refunded and alerted). A provider reports money taken (Click Complete with `error = 0`) for a payment we will not
  honour: (a) the payment was failed by **us** for a non-provider reason — `error_code` ∈ {`expired`, `abandoned`,
  `cancelled`} (a superseded Click attachment is `abandoned`, §2.7.2); (b) the order is `cancelled`, or
  `awaiting_payment` but not payable (`attaching=False`) because the grace is over; (c) the order is already paid by
  **another** payment (double payment); (d) the Click port is fake under the production settings and the transaction
  was attached while Click was live (above). **One rule for all of them: accept and reverse.** Click's SHOP API
  documentation (docs.click.uz, SHOP API requests) settles it: after a successful Prepare and withdrawal of funds, the
  answer to Complete cannot be an error except `-4` (this payment was already confirmed) or `-9` (a repeated attempt to
  confirm a payment that was cancelled); if the goods cannot be provided, the merchant answers Complete **successfully**
  and cancels the payment through the Merchant API (`payment/reversal`); repeated error answers send the payment to
  manual investigation at Click. So: `commerce.services.record_late_capture(order_id, payment.id, "click",
  click_trans_id, case=<"failed_by_us" | "not_payable" | "double_payment" | "fake_mode">)` (when several cases
  apply, the first of `double_payment`, `failed_by_us`, `not_payable` is recorded; `fake_mode` is set only by the fake
  Click rule below) under the Order → Payment locks: `payments.services.mark_late_captured(payment_id, provider_txn_id, at)` — the **only** transition into `paid`
  from `failed` (case (a); case (b) once the order was cancelled) or from `pending` (case (b) while the expiry job has
  not run yet, case (c), and case (d), where the payment attached while Click was live is normally still `pending`)
  and only for this case: status `paid`, `confirmed_at = at`, `failed_at =
  null`, `error_code = "late_capture"`, `provider_state` merged; the `payment_one_active_per_order` partial unique
  excludes `error_code = 'late_capture'` (migration `payments.0002_payment_active_excludes_late_capture`), so case (c)
  can coexist with the order's real paid payment, and the payment selectors treat such rows by the display rule of §2.8
  — then `Refund(origin=late_capture, amount=payment.amount)` `requested` + outbox `refund.submit` (B5
  `payments.services.create_refund`), event `refund_requested {refund_id, amount, provider, manual: false,
  manual_reason: null}` (source system; `manual_required` without the outbox message, `manual: true` and
  `manual_reason: "no_paydoc"` when the payment has no `click_paydoc_id`, `"fake_mode"` when the Click port is fake
  under the production settings, below — every `refund_requested` event carries `manual_reason`, `"payme"` for a Payme
  refund, so the admin UI renders the right instruction, admin §4, §5.1), audit `payment.late_capture {order_id, payment_id,
  provider, case}`, ERROR log `payment.late_capture` (alert). Answer `0` with `merchant_confirm_id = payment.id` (Click
  keeps the capture; we return it). The order itself never changes status because of a refused capture.
  - **A reversal Click refuses** — Click reverses only payments of the current reporting month made with an online card,
    and UZCARD may refuse — ends as refund `failed` (B5 `submit_refund` → `mark_failed`, event `refund_failed`), which
    shows in admin `attention.refunds_needing_action`. Staff either `retry` it (a transient refusal) or close it with
    `…/resolve/ {state: "succeeded"}`, which P2 extends from `unknown` / `manual_required` to a `failed` Click refund
    (admin §7 item 18) after a fresh Click status check (below). The customer keeps seeing "To‘lov qaytarilmoqda" until
    then (§2.8).
  - **Fresh Click status check on that resolve.** `failed` does not prove that Click kept the money: B5's `_reversed()`
    answers "not reversed" when its own status call fails, so a reversal Click performed can be recorded `failed`, and
    a staff payout on top would pay the customer twice. `commerce.services.resolve_refund(…, "succeeded", …,
    manual_payout=…)` of a `failed` Click refund therefore first asks Click, **outside any row lock** (no HTTP under a
    lock), `ClickPort.payment_status(click_paydoc_id)`:
    - `reversed` → Click returned the money: the refund is closed `succeeded` (a Click reversal, whatever
      `manual_payout` says; staff must not pay out);
    - `paid` and no `manual_payout: true` → 409 `click_not_reversed` (M3; `invalid_transition` before) "Click to‘lovni qaytarmagan. Avval mijozga
      qo‘lda to‘lang, so‘ng «qo‘lda to‘landi» belgisi bilan yakunlang yoki qaytarishni qayta urinib ko‘ring."
      (`MESSAGE_REFUND_STILL_PAID`), nothing written;
    - `paid` and `manual_payout: true` (staff paid the customer outside Click) → closed `succeeded`, audit
      `refund.state_changed` with `manual_payout: true`;
    - Click transport error → 503 `provider_unavailable`, nothing written (retry later);
    - the Click port is fake under the production settings → 409 `invalid_transition` "Click sinov rejimida: to‘lov
      holatini tekshirib bo‘lmaydi. Click ulanganidan keyin yakunlang." (`MESSAGE_REFUND_CLICK_FAKE`): an unverified
      payout could double the refund (§6.14 operator procedure);
    - a payment without `click_paydoc_id` was never sent to Click, so there is nothing Click could have reversed: no
      check, `manual_payout: true` required (M3: else 409 `conflict` "Bu to‘lovda Click to‘lov raqami yo‘q, shuning
      uchun Click’dan holatini tekshirib bo‘lmaydi. Mijozga qo‘lda to‘lang, so‘ng «qo‘lda to‘landi» belgisi bilan
      yakunlang." — `MESSAGE_REFUND_NO_PAYDOC`, never the "Click has not reversed" answer: Click was not asked).
  - **Guard on that resolve.** A `failed` refund is closed, so a `retry` (a **new** refund of the same payment) may
    already exist, open or succeeded; resolving the old failed one as well would pay the customer twice and push
    revenue below zero. `commerce.services.resolve_refund` therefore closes a `failed` Click refund as `succeeded` only
    when — checked **before** the status check (M3: a refund that can no longer be closed is refused without asking
    Click) and again after it under the Order → Payment locks — (1) it is the payment's **newest** refund, (2) the payment is still `paid`
    and (3) the payment has no other refund that is open (`requested`, `submitting`, `manual_required`, `unknown`) or
    `succeeded`; otherwise 409 `invalid_transition` with the built Uzbek message "Bu qaytarishni yakunlab bo‘lmaydi."
    (`MESSAGE_REFUND_NOT_RESOLVABLE`). `resolve` from `unknown` / `manual_required` is unchanged (such a refund is the
    payment's only open one, `refund_one_open_per_payment`). As a second layer, `commerce.tasks.submit_refund` re-reads
    the payment before any port call and, when it is no longer `paid`, ends the refund `failed` (`error_code
    "payment_not_paid"`, event `refund_failed`) without calling Click.
  - **No retry while Click is fake.** `…/retry/` of a `failed` Click refund while the Click port is fake under the
    production settings answers 409 `invalid_transition` with `MESSAGE_REFUND_CLICK_FAKE`, nothing written: the retry
    would create a `manual_required` refund whose resolve skips the status check, so a reversal Click may already have
    performed could be paid out a second time. A new refund that never reached Click (a cancel while Click is fake)
    stays `manual_required` and is resolved as before, because Click was never asked to reverse it.
- **Refund and status calls for Click** are keyed by the Click payment id of the Merchant API (`DELETE
  …/payment/reversal/{service_id}/{payment_id}`, `GET …/payment/status/{service_id}/{payment_id}`), which is our
  `provider_state.click_paydoc_id` — never `click_trans_id` (`Payment.provider_txn_id`) **(verify M3)**. P2 exposes
  it on `payments.selectors.RefundRow.provider_payment_ref` and changes B5's `commerce.tasks.submit_refund`,
  `_resolve_after_failure` and `_reversed` to pass it to `ClickPort.refund` / `payment_status` (the built handler reads
  `refund.provider_txn_id`; the `refund.submit` payload is not what it uses). A Click payment without a
  `click_paydoc_id` (seeded or legacy rows) is never sent to Click with another id: its refund becomes
  `manual_required` (created that way, and re-checked by `submit_refund` before any port call), as does any Click
  refund while the Click port is fake under the production settings (§6.14).

#### 2.13.1 Click SHOP API
Configured in the Click merchant cabinet: Prepare URL `…/api/v1/webhooks/click/prepare/`, Complete URL
`…/api/v1/webhooks/click/complete/`. Requests are `application/x-www-form-urlencoded` POSTs; any other content type
→ `-8` **(verify M3; if the sandbox sends JSON, the protocol switch `CLICK_ACCEPT_JSON` parses it with
`parse_int=str, parse_float=str` and requires string values, so the signed strings are never re-serialised)**.
Responses are `application/json`.

**Request parameters**

| Param | Type | Prepare | Complete | Meaning |
|---|---|---|---|---|
| `click_trans_id` | int (bigint) | ✓ | ✓ | Click's transaction id → `Payment.provider_txn_id` |
| `service_id` | int | ✓ | ✓ | must equal `CLICK_SERVICE_ID` |
| `click_paydoc_id` | int | ✓ | ✓ | Click payment document id; stored; used for refunds and status checks; missing → `-8` |
| `merchant_trans_id` | str | ✓ | ✓ | our account reference `"<order_id>-<ref>"` (the `transaction_param` of the pay URL, §2.7.2) |
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
{"click_trans_id": 2875510123, "merchant_trans_id": "10248-3f9a0c1b2d", "merchant_prepare_id": 301, "error": 0, "error_note": "Success"}
// complete
{"click_trans_id": 2875510123, "merchant_trans_id": "10248-3f9a0c1b2d", "merchant_confirm_id": 301, "error": 0, "error_note": "Success"}
// errors: merchant_prepare_id / merchant_confirm_id are null
```

**Error codes** (`error_note` texts as Click documents them)

| `error` | `error_note` | We answer it when |
|---|---|---|
| 0 | `Success` | accepted, including every refused capture (accepted and reversed, §2.13.0) |
| -1 | `SIGN CHECK FAILED!` | signature mismatch; under the production settings with a fake Click: every Prepare, and every Complete that does not name a payment attached while Click was live (§2.13.0) |
| -2 | `Incorrect parameter amount` | `amount` not a non-negative decimal with ≤2 dp, not integral so'm, or ≠ `payment.amount` (= `order.total`) |
| -3 | `Action not found` | `action` ≠ 0 on prepare / ≠ 1 on complete |
| -4 | `Already paid` | prepare only: the order is already paid (a Complete for a payment that is already captured answers `0` from state; a Complete for an order paid by another payment is a refused capture, `0`) |
| -5 | `User does not exist` | `merchant_trans_id` is malformed, its HMAC part does not verify, or the order does not exist |
| -6 | `Transaction does not exist` | complete: `merchant_prepare_id` is not a Click payment of this order bound to this `click_trans_id` |
| -7 | `Failed to update user` | reserved (never sent: a DB failure answers 500 so Click retries) |
| -8 | `Error in request from click` | missing/malformed parameters (incl. `click_paydoc_id`); wrong content type; `service_id` ≠ ours; IP outside `CLICK_ALLOWED_IPS` (when set); a conflicting semantic fingerprint under the same key |
| -9 | `Transaction cancelled` | prepare: the order is cancelled or not payable with `attaching=True`; a Payme transaction of this order is open (state 1); complete: Click reported `error < 0`; the payment failed at Click earlier (a repeated Complete of a payment Click cancelled) |

**Prepare (action 0).** Fake Click under the production settings → `-1` before anything else (§2.13.0). Steps 1 of §2.13.0 (params `-8` → content type `-8` → IP `-8` → signature `-1` → action `-3`
→ `service_id` `-8` → account reference `-5`), advisory lock, then:
1. **duplicate / state replay:** an inbox row under `prepare:{click_trans_id}` with a conflicting fingerprint → `-8`
   (`rejected:` row); when a payment with `provider = click`, `provider_txn_id = click_trans_id` already exists, the
   answer comes from its state with no write except the `dup:` row (§2.13.0), read **under the lock of that payment's
   order** (`lock_order(payment.order_id)`, then the payment is re-read): bound to another order than
   `merchant_trans_id` names → `-8`; `pending`, `paid` or `refunded` (captured, then returned — the same rule as
   Complete step 1) → `0` with the same `merchant_prepare_id` (a plain or re-signed retry, or a delayed duplicate);
   `failed` (by Click, superseded, expired or cancelled) → `-9`. A delayed or re-sent Prepare of an old transaction
   therefore never supersedes the customer's live Click payment (decided 2026-10-26, integration);
2. `lock_order(order_id)`; order exists (`-5`);
3. already paid (`processing`+ or a non-late `paid` payment) → `-4`;
4. not payable with `attaching=True` (cancelled, or `now ≥ reservation_expires_at`) → `-9`;
5. a Payme transaction of this order is open (state 1) → `-9` (the customer is paying with Payme; Payme debits only on
   `PerformTransaction`). An open **Click** transaction with another `click_trans_id` does **not** refuse: the new
   Prepare supersedes it (§2.7.2, step 8);
6. Click `error < 0` → `-9`, no effect;
7. amount ≠ `order.total` → `-2`;
8. `ensure_payable_payment(order, "click", superseding=click_trans_id)`: the active Click payment is reused when it
   is not attached; when it is attached to **another** `click_trans_id` (an open transaction, any age) it is failed
   `abandoned` (unilateral, no `payment_failed` event, as B5's silent fails) and a new `pending` row is created — a later
   Complete of the old transaction is a refused capture (§2.13.0); then `attach_transaction(payment.id, click_trans_id,
   {merchant_trans_id, merchant_prepare_id: payment.id, click_trans_id, click_paydoc_id, service_id, prepare_at,
   sign_time})` → `0` with `merchant_prepare_id = payment.id`. `ensure_payable_payment` and `attach_transaction` form
   one unit: a refusal of the attach rolls back the supersede, so the old transaction stays attached. `TransactionTaken`
   (a concurrent prepare bound the row) maps to `-8` defensively. No order event (a prepare is not a state change).

**Complete (action 1).** Steps 1 of §2.13.0 (as prepare), advisory lock, then the steps below. Fake Click under the
production settings: only a Complete that names a payment attached while Click was live gets past step 3 (otherwise
`-1` and a `rejected:` row `fake_mode`), and step 7 never applies — such a capture goes to step 8 with `case:
"fake_mode"` (§2.13.0).
1. **duplicate / state replay:** an inbox row under `complete:{click_trans_id}` with a conflicting fingerprint → `-8`
   (`rejected:` row); `Payment(id = merchant_prepare_id, provider click, provider_txn_id = click_trans_id)` was already
   captured — `paid` (normally or as a late capture) or `refunded` afterwards — → `0` with `merchant_confirm_id =
   payment.id` and no state change, only a `dup:` row (a captured status is terminal for this transaction, so reading
   it before the Order lock is safe; the signature of the retry was verified in step 1 of §2.13.0);
2. `lock_order(order_id)`; order exists (`-5`);
3. `Payment(id = merchant_prepare_id, order, provider click, provider_txn_id = click_trans_id)` exists, locked (`-6`);
4. Click `error < 0`: a `pending` payment → `fail_payment_effects(order_id, payment.id, str(error),
   provider_txn_id=click_trans_id)`
   (payment `failed`, event `payment_failed {provider, transaction_id, error_code}`; the order stays payable until
   expiry and the app may pay again); → `-9` (no money was taken: Click reports the payment failed);
5. amount ≠ `payment.amount` → `-2` (payment unchanged);
6. the payment was failed **by Click** earlier (numeric `error_code`) → `-9` (a repeat of the same failed Complete;
   `error` is part of the Complete fingerprint, so a Complete with `error = 0` after a stored Complete with
   `error < 0` under the same key is a fingerprint conflict, `-8`, at step 1);
7. **honoured capture:** the payment is `pending`, it is the order's active payment, the order is not paid by another
   payment, and `is_payable(order, payment, now, attaching=False)` → `mark_paid(order_id, payment.id, "click",
   click_trans_id)` (B5 `confirm_payment` + `confirm_payment_effects` — reservations converted, hold captured, purchase
   + cashback credit, `processing`, events `payment_confirmed` (+ `cashback_credited`), outbox `billz.push_sale`,
   `bts.create_shipment` (delivery), `push.order_status processing`); `provider_state += {complete_at, error,
   error_note}` → `0` with `merchant_confirm_id = payment.id`;
8. **otherwise** it is a refused capture (§2.13.0: failed by us — including a superseded attachment —, order
   cancelled or past the grace, or paid by another payment) → `record_late_capture(order_id, payment.id, "click",
   click_trans_id, case=…)` and `0` with `merchant_confirm_id = payment.id` (accept and reverse).

#### 2.13.2 Payme Merchant API (JSON-RPC 2.0)
Endpoint `POST …/api/v1/webhooks/payme/`, `Content-Type: application/json`. Every answer is HTTP 200 with a JSON-RPC
body; amounts are **tiyin** (so'm × 100) and exist only in this adapter; times are epoch **milliseconds**.

**Auth:** `Authorization: Basic base64("Paycom:" + key)` where key = `PAYME_KEY` (the test key in the sandbox).
Missing or wrong → error `-32504`; under the production settings a fake Payme answers `-32504` to every call (§2.13.0). The optional `PAYME_ALLOWED_IPS` check runs after auth and answers `-32504` too
(defence in depth only).

```jsonc
// request
{"jsonrpc": "2.0", "id": 12345, "method": "CreateTransaction",
 "params": {"id": "5305e3bab097f420a62ced0b", "time": 1729673880000, "amount": 78440000,
            "account": {"order_id": "10248-3f9a0c1b2d"}}}   // key = PAYME_ACCOUNT_FIELD (default "order_id"), as configured in the Payme cabinet
// success
{"jsonrpc": "2.0", "id": 12345, "result": {"create_time": 1729673881000, "transaction": "301", "state": 1}}
// error
{"jsonrpc": "2.0", "id": 12345,
 "error": {"code": -31008, "message": {"uz": "Amalni bajarib bo‘lmaydi.", "ru": "Невозможно выполнить операцию.",
                                        "en": "Unable to perform the operation."}, "data": null}}
```

**Error codes** (the account-error codes and `-32400` are protocol constants, **verify M3** against the
`test.paycom.uz` checklist)

| Code | `message.uz` / `ru` / `en` | When |
|---|---|---|
| -32300 | "So‘rov POST bo‘lishi kerak." / "Метод запроса должен быть POST." / "Request method must be POST." | non-POST |
| -32700 | "JSON xato." / "Ошибка парсинга JSON." / "Parse error." | body is not JSON |
| -32600 | "So‘rov noto‘g‘ri." / "Неверный запрос." / "Invalid request." | missing `method`/`params`/`id`, wrong types |
| -32601 | "Metod topilmadi." / "Метод не найден." / "Method not found." | unknown method; also `ChangePassword` (keys rotate through env, §7 `M2-P6`) |
| -32504 | "Ruxsat yetarli emas." / "Недостаточно привилегий для выполнения метода." / "Insufficient privileges." | auth / IP; every call to a fake Payme under the production settings (§2.13.0) |
| -32400 | "Tizim xatosi." / "Системная ошибка." / "System error." | unexpected exception (everything rolled back) |
| -31001 | "Noto‘g‘ri summa." / "Неверная сумма." / "Wrong amount." | `amount` not a JSON integer, ≤ 0, or ≠ `order.total × 100` |
| -31003 | "Tranzaksiya topilmadi." / "Транзакция не найдена." / "Transaction not found." | unknown `params.id` |
| -31007 | "Buyurtma bajarilgan, tranzaksiyani bekor qilib bo‘lmaydi." / "Заказ выполнен. Невозможно отменить транзакцию." / "The order is fulfilled; the transaction cannot be cancelled." | cancel of a performed transaction without an open refund |
| -31008 | "Amalni bajarib bo‘lmaydi." / "Невозможно выполнить операцию." / "Unable to perform the operation." | order paid, cancelled or not payable; transaction not in state 1; 12-hour timeout |
| -31050 | "Buyurtma topilmadi." / "Заказ не найден." / "Order not found." (`data` = `PAYME_ACCOUNT_FIELD`, default `"order_id"`) | missing/malformed `account[PAYME_ACCOUNT_FIELD]`, HMAC part not verifying, unknown order (`PAYME_ERR_ORDER_NOT_FOUND`) |
| -31051 | "Buyurtma boshqa to‘lov bilan kutilmoqda." / "Заказ ожидает оплаты другой транзакцией." / "The order is awaiting another transaction." (`data` = `PAYME_ACCOUNT_FIELD`) | the order already has another active Payme transaction in state 1, or a Click transaction in flight (`PAYME_ERR_ORDER_BUSY`; the range −31050…−31099 is Payme's account-error range) |

**States** (`provider_state.state`): `1` created, `2` performed, `-1` cancelled before perform, `-2` cancelled after
perform. **Reasons** (`CancelTransaction.reason`, stored): 1 recipient not found/inactive, 2 debit error at the
processing centre, 3 transaction execution error, 4 cancelled by timeout, 5 refund, 10 unknown **(verify M3)**.
**Timeout:** a state-1 transaction older than 12 h (`now − create_time > 43 200 000 ms`) is cancelled by us on the
next touch (state −1, reason 4, payment failed, `payment_failed` event) and the call answers -31008.

Every method resolves the order from `account[PAYME_ACCOUNT_FIELD]` (CheckPerform, Create; the one setting also builds
the checkout URL's `ac.` key, §2.7.2) or from the payment found by
`params.id` (the others), then takes the advisory lock and **`lock_order`** before reading state (§2.13.0).

| Method | Params | Logic (in order) | `result` |
|---|---|---|---|
| `CheckPerformTransaction` | `amount`, `account` | account reference (-31050) → order exists (-31050) → paid / cancelled / not payable with `attaching=True` (-31008) → another transaction in flight (-31051) → amount (-31001) | `{"allow": true}` (fiscal `detail` only when `PAYME_FISCAL_DETAIL_ENABLED`, §7 `M2-P5`) |
| `CreateTransaction` | `id`, `time`, `amount`, `account` | **existing** `id`: state ≠ 1 → -31008; timed out → cancel (reason 4) and -31008; else the stored answer. **new** `id`: account (-31050) → paid / cancelled / not payable with `attaching=True` (-31008) → another Payme state-1 transaction or a Click transaction in flight (-31051) → amount (-31001) → `time` older than 12 h (-31008) → `ensure_payable_payment(order, "payme")` (reuses the pending Payme row only when it has no `provider_txn_id`) → `attach_transaction(provider_txn_id = id, provider_state = {transaction: id, time, create_time: now_ms, state: 1, account})`; `TransactionTaken` → -31051 | `{"create_time", "transaction": "<payment id>", "state": 1}` |
| `PerformTransaction` | `id` | unknown (-31003) → state 2: the stored answer → state ≠ 1: -31008 → timed out: cancel (reason 4), -31008 → not payable with `attaching=False` (cancelled, paid by another payment, or expired beyond the grace): -31008, the transaction stays in state 1 and Payme cancels it **(verify M3)**; no money is taken, so this is not a refused capture → `mark_paid(order_id, payment.id, "payme", id)`; `provider_state += {state: 2, perform_time: now_ms}` | `{"transaction", "perform_time", "state": 2}` |
| `CancelTransaction` | `id`, `reason` | (fake Payme under the production settings: `-32504` like every method, so a cabinet refund cannot complete, §2.13.0, §6.14) unknown (-31003) → state 1: state −1, `cancel_time`, `reason`; `fail_payment_effects(order_id, payment.id, str(reason), provider_txn_id=id)` (payment failed, `payment_failed` event; also on an already cancelled/expired order) → state 2 **with an open `manual_required` refund** (staff refunded in the Payme cabinet after an admin cancel, O5): state −2, `cancel_time`, `reason`; `commerce.services.resolve_refund(refund_id, "succeeded", actor_id=None)` (refund succeeded, payment `refunded`, event `refund_succeeded`) → state 2 whose payment is already `refunded` by a succeeded refund (staff resolved the manual refund before the cabinet cancel arrived): state −2, `cancel_time`, `reason` merged, no new refund → state 2, payment still `paid`, without an open refund: -31007 → state −1/−2: the stored answer | `{"transaction", "cancel_time", "state"}` |
| `CheckTransaction` | `id` | unknown (-31003) | `{"create_time", "perform_time" (0 if none), "cancel_time" (0 if none), "transaction", "state", "reason" (null if none)}` |
| `GetStatement` | `from`, `to` (ms) | Payme payments whose **Payme** transaction time `provider_state.time` (the `params.time` of `CreateTransaction`) ∈ [`from`, `to`], ascending by that time **(verify M3)**. M2 filters on the JSON key (volumes are small); M3 adds an expression index on `(provider_state->>'time')::bigint` for Payme rows if the statement is slow | `{"transactions": [{"id", "time", "amount", "account": {<PAYME_ACCOUNT_FIELD>: account_ref}, "create_time", "perform_time", "cancel_time", "transaction", "state", "reason", "receivers": null}]}` |

Every method writes a `WebhookInbox` row (§2.13.0). The `transaction` value is our `Payment.id` as a string; the
Payme id is `Payment.provider_txn_id`.

#### 2.13.3 `POST webhooks/bts/` and the polling job
- **Auth (M3):** shared secret `BTS_WEBHOOK_SECRET`. BTS configures only a URL (`/v1/webhook/webhook-config`;
  its own optional `has_token` sends a token of an unspecified name and place, not our header), so the webhook is
  configured as `webhook_url = https://<api host>/api/v1/webhooks/bts/?token=<BTS_WEBHOOK_SECRET>` with BTS's
  `has_token` off. The header `X-GD-Webhook-Token` is accepted too (either one). The view strips the parameter
  before anything is logged or stored (`WebhookInbox.path` has no query string), and the edge proxy's log format
  redacts `token=` (ops runbook). `compare_digest`; mismatch → 401 `{"ok": false}` + a bounded `rejected:` inbox
  row (§2.13.0). A fake BTS under the production settings answers every call that way (§2.13.0).
- **Body (M3, docs.bts.uz `/page/webhook`, `/page/test-update-status`):** BTS's notification document
  `{"clientId": str|int|null|"", "orderId": int, "barcode": str, "status": {"id": int, "code"?: int, "name":
  str}, "date": "YYYY-MM-DD[ HH:MM:SS]"}` — `status.id` is BTS's own scale (`6` = delivered = code `1200`). Every
  delivery shape is read: a JSON body (also a JSON string holding the document); flat form fields (multipart or
  urlencoded) including PHP bracket keys `status[id]` / `status[code]` / `status[name]` and `status` as a JSON
  string; a form whose single field value — or name, when the field has none (BTS's curl example) — is the JSON
  document. A body over 16 KiB → 413 `{"ok": false}`, nothing stored. Nothing else is assumed; BTS's retry policy
  is undocumented, so polling (below) is the safety net for a delivery we answered 503.
- **Identity, never the status:** we read only the parcel identity and **never trust the status in the
  payload**: our `Shipment` is found by `clientId` (= `BTS_CLIENT_ID_PREFIX` + our order id; `null`, `""`, a
  value of another environment's prefix are skipped) or else by `orderId` = the tracking number **stored on our
  Shipment** (our tracking number **is** BTS's `orderId`, §2.14.3). The `barcode` is never read. The view
  **re-tracks with the tracking number stored on our Shipment** through the BTS port `track(tracking_number)`.
  An `orderId` that differs from the tracking number of the Shipment found by `clientId` is ignored with a
  WARNING `bts.webhook_tracking_mismatch {order_id}`, so a payload pairing order X with another parcel's number
  cannot move order X. The authoritative status is applied through `commerce.services.apply_bts_status(order_id,
  status, via="webhook", payload=<stable fields of the tracked parcel, as polling below>)` (B5: shipment event;
  port `200` → `handed_to_courier`, `300` → `delivered`, others recorded only — the live adapter translates BTS's
  own codes, §2.14.3; events with source `bts`; `push.order_status`). A forged webhook can therefore only trigger
  a re-track of a parcel we created. Inbox event key: `"{tracking number}:{status.code or status.id}:{date}"` (a
  random key when either is missing, so nothing is deduplicated by mistake).
- → 200 `{"ok": true}`; unknown parcel (no Shipment, or a Shipment without a tracking number yet) → 200 `{"ok": true,
  "ignored": true}` + WARNING log (BTS must not retry forever); a definitive refusal of the re-track (`BtsRejected`,
  e.g. BTS no longer knows our tracking number) → the same 200 `{"ok": true, "ignored": true}` + WARNING
  `bts.track_rejected {order_id}`, nothing applied; BTS transport failure during the re-track → 503 `{"ok": false}`,
  nothing stored (BTS retries; polling catches up). The webhook **never writes `IntegrationState(bts)`**: neither a
  successful re-track nor a refusal moves `last_sync_at` or `consecutive_failures`, so one webhook that tracked a
  parcel can never hide polling runs in which BTS refused every parcel; the health of the BTS link is the polling
  outcome below alone.
- **Polling** (beat `commerce.poll_bts`, every 600 s, P2) — the fallback when BTS webhooks do not arrive (Backend
  PDF §14):
  - **Single flight:** the run starts only if `cache.add("gd:lock:poll_bts", …, timeout=600)` succeeds and deletes
    the key at the end; a second run that overlaps (a slow run, two beat processes) returns at once.
  - **Round robin, not by `last_status_at`:** `apply_status` writes nothing when the status is unchanged, so ordering
    by `last_status_at` would poll the same oldest parcels forever once more than one batch sits unchanged in transit.
    Candidates are shipments `created` whose order is `processing` or `handed_to_courier` and whose `last_status_at`
    (or creation, before the first status) is older than `BTS_POLL_STALE_MINUTES` (30), taken in `id` order **after
    the cursor** `IntegrationState(bts).data["poll_cursor"]` (the last shipment id attempted; no migration), wrapping
    to the lowest id when the end is reached. The cursor is written after **every attempted parcel, whatever the
    outcome**, through `integrations.services.set_state_data("bts", "poll_cursor", shipment_id)` (a single
    `UPDATE … SET data = data || {…}`; the built `integrations.services` has no writer of `data`, P2 adds it), so every
    eligible parcel is polled in turn and no single parcel can hold the cursor.
  - **Bounded:** at most `BTS_POLL_BATCH` (100) parcels and `BTS_POLL_MAX_SECONDS` (300, below the 600 s schedule) per
    run, whichever comes first (each `track()` is bounded at 10 s by the port).
  - Each parcel: `track()` → `apply_bts_status(…, via="polling", payload=<stable fields>)` where the stable fields are
    the tracking number, the status code and BTS's status time when it sends one — never the whole tracked document,
    whose volatile fields would make every poll a "new" status and append a `ShipmentEvent` every 10 minutes. The
    webhook re-track passes the same stable fields.
  - **Failures are per parcel.** `BtsUnavailable` (transport error, 5xx, timeout) for one parcel → WARNING
    `bts.poll_parcel_failed {shipment_id, kind: "unavailable"}`, the cursor moves past it and the run continues; only
    `BTS_POLL_MAX_CONSECUTIVE_FAILURES` (3) transport failures **in a row**, on different parcels, mean BTS itself is
    down: the run stops there (the cursor stays after the last attempted parcel, so the parcels it skipped are polled
    on the next pass) and calls `integrations.services.record_failure("bts", …)` (`consecutive_failures` + 1). A
    definitive answer (`BtsRejected`, e.g. BTS does not know the parcel) → WARNING `bts.poll_parcel_failed
    {shipment_id, kind: "rejected"}`, nothing applied, the cursor advances, and it resets the transport-failure streak
    (BTS answered). So a parcel whose tracking always fails costs one `track()` per pass and never stops the others.
  - **Run outcome — success needs a tracked parcel.** The run counts `tracked` (a `track()` that returned a parcel),
    `rejected` (`BtsRejected`) and `unavailable` (`BtsUnavailable`) separately. A run that stopped on the streak →
    `record_failure("bts", "poll stopped: N transport failures in a row")`; otherwise a run with **at least one**
    tracked parcel → `record_sync_success("bts")` (`last_sync_at`, `consecutive_failures` 0); a run that attempted
    parcels but tracked **none** (every one refused, or refused and unavailable without reaching the streak — a BTS
    outage that answers "unknown parcel", a wrong account) → `record_failure("bts", "poll: no parcel tracked
    (rejected R, unavailable U)")` and ERROR `bts.poll_nothing_tracked {rejected, unavailable}` (alert); a run with no
    candidate writes nothing (no evidence either way). `consecutive_failures > 0` is shown to staff as admin
    `GET dashboard/` `attention.bts_polling_failing: true` (admin §2.10) until a run tracks a parcel again.

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
  request language. For `platform = android` and a non-empty `ANDROID_SMS_APP_HASH`, a second line with the 11-character
  app hash is appended (Android SMS Retriever autofill; the template with the hash line needs its own Eskiz approval);
  iOS reads the code from the text (`textContentType = .oneTimeCode`). The Eskiz test account only delivers its own
  test text (use fake mode until production).
- **Handler** `sms.send` (`accounts/tasks.py`): claim `sent_at` in its own transaction **before** the call; read and
  decrypt `gd:otp:payload:{sms_code_id}` (missing — expired or flushed — → nothing is sent, INFO log, done: the user
  resends after 60 s); a connection error raised before the request left (connect failure) un-claims and re-raises
  (outbox retry; the ciphertext is kept); a timeout or 5xx after sending keeps the claim and is never resent; 4xx
  (invalid number, no balance) is logged with `IntegrationState(eskiz)` error and not retried. After a definitive
  outcome the Redis key is deleted. `accounts.cleanup_auth` deletes `done`/`dead` `sms.send` outbox rows older than
  24 h together with the `SmsCode` rows.
- **Delivery callback:** optional; M2 sets no `callback_url` and exposes no endpoint (M3 may add
  `webhooks/eskiz/` to record `DELIVRD`/`UNDELIV`).
- **Fake (`ESKIZ_MODE=fake`):** phone ending `0000` → rejected (definitive); ending `9999` → transport down; otherwise
  accepted with id `fake-<sms_code_id>`. Under the local/test settings every rendered message is kept in
  `FakeEskiz.sent` (tests read the code there); under the production settings the fake keeps nothing and logs one INFO
  line `sms.fake_accepted {sms_code_id}` per message (ids only, never the text or code), so no plain code stays in a
  worker's memory (§6.4). The fixed code `ESKIZ_FAKE_CODE` (default `123456`, so black-box e2e can sign in) exists
  **only** under `config.settings.local` and `config.settings.test` (`DEV_FEATURES_ALLOWED`);
  `config.settings.production` — which staging also runs — refuses the variable at boot, so no internet-facing
  deployment accepts a known code.
- **Fake Eskiz in production (demo/staging posture, product-owner decision 2026-10-26).** The production settings
  accept `ESKIZ_MODE=fake` (startup WARNING, mode reported by `health/?ready=1` and admin `settings/`, §6.14), but
  **no SMS is delivered**: `send-code` and the `verify_device` step answer as usual and the code never arrives. Nobody
  can register, sign in with a password on a device without a device key, reset a password, complete a social link,
  set a first password on a social-only account or delete a social-only account; signed-in sessions, known-device
  password sign-ins and linked social sign-ins (when OAuth is live) keep working. This posture is for demos and staging
  only; a public launch needs `ESKIZ_MODE=live`.

#### 2.14.2 FCM push (`integrations/fcm/`; live adapter M3, payload contract now)
HTTP v1 `POST https://fcm.googleapis.com/v1/projects/{FCM_PROJECT_ID}/messages:send`, one request per message over
`httpx` with an OAuth2 access token obtained from the service-account key (`FCM_CREDENTIALS_FILE`) by a PyJWT RS256
JWT-bearer grant (both pinned already; firebase-admin is **not** added) **(verify M3)**. P0 extends the port:
`PushMessage(token, title, body, data, *, data_only=False, android_channel="", android_priority="high",
apns_push_type="alert", apns_priority=10, image_url="", collapse_key="")`. The fake records every field under the
local/test settings only; under the production settings it keeps nothing (one INFO line with ids per message), so
pushes are reported `sent` but reach no device (§6.14).
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
| `order_status` | `inbox_id`, `order_id`, `status` | title = customer label (§4.3) in the customer's language; body = template text (`{raqam}` only) | `orders` / high |
| `appointment` | `inbox_id`, `appointment_id`, `action` | fixed texts (`notifications/texts.py`); sent only while the action is still true when the outbox message is handled (`confirmed`/`rescheduled`: status `requested` or `confirmed`; `rejected`: `rejected`; `cancelled`: `cancelled`), otherwise no inbox row and no push (INFO log) | `appointments` / high |
| `campaign` | `inbox_id`, `campaign_id`, `product_id` (or `""`), `link_url` (the campaign's https destination, or `""`; gap ADM-10.1) | as authored; `notification.image` = campaign image URL | `promo` or `general` / normal |
| `account_security` | `inbox_id`, `event` (`social_linked`, `social_unlinked`, `password_reset`, `password_changed`), `provider` (or `""`) | catalog `push.account_security.<event>` (title + body; `{provider}` only) | `general` / high |
| `session_replaced` | `sid` (the revoked session) | **retired (B-W1)**: no longer produced; the handler only drains messages queued before the release. Data-only (APNs `content-available: 1`, `apns-push-type: background`, priority 5) | – / high |

- **`push.account_security` payload — the one definition** every producer (P1: `auth/reset-password/`,
  `auth/social/complete/`, `me/social/` POST and DELETE, `me/password/`) writes and the handler (P3) reads; P0 declares
  it as `accounts.topics.ACCOUNT_SECURITY_PAYLOAD_KEYS` and P3 registers the same tuple (imported, not re-typed) in
  `notifications.topics.PAYLOAD_KEYS`. Every key is always present:
  ```jsonc
  {"customer_id": 5,
   "event": "password_reset",          // AccountSecurityEvent
   "event_id": "3f2c9a…32 hex",        // uuid4 hex made by the producer; the suffix of the outbox dedupe key
                                       // push.account_security:{customer_id}:{event}:{event_id} and of the
                                       // inbox dedupe_key account:{customer_id}:{event}:{event_id} (§2.12)
   "provider": "",                     // CustomerSocialProvider for social_linked / social_unlinked, else ""
   "farewell_tokens": []}              // FCM tokens of the devices a reset or a password change signed out, else []
  ```
- **Fan-out (B-W1).** A transactional push (`order_status`, `appointment`, `account_security`) goes to **every** active,
  push-enabled device of the customer with a token (newest `last_active_at` first, at most 10, one FCM batch, the same
  `inbox_id` in each). The inbox row is `sent` when at least one device got it; a dead token prunes its device; when no
  device got it and one answered `not_sent` the message is retried later. A campaign still targets one device per
  customer (its newest pushable one).
- **Farewell notices.** A `push.account_security` payload may carry `farewell_tokens`: the FCM tokens captured from the
  devices that a reset or a password change signed out (§1.9). The handler sends each the same visible notice as the
  current device (same `inbox_id`; an app without a session shows it and opens the sign-in screen), then blanks the
  tokens in the payload, as `push.session_replaced` does. There is no SMS notice (`M2-N3`): a reset or an SMS-only link
  needs the SMS code, so whoever did it already receives that SIM's messages; the previous device is the channel the
  owner still holds.
- **C1:** no names, phones, addresses or amounts in `title`, `body` or `data` — ids, the order number and the provider
  name only.
- **Token pruning** — only when FCM says the **token** is dead: HTTP 404 `UNREGISTERED`, `SENDER_ID_MISMATCH`, or
  `INVALID_ARGUMENT` whose error detail names the registration token (`"The registration token is not a valid FCM
  registration token"`, field `message.token`) → result error `unregistered` →
  `accounts.services.invalidate_fcm_token(device_id)` (`fcm_token=""`, `push_enabled=False`, `fcm_invalidated_at=now`)
  in the same handler; the app re-registers through `PUT me/device/` (it does so on every cold start).
- **Any other `INVALID_ARGUMENT`** (a malformed message: bad data value, payload too large, bad APNs header) →
  result error `payload_invalid`: the inbox row gets `push_state = failed`, `push_error = "payload_invalid"`, the
  handler logs ERROR `push.payload_invalid {topic, field}` (alert) and **returns normally**, so the outbox message ends
  `done` (no retry, no `outbox.dead` audit), and the device is **never** touched, so a payload bug in a deploy cannot
  wipe every token. (In the outbox `failed` means "retry later"; a handler cannot end a message as failed.)
- `UNAVAILABLE`, `INTERNAL`, `QUOTA_EXCEEDED`, HTTP 5xx, transport errors → `FcmUnavailable` (outbox retry).

#### 2.14.3 BTS (`integrations/bts/`; B5 port + P2 additions; live adapter M3)
| Port method | Use | Notes (verify M3 against `apitest`) |
|---|---|---|
| `regions()` | `delivery/regions/`, address validation | BTS region/city reference; cached (served stale while a refresh fails), refreshed single-flight by the beat `delivery.refresh_bts_regions` |
| `quote(region_code, city_code)` | `delivery/calculate/`, checkout | tariff calculation with `BTS_SENDER_CITY_CODE`, `BTS_DEFAULT_PARCEL_WEIGHT_G`, one parcel; live: bounded at 4 s (a customer request waits) and cached 5 min per (sender city, city, weight, pickup type); `BtsRejected` → 409 `delivery_unavailable`, `BtsUnavailable` → 503 |
| `find_by_client_id(client_id, since)` | before every create (B5) | `clientId` = `BTS_CLIENT_ID_PREFIX` + order id (default prefix empty: the bare order id); live: BTS's order list from the day before the shipment row was created (`since`) to a week ahead; any doubt (a full page without pagination data, a failed page) → `BtsUnavailable`; two live parcels → ERROR `bts.duplicate_parcels` |
| `create(request)` | outbox `bts.create_shipment` (B5) | recipient = the order's recipient snapshot; `ready_to_take` = `BTS_READY_TO_TAKE` (true); called **outside** the Order row lock (checks committed first, the order re-locked to record; a parcel of an order cancelled meanwhile is cancelled at BTS as an orphan) |
| `cancel(parcel)` | admin cancel (B5) | succeeds only at status `100` (O1) |
| `track(tracking_number)` | webhook re-track, polling (P2) | returns the parcel with its current status; always called with **our** stored tracking number; `BtsRejected` = a definitive per-parcel answer (unknown parcel), `BtsUnavailable` = transport error, 5xx or timeout (§2.13.3) |

**Tracking number (M3 live).** BTS's `track` and `cancel` take only BTS's `orderId` (the waybill number), so the
live adapter stores it as both `Shipment.tracking_number` and `bts_order_id`; BTS's `barcode` is not stored.

**Status translation (M3 live).** BTS's own codes (`GET /v1/status/index`) differ from the port vocabulary above
(`100` accepted / cancellable, `200` handed to the courier, `300` delivered); the live adapter translates:

| BTS code | Port status | Order effect |
|---|---|---|
| `100`, or `null` (a new order) | `100` | none (cancellable) |
| `200` courier took it, `300` in the sending office, `400`, `500`, `600`, `700`, `800`, `1100` courier delivering | `200` | `processing` → `handed_to_courier` |
| `1200` delivered | `300` | → `delivered` |
| `1300` deleted, `1400` expired | the code itself | recorded only; polling logs ERROR `bts.parcel_gone` once (staff act) |
| any other code | the code itself (`unknown` when not a number) | recorded only |

**Failures (M3 live).** No answer, HTTP 5xx and an answer that says the request was not processed (a redirect,
408 / 425 / 429, 401 / 403) are `BtsUnavailable`; another 4xx is `BtsRejected`.

Fake additions (P2): `regions()` returns the four regions of the fake tariff table (`10` Toshkent shahri, `11`
Toshkent viloyati, `12` Sirdaryo, `13` Samarqand) with two cities each; `track()` returns `scenario["track_status"]`
or the parcel's last status, `scenario["track"] == "rejected"` → `BtsRejected`, `"down"` → `BtsUnavailable`;
`scenario["quote"] == "rejected"` → `BtsRejected`, `"down"` → `BtsUnavailable`.

**Fake BTS under the production settings** (`core.integration_modes.fake_in_production("bts")`; P2 owns `bts/fake.py`). The
fake never reports a remote effect that did not happen and keeps nothing in its per-process parcel table: `regions()`
→ `[]`; `quote()` → `BtsRejected("fake_mode")` (delivery is not offered anyway, §2.6); `create()` →
`BtsRejected("fake_mode")`, so the B5 handler marks the shipment `failed` with `shipment_failed {error_code:
"fake_mode"}` (admin `attention.bts_failed`; staff ship by hand) and no tracking number is ever made up;
`find_by_client_id()` → `None`; `cancel()` and `track()` → `BtsUnavailable` (an admin cancel of a parcel created while
BTS was live answers 503 until BTS is live again, never a false `bts_cancelled: true`), and `commerce.poll_bts`
returns at once without polling.

### 2.15 `POST dev/payments/{order_id}/simulate/` (local/test settings only)
Mounted only when `DEV_FEATURES_ALLOWED` (local/test settings), `FAKE_PAYMENTS_ENABLED=true` **and** `CLICK_MODE` =
`PAYME_MODE` = `fake`; settings validation refuses `FAKE_PAYMENTS_ENABLED` together with any live payment mode, and the
production settings refuse it outright — whatever the provider modes — so the endpoint can never sign callbacks with a
real `CLICK_SECRET_KEY`/`PAYME_KEY` and never exists on a deployed server (where fake callbacks are refused anyway,
§2.13.0). **C**, own order. Body
`{"outcome": "success" | "fail", "provider": PaymentProvider | null}`. It builds a correctly signed Click
prepare + complete (or Payme `CheckPerformTransaction` + `CreateTransaction` + `PerformTransaction` / `CancelTransaction`)
sequence and runs it through the **real webhook views** in-process, so the mobile team can finish a purchase without
provider accounts. → 200 `{"order_id", "provider", "steps": [{"call": "prepare", "response": {…}}, …]}`.

---
## 3. Enumerations (customer-facing subsets)

Literals live in `glowderma/core/enums.py` (P0 adds the new classes); labels in §5. Every new enum must have a choice
set of its own (drf-spectacular 0.30 reports two override names for one set as an error, and `spectacular
--fail-on-warn` fails); P0 runs the schema validation with all of them registered. `SessionRevokeReason` (admin §3)
gains `refresh_reused` (§2.1.6).

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
| ServiceCategory (admin §3.1, FND-7) | `dermatology`, `cosmetology` | `services/` `category` |
| AppointmentStatus | `requested`, `confirmed`, `rejected`, `attended`, `cancelled` (the `cancelled` literal reserved by T3 is now public) | appointments |
| BannerLinkKind | `none`, `product`, `category`, `url` (gap X-BANNER-URL) | banners |
| **OtpPurpose** (new) | `register`, `login_new_device`, `reset_password`, `link_social` (`send-code` accepts all but `login_new_device`) | `auth/send-code/`, `auth/verify-code/` |
| **NextStep** (new) | `verify_device`, `verify_phone` | `auth/login/`, `auth/social/` |
| **CustomerSocialProvider** (new; component `CustomerSocialProviderEnum`) | `google`, `facebook`, `apple` | `auth/social/`, `me/social/`, `Me.linked_providers`. The staff OAuth routes keep their own `SocialProviderEnum` (`google`, `facebook`; admin §2.1), so Apple never appears on staff routes |
| **SocialTokenType** (new) | `id_token`, `access_token` | `auth/social/`, `me/social/` |
| OrderChannel (admin §3) | `ios`, `android` | also the device platform: `X-GD-Platform`, `me/device/` `platform` (one enum: a second enum with the same literals would break `ENUM_NAME_OVERRIDES`, which refuses two names for one choice set) |
| **StockStatus** (new) | `in_stock`, `out_of_stock` | products |
| **CustomerProductOrdering** (new) | `default`, `price`, `-price`, `name` | `catalog/products/` |
| **CartLineStatus** (new) | `ok`, `insufficient`, `out_of_stock`, `unavailable` | cart, `OUT_OF_STOCK.details.lines[].status` |
| **OrderScope** (new) | `active`, `completed`, `cancelled`, `all` | `me/orders/` |
| **CustomerRefundState** (new) | `in_progress`, `completed` | `OrderDetail.refund` |
| **CashbackEarnedState** (new) | `none`, `credited`, `reversed` | `OrderDetail.cashback` |
| **TimelineType** (new) | `order_created`, `payment_confirmed`, `payment_failed`, `shipment_created`, `handed_to_courier`, `delivered`, `cancelled`, `refund_in_progress`, `refund_succeeded` (exactly the shown types of §4.1; hidden and folded event types never appear) | `OrderDetail.timeline` |
| **ProgressStep** (new) | `placed`, `paid`, `with_courier`, `delivered`, `picked_up`, `cancelled` | `OrderDetail.progress[].step` |
| **ProgressState** (new) | `done`, `current`, `upcoming`, `skipped` | `OrderDetail.progress` |
| **LedgerSource** (new) | `online`, `store`, `admin` | ledger |
| **AppointmentScope** (new) | `upcoming`, `past` | `me/appointments/` |
| **AppointmentCancelledBy** (new; = `Appointment.CancelledBy`) | `customer`, `admin` | appointments |
| **InboxKind** (new; = `InboxNotification.Kind`) | `promo`, `general`, `order_status`, `appointment` | inbox |
| **InboxLinkKind** (new) | `none`, `product`, `order`, `appointment`, `url` (gap ADM-10.1) | inbox |
| **PushDataType** (new; FCM `data.type`) | `order_status`, `appointment`, `campaign`, `account_security`, `session_replaced` | §2.14.2 |
| **AccountSecurityEvent** (new) | `social_linked`, `social_unlinked`, `password_reset`, `password_changed` | `push.account_security`, §2.14.2 |

**Audit actions added in M2** (shown by the admin `audit-log/`; `actor_type` in brackets):

| Area | `action` values |
|---|---|
| Customer auth (`customer`) | `customer.registered` (`{reactivated, terms_version}`), `customer.login_succeeded` (`{method, new_device}`), `customer.login_failed` (`{method}`), `customer.logout` (`{sid, scope}`), `customer.session_revoked` (B-W1, `{sid, current}`; `[staff]` from the admin card), `customer.sessions_revoked` (B-W1, `{count, scope: "others"}`), `customer.password_reset`, `customer.password_changed` (`{first_password}`), `customer.history_reset` (`{reason: "dormant", last_activity_at}`), `customer.refresh_reused` (`{sid}`), `customer.social_linked` (`{provider, via?}`), `customer.social_unlinked` (`{provider, reason: "customer" \| "password_reset" \| "dormant"}`), `customer.consent_accepted` (`{terms_version}`), `customer.deleted` (`{orders_cancelled, appointments_cancelled}`), `auth.device_mismatch` (`{sid}`, ≤1 per session per hour) |
| Appointments (`customer`) | `appointment.requested`, `appointment.cancelled` (with `by: "customer"`) |
| Payments (`system`) | `payment.late_capture` (`{order_id, payment_id, provider, case: "failed_by_us" \| "not_payable" \| "double_payment" \| "fake_mode"}`) |
| POS (`integration` for resolves; `staff` for the commands) | `pos.qr_resolved` (`{pos_device_id, customer_id, sale_ref}`), `pos.qr_rejected` (`{pos_device_id, reason}`), `pos.device_created` (`{pos_device_id, name}`), `pos.device_revoked` (`{pos_device_id}`), `pos.device_rotated` (`{pos_device_id}`) — the three command actions carry the `--actor` staff user |

---

## 4. Order timeline for customers

### 4.1 `timeline[]` — from OrderEvent (admin §4) to customer entries
`OrderDetail.timeline` is built by `commerce.selectors.customer_timeline(order)` (P2) from the order's events, newest
first (`at` desc, `id` desc). Admin-only facts (staff names and notes, Billz ids, internal BTS failures, refund
failures) never reach the customer. Events of a late-captured payment (§2.13.0) appear only as `refund_in_progress` /
`refund_succeeded`.
```jsonc
// TimelineEntry
{"type": "payment_confirmed", "at": "2026-10-23T14:05:00+05:00",       // TimelineType
 "title": "To‘lov qabul qilindi", "subtitle": "Click orqali · cashback +5 680 so‘m"}   // subtitle | null; resolved language
```

| OrderEvent `type` | Customer entry | `title` (uz) · key | `subtitle` (uz) · key |
|---|---|---|---|
| `order_created` | shown | "Buyurtma berildi" · `label.timeline.order_created` | "{item_count} ta mahsulot · {total} so‘m" · `label.timeline.sub_items_total` |
| `reservation_created` | folded (feeds `pay_by`) | – | – |
| `payment_confirmed` (+ folded `cashback_credited`) | shown | "To‘lov qabul qilindi" · `label.timeline.payment_confirmed` | "{provider} orqali" + (credit ? " · cashback +{amount} so‘m") · `label.timeline.sub_paid_via`, `label.timeline.sub_cashback` |
| `cashback_credited` | folded into `payment_confirmed` | – | – |
| `billz_sale_recorded` | hidden | – | – |
| `payment_failed` | shown | "To‘lov amalga oshmadi" · `label.timeline.payment_failed` | "{provider} · qayta urinib ko‘ring" while `awaiting_payment` (`label.timeline.sub_payment_failed_retry`), else "{provider}" (`label.timeline.sub_provider`) |
| `shipment_created` | shown | "Yetkazib berishga topshirildi" · `label.timeline.shipment_created` | "BTS kuzatuv raqami: {tracking_number}" · `label.timeline.sub_tracking` |
| `shipment_failed` | hidden (staff retry; the order stays "Tayyorlanmoqda") | – | – |
| `status_changed` → `handed_to_courier` | shown as `handed_to_courier` | "Kuryerga topshirildi" · `label.timeline.handed_to_courier` | null |
| `status_changed` → `delivered` | shown as `delivered` | delivery "Yetkazildi" · `label.timeline.delivered`; pickup "Olib ketildi" · `label.timeline.picked_up` | null |
| `handed_to_courier` (BTS) | shown | "Kuryerga topshirildi" · `label.timeline.handed_to_courier` | "BTS" · `label.timeline.sub_bts` |
| `delivered` (BTS) | shown | "Yetkazildi" · `label.timeline.delivered` | "BTS" · `label.timeline.sub_bts` |
| `cancelled` (+ folded `refund_requested` with the same `refund_id`) | shown | "Buyurtma bekor qilindi" · `label.timeline.cancelled` | by reason: `admin` "Do‘kon tomonidan bekor qilindi" · `label.timeline.sub_cancelled_admin`; `customer_request` "Siz bekor qildingiz" · `label.timeline.sub_cancelled_customer`; + (refund ? " · to‘lov qaytariladi" · `label.timeline.sub_refund_follows`). The staff `note` is never shown. |
| `reservation_expired` | shown as `cancelled` | "Buyurtma bekor qilindi" · `label.timeline.cancelled` | "To‘lov muddati tugadi" · `label.timeline.sub_expired` |
| `refund_requested` (not folded: late capture, staff retry) | shown as `refund_in_progress` | "To‘lov qaytarilmoqda" · `label.timeline.refund_in_progress` | "{amount} so‘m" · `label.timeline.sub_amount` |
| `refund_succeeded` | shown | "To‘lov qaytarildi" · `label.timeline.refund_succeeded` | "{amount} so‘m · {provider}" · `label.timeline.sub_amount_provider` |
| `refund_failed` | hidden (the order still reads "To‘lov qaytarilmoqda" through `refund.state`) | – | – |

Order №10248 test vector (admin §4 events) → customer timeline, newest first:
1. `handed_to_courier` "Kuryerga topshirildi" — "BTS"
2. `shipment_created` "Yetkazib berishga topshirildi" — "BTS kuzatuv raqami: 48213907"
3. `payment_confirmed` "To‘lov qabul qilindi" — "Click orqali · cashback +5 680 so‘m"
4. `order_created` "Buyurtma berildi" — "3 ta mahsulot · 784 400 so‘m"

Numbers use the admin `N()` rule (NBSP thousands separator) for uz/ru and a comma separator for en. `{provider}` is
rendered as the brand name ("Click", "Payme").

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
"state": "done", "at": cancelled_at}` (`label.progress.cancelled`). A pickup order delivered by a manual
`status_changed` has no `handed_to_courier_at`; a delivery order delivered by BTS without a `200` first marks
`with_courier` `done` with `at` null.

### 4.3 Customer-facing status labels

| OrderStatus | delivery (uz · key `label.order_status.<s>`) | pickup override |
|---|---|---|
| `awaiting_payment` | "To‘lov kutilmoqda" | – |
| `processing` | "Tayyorlanmoqda" | – |
| `handed_to_courier` | "Kuryerga topshirildi" | – (cannot happen) |
| `delivered` | "Yetkazildi" | "Olib ketildi" · `label.order_status_pickup.delivered` |
| `cancelled` | "Bekor qilindi" | – |

Admin §5.8 labels stay unchanged for the admin UI and exports. The **push and inbox title** of an order-status
notification is this customer label in the customer's language (P3 changes `deliver_order_status`, which used the
admin Uzbek labels); the **body** is the admin-edited StatusTemplate text.

---

## 5. i18n message catalog

`glowderma/core/i18n.py` (P0) holds `CATALOG: dict[key, {"uz", "ru", "en"}]` and `t(key, lang, **params)`; a unit test
asserts every key has three non-empty texts with the same placeholders, and a contract test asserts every key named
in §2–§4 exists. The admin audience keeps its Uzbek strings (`core.api.exceptions.MESSAGES`); the customer exception
handler renders through the catalog. Placeholders: `{n}`, `{m}`, `{limit}`, `{max}`, `{available}`, `{order_id}`,
`{provider}`, `{amount}`, `{percent}`, `{item_count}`, `{total}`, `{tracking_number}`, `{code}` (SMS only).

### 5.0 Code → catalog key
A top-level code renders `error.<code>` and a field code `field.<code>`, unless the endpoint table names another key;
`CustomerError(code, status, *, key=None, details=None, params=None)` (P0) carries that key explicitly (default
`error.<code>`), and `field_error(code, message_key=…)` does the same for fields. The codes whose key is not the default:

| Code | Where | Catalog key |
|---|---|---|
| `not_cancellable` | `orders/{id}/cancel/` · `appointments/{id}/cancel/` · any other (a reused B5/B6 service raising `NotCancellable`) | `error.order_not_cancellable` · `error.appointment_not_cancellable` · `error.not_cancellable` |
| `OUT_OF_STOCK` | cart add / patch (one product) · checkout (lines) | `error.out_of_stock_one` · `error.OUT_OF_STOCK` |
| `invalid_transition` | any customer view (a reused service raising `InvalidTransition`) | `error.invalid_transition` |
| `delivery_unavailable` | BTS refuses the route · delivery not offered (§2.6) | `error.delivery_unavailable` · `error.delivery_not_offered` |
| field `otp_invalid` (on `code`) | `auth/verify-code/` | `field.otp_invalid` |
| field `invalid` | `phone` (every auth body) · `code` (verify) · booking grid | `field.phone_invalid` · `field.otp_format` · `field.booking_grid` |
| field `does_not_exist` / `min_length` / `max_value` / `in_past` / `out_of_range` / `required` | as named in the endpoint tables (e.g. `field.address_not_found`, `field.name_min`, `field.cashback_max`, `field.booking_too_soon`, `field.booking_horizon`, `field.address_required`) | the key in the table; elsewhere the generic `field.<code>` |

A package that meets a code with no key here or in its table stops and reports (plan §0.1); a contract test maps every
code a customer view can raise to an existing key.

### 5.1 Top-level codes (`error.*`)

| Key | uz | ru | en |
|---|---|---|---|
| `error.validation_error` | Maydonlarni tekshiring. | Проверьте поля. | Please check the fields. |
| `error.bad_request` | So‘rov noto‘g‘ri. | Некорректный запрос. | Malformed request. |
| `error.parse_error` | So‘rovni o‘qib bo‘lmadi. | Не удалось разобрать запрос. | The request could not be parsed. |
| `error.not_authenticated` | Avtorizatsiya talab qilinadi. | Требуется авторизация. | Please sign in. |
| `error.SESSION_EXPIRED` | Sessiya tugadi. Qayta kiring. | Сессия истекла. Войдите снова. | Your session has ended. Please sign in again. |
| `error.SESSION_REPLACED` | Hisobingizga boshqa qurilmadan kirildi. | В ваш аккаунт вошли с другого устройства. | Your account was signed in on another device. |
| `error.password_changed` | Parolingiz o‘zgartirildi. Qayta kiring. Bu siz bo‘lmasangiz, sotuvchi bilan bog‘laning. | Пароль изменён. Войдите снова. Если это были не вы, свяжитесь с продавцом. | Your password was changed. Please sign in again. If this wasn't you, contact the seller. |
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
| `error.otp_network_limit` | Bu tarmoqdan bugun juda ko‘p kod so‘raldi. Boshqa tarmoq (Wi-Fi yoki mobil internet) orqali urinib ko‘ring. | С этой сети сегодня запрошено слишком много кодов. Попробуйте через другую сеть (Wi-Fi или мобильный интернет). | Too many codes were requested from this network today. Please try over another network (Wi-Fi or mobile data). |
| `error.otp_locked` | Urinishlar soni tugadi. {m} daqiqadan so‘ng qayta urinib ko‘ring. | Попытки закончились. Повторите через {m} мин. | No attempts left. Try again in {m} min. |
| `error.otp_invalid` | Kod noto‘g‘ri. Yana {n} marta urinishingiz mumkin. | Неверный код. Осталось попыток: {n}. | Wrong code. {n} attempts left. |
| `error.otp_expired` | Kod muddati tugagan. Yangi kod oling. | Срок действия кода истёк. Получите новый код. | The code has expired. Please request a new one. |
| `error.sms_unavailable` | SMS xizmati vaqtincha ishlamayapti. Birozdan so‘ng urinib ko‘ring. | SMS-сервис временно недоступен. Попробуйте чуть позже. | The SMS service is temporarily unavailable. Please try again later. |
| `error.verification_invalid` | Tasdiqlash muddati tugagan. Kodni qaytadan oling. | Подтверждение истекло. Получите код заново. | Verification has expired. Please request a new code. |
| `error.phone_registered` | Bu raqam ro‘yxatdan o‘tgan. Hisobingizga kiring. | Этот номер уже зарегистрирован. Войдите в аккаунт. | This number is already registered. Please sign in. |
| `error.phone_not_registered` | Bu raqam bilan hisob topilmadi. Ro‘yxatdan o‘ting. | Аккаунт с этим номером не найден. Зарегистрируйтесь. | No account uses this number. Please sign up. |
| `error.terms_not_published` | Ro‘yxatdan o‘tish vaqtincha mavjud emas. Keyinroq urinib ko‘ring. | Регистрация временно недоступна. Попробуйте позже. | Sign-up is temporarily unavailable. Please try again later. |
| `error.social_token_invalid` | Kirish amalga oshmadi. Qayta urinib ko‘ring. | Не удалось войти. Попробуйте ещё раз. | Sign-in failed. Please try again. |
| `error.social_ticket_invalid` | Ijtimoiy tarmoq orqali kirish muddati tugadi. Qaytadan boshlang. | Время входа через соцсеть истекло. Начните заново. | Social sign-in has expired. Please start again. |
| `error.social_provider_linked` | Bu hisobga boshqa {provider} profili bog‘langan. | К этому аккаунту уже привязан другой профиль {provider}. | Another {provider} profile is already linked to this account. |
| `error.social_identity_linked` | Bu {provider} profili boshqa hisobga bog‘langan. | Этот профиль {provider} привязан к другому аккаунту. | This {provider} profile is linked to another account. |
| `error.last_sign_in_method` | Bu yagona kirish usuli. Avval parol o‘rnating. | Это единственный способ входа. Сначала задайте пароль. | This is your only sign-in method. Please set a password first. |
| `error.terms_update_required` | Foydalanish shartlarining yangi tahririni qabul qiling. | Примите новую редакцию условий использования. | Please accept the updated terms of use. |
| `error.address_limit` | Ko‘pi bilan {limit} ta manzil saqlash mumkin. | Можно сохранить не более {limit} адресов. | You can save up to {limit} addresses. |
| `error.product_unavailable` | Mahsulot hozir sotuvda yo‘q. | Товар сейчас недоступен. | This product is not available right now. |
| `error.out_of_stock_one` | Bu mahsulot qolmagan. | Этот товар закончился. | This product is out of stock. |
| `error.OUT_OF_STOCK` | Ba’zi mahsulotlar yetarli emas. | Некоторых товаров недостаточно. | Some products are not available in the requested quantity. |
| `error.cart_full` | Savatga ko‘pi bilan {limit} xil mahsulot qo‘shish mumkin. | В корзину можно добавить не более {limit} разных товаров. | You can add up to {limit} different products to the cart. |
| `error.cart_empty` | Savat bo‘sh. | Корзина пуста. | Your cart is empty. |
| `error.delivery_unavailable` | Bu hududga yetkazib berish mavjud emas. | Доставка в этот регион недоступна. | Delivery to this area is not available. |
| `error.delivery_not_offered` | Yetkazib berish hozircha mavjud emas. Olib ketishni tanlang. | Доставка пока недоступна. Выберите самовывоз. | Delivery is not available right now. Please choose pickup. |
| `error.online_sales_unavailable` | Onlayn buyurtma vaqtincha mavjud emas. Mahsulotlarni do‘konimizdan xarid qilishingiz mumkin. | Онлайн-заказ временно недоступен. Товары можно купить в нашем магазине. | Online ordering is temporarily unavailable. You can buy the products in our store. |
| `error.open_order_exists` | Sizda to‘lanmagan buyurtma bor (№{order_id}). Avval uni to‘lang yoki bekor qiling. | У вас есть неоплаченный заказ (№{order_id}). Оплатите или отмените его. | You have an unpaid order (№{order_id}). Please pay or cancel it first. |
| `error.price_changed` | Narxlar yangilandi. Buyurtmani qayta tekshiring. | Цены обновились. Проверьте заказ ещё раз. | Prices have changed. Please review your order. |
| `error.already_paid` | Buyurtma allaqachon to‘langan. | Заказ уже оплачен. | This order is already paid. |
| `error.not_payable` | Bu buyurtmani to‘lab bo‘lmaydi. | Этот заказ нельзя оплатить. | This order cannot be paid. |
| `error.order_expired` | To‘lov muddati tugadi. Mahsulotlarni savatga qaytarib, qaytadan rasmiylashtiring. | Время на оплату истекло. Верните товары в корзину и оформите заказ заново. | The payment time has expired. Put the products back in the cart and place the order again. |
| `error.payment_in_progress` | To‘lov {provider} ilovasida jarayonda. Uni yakunlang yoki biroz kuting. | Оплата в {provider} уже идёт. Завершите её или подождите. | A {provider} payment is in progress. Please finish it or wait. |
| `error.order_not_cancellable` | Bu buyurtmani ilovadan bekor qilib bo‘lmaydi. Sotuvchi bilan bog‘laning. | Этот заказ нельзя отменить в приложении. Свяжитесь с продавцом. | This order cannot be cancelled in the app. Please contact the seller. |
| `error.not_cancellable` | Buni bekor qilib bo‘lmaydi. | Это нельзя отменить. | This cannot be cancelled. |
| `error.invalid_transition` | Bu amalni hozir bajarib bo‘lmaydi. Ma’lumotlarni yangilang. | Это действие сейчас недоступно. Обновите данные. | This action is not available right now. Please refresh. |
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
| `field.otp_invalid` | Kod noto‘g‘ri. Yana {n} marta urinishingiz mumkin. | Неверный код. Осталось попыток: {n}. | Wrong code. {n} attempts left. |
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

### 5.3 Labels (`label.*`), pushes (`push.*`) and SMS (`sms.*`)

| Key | uz | ru | en |
|---|---|---|---|
| `label.session.browser` (B-W1) | Veb-brauzer | Веб-браузер | Web browser |
| `label.order_status.awaiting_payment` | To‘lov kutilmoqda | Ожидает оплаты | Awaiting payment |
| `label.order_status.processing` | Tayyorlanmoqda | Готовится | Being prepared |
| `label.order_status.handed_to_courier` | Kuryerga topshirildi | Передан курьеру | With the courier |
| `label.order_status.delivered` | Yetkazildi | Доставлен | Delivered |
| `label.order_status_pickup.delivered` | Olib ketildi | Получен | Picked up |
| `label.order_status.cancelled` | Bekor qilindi | Отменён | Cancelled |
| `label.payment_status.pending` / `.paid` / `.failed` / `.refunded` | To‘lov kutilmoqda / To‘langan / To‘lov o‘tmadi / Qaytarilgan | Ожидает оплаты / Оплачен / Оплата не прошла / Возвращён | Awaiting payment / Paid / Payment failed / Refunded |
| `label.payment.in_progress` | To‘lov tekshirilmoqda | Оплата проверяется | Payment is being confirmed |
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
| `label.timeline.sub_provider` | {provider} | {provider} | {provider} |
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
| `label.timeline.sub_amount` | {amount} so‘m | {amount} сум | {amount} so'm |
| `label.timeline.refund_succeeded` | To‘lov qaytarildi | Деньги возвращены | Payment refunded |
| `label.timeline.sub_amount_provider` | {amount} so‘m · {provider} | {amount} сум · {provider} | {amount} so'm · {provider} |
| `label.progress.placed` / `.paid` / `.with_courier` / `.delivered` / `.picked_up` / `.cancelled` | Buyurtma berildi / To‘lov qabul qilindi / Kuryerda / Yetkazildi / Olib ketildi / Bekor qilindi | Оформлен / Оплачен / У курьера / Доставлен / Получен / Отменён | Placed / Paid / With courier / Delivered / Picked up / Cancelled |
| `label.ledger.credit` / `.debit` | Cashback hisoblandi / Cashback ishlatildi | Начислен кешбэк / Списан кешбэк | Cashback earned / Cashback used |
| `label.ledger.reversal_back` / `.reversal_taken` | Cashback qaytarildi / Cashback bekor qilindi | Кешбэк возвращён / Кешбэк отменён | Cashback returned / Cashback reversed |
| `label.ledger.adjust_add` / `.adjust_subtract` | Bonus qo‘shildi / Balans tuzatildi | Начислен бонус / Баланс скорректирован | Bonus added / Balance corrected |
| `label.ledger.sub_order_percent` | Buyurtma №{order_id} · {percent}% | Заказ №{order_id} · {percent}% | Order №{order_id} · {percent}% |
| `label.ledger.sub_store_percent` | Do‘kondagi xarid · {percent}% | Покупка в магазине · {percent}% | In-store purchase · {percent}% |
| `label.ledger.sub_order` | Buyurtma №{order_id} | Заказ №{order_id} | Order №{order_id} |
| `label.ledger.sub_order_cancelled` | Buyurtma №{order_id} bekor qilindi | Заказ №{order_id} отменён | Order №{order_id} cancelled |
| `label.ledger.sub_store_returned` | Do‘kondagi xarid qaytarildi | Покупка в магазине возвращена | In-store purchase returned |
| `label.ledger.sub_previous` | Avvalgi hisob | Прежний аккаунт | Previous account |
| `label.cashback.promo_rule` | Aksiyadagi mahsulotlarga cashback berilmaydi. | На акционные товары кешбэк не начисляется. | Promotional products do not earn cashback. |
| `label.appointment_status.requested` / `.confirmed` / `.rejected` / `.attended` / `.cancelled` | Tasdiqlanishi kutilmoqda / Tasdiqlandi / Rad etildi / Qabul bo‘lib o‘tdi / Bekor qilindi | Ожидает подтверждения / Подтверждена / Отклонена / Состоялась / Отменена | Awaiting confirmation / Confirmed / Declined / Completed / Cancelled |
| `label.role.dermatologist` / `.cosmetologist` | Dermatolog / Kosmetolog | Дерматолог / Косметолог | Dermatologist / Cosmetologist |
| `label.service_category.dermatology` / `.cosmetology` | Dermatologiya / Kosmetologiya | Дерматология / Косметология | Dermatology / Cosmetology |
| `push.account_security.social_linked.title` / `.body` | Hisobingizga yangi kirish usuli qo‘shildi / Hisobingizga {provider} profili bog‘landi. Agar bu siz bo‘lmasangiz, parolni tiklang. | Добавлен новый способ входа / К аккаунту привязан профиль {provider}. Если это были не вы, восстановите пароль. | A new sign-in method was added / A {provider} profile was linked to your account. If this wasn't you, reset your password. |
| `push.account_security.social_unlinked.title` / `.body` | Kirish usuli o‘chirildi / {provider} profili hisobingizdan uzildi. | Способ входа удалён / Профиль {provider} отвязан от аккаунта. | A sign-in method was removed / The {provider} profile was unlinked from your account. |
| `push.account_security.password_reset.title` / `.body` | Parol o‘zgartirildi / Hisobingiz paroli yangilandi. Agar bu siz bo‘lmasangiz, sotuvchi bilan bog‘laning. | Пароль изменён / Пароль аккаунта обновлён. Если это были не вы, свяжитесь с продавцом. | Password changed / Your account password was changed. If this wasn't you, contact the seller. |
| `push.account_security.password_changed.title` / `.body` | Parol o‘rnatildi / Hisobingiz uchun yangi parol o‘rnatildi. Agar bu siz bo‘lmasangiz, sotuvchi bilan bog‘laning. | Пароль задан / Для аккаунта задан новый пароль. Если это были не вы, свяжитесь с продавцом. | Password set / A new password was set for your account. If this wasn't you, contact the seller. |
| `sms.otp` | Glow Derma: tasdiqlash kodingiz {code}. Kodni hech kimga bermang. | Glow Derma: ваш код подтверждения {code}. Никому его не сообщайте. | Glow Derma: your verification code is {code}. Do not share it with anyone. |

---

## 6. Security notes

1. **Ownership on every resource.** Every **C** query starts from `request.user` (the Customer): addresses, cart,
   orders, payments, appointments, inbox, ledger, holds, social identities. Ids of other customers answer 404 (never
   403); rows before a reactivated account's history horizon are treated as another customer's (§2.2.3). A contract
   test walks every **C** route with a second customer's ids. Webhooks bind each callback to the order/payment it
   names (`merchant_trans_id` with its HMAC reference + `merchant_prepare_id` + `click_trans_id`; Payme
   `account[PAYME_ACCOUNT_FIELD]` with its HMAC reference + `params.id`) and act on that payment only (§2.13.0 step 4).
2. **Audience separation.** Customer tokens carry `aud="customer"`; staff tokens `aud="staff"`; each authentication
   class pins its audience, so neither token works on the other API. The URL-walk test (spec) covers every customer
   route with anonymous, staff-token, Basic and Django-session requests; POS routes with a customer token; webhook
   routes with tokens (ignored) and bad signatures.
3. **Rate limits** (§1.8) on SMS, verification, login, refresh (per token and per IP for unknown tokens), social,
   checkout, pay, QR, POS (including failed key auth) and anonymous reads, keyed on the device first because of CGNAT.
   OTP day caps, the daily wrong-code cap and lockouts are derived from `SmsCode` rows so a cache flush does not reset
   them; the global SMS budget is a circuit breaker with an alert (§2.1.1). Residual risk: a few IPs can trip the
   global breaker (`SMS_GLOBAL_HOURLY_LIMIT`, 500 per hour) and stop sign-up, reset and new-device sign-in for every
   customer until the next hour — an outage by design (it caps the SMS bill); the ERROR `otp.budget_tripped` pages the
   on-call, whose runbook is to check the per-IP counters, block the abusing networks at the edge proxy and, if the
   traffic is real, raise the limit; M3 considers a per-/24 (IPv4) or per-/48 (IPv6) hourly counter in front of the
   breaker. A distributed attacker can
   still exhaust one victim's daily SMS cap (denial of reset/new-device sign-in for that day), and anyone who knows a
   phone can keep its **unknown-device** password sign-in (and the password step of a social completion) locked by
   sending 5 wrong passwords per lock cycle (1 to 15 minutes) — the owner's known device has its own guard and stays
   usable (§2.1.4), and the password checks of a signed-in session (password change, deletion, social link/unlink) use
   the customer-id lock `customer_reauth`, which only a session holder can fill (§1.8). The price of that split: a thief
   holding a stolen session can guess on both paths, at most 5 + 5 passwords per lock cycle, both locks escalating to
   15 minutes; M3 evaluates App Attest / Play Integrity attestation on `auth/send-code/` and `auth/login/`.
4. **OTP storage.** Codes only as `HMAC-SHA256(code, SMS_CODE_HMAC_KEY)` (key ≥32 characters, required in production
   and refused when it is a placeholder or a committed value, plan §7), compared with `compare_digest`; the plain code
   exists only in memory, in the AES-GCM ciphertext in Redis for its 120 s lifetime (`SECRETBOX_KEY`, exactly 32 bytes,
   §2.1.1) and in the SMS itself — never in the outbox payload, the database, a backup, a log or a fake adapter's
   message list outside the local/test settings (§2.14.1). 5 attempts per code, 10 wrong codes per phone per day (≈10⁻⁵ success odds per phone per day for a
   guesser); resend ≥60 s; verification tokens are 256-bit, stored as SHA-256, single use, bound to (phone, purpose)
   and, for `link_social`, to the ticket, 10 min. Logs and throttle keys carry `pii_ref(x)` = first 16 hex chars of
   `HMAC-SHA256(LOG_PII_KEY, x)` for phones and fids (a plain SHA-256 of a +998 number is brute-forced in seconds).
   `SmsCode` rows and done `sms.send` outbox rows are deleted after 24 h (`accounts.cleanup_auth`).
5. **Passwords:** Argon2 (`PASSWORD_HASHERS`); a dummy hash check runs whenever no stored hash is compared (unknown
   phone, deleted customer, empty password), so `auth/login/` costs the same for every phone state (§2.1.4); min 8 +
   Django validators, never returned or logged. The password checks of a signed-in customer are locked by their own
   customer-id guard (`customer_reauth`) and also counted in the phone's `customer_login` guard (§1.8).
   `auth/reset-password/` (recovery) revokes all sessions, unlinks social identities and rotates the device key;
   `me/password/` (signed in, current password or SMS code) keeps the session and identities.
6. **Secrets on the device.** Refresh token and device key: iOS Keychain (`kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly`,
   no iCloud sync) / Android Keystore-backed EncryptedSharedPreferences; access token in memory; the refresh and
   access tokens are deleted on any `SESSION_*` or `password_changed`, on logout and on a 401 from `auth/refresh/` (the device key is kept so
   the next sign-in on this install skips the SMS; it is deleted on account deletion). The QR token is never persisted
   and the QR screen is excluded from screenshots/recents where the OS allows (`FLAG_SECURE` on Android). Server side
   the only raw refresh token ever stored is the 120-second rotation-grace cache entry in Redis.
7. **Refresh rotation** with reuse detection (§2.1.6): for `CUSTOMER_REFRESH_GRACE_SECONDS` (120 s) after a rotation the
   old token returns the very same tokens (concurrent refreshes of one app never revoke), and after that it re-rotates
   for at most `CUSTOMER_REFRESH_UNCONFIRMED_SECONDS` (10 minutes) and only until an access token of the new rotation is
   first used, so a lost response on a mobile network does not sign the user out; an old token that comes back **after**
   that, or a token replaced by a re-rotation, kills the session (`refresh_reused` → `SESSION_EXPIRED`). Within the
   120 s a thief presenting a stolen old token gets the same tokens as the app instead of a revocation — the grace the
   spec allows; their next presentation after it is reuse. The app refreshes single-flight and persists the new refresh
   token before discarding the old one (§1.2), so it never presents a stale token itself. Rotation runs
   under a row lock and re-rotation rewrites the grace cache, so concurrent refreshes cannot fork the chain; the
   rotation is an integer counter, so confirmation does not depend on clock precision.
8. **Device binding and A2.** `X-GD-Device-Id`/`X-GD-Platform` must match the session's device; a token copied to
   another install is refused (§1.2). The fid is not a secret; A2's "known device" is proven by the device key
   (§1.9), so someone who has the password and learns the fid still needs the SMS code.
9. **PII in pushes (C1).** FCM is a foreign service: titles/bodies/data carry no names, phones, addresses or amounts —
   ids, order numbers and provider names only (§2.14.2). The same holds for logs shipped off-host and error tracking
   (scrubbed; `pii_ref` instead of phones).
10. **Consent (C3).** `terms_version` (which versions both the terms and the privacy policy), `consent_at`,
    `consent_ip` at registration, social completion and every re-acceptance; registration is refused while the terms
    are not published (§2.1.3); checkout and booking refuse an outdated consent (§2.2.4). The biometric consent of
    the Phase-2 AI feature is separate and not part of M2.
11. **Payments.** Card data never reaches the backend (provider pages, opened in the system browser). The provider
    account value is an HMAC-protected reference, not a guessable id (§2.7.2). Click MD5 over raw form strings +
    `service_id` binding + amount equality + `merchant_prepare_id` binding; Payme Basic auth with `PAYME_KEY`, integer
    amount equality in tiyin, one active transaction per order (partial unique index + `-31051`), 12-hour timeout; IP
    allow-lists are defence in depth only; a provider in fake mode under the production settings starts no transaction
    and honours no capture (§2.13.0; a Click Complete of a transaction attached while Click was live is accepted and
    reversed with a manual refund). Every handler takes the Order lock before reading state and names the payment it
    changes (§2.13.0). Click callbacks are idempotent from state (a re-signed retry of an applied call succeeds again).
    Captures on non-payable orders, on payments we failed (including a Click attachment superseded by a newer Prepare)
    and double payments are **refused captures**: accepted, reversed through the Click Merchant API by `click_paydoc_id`
    and alerted, as Click's protocol requires; a reversal Click refuses becomes a staff payout, closed only after a fresh
    Click status check (§2.13.0). A transaction can be
    started only before `reservation_expires_at`; only completing one gets the 5-minute grace. Every callback is stored
    (WebhookInbox, append-only, 180-day retention, C4) with credentials stripped; forged requests are stored only as
    bounded, per-IP capped rows.
12. **Financial invariants** (spec): money as integer so'm (tiyin only inside the Payme adapter); lock order (Customer
    → Cart →) ProductStock → Order → Payment → LoyaltyAccount; the balance changes only in
    `loyalty.services.post_entry`; cashback holds are checked under the account lock; stock and visibility are
    re-checked under `ProductStock` row locks; one unpaid order per customer is serialised by the customer-level lock,
    which is `FOR NO KEY UPDATE` so that foreign-key inserts (`Purchase` on a capture) never wait for it (no deadlock
    with `DELETE me/`, §2.7.1);
    checkout, pay, cancel, reorder, booking and cart adds are idempotent; outbox handlers are idempotent at the provider
    boundary.
13. **POS minimisation** (§2.10): hashed per-device keys with revoke/rotate commands, failed-auth throttling per IP,
    single-use QR via `GETDEL` with at most two live tokens per customer, one error for every failure, no name, history
    or internal id in the response, every resolve audited.
14. **Fake providers and dev switches** (product-owner decision 2026-10-26). Two different things:
    - **Fake provider adapters** (`CLICK_MODE`, `PAYME_MODE`, `BTS_MODE`, `ESKIZ_MODE`, `FCM_MODE`, `BILLZ_MODE`,
      `OAUTH_MODE` = `fake`) are **allowed** under `config.settings.production`, which every internet-facing deployment
      (staging included) runs. Each process logs one startup WARNING listing every fake provider (logger
      `glowderma.integrations`, built in M1), and the effective mode of every provider (`billz`, `click`, `payme`, `bts`,
      `eskiz`, `fcm`, `oauth`) is reported by `GET /api/v1/health/?ready=1` (`integrations`, shown only to callers that
      pass the admin network gate) and by admin `GET settings/` (`integrations.providers`, admin §2.11, §2.15). Fake and live providers may be **mixed** (live adapters arrive one at a time in M3), so the
      rule is per provider: in that posture a fake adapter never reports a real-money or real-world effect that did not
      happen and never accepts a made-up credential. Fake Click/Payme/BTS start no transaction and honour no capture
      (§2.13.0: a Click Complete of a transaction attached while Click was live is accepted and reversed by hand; what a
      provider switched to fake still holds is the operator procedure below) and fake payment providers are not offered (§2.4), so no order is ever paid
      through a fake provider; a Click refund through the fake port becomes `manual_required`, never `succeeded`
      (§2.13.0); fake BTS turns home delivery off (§2.6: pickup only, `fulfilment_methods`) and never makes up a tariff,
      a parcel, a tracking number or a cancellation (§2.14.3), so a customer paying through a live provider is never
      charged a fake tariff for a parcel nobody ships; **online selling requires live Billz** (§2.3): with a fake Billz
      the stock is unknown, so every product reads `available: 0`, `settings/public/` answers `online_sales_enabled:
      false`, cart adds and checkout answer 409 `online_sales_unavailable`, and the fake never invents or refreshes
      stock (the admin Billz import, `POST products/` and a `billz_id` change answer 409, admin §2.3; `sync_stock` is
      never reached through it); an order placed while Billz was live and paid after the switch is never recorded as a
      Billz sale it did not send: the `billz.push_sale` handler does not call the fake, leaves the purchase's
      `billz_pushed_at` null (no `billz_sale_id`, no `billz_sale_recorded` event) and logs WARNING
      `billz.sale_not_pushed {order_id, purchase_id}`, so staff enter the sale in Billz by hand and the M3 live adapter
      can find every unpushed online sale; fake OAuth turns social sign-in
      off for customers **and staff** — 404 on the customer social routes (§2.1.8) and on the staff `POST
      auth/oauth/{provider}/` and `POST auth/oauth/{provider}/link/` (admin §2.1; unlinking keeps working, email +
      password sign-in is unaffected) — because a fake verifier accepts any `fake:<subject>:<email>` code with a
      caller-chosen nonce and would sign anyone in as any linked identity, and with `STAFF_GOOGLE_AUTOLINK` even as any
      staff email; fake Eskiz delivers no SMS (demo/staging posture, §2.14.1); fake FCM delivers no push; fakes keep no
      message history (§2.14.1, §2.14.2).
    - **Switching a payment provider from `live` to `fake`** is an operator procedure, not something the backend makes
      safe. A provider holds **open money** while it has a `pending` payment with an open transaction (Click: prepared,
      no Complete; Payme: state 1) or a refund that is not `succeeded` while its payment is still `paid` (`requested`,
      `submitting`, `unknown`, `manual_required`, `failed`). The runbook: do not switch while the provider holds open
      money — drain it first (let open transactions complete or expire and every refund of that provider settle — admin
      `GET payments/?provider=…&refund_state=…`). If a switch happens anyway (a kill switch), the backend keeps
      only what is already written: a Click Complete of a transaction attached while Click was live is accepted and
      reversed by hand (§2.13.0); a Click refund becomes `manual_required` (staff pay out and resolve); a `failed` Click
      refund cannot be status-checked and so cannot be resolved as `succeeded` until Click is live again (admin §7 item
      18); fake Payme answers `-32504` to everything, so a Payme refund cannot complete through Payme. **Money held by a
      provider that was switched to fake is reconciled by hand in that provider's cabinet** (or by switching it back to
      live); there is no automation for it. The only machinery is a check at worker start (Celery `worker_ready`,
      `commerce.tasks.report_fake_provider_open_money`; `CoreConfig.ready()` must not query the database): for each
      payment provider that is fake under the production settings and holds open money it logs one ERROR
      `providers.fake_with_open_money {provider, open_transactions, open_refunds, order_ids}` (at most 20 order ids),
      which pages the on-call.
    - **Dev switches** — `FAKE_PAYMENTS_ENABLED` (dev simulate, §2.15), `FAKE_SCENARIO_HEADER_ENABLED` (§1.2),
      `ESKIZ_FAKE_CODE` (fixed OTP), `POS_API_KEYS` (seeded POS keys, §2.10), `FAKE_NOW` and the `*` nonce wildcard of
      the OAuth fakes — work only under the local/test settings (`DEV_FEATURES_ALLOWED`, a settings-module constant,
      not an env variable) and are **refused at boot** by the production settings whatever the provider modes;
      `FAKE_PAYMENTS_ENABLED` and the scenario header additionally require every relevant provider mode to be `fake`.
      Staging therefore cannot complete an online purchase or receive a code without live providers.
15. **Enumeration.** Customer ids in URLs are sequential but always ownership-checked; phone existence is revealed only
    by the register/reset branches of `send-code` under SMS throttles (§7 `M2-A9`); a capped phone, device or network
    and a tripped global SMS budget answer `auth/login/` the same way for any password (§2.1.4 step 3); POS failures
    reveal nothing.
16. **Account takeover recovery.** Linking a social identity needs the account's strongest factor: the SMS code plus
    the password when one is set (completion), or, from a signed-in session, the password — or an SMS code when the
    account has none (§2.2.8); unlinking needs the same. Every link, unlink, reset and password change sends an
    `account_security` push and inbox row, to every signed-in device (B-W1); a reset and a password change also
    notify the devices they signed out (`farewell_tokens`, §2.14.2), and those devices' next request answers 401
    `password_changed` with a message (§1.2). The customer sees every signed-in phone and browser in «Faol seanslar»
    and can sign any of them out (§2.2.10). `reset-password` revokes every session and unlinks every identity
    (§2.1.5).
17. **Recycled numbers (PD law).** A reactivated account, and a dormant account recovered by SMS (`M2-A16`), sees only
    history created after `Customer.history_from`, with the previous holder's name, image, addresses, cart, language
    and consent reset; the previous holder keeps no way in — deletion and the dormant rule both remove every social
    identity and device key, and the recovering sign-in (reset / dormant rule) revokes every other session (§2.2.3). An account counts as dormant only
    when nothing happened on it for `CUSTOMER_DORMANT_DAYS` — sign-ins, purchases, orders and appointments, including
    staff bookings and attended visits — so it never has an open order or appointment to cancel.
18. **Uploads** (`me/image/`) go through `core.images.process_image` (re-encode, EXIF strip, 5 MiB, type sniffing).

---

## 7. Cross-references: open questions and provisional defaults

Existing points this contract implements (`docs/decisions/open-questions.md`):

| # | Provisional default used here |
|---|---|
| A1 | Delete scrubs and deactivates (incl. language, consent fields, device keys, QR tokens, Apple tokens revoked); the phone stays unique; re-registration reactivates the same row with balance and purchases; history shown from `Customer.history_from` (§2.2.3, §2.1.3, `M2-A15`, `M2-A16`) |
| A2 | Password login; SMS code for sign-up, reset and **password** login on a device without a valid device key (§2.1.4, §1.9) |
| A3 | 5 attempts per code, 15-minute lockout, 10 codes and 10 wrong codes per phone per day (`CUSTOMER_OTP_*`) |
| A6 | Social sign-in allowed (Google, Facebook, Apple); the first sign-in verifies a phone by SMS and links to that phone's customer, with the password when the account has one (§2.1.8–9) |
| A7 | Google iOS/Android client ids, the Facebook app, and for Sign in with Apple the bundle id (`APPLE_BUNDLE_IDS`), team id, key id and private key are needed from the client; fake providers until then (`GOOGLE_APP_CLIENT_IDS`, `APPLE_BUNDLE_IDS` empty = Apple disabled in live mode, fake in local/test) |
| O1 / O5 | Admin cancel rules and refund display unchanged; the customer sees "To‘lov qaytarilmoqda" until `succeeded` |
| O3 | Reservation 15 minutes (`SiteSettings.reservation_minutes`), extended only by the in-flight payment grace (`M2-P8`) |
| O4 | Pickup reuses `delivered`, labelled "Olib ketildi" for customers |
| O7 | Only Click and Payme; `payment_providers` in `settings/public/` keeps the app generic |
| L1–L5 | Engine defaults unchanged (rate before purchase, cashback-paid part does not earn, seed tiers, spendable immediately, negative balance allowed and shown as `negative`) |
| L6 / L7 | No POS redemption; how a resolved in-store sale is discovered stays open (M3); M2 only resolves and audits. The M3 Billz live adapter follows docs.billz.io (`openapi.yaml`); refunds of paid sales, per-receipt sale discovery and webhooks (none exist) stay explicit stubs (decision 2026-10-26) |
| P1 | A `requested` appointment holds the slot (exclusion constraint) |
| P2 / P3 | No deposits; cancellation terms are free text (`booking_policy`), enforced only by `BOOKING_CANCEL_MIN_LEAD_MINUTES` (0) |
| C-4 | Cashback never pays the delivery fee |
| C-6 | Customer cancel only from `awaiting_payment` (and not while a Click payment is in flight) |
| T5 | WebhookInbox written after processing in the same transaction; replay rules per provider in §2.13.0 |

New M2 points. The product owner decided on 2026-10-26 that every M2 open question uses the default below; each is
recorded in `docs/decisions/open-questions.md` as a **provisional default (decided 2026-10-26)** — implement-ready, and
the client may still revise it later through a setting or a data field:

| # | Question for the client | Provisional default |
|---|---|---|
| M2-A9 | May the app reveal whether a phone is registered (register / reset flows)? | Yes, throttled (`phone_registered` / `phone_not_registered`) |
| M2-A10 | Does a social sign-in on a new phone also need the SMS code? | No: linking requires the account's strongest factor — the SMS code plus the password if set (completion), or from a signed-in session the password, or an SMS code for accounts without one (never the bearer alone) — so the provider sign-in is a factor of equal strength; only password sign-in on a device without a device key needs the code |
| M2-A11 | Customer password rule | ≥8 characters, not only digits, not common, not similar to the phone |
| M2-A12 | Deleting an account: confirmation and open items | Password (or an SMS code for social-only accounts) required, guarded by its own re-authentication lock (failures also count against sign-in; a phone-only attacker cannot block it); unpaid orders and open appointments are cancelled; paid orders continue; refused while a Click payment is in flight (≤ 2 minutes) |
| M2-A13 | Minimum app version enforcement | Off (`MIN_APP_VERSION_*=0.0.0`) until the first store release |
| M2-A14 | Apple App Store guideline 4.8: Google/Facebook sign-in on iOS requires an equivalent privacy-focused option (Sign in with Apple) | **Decided by the product owner (2026-10-26):** offer Sign in with Apple on iOS (`apple` provider, ID token with nonce, token revocation on unlink and deletion per guideline 5.1.1(v)); `APPLE_BUNDLE_IDS` empty keeps it disabled (live) or fake (local/test) until the client provides the bundle id (A7) |
| M2-A15 | Recycled phone numbers: what does the person who reactivates a deleted account see? | Balance and purchases kept (A1); orders, appointments, inbox and ledger order links only from the reactivation (`Customer.history_from`) |
| M2-A16 | Recycled numbers of accounts that were never deleted: what does a new holder who recovers the account by SMS see? | After a password reset or an SMS-only social link on an account inactive for more than 180 days (`CUSTOMER_DORMANT_DAYS`; last sign-in, refresh, purchase, order or appointment — staff bookings and attended visits included, an open appointment always counts), the account is reset as at deletion: the history horizon moves to now; name, image, addresses and cart are scrubbed, language and consent reset, and every existing social identity and device key removed, so the previous holder cannot sign in; such an account has no open order or appointment to cancel; balance and purchases are kept (A1) |
| M2-A17 | How does a signed-in customer change or add a password? | `POST me/password/` with the current password, or an SMS code when the account has none; the session and social identities are kept. `auth/reset-password/` stays the recovery path that unlinks every identity |
| M2-C12 | Show "price changed since you added it" in the cart | Yes — needs `CartItem.price_at_add` (migration `commerce.0004`) |
| M2-C13 | Several unpaid orders at once? | No: one `awaiting_payment` order per customer (`CHECKOUT_MAX_OPEN_ORDERS=1`, serialised by the Customer row lock) |
| M2-C14 | Minimum amount paid by card when cashback is used | 1 000 so'm (`CHECKOUT_MIN_PAYABLE`), so cashback can never make an order free |
| M2-C15 | Per-line quantity ceiling (C-2) and distinct lines | 10 per line ("contact the seller" above it), 30 lines |
| M2-C16 | Refill the cart after an expired or cancelled order? | Not automatically; `orders/{id}/reorder/` copies the lines back on request |
| M2-O9 | Parcel weight for the BTS tariff | 1 000 g per order (`BTS_DEFAULT_PARCEL_WEIGHT_G`); product weights not modelled |
| M2-O10 | Pickup "ready to collect" notification | None in M2 (no status for it); staff mark pickup orders delivered |
| M2-P5 | Payme fiscal receipt data (IKPU / package codes per product) | Not sent (`PAYME_FISCAL_DETAIL_ENABLED=false`); enabling needs product fiscal codes (a catalog migration) |
| M2-P6 | Payme `ChangePassword` | Not supported (-32601); keys rotate through env |
| M2-P7 | Click Complete that we cannot honour (expired, cancelled, failed by us, superseded, double payment) | **Decided by the provider protocol (2026-10-26):** accept (`0`) and reverse through `payment/reversal` by `click_paydoc_id` — Click's SHOP API allows no other answer after funds were withdrawn; a reversal Click refuses ends as refund `failed` for a staff payout, closed as `succeeded` only after a fresh Click status check (Click reversed it after all → no payout; still paid → `manual_payout: true` after paying) (§2.13.0) |
| M2-P8 | Grace for a payment already started at the provider when the reservation ends | 5 minutes after `reservation_expires_at` (`PAYMENT_IN_FLIGHT_GRACE_SECONDS=300`), only for completing a transaction started before it; Click counts as in flight for 2 minutes after Prepare (`CLICK_INFLIGHT_SECONDS=120`, never longer than the grace), which blocks only a provider switch, the customer cancel and `DELETE me/`; paying again with Click supersedes the open Click transaction |
| M2-P9 | Provider deep links (PDF §16.3) and the return address | No `app_url` in v1: the system browser opens `web_url` and the provider page hands off to its app; return through the https link `PAYMENT_RETURN_URL` with the `glowderma://` fallback; the website must serve the app-link files |
| M2-P10 | Payme error for "order awaiting another transaction" | `-31051` (account-error range), confirmed against the test.paycom.uz checklist in M3 |
| M2-L9 | What the cashier sees after scanning | Phone (full for Billz, masked for display), available cashback, tier percent, an opaque `customer_ref`; no name, no internal id |
| M2-L10 | QR refresh | New token every 50 s and on screen focus; at most 2 live tokens per session (B-W1; per customer before), each valid 60 s, usable once and only while its session is open |
| M2-B1 | Booking limits | ≥60 min ahead, ≤30 days ahead, ≤3 open appointments, cancellation any time before the start |
| M2-N1 | Show the admin's adjustment reason to the customer? | No (generic "Bonus qo‘shildi" / "Balans tuzatildi") |
| M2-N2 | Push for a customer's own order cancellation | Yes (state machine row 5 pushes `cancelled`) |
| M2-N3 | Security notices (social link/unlink, password reset and change) | Push + inbox row (`account_security`) to every signed-in device (B-W1), and for a reset or a password change a visible push to the devices it signed out plus a 401 message there; no SMS (re-evaluated in revision 2: a reset needs the SMS code, so an SMS notice would reach whoever did it; an SMS template would also need Eskiz approval) |
| M2-F1 | May production run fake provider adapters? | **Decided by the product owner (2026-10-26):** yes, warned at startup and reported per provider in `health/?ready=1` and admin settings; fake callbacks are refused (a Click Complete of a transaction attached while Click was live is accepted and reversed), fake payment providers are not offered, the dev simulate endpoint and the fixed OTP exist only under local/test settings, and fake Eskiz delivers no SMS (demo/staging posture, §6.14). Mixed with live providers, a fake never reports an effect that did not happen: fake BTS turns home delivery off (pickup only) and makes up no parcel; online selling requires live Billz — with fake Billz the stock is unknown, every product is unavailable online (`online_sales_enabled: false`), the admin import is refused and no sale is recorded (an earlier order's sale is entered by hand). Switching a payment provider from live to fake is an operator procedure: not while it holds open money (open transactions, unsettled refunds; the worker logs an ERROR at start listing them), and money a provider switched to fake still holds is reconciled by hand in its cabinet (§2.3, §2.6, §2.14.3, §6.14) |
| M2-T1 | Terms not yet versioned in settings | Registration answers 503 `terms_not_published` until staff publish `terms_version` with the uz terms and privacy texts |

Gap closure — catalog package (`feat/gap-catalog`, 2026-10-26; recorded in `docs/decisions/open-questions.md` as
provisional defaults):

| # | Point | Provisional default used here |
|---|---|---|
| GC-1 | Search over names in three languages with partial matches (Backend EN §4) | Folded on both sides: six apostrophe encodings of o‘/g‘ are one character, `ё` = `е` (§2.3 `search`) |
| GC-3 | Banner and campaign destinations "product or link" | `link.kind` `url` with an absolute `https` `url` ≤500 (§2.3 banners, §2.12, FCM `link_url` §2.14.2) |
| GC-5 / GC-6 | "Recheck stock against Billz (or a very recent cache) at cart and checkout" | Delta every 300 s; cart writes and checkout re-read lines older than 900 s (4 s budget, before the transaction); checkout refuses 503 only when Billz cannot confirm a line older than 3600 s; cart writes never refuse; a pushed sale's products are re-read 30 s later (§2.5, §2.7.1 row 4a) |

B-W1 — web client support (`feat/web-client`, 2026-10-27; rows `W-1`… in `docs/decisions/open-questions.md`):

| # | Point | Provisional default used here |
|---|---|---|
| W-1 | Several devices at once | Any number of phones and browsers; one open session per device; `SESSION_REPLACED` retired (§1.9) |
| W-2 | `X-GD-App-Version` for the website | Optional, validated when sent, never floored (§1.2) |
| W-3 | Session list location | Local GeoIP City `.mmdb` (`GEOIP_CITY_DB`), English names, "—" when unknown; never an online lookup (§2.2.10) |
| W-4 | Browser label | Small built-in user-agent parser, no dependency (§2.2.10) |
| W-5 | Pushes with several devices | Transactional pushes to every pushable device (≤10); campaigns one device per customer (§2.14.2) |
| W-6 | Website payment return | `PAYMENT_WEB_RETURN_URL` template, chosen by the pay request's platform (§2.7.2) |
| W-7 | Slugs | Derived once from the uz name (else Billz name), transliterated, unique, never only digits, staff-editable, no redirect history (§2.3) |
| W-8 | SEO texts | Per-language `seo_title` / `seo_description`, shown only in their own language (§2.3) |
