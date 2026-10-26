# M2 customer API — implementation plan (slice B7)

**Contract:** `docs/api/customer-api-v1.md` v1.1.0-draft (this plan names its sections as "C§"). **Spec:**
`docs/specs/2026-10-24-backend-foundation-design.md` (Customer (M2), Financial integrity, Milestones → M2). **Shared
rules:** `docs/api/admin-api-v1.md` §1/§3/§4, `AGENTS.md`. Where this plan and the contract disagree, the contract
wins and this file is fixed. The review findings of revision 1 and what was decided about each are in §11.

**Goal.** The iOS/Android app can sign up, sign in on one device, browse, fill a server cart, check out with stock
reservation and a cashback hold, pay with Click or Payme (callbacks verified, idempotent from state, refused captures
refunded), track BTS delivery, see cashback and show the personal QR, book specialists, and read its inbox — on the
built M1 models with **three** small migrations (`accounts.0006`, `commerce.0004`, `payments.0002`), reusing the
B4/B5/B6 services instead of redefining them.

---

## 0. Branching, packages and order

```
feat/m1c-orders (B5) ─┐
                      ├─ feat/m2-base (lead: merge, gate green, AGENTS.md amended for M2 — §0.1)
feat/m1-followups ────┘        │
                               └─ feat/m2-p0-seams (P0, sequential, small)
                                     ├─ feat/m2-p1-accounts    (P1)  ┐
                                     ├─ feat/m2-p2-commerce    (P2)  ├ parallel, disjoint files
                                     └─ feat/m2-p3-engagement  (P3)  ┘
merge order into feat/m2-integration: P2 → P3 → P1 → lead e2e + schema regeneration
```

| Package | Scope | Worktree / branch | Test DB |
|---|---|---|---|
| **P0 seams** | Everything two packages would otherwise both edit: customer auth class + base views, device headers and device key, the `accounts.0006` migration, i18n catalog, error codes + `details`, throttle scopes, enums, settings/env/beat, URL mounts, fixtures, the FCM port extension, small core helpers (`secretbox`, `pii`, `qr_store`), the cross-package service stubs | `/home/user/wt/m2-p0`, `feat/m2-p0-seams` | `test_gd_m2_p0` |
| **P1 accounts** | Customer auth (OTP, register, login, reset, refresh, logout, social incl. Apple and the nonce), `me/*` incl. delete + addresses + device + social identities, public catalog, `settings/public/`, Eskiz port | `/home/user/wt/m2-p1`, `feat/m2-p1-accounts` | `test_gd_m2_p1` |
| **P2 commerce** | Cart, delivery regions/calculate, checkout, pay, customer cancel, reorder, Click/Payme callbacks, refused-capture refunds and the Click status check, expiry grace, customer orders + timeline, BTS webhook + polling, dev simulate | `/home/user/wt/m2-p2`, `feat/m2-p2-commerce` | `test_gd_m2_p2` |
| **P3 engagement** | Loyalty summary/ledger, QR + POS resolve + POS key commands, specialists/services/slots, booking + customer cancel, inbox list/read/ack, push senders (`inbox_id`, customer titles, account-security notices, token pruning rules) | `/home/user/wt/m2-p3`, `feat/m2-p3-engagement` | `test_gd_m2_p3` |

Why this split (instead of the suggested three): the customer authentication class, the error-envelope extension,
the i18n catalog, settings, URL mounts, fixtures and the FCM port are needed by **every** package; building them once
in P0 removes every shared-file conflict, so P1–P3 own disjoint files. The public catalog moves into P1's `customers`
module because availability (`commerce.selectors.available_stock`) may not be imported by `catalog` (layering), and
`DELETE me/` lives in `customers` (top layer) because it orchestrates commerce and appointments.

### 0.1 Rules for every package
- Dev loop of `AGENTS.md` with the package's `TEST_DB_NAME`; coverage ≥85 %; `makemigrations --check`; mypy;
  lint-imports (the layer contract is unchanged).
- **Schema file.** `AGENTS.md` asks every API-changing task to regenerate and commit `openapi/schema.yaml`, and the
  spec's slice gate diffs it. Three parallel packages cannot each commit one generated file without hand-merging it,
  so `AGENTS.md` (Dev loop, **"M2 exception"**) scopes the rule for M2: a package runs `spectacular --validate
  --fail-on-warn` (validation only, written to a scratch path) and does not commit the schema; the lead regenerates,
  diffs and commits it once on `feat/m2-integration`. The review gate of each package checks validation, not the
  diff.
- Only the files listed under "Owns" (plus new test files in the module's `tests/`). Anything else → stop and report.
- Every handler: `@extend_schema` with the operationId of C§1.1, `CUSTOMER_ERRORS`/`PUBLIC_CUSTOMER_ERRORS`, the
  device-header parameters (P0 helper), `IDEMPOTENCY_KEY_HEADER` on **I** routes, `@idempotent()` on **I** routes.
- Messages only through `core.i18n.t(key, lang, …)` with the keys of C§5 (P0 ships the whole catalog; a package that
  needs a new key stops and reports — the contract changes first).
- Services take ids and scalars, return dataclasses; cross-module writes only through the lower module's services; lock
  order Customer → Cart → ProductStock → Order → Payment → LoyaltyAccount (C§2.7.1); time via `timeutils.now()`.
- Logs and throttle keys never carry a raw phone or fid: `core.pii.pii_ref(value)` (C§6.4).
- Commits: small, imperative (`feat(customers): add the send-code endpoint`), never pushed.

---

## 1. P0 — seams (sequential, before P1–P3)

### Owns
| File | Change |
|---|---|
| `glowderma/core/enums.py` | the new enums of C§3 (`OtpPurpose`, `NextStep`, `SocialProvider` incl. `apple`, `SocialTokenType`, `DevicePlatform`, `StockStatus`, `CustomerProductOrdering`, `CartLineStatus`, `OrderScope`, `CustomerRefundState`, `CashbackEarnedState`, `TimelineType`, `ProgressStep`, `ProgressState`, `LedgerSource`, `AppointmentScope`, `AppointmentCancelledBy`, `InboxKind`, `InboxLinkKind`, `PushDataType`, `AccountSecurityEvent`) |
| `glowderma/core/i18n.py` (new) | `CATALOG` with every key of C§5 (incl. `push.*`), `t(key, lang, **params)`, `resolve_language(request, customer=None)` (Accept-Language q-parsing, fallback customer → `uz`), `format_number(n, lang)`, `format_percent(p, lang)` |
| `glowderma/core/secretbox.py` (new) | `seal(plaintext: bytes, *, aad: bytes) -> str` / `open_(token: str, *, aad: bytes) -> bytes`: AES-256-GCM (`cryptography`, already pinned through PyJWT[crypto]) under `SMS_PAYLOAD_KEY`, random 96-bit nonce |
| `glowderma/core/pii.py` (new) | `pii_ref(value: str) -> str` = first 16 hex of HMAC-SHA256(`LOG_PII_KEY`, value) |
| `glowderma/core/qr_store.py` (new) | raw-Redis store for QR keys (C§2.9.3): `put(key_hash, customer_id, ttl, max_live)`, `take(key_hash) -> str \| None` (`GETDEL`), `revoke_all(customer_id)`; literal prefix `gd:loyalty:qr:`; LocMem fallback with a process lock for tests. Lives in core so `accounts` (revocation) and `loyalty` (issue/resolve) can both use it |
| `glowderma/core/api/exceptions.py` | customer codes (C§1.6) as `CustomerError(code, status, *, details=None, params=None)` subclasses (incl. `terms_update_required`, `slot_taken`, `last_sign_in_method`, `sms_unavailable`, `terms_not_published`); `ENDPOINT_FIELD_CODES` += `otp_invalid`, `password_too_short`, `password_too_common`, `password_entirely_numeric`, `password_too_similar`, `password_unchanged`, `password_incorrect`, `terms_outdated`, `push_token_missing`, `latlng_pair`, `address_incomplete`; `AUTH_FAILURE_CODES` unchanged; the handler adds `details` (null by default) and renders `message`/field messages through `core.i18n` **only** when the resolved view is a customer/public/POS view (`view.audience` attribute); admin bodies stay byte-identical (regression test) |
| `glowderma/core/api/schema.py` | `CUSTOMER_ERRORS`, `PUBLIC_CUSTOMER_ERRORS`, `DEVICE_HEADERS` (`X-GD-Device-Id`, `X-GD-Platform`, `X-GD-App-Version`), `DEVICE_KEY_HEADER`, `ACCEPT_LANGUAGE_HEADER` |
| `glowderma/core/throttling.py` | new scopes `sms_device: 10/min`, `customer_auth_device: 30/min`, `customer_auth_ip: 1200/min`, `refresh_unknown: 60/min`, `qr: 6/min`, `public: 300/min`, `public_ip: 3000/min`; `sms_ip` default `600/min` (used only by `auth/send-code/`) |
| `glowderma/core/audit.py` | `record()` accepts a `Customer` or `PosDevice` actor: `actor_type` `customer`/`integration`, `actor_label` = masked phone / device name (no `get_username` on non-staff) |
| `glowderma/accounts/models.py` + `migrations/0006_customer_auth_hardening.py` | `Device.key_hash = CharField(64, blank=True)`; partial index `accounts_authsession_prev_refresh` on `AuthSession.previous_refresh_hash` `WHERE previous_refresh_hash <> ''`; `AuthSession.revoke_reason` choice `refresh_reused`; `CustomerSocialIdentity.provider` choice `apple`; `Customer.is_authenticated = True`, `is_anonymous = False` (properties, no SQL) |
| `glowderma/accounts/tokens.py` | access TTL by audience (`STAFF_ACCESS_TTL` / `CUSTOMER_ACCESS_TTL`); customer access claim `rot` |
| `glowderma/accounts/authentication.py` | `CustomerJWTAuthentication` (aud `customer`; `auth:sid:{sid}` cache value `active:{abs_expiry}:{pii_ref(fid)}:{rot}:{confirmed}` so the device check needs no query; tombstones `revoked:<reason>`; device/platform header match; `last_active_at`/`app_version` bump ≤ every 5 min; first sighting of the current `rot` → the conditional confirmation UPDATE of C§2.1.6); `OptionalCustomerAuthentication` |
| `glowderma/accounts/permissions.py` | `IsActiveCustomer` |
| `glowderma/accounts/api/customer_base.py` (new) | `CustomerAPIView`, `CustomerGenericViewSet`, `PublicAPIView`, `WebhookAPIView` (CSRF-exempt, parsers set per view, no auth, provider-native responses), device-header parsing + 400 `device_required` + 426 `app_update_required` gate (a mixin used by the auth views too). **Not** `PosAPIView` (P3, `loyalty/api/pos/base.py`) |
| `glowderma/accounts/api/throttles.py` | `CustomerRateThrottle` (`customer`), `PublicDeviceThrottle` (`public`), `PublicIPThrottle` (`public_ip`), `CheckoutThrottle`, `PayThrottle`, `QrThrottle`, `SmsPhoneThrottle`, `SmsDeviceThrottle`, `SmsIPThrottle`, `CustomerAuthDeviceThrottle`, `CustomerAuthIPThrottle`, `RefreshUnknownThrottle` (counts only misses), `PosRateThrottle` |
| `glowderma/accounts/services.py` | `start_customer_session(customer_id, *, fid, platform, app_version, issue_device_key) -> SessionStart(session, raw_refresh, raw_device_key \| None)` (C§1.9 steps 1–6: deactivated devices lose their token, farewell payload carries `sid` + token, skipped when the token is held by another active row, `core.qr_store.revoke_all`), `revoke_customer_sessions(customer_id, reason, except_sid=None)` (also `qr_store.revoke_all`), `register_device(customer_id, fid, …)` (a token taken from another row clears it there), `device_is_known(customer_id, fid, raw_device_key) -> bool` (`compare_digest`), `invalidate_fcm_token(device_id)`, tombstone TTL = max(staff, customer access TTL) + 60 |
| `glowderma/accounts/topics.py` (new) | `SMS_SEND = "sms.send"`, `PUSH_SESSION_REPLACED = "push.session_replaced"`, `PUSH_ACCOUNT_SECURITY = "push.account_security"` + dedupe-key builders (`sms.send:{sms_code_id}`, `push.session_replaced:{session_id}`, `push.account_security:{customer_id}:{event}:{uuid}`) |
| `glowderma/accounts/tasks.py` (new) | handler `push.session_replaced` (data-only FCM message through the extended port; blanks the token in the payload after the send; `unregistered` → `invalidate_fcm_token`); beat task `accounts.cleanup_auth` (delete `SmsCode` older than 24 h, `done`/`dead` `sms.send` outbox rows older than 24 h, customer `AuthSession` rows revoked or expired for >90 days) |
| `glowderma/integrations/fcm/{ports,fake}.py` | `PushMessage(token, title, body, data, *, data_only, android_channel, android_priority, apns_push_type, apns_priority, image_url, collapse_key)`; `PushResult.error` ∈ {`unregistered`, `payload_invalid`, …}; the fake records every field and maps tokens `…-bad` → `unregistered`, `…-payload` → `payload_invalid`, `…-down` → `FcmUnavailable` (moved here from P3 so P0's session-replaced push can be built) |
| `glowderma/catalog/selectors.py` | `customer_visible_products()` (C§2.3 visibility) |
| `glowderma/commerce/services.py` | stub `cancel_by_customer(order_id: int, customer_id: int, *, at=None) -> int` raising `NotImplementedError("P2")` |
| `glowderma/commerce/selectors.py` | stub `customer_inflight_order(customer_id: int) -> int \| None` returning `None` (P2 implements C§2.2.3 step 1) |
| `glowderma/appointments/services.py` | stub `cancel_by_customer(appointment_id: int, customer_id: int, *, reason: str = "", at=None) -> Appointment` raising `NotImplementedError("P3")` |
| URL stubs | `glowderma/{customers,site_config,delivery,commerce,loyalty,appointments,notifications}/api/customer/{__init__,urls}.py`, `glowderma/commerce/api/webhooks/{__init__,urls}.py`, `glowderma/commerce/api/dev/{__init__,urls}.py`, `glowderma/loyalty/api/pos/{__init__,urls}.py` — each `urlpatterns = []` |
| `config/urls.py` | `CUSTOMER_API_MODULES = ("customers", "site_config", "delivery", "commerce", "loyalty", "appointments", "notifications")` mounted at `api/v1/`; `api/v1/webhooks/` → commerce webhooks; `api/v1/pos/` → loyalty pos; `api/v1/dev/` only when `FAKE_PAYMENTS_ENABLED` and `CLICK_MODE == PAYME_MODE == "fake"` |
| `config/settings/base.py`, `local.py`, `test.py`, `production.py` | every setting of §7 with validation (§7 rules); `ESKIZ_FAKE_CODE` read only in `local.py`/`test.py` |
| `config/celery.py` | beat entries of §8 (`commerce.poll_bts` points at a P0 stub task `return []`, implemented by P2) |
| `.env.example` | the lines of §7 |
| `conftest.py`, `glowderma/accounts/tests/factories.py` | fixtures `customer`, `customer_session`, `customer_token`, `customer_client` (bearer + device headers `fid-test-0001`, `ios`, `1.0.0` + a known device key), `public_client`, `pos_device` + `pos_key`, `pos_client`; helpers `device_headers(fid=…, platform=…, version=…, key=…)`, `raw_device_key(device)` |

### Tests (P0)
- Audience separation both ways; customer token on admin routes → 401; staff token on a dummy customer route → 401.
- Revocation codes through the cache (`replaced` → `SESSION_REPLACED`; `refresh_reused`, `logout`, … →
  `SESSION_EXPIRED`) without a DB hit; deleted customer → `SESSION_EXPIRED`.
- Device headers: missing → 400 `device_required`; fid mismatch → 401 + one audit row per hour; platform mismatch;
  `X-GD-App-Version` below the floor → 426 with `details`.
- `start_customer_session`: second device revokes the first (`replaced`), deactivates its device **and clears its
  token**, enqueues `push.session_replaced` with `sid`; no farewell push when the token now belongs to another active
  device; device key issued only with `issue_device_key`, stored as SHA-256; `device_is_known` false for fid-only and
  for a wrong key; **race:** two concurrent sign-ins of one customer on two devices (threads, `transaction=True`) end
  with exactly one open session and one active device.
- Access-token confirmation: the first request with the current `rot` writes `last_used_at` once; later requests do
  not write.
- `register_device`: a token held by another row is removed there; `push_enabled` without a token → error.
- `secretbox` round trip, tamper and wrong-AAD failure; `pii_ref` keyed (differs with another key); `qr_store`
  `take` is single-use under two threads, `max_live` evicts the oldest, `revoke_all` empties.
- FCM port: the fake records `data_only`/APNs/Android fields; error mapping.
- i18n: every catalog key has uz/ru/en with identical placeholder sets; `resolve_language` matrix (`ru-RU`, `en;q=0.5,
  uz`, unsupported, customer fallback).
- Error envelope: customer views get `details` and localized messages; admin responses unchanged (snapshot test of
  one validation error and one 409).
- `core.idempotency` with a customer principal (replay / 422 / 409) and audience scoping (same key, staff vs customer);
  a 409 from an idempotent view leaves no row written (rollback).
- `accounts.cleanup_auth` purges only the right rows (incl. done `sms.send` outbox rows); the throttle scope validation
  knows the new scopes.
- Settings validation: `FAKE_PAYMENTS_ENABLED` with a live payment mode, `FAKE_SCENARIO_HEADER_ENABLED` with any live
  provider mode, production with `ESKIZ_MODE=fake` → `ImproperlyConfigured`; `ESKIZ_FAKE_CODE` ignored by production.
- Migration `accounts.0006` round trip (AGENTS.md rule: end with a plain `migrate`).
- The automatic URL-walk auth matrix is extended: every route under `api/v1/` that is not admin/health/schema is
  classified **C**/**O**/**S**/**K** from its base class and probed accordingly.

### Depends on
`feat/m2-base` green (the `AGENTS.md` "M2 exception" of §0.1 is already in place).

---

## 2. P1 — accounts, profile, public catalog, settings

### Owns
`glowderma/customers/{services.py, selectors.py, api/customer/*}` (new views: auth, me, social, catalog, delete),
`glowderma/accounts/services.py` (OTP and credential flows only — below the P0 block), `glowderma/accounts/tasks.py`
(the `sms.send` handler, appended), `glowderma/integrations/eskiz/{__init__,ports,fake,registry}.py` (new),
`glowderma/integrations/oauth/{ports,fake,google,facebook,apple (new),registry}.py` (`verify_app_token`, Apple),
`glowderma/site_config/{selectors.py, api/customer/*}`, `glowderma/catalog/selectors.py` (customer list helpers, after
P0's function), tests in `customers/tests/`, `accounts/tests/`, `integrations/tests/`, `site_config/tests/`.

### Endpoints
C§2.0 rows 1–22: `auth/send-code/`, `auth/verify-code/`, `auth/set-password/`, `auth/login/`, `auth/reset-password/`,
`auth/refresh/`, `auth/logout/`, `auth/social/nonce/`, `auth/social/`, `auth/social/complete/`, `me/`
(GET/PATCH/DELETE), `me/consent/`, `me/image/`, `me/device/`, `me/social/` + `me/social/{provider}/`,
`me/addresses/*`, `catalog/banners/`, `catalog/categories/`, `catalog/products/`, `catalog/products/{id}/`,
`settings/public/`.

### Services
| New | Where | Notes |
|---|---|---|
| `send_code(phone, purpose, *, ip, fid, lang, platform, social_jti=None) -> CodeIssue` | `accounts.services` | refuses `login_new_device`; global budget (Redis hour counter) → `SmsUnavailable`; `pg_advisory_xact_lock(hashtext('otp:'+phone))`; lockout, daily wrong-code cap, resend window, phone/fid/IP day caps (C§2.1.1); HMAC row; `secretbox.seal` → Redis `gd:otp:payload:{id}` (120 s); outbox `sms.send {sms_code_id, lang, platform}`; `gd:social:otp:{id}` = ticket jti for `link_social` |
| `issue_login_code(phone, *, ip, fid, lang, platform)` | `accounts.services` | the `login_new_device` path used only by `auth/login/` (same internals; reuses a live code < 60 s) |
| `otp_blocked(phone, now) -> OtpBlock \| None` | `accounts.services` | the pre-password check of login step 3 |
| `verify_code(phone, purpose, code) -> raw verification token` | `accounts.services` | advisory lock + `select_for_update`; attempts, exhaustion, daily wrong cap |
| `consume_verification(token, *, purpose, phone=None) -> (phone, sms_code_id)` | `accounts.services` | single use under lock |
| `rotate_customer_refresh(raw, *, fid) -> RefreshResult` | `accounts.services` | C§2.1.6 steps 1–6: row lock on `refresh_hash OR previous_refresh_hash`, rotate / replay / re-rotate / reuse → revoke `refresh_reused` + audit; grace cache written before commit and deleted on rollback |
| `logout_customer(raw)` | `accounts.services` | revoke + device inactive, token cleared, QR tokens revoked |
| `issue_social_nonce(fid)`, `consume_social_nonce(nonce, fid)`, `remember_id_token(provider, claims)` | `accounts.services` | Redis, single use, replay cache until `exp` |
| `register(...)`, `login(...)`, `reset_password(...)` (unlinks identities, clears other device keys, `push.account_security`), `social_sign_in(...)`, `complete_social(...)` (fid + ticket binding, password for password accounts), `link_social(...)`, `unlink_social(...)`, `accept_consent(...)`, `update_profile(...)`, `set_image(...)`, `delete_account(customer_id, *, password=None, verification_token=None)` | `customers.services` | orchestrators (need `site_config` for the terms version and publication check and, for delete, `commerce`/`appointments`); `delete_account` locks the Customer row first, refuses on `commerce.selectors.customer_inflight_order` |
| `public_settings()` | `site_config.selectors` | cached 60 s, invalidated on `update_settings` commit; `terms_version` null while unpublished |
| `product_cards(qs, lang, customer_id=None)` | `customers.selectors` | one query for products + `available_stock` + main image + cart quantities |
| `verify_app_token(token, token_type, nonce)` | `integrations.oauth` (Google, Facebook, Apple) | Apple: RS256 against Apple's JWKS, `aud` ∈ `APPLE_APP_BUNDLE_IDS`, nonce = SHA-256 of ours |

**Reused (do not redefine):** `accounts.services.{add_address, update_address, set_default_address, remove_address,
set_customer_password, check_customer_password, delete_customer, reactivate_customer, start_customer_session (P0),
register_device (P0), revoke_customer_sessions (P0), device_is_known (P0)}`, `accounts.api.guard.LoginGuard` (scopes
`customer_login`, `customer_delete`), `catalog.selectors.{filter_products_search, customer_visible_products,
category_queryset, banner_queryset}`, `commerce.selectors.available_stock` (B5), `site_config.models.SiteSettings.load`,
`core.images.process_image`, `core.secretbox`, `core.pii`, `commerce.services.cancel_by_customer` (P2) and
`appointments.services.cancel_by_customer` (P3) from `delete_account` (called only when the customer has open items;
P1 tests cover the no-open-items path with the P0 stubs, the lead's integration test the rest).

### Models / migrations
None (P0 owns `accounts.0006`). The link-social ticket binding and the OTP fid/IP day counters live in Redis (§11).

### Fakes
- **FakeEskiz** (C§2.14.1): phone ending `0000` → `EskizRejected`, `9999` → `EskizUnavailable`, else accepted
  `fake-<sms_code_id>`; `FakeEskiz.sent` list (rendered texts, so tests can read the code without a DB lookup); the
  code generator returns `ESKIZ_FAKE_CODE` only under local/test settings.
- **OAuth `verify_app_token` fakes**: `fake:<subject>:<email>` valid (nonce must equal the issued nonce or `*` in unit
  tests), `fake-unverified:…` valid without verified email, `fake-fail` → `exchange_failed`, `fake-down` →
  `unavailable`, `fake-expired` → `invalid_token`; Apple `fake-apple:<subject>:<email>` (relay addresses allowed).

### Tests (P1)
- Every validation row of C§2.1–§2.4 and C§2.2.8 (one test per code, uz message; one ru and one en rendering per
  endpoint group).
- OTP: resend window (61 s ok, 59 s 429 with `Retry-After`), daily cap 10 (Tashkent day boundary), fid cap, IP cap,
  daily wrong-code cap across codes, lockout after 5 wrong codes, superseded code, expired code, `compare_digest`
  path, `login_new_device` refused by send-code, global budget → 503 + ERROR log; verification token single use /
  purpose / phone binding / expiry; **race:** two concurrent `send-code` for one phone → one SMS, one live code, the
  other 429; **race:** two concurrent `verify-code` with the right code → one token issued, the other `otp_expired`.
- **No plain code at rest:** after `send-code`, no `OutboxMessage.payload`, no `SmsCode` column and no log record
  contains the 6 digits; the handler sends the right text from the ciphertext and deletes it; missing ciphertext →
  nothing sent.
- Register: new customer; deleted customer reactivated with the same id, ledger and purchases intact (spec test
  "delete → reactivate keeps balance and purchases"), `reactivated_at` set; consent stamped; outdated terms; terms
  unpublished → 503 `terms_not_published`.
- Login: device key known → tokens; fid known but key missing/wrong → `verify_device` + SMS; second call with the
  token → tokens, new device key, the old session `SESSION_REPLACED`; wrong password timing parity (hasher called for
  unknown phones); guard lockout; **oracle test:** with the phone's code cap exhausted, a right and a wrong password
  get the same 429 body.
- Refresh: rotation; concurrent refresh threads → identical tokens and one chain; lost response → retry after 45 s and
  after 10 min with the old token → 200 (re-rotation, nothing revoked); grace-cache miss → re-rotate; old token after
  the new access token was used → 401 `SESSION_EXPIRED`, audit `customer.refresh_reused`, session dead for the new token
  too; sliding expiry; device mismatch; junk tokens hit `refresh_unknown`.
- Social: nonce issue/consume single use and fid binding; replayed ID token refused; linked → tokens; unknown →
  ticket bound to the fid (another fid → `social_ticket_invalid`); send-code with ticket; complete for active
  (password required when set, wrong → `password_incorrect`), social-only, no and deleted customer; a verification token
  from a code requested without/with another ticket → `verification_invalid`; `social_provider_linked`,
  `social_identity_linked` (unique-race); Apple happy path and missing bundle id → 404; `push.account_security`
  enqueued on link.
- `me/social/`: link with password, without password on a social-only account, unlink, `last_sign_in_method`.
- Reset: sessions revoked, identities unlinked with audit rows, other device keys cleared, new device key returned,
  `push.account_security` enqueued.
- `DELETE me/`: password check + `customer_delete` guard; social-only needs a `reset_password` verification token;
  in-flight order (stub returning an id) → 409 `payment_in_progress` and nothing written; scrub (name, image file,
  addresses, identities, device tokens and keys), sessions revoked with tombstones, QR tokens revoked, audit;
  re-registration afterwards.
- Addresses through the services (default handling, `address_text` recomputed, limit 10, foreign id 404, POST requires
  an Idempotency-Key and replays).
- Catalog: visibility (hidden product, inactive category, price 0), availability = stock − active reservations,
  search in ru/en, `in_stock`, ordering, language fallback chain to `billz_name`, banner window and link downgrade,
  page-size allow-list, optional auth (invalid token → 401, absent → anonymous), cache headers (`public` anonymous,
  `private, no-cache` with a bearer).
- `sms.send` handler: claim before call, no resend after claim, connect-error un-claim, 4xx not retried, Android app
  hash line.

### Depends on
P0. For the delete orchestration at integration time: P2 (`commerce.services.cancel_by_customer`,
`commerce.selectors.customer_inflight_order`) and P3 (`appointments.services.cancel_by_customer`).

---

## 3. P2 — cart, delivery, checkout, payments, orders, BTS

### Owns
`glowderma/commerce/{models.py, migrations/0004_cartitem_price_at_add.py, services.py, selectors.py, payment_ref.py
(new), tasks.py, seeds.py (call-site updates for the changed signatures only), api/customer/*, api/webhooks/*,
api/dev/*}`, `glowderma/payments/{models.py, migrations/0002_payment_active_excludes_late_capture.py, services.py,
selectors.py, topics.py}`, `glowderma/delivery/{services.py, selectors.py, api/customer/*}`,
`glowderma/integrations/click/*`, `glowderma/integrations/payme/*`, `glowderma/integrations/bts/{ports,fake,registry}.py`,
tests in those modules.

### Endpoints
C§2.0 rows 23–34 and 49–52: `cart/*`, `delivery/regions/`, `delivery/calculate/`, `orders/checkout/`,
`orders/{id}/pay/`, `orders/{id}/cancel/`, `orders/{id}/reorder/`, `me/orders/`, `me/orders/{id}/`,
`webhooks/click/prepare/`, `webhooks/click/complete/`, `webhooks/payme/`, `webhooks/bts/`,
`dev/payments/{order_id}/simulate/`.

### Services
| New / changed | Module | Notes |
|---|---|---|
| `cart_view(customer_id, lang)`, `add_to_cart`, `set_cart_quantity`, `remove_from_cart`, `clear_cart`, `reorder_into_cart(order_id, customer_id)` | `commerce.services` / `selectors` | Cart row lock; `price_at_add`; clamping to `max_quantity`; C§2.5/C§2.7.4 errors |
| `cashback_max(items_subtotal, delivery_fee, available)` | `commerce.selectors` | the one formula of C§2.7 (cart, calculate, checkout) |
| `create_order(…, delivery_fee: int, expected_total: int \| None = None, min_payable: int = 0, visible_only: bool = False)` | `commerce.services` (B5, changed) | **B5 changes, all of them:** (1) the BTS quote leaves `create_order` — the caller passes `delivery_fee` (no HTTP call under `ProductStock` locks; the seeder passes its fee); (2) visibility through `catalog.selectors.customer_visible_products()` when `visible_only` (status, price ≥ 1000, active category) under the stock locks; (3) `OutOfStock(lines=[{product_id, name, requested, available, status}])` with every problem line; (4) `CashbackAboveMax(max)` from `min_payable` evaluated on the live subtotal (replaces the bare `cashback_used ≤ items_subtotal` check); (5) `PriceChanged(totals)` when `expected_total` differs; (6) typed `AddressNotFound` / `AddressIncomplete` instead of the generic `CheckoutError`; (7) evaluation order stock → cashback max → expected total → rows → `create_hold` (`InsufficientCashback`) |
| `checkout(customer_id, request) -> order_id` | `commerce.services` | quote first (no row lock), then `select_for_update` on the Customer row, open-order rule and cart read under it, `create_order`, cart emptied, `Cart.last_checkout_order` |
| `is_payable(order, payment, now, *, attaching)`, `in_flight(payment, now)`, `customer_inflight_order(customer_id)` | `commerce.services` / `selectors` | C§2.7.2 definitions; the selector replaces the P0 stub |
| `ensure_payable_payment(order_id, provider) -> PaymentRow` | `commerce.services` | reuse / in-flight refusal (`PaymentInProgress`) / switch (`abandoned`) / stale Click attachment → new row / new row after a failure; updates `Order.payment_provider`; under `lock_order` |
| `cancel_by_customer(order_id, customer_id, *, at=None)` | `commerce.services` | replaces the P0 stub; state.py row 5; Click in flight → `PaymentInProgress` |
| `mark_paid(order_id, payment_id, provider, provider_txn_id, at=None)` | `commerce.services` (B5, **signature changed**) | refuses unless `payment_id` is the order's pending payment of `provider`; callers (seed, sandbox) updated |
| `fail_payment_effects(order_id, payment_id, error_code, at=None, *, provider_txn_id=None)` | `commerce.services` (B5, **signature changed**) | acts on that payment only |
| `record_late_capture(order_id, payment_id, provider, provider_txn_id, *, case, at=None)` | `commerce.services` | refused-capture rule (C§2.13.0): `payments.mark_late_captured` + `create_refund(origin=late_capture)` + `refund_requested` event + audit `payment.late_capture` + ERROR log |
| `expire_reservation` / `expire_due_reservations` | `commerce.services` (B5, changed) | skip an order whose payment is in flight and was attached before `reservation_expires_at`, until the grace ends |
| `customer_orders(customer_id, scope)`, `customer_order_detail(order_id, customer_id, lang)`, `customer_timeline(order, lang)`, `progress(order, lang)` | `commerce.selectors` | C§2.8, C§4; history horizon (`Customer.reactivated_at`); live item names in the resolved language |
| `build(order_id)`, `verify(ref) -> order_id \| None` | `commerce.payment_ref` | C§1.4/C§2.7.2 account reference, `compare_digest` |
| `poll_bts()` | `commerce.tasks` | replaces the P0 stub; C§2.13.3 |
| handler `payments.click_status_check` | `commerce.tasks` | C§2.13.0 `true` mode: Click `payment_status(click_paydoc_id)` → `record_late_capture` if still paid |
| webhook inbox helper (`store_delivery`, `store_rejected` with the per-IP cap and 1 KiB truncation, `semantic_fingerprint`) | `commerce/api/webhooks/inbox.py` | C§2.13.0 |
| `attach_transaction(payment_id, provider_txn_id, provider_state)` (refuses a different existing txn id → `TransactionTaken`), `merge_provider_state(payment_id, patch)`, `mark_late_captured(payment_id, provider_txn_id, at, raw=None)` (pending or failed-by-us → `paid`, `error_code="late_capture"`, `failed_at=None`) | `payments.services` | under the Payment lock |
| `active_payment`, `display_payment` ignore `error_code = "late_capture"`; `payme_statement(from_ms, to_ms)` by `provider_state.time`; `payment_by_txn(provider, txn_id)` | `payments.selectors` (B5, changed) | |
| `refund.submit` payload for Click carries `provider_state.click_paydoc_id` | `payments.services` (`_new_refund`) | C§2.13.0 |
| `region_catalog()` | `delivery.selectors` | BTS `regions()` cached 24 h (`gd:bts:regions`) |
| Click protocol (`sign_ok`, `parse_amount`, `fingerprint`, `ERRORS`, `CLICK_ACCEPT_JSON`), Payme protocol (`auth_ok`, `rpc_error`, `MESSAGES`, `TIMEOUT_MS`, `ERR_ORDER_NOT_FOUND = -31050`, `ERR_ORDER_BUSY = -31051`, `INTERNAL_ERROR_RESPONSE`) | `integrations.{click,payme}.protocol` | pure functions and constants, no DB |
| `ClickPort.pay_url(account_ref, amount, return_url)`, `PaymePort.checkout_url(account_ref, amount_tiyin, lang, return_url)` | `integrations.{click,payme}` ports + fakes | |
| `BtsPort.track(tracking_number)`, `BtsPort.regions()`; `quote` may raise `BtsRejected` | `integrations.bts` | fakes per §6 |

**Reused (do not redefine):** `commerce.services.{create_order, mark_paid, confirm_payment_effects,
fail_payment_effects, expire_reservation, apply_bts_status, resolve_refund, record_event, lock_order}` (B5, changed as
above), `commerce.selectors.available_stock`, `payments.services.{create_payment, confirm_payment, fail_payment,
create_refund}`, `payments.selectors.{active_payment, display_payment, refunds_for_order}`,
`delivery.services.{ensure_shipment, request_bts_create, apply_status}`, `delivery.selectors.shipment_info`,
`loyalty.selectors.account_summary` (cart and calculate `cashback`), `loyalty.services.create_hold` (inside
`create_order`), `catalog.selectors.customer_visible_products` (P0), `site_config.SiteSettings` (pickup address).

### Models / migrations
- `commerce.0004_cartitem_price_at_add`: `CartItem.price_at_add = BigIntegerField(null=True)` (null for lines created
  before the migration → `price_changed: false`).
- `payments.0002_payment_active_excludes_late_capture`: the partial unique `payment_one_active_per_order` becomes
  `status IN ('pending','paid') AND error_code <> 'late_capture'`, so a late-captured payment of a double payment can be
  `paid` next to the order's real paid payment until its refund succeeds.
- Nothing else: `Order.metadata` carries `checkout.*`/`fake.*`, `Payment.provider_state` carries the protocol fields
  (design 02 §2.1), `Refund.Origin.LATE_CAPTURE` exists. No unique index on open orders (§11). B5 migration numbers are
  re-based by the lead if B5 adds migrations before the merge.

### Tests (P2)
- Cart: every C§2.5 rule (clamp, `max_reached`, `OUT_OF_STOCK` on zero, `product_unavailable`, `cart_full`, bulk
  delete limits, `price_changed` after an admin price edit, statuses after a Billz stock drop); POST requires an
  Idempotency-Key and a retried add does not add twice; reorder (clamp, unavailable lines kept, `cart_full`, horizon
  404).
- Delivery calculate: own address, region unknown, BTS rejected → 409, BTS down → 503, pickup → 0, `cashback_max` and
  `expected_total` equal checkout's numbers for the same cart.
- Checkout: every row of C§2.7.1 in order (incl. a product in a hidden category → `OUT_OF_STOCK` `unavailable`, a
  1-so'm remainder → `cashback_max`); snapshots and events equal to B5's admin expectations; cart emptied; replay returns
  the stored 201 with an empty cart; consent outdated → 409 `terms_update_required`; no HTTP call while a row lock is
  held (fake BTS asserts no open `select_for_update`).
- **Race — two checkouts for the last unit** (threads, `transaction=True`): exactly one order, one reservation; the
  other gets `OUT_OF_STOCK` with `available: 0`.
- **Race — two checkouts of one customer with different keys**: one order, the other `open_order_exists` (the second
  blocks on the Customer row lock).
- **Race — checkout vs the Customer lock held by a delete** (a thread holding `select_for_update` on the customer and
  then deactivating it): checkout ends with 401 `SESSION_EXPIRED`, no order.
- Pay: reuse, switch Click↔Payme (`abandoned`), Click in flight → `payment_in_progress`, stale Click attachment → new
  row, Payme state-1 refusal, not payable → 409 `order_expired` **with no write** (the order stays `awaiting_payment`
  until the job), already paid, URL format (Click query with `account_ref`, Payme base64 with tiyin, language and
  `account_ref`), `return_url` https.
- Customer cancel: from `awaiting_payment` (Click pending failed; Payme state-1 left pending), Click in flight → 409,
  refused otherwise.
- Customer orders: scope filters, ownership 404, history horizon (orders before `reactivated_at` invisible and 404),
  №10248 timeline vector (C§4.1) in uz/ru/en, progress for delivery, pickup and cancelled orders, refund mapping,
  `payment.in_progress`, `actions`, item names in ru/en.
- **Click sandbox suite (offline, §5.1)** and **Payme sandbox suite (offline, §5.2)** — every row.
- **Race — expiry vs late callback:** (a) Click complete holding the Order lock while the expiry job runs → the job
  skips (`skip_locked`), the payment wins; (b) the job wins first → complete is a refused capture: `0` + late-capture
  refund (default) or `-9` + a delayed status check (`true`); (c) prepare at T−30 s, expiry at T, complete at T+60 s →
  paid (grace); (d) complete after the grace → refused capture; (e) prepare at T+10 s → `-9` (attaching after
  `reservation_expires_at`).
- **Race — pay switch vs Click complete** (threads): the switch to Payme and the Complete of the prepared Click payment
  run concurrently → either the Complete wins (Click paid, the switch answers `already_paid`) or the switch is refused
  `payment_in_progress`; never a Payme row confirmed with a Click transaction id.
- **Race — duplicate concurrent Click complete** (two threads, same payload): one effect, both answers `0`.
- **Race — two Payme `CreateTransaction` with different ids for one order** (threads): one state-1 transaction, the
  other `-31051`; the first transaction stays `CheckTransaction`-able (never overwritten).
- Account reference: a wrong HMAC part → Click `-5`, Payme `-31050` (`data: "order_id"`), before any order lookup.
- Webhook inbox: forged signature never occupies the real key (`rejected:` rows, bounded to 1 KiB, no raw body); more
  than 10 forged requests per IP per minute store nothing more; credentials absent from stored headers; `path` without
  the BTS token; Payme read-only methods without `raw_body`; `entity_ref` set.
- Outbox "crash after provider success": Click complete commits, the response is lost, Click retries **re-signed**
  (new `sign_time`) → `0` from state, one purchase, one credit; the same for Prepare.
- BTS: webhook bad token 401 (bounded row); query-token stripped from the stored path; re-track is authoritative
  (payload says `300`, track says `200` → `handed_to_courier`); a payload tracking number of another parcel is ignored
  (track called with ours, WARNING); unknown parcel ignored; transport failure 503 and nothing stored; polling picks
  stale shipments only; regions cache.
- Dev simulate: mounted only with the flag and fake payment modes; runs the real views; refused in production settings.
- Migrations `commerce.0004` and `payments.0002` round trip.

### Depends on
P0; B5 merged (`feat/m2-base`).

---

## 4. P3 — loyalty, QR/POS, appointments, inbox

### Owns
`glowderma/loyalty/{qr.py (new), selectors.py, api/customer/*, api/pos/* (incl. base.py: PosAPIView), authentication.py
(new: PosKeyAuthentication with the failed-auth counter, IsActivePosDevice), services.py (PosDevice key creation,
revoke, rotate), management/commands/{createposkey,revokeposkey,rotateposkey}.py}`,
`glowderma/appointments/{services.py, selectors.py, api/customer/*}`, `glowderma/notifications/{services.py,
selectors.py, tasks.py (the `push.account_security` handler), texts.py, api/customer/*}`, tests in those modules.

### Endpoints
C§2.0 rows 35–48: `me/cashback/`, `me/cashback/ledger/`, `me/qr/`, `pos/cashback/resolve-qr/`, `specialists/`,
`specialists/{id}/`, `services/`, `specialists/{id}/slots/`, `appointments/`, `me/appointments/`,
`me/appointments/{id}/`, `appointments/{id}/cancel/`, `me/notifications/`, `me/notifications/unread-count/`,
`me/notifications/{id}/read/`, `me/notifications/read-all/`, `me/notifications/ack/`.

### Services
| New / changed | Module | Notes |
|---|---|---|
| `issue_qr(customer_id) -> QrToken`, `resolve_qr(token) -> customer_id \| None` | `loyalty.qr` | 128-bit token, HMAC key, `core.qr_store.put(…, ttl=60, max_live=2)` / `take` (`GETDEL`) |
| `create_pos_device(name, location_label, actor_email) -> (PosDevice, raw_key)`, `revoke_pos_device(prefix, actor_email)`, `rotate_pos_key(prefix, actor_email) -> raw_key`, `authenticate_pos_key(raw)` | `loyalty.services` / `loyalty.authentication` | SHA-256, `compare_digest`, `last_used_at` ≤ once per minute; failed-auth Redis counter per IP; audits `pos.device_created/revoked/rotated` with the staff actor; `POS_API_KEYS` seeding in fake mode (seeder) |
| `pos_customer_ref(device_id, customer_id)` | `loyalty.qr` | opaque `c_…` (C§2.10) |
| `customer_ledger_page(customer_id)`, `ledger_item_view(row, lang, horizon)` | `loyalty.selectors` | over `ledger_for_customer` rows, paginated in SQL; before-horizon rows without order links |
| `effective_holds(customer_id, now)` | `loyalty.selectors` | C§2.9.1: an **active** hold whose `expires_at` has passed exists only while the expiry job skips its order for an in-flight payment (otherwise expiry would have released it), so the selector shows `expires_at + PAYMENT_IN_FLIGHT_GRACE_SECONDS` for such holds and `expires_at` otherwise — no read of commerce or payments (layering) |
| `free_slots(professional_id, service_id, date, now)`, `check_customer_slot(...)` | `appointments.selectors` / `services` | C§2.11 algorithm (grid relative to the hours start, lead time, horizon, blocks, break, open appointments) |
| `request_by_customer(customer_id, professional_id, service_id, start, note)` | `appointments.services` | Customer row lock first, then count + own overlap; exclusion race → 409 `slot_taken` (reuse `_lost_the_slot`); event `requested` source `customer`; audit |
| `cancel_by_customer(appointment_id, customer_id, *, reason="", at=None)` | `appointments.services` | replaces the P0 stub |
| `customer_appointments(customer_id, scope)` | `appointments.selectors` | history horizon |
| `inbox_page(customer_id)`, `unread_count(customer_id)` | `notifications.selectors` | `inbox_customer_unread_idx`; history horizon |
| `acknowledge(customer_id, inbox_ids)`, `mark_read(customer_id, inbox_id)`, `mark_all_read(customer_id)` | `notifications.services` | read and read-all call `acknowledge` first; campaign `delivered` increment only on a real transition |
| `deliver_order_status` (changed): title = `t('label.order_status[_pickup].<status>', customer.language)`; skip orders created before `customer.reactivated_at`; `deliver_appointment` unchanged texts; `deliver_account_security(payload)` (new, handler of `push.account_security`) | `notifications.services` / `tasks` | C§2.14.2, C§4.3 |
| push payloads with `inbox_id`; channel/priority per `data.type`; `unregistered` → `accounts.services.invalidate_fcm_token`; `payload_invalid` → message failed + ERROR, device untouched | `notifications.services` (`_push_messages`, `_deliver`, `_settle`) | C§2.14.2, using the P0 port |

**Reused (do not redefine):** `loyalty.selectors.{account_summary, customer_totals, tier_for, active_policy,
rules_of, ledger_for_customer, held_amount}`, `professionals.selectors.{day_schedules, booking_catalogue}`,
`appointments.services.{active_link, record_event, _lost_the_slot}`, `appointments.selectors.get_appointment`,
`notifications.services.{_deliver, deliver_order_status, deliver_appointment, send_batch}`,
`notifications.selectors.customer_push_device`, `accounts.services.invalidate_fcm_token` (P0), `core.qr_store` (P0),
`integrations.fcm` port (P0).

### Models / migrations
None (`PosDevice`, `InboxNotification.delivered_at`, `CampaignRecipient.delivered_at`, `Campaign.delivered`,
`Appointment.CancelledBy.CUSTOMER`, `AppointmentEvent.Source.CUSTOMER` exist).

### Tests (P3)
- Cashback summary numbers equal the admin card for Malika (seed vectors); negative balance flag; holds listed with the
  effective expiry during a grace; ledger titles/subtitles for every kind in uz/ru/en; adjustment reason never leaked;
  history horizon on the ledger (amounts kept, order links removed).
- QR: token format; TTL 60 s (pinned clock + cache expiry); **race — double resolve** (two threads, one token): exactly
  one 200, one `qr_invalid`; a third token evicts the first; throttle `qr`; tokens revoked on logout/replace/delete;
  expired token; deleted customer; malformed token; audit rows; response field allow-list (snapshot: no name, no
  `customer_id`, stable `customer_ref` per device).
- POS auth: unknown/inactive key 401, 21st failed auth from one IP → 429 without a lookup, key never logged, throttle
  `pos`, `createposkey` prints the key once and stores only the hash, requires `--actor`; `revokeposkey` → 401 on the next
  call; `rotateposkey` → old key 401, new key 200; audits.
- Slots: hours override vs weekly, day off, break, blocked period across midnight, open appointments (requested holds,
  rejected frees), lead time, horizon, slot step alignment; booking validation rows of C§2.11; **race — two customers
  book the same slot** → one 201, one 409 `slot_taken` (including the 40P01 deadlock branch); **race — one customer books
  two different slots concurrently at the limit** → one 201, one `booking_limit`; own-overlap; customer cancel and the
  slot freed; ownership 404; horizon.
- Inbox: pagination + `unread_count`; read one/all; ack idempotent (a retried ack does not double-count
  `Campaign.delivered`); read before ack counts once; foreign ids ignored; horizon.
- Pushes: `inbox_id` present for the senders; order-status title in ru and en and "Olib ketildi" for a pickup
  delivery; no push for an order before the horizon; account-security push texts; channel/priority mapping; no PII
  (assert title/body/data contain no customer name or phone for seeded customers); `unregistered` prunes the device;
  `payload_invalid` leaves every device untouched and fails the message.

### Depends on
P0.

---

## 5. Offline sandbox suites (P2)

Run with the real webhook views and a pinned clock; signatures built by `integrations/click/sandbox.py` /
`integrations/payme/sandbox.py` (test helpers, also used by `dev/payments/…/simulate/`). `R` = `reservation_expires_at`.

### 5.1 Click (order 10248, total 784 400, account reference `10248-<ref>`, payment id 301)

| # | Call | Input variation | Expected `error` | Expected state |
|---|---|---|---|---|
| 1 | prepare | valid | 0, `merchant_prepare_id` 301 | payment pending, txn attached, `prepare_at`, `click_paydoc_id` stored |
| 2 | prepare | same payload again | 0 (replayed) | unchanged; one effect |
| 3 | prepare | same `click_trans_id`, re-signed (new `sign_time`) | 0, same prepare id | unchanged |
| 4 | prepare | wrong `sign_string` | -1 | nothing; bounded `rejected:` row |
| 5 | prepare | `action=1` on prepare URL | -3 | nothing |
| 6 | prepare | unknown order / wrong HMAC part / bare order id | -5 | nothing |
| 7 | prepare | `amount=784401` / `"784400.50"` / `"abc"` | -2 | nothing |
| 8 | prepare | `service_id` other / missing param / missing `click_paydoc_id` / JSON body | -8 | nothing |
| 9 | prepare | order cancelled, or `now ≥ R` | -9 | nothing |
| 10 | prepare | order paid | -4 | nothing |
| 11 | prepare | new `click_trans_id` while 301 is prepared < 10 min ago | -9 | 301 untouched |
| 12 | prepare | new `click_trans_id` while 301 was prepared > 10 min ago (before `R`) | 0, new prepare id | 301 failed `abandoned`, new pending |
| 13 | prepare | while a Payme transaction is in state 1 | -9 | nothing |
| 14 | complete | valid | 0, `merchant_confirm_id` 301 | paid, order `processing`, events, outbox |
| 15 | complete | same payload again | 0 (replayed) | one purchase, one credit |
| 16 | complete | re-signed retry after success | 0 (from state) | one purchase, one credit |
| 17 | complete | unknown `merchant_prepare_id` | -6 | nothing |
| 18 | complete | `error=-5017` | -9 | payment failed, `payment_failed` event, order still payable |
| 19 | complete | amount differs | -2 | pending |
| 20 | complete | prepared before `R`, complete within the grace | 0 | paid normally |
| 21 | complete | after expiry beyond the grace (order cancelled by the job, payment failed `expired`), reverses=false | 0 | payment `paid` `late_capture`, `Refund(late_capture)` requested + `refund.submit` (with `click_paydoc_id`) + `refund_requested` event + audit `payment.late_capture {case: failed_by_us}` |
| 22 | complete | as 21, reverses=true | -9 | payment stays failed; `payments.click_status_check` enqueued with a 900 s delay |
| 23 | complete | as 22, status check finds the payment still paid (`click.status = "paid"`) | – | late-capture refund created, ERROR `payment.click_not_reversed` |
| 24 | complete | as 22, status check finds it reversed | – | nothing, INFO log |
| 25 | complete | 301 failed `abandoned` by a switch to Payme, reverses=false | 0 | late-capture refund `{case: failed_by_us}`; the Payme payment untouched |
| 26 | complete | the order was meanwhile paid by Payme (double payment), reverses=false | 0 | 301 `paid` `late_capture` next to the Payme paid row (index allows it), refund `{case: double_payment}`, order unchanged |
| 27 | complete | as 26, reverses=true | -4 | status check enqueued |
| 28 | complete | after a customer cancel (only possible once Click left the in-flight window), reverses=false | 0 | refund `{case: failed_by_us}` |
| 29 | complete | bad signature (incl. `merchant_prepare_id` tampered) | -1 | nothing |
| 30 | complete | payment failed by Click earlier (`error_code "-5017"`) | -9 | nothing |
| 31 | complete | payment already refunded after a capture | 0 (from state) | nothing new |

### 5.2 Payme (mirrors the `test.paycom.uz` checklist; amounts in tiyin 78 440 000; codes marked † verify M3)

| # | Method | Variation | Expected |
|---|---|---|---|
| 1 | any | no/wrong Basic auth | -32504 (bounded `rejected:` row) |
| 2 | – | body not JSON / no `method` / unknown method / `ChangePassword` / GET | -32700 / -32600 / -32601 / -32601 / -32300 |
| 3 | CheckPerformTransaction | unknown order / wrong HMAC part / missing `order_id` | -31050 (`data: "order_id"`) |
| 4 | CheckPerformTransaction | wrong amount / amount `78440000.5` / amount `0` | -31001 |
| 5 | CheckPerformTransaction | cancelled / paid / `now ≥ R` order | -31008 |
| 6 | CheckPerformTransaction | another transaction in state 1, or a Click transaction in flight | -31051 † (`data: "order_id"`) |
| 7 | CheckPerformTransaction | valid | `{"allow": true}` |
| 8 | CreateTransaction | valid new id | state 1, `transaction` "301" |
| 9 | CreateTransaction | same id again | same `create_time`, state 1 |
| 10 | CreateTransaction | other id while 8 is state 1 ("account awaiting payment") | -31051 † |
| 11 | CreateTransaction | `time` older than 12 h | -31008 |
| 12 | CreateTransaction | new id after `R` (within the grace) | -31008 (attaching needs `now < R`) |
| 13 | PerformTransaction | unknown id | -31003 |
| 14 | PerformTransaction | valid | state 2, order `processing` |
| 15 | PerformTransaction | again | same `perform_time`, state 2, one purchase |
| 16 | PerformTransaction | created before `R`, performed within the grace | state 2 |
| 17 | CancelTransaction | state 2, no refund open | -31007 |
| 18 | CancelTransaction | state 2 after an admin cancel (`manual_required`) | state -2, refund `succeeded`, payment `refunded`, `refund_succeeded` event |
| 19 | CancelTransaction | state 1 (reason 3) | state -1, payment failed, `payment_failed` event |
| 20 | CancelTransaction | again | same `cancel_time`, state -1 |
| 21 | PerformTransaction | state -1 | -31008 |
| 22 | CreateTransaction / Perform | state-1 transaction older than 12 h | state -1 reason 4, -31008 |
| 23 | PerformTransaction | order expired beyond the grace while state 1 | -31008, payment stays pending (Payme cancels it †) |
| 24 | CheckTransaction | each state | fields per C§2.13.2 (0 for missing times, `reason` null) |
| 25 | GetStatement | window covering the Payme `time` of 8 and 19 | both, ascending by Payme `time` (not our `create_time`) † |
| 26 | any | an exception inside the handler | JSON-RPC -32400 †, nothing written |

---

## 6. Fake provider behaviour (deterministic switches for e2e)

Black-box e2e drives every branch with `X-GD-Fake-Scenario` (copied into `Order.metadata["fake"]` at checkout; read
directly by `delivery/calculate/`, which has no order yet; honoured only with every provider mode fake and the flag on)
or with the phone/token grammar below.

| Provider | Switch | Effect |
|---|---|---|
| Eskiz | phone `…0000` / `…9999` / other | rejected / transport down / accepted; code = `ESKIZ_FAKE_CODE` (local/test settings only) |
| OAuth | token `fake:<sub>:<email>`, `fake-unverified:…`, `fake-apple:<sub>:<email>`, `fake-fail`, `fake-down`, `fake-expired` | valid / valid unverified / Apple valid / `social_token_invalid` / 503 / `social_token_invalid` |
| BTS | `bts.quote`: `"rejected"` / `"down"`; `bts.tariff`: int | 409 `delivery_unavailable` / 503 / fixed fee |
| BTS | `bts.fail_attempts`, `bts.down`, `bts.existing`, `bts.tracking_number`, `bts.status`, `bts.cancel` | B5 rules (create/cancel) |
| BTS | `bts.track_status`: `"100"`/`"200"`/`"300"`/other | what `track()` answers (webhook re-track, polling) |
| Click | `click.refund`: `succeeded`/`failed`/`unknown`/`down` | B5 refund rules (also used by the late-capture refund) |
| Click | `click.status`: `paid`/`reversed`/`down` | what `payment_status()` answers to the delayed status check |
| Click | `click.transaction_id`, `click.paydoc_id` | fixed ids for `dev/…/simulate/` |
| Payme | `payme.transaction_id` | fixed Payme id for `dev/…/simulate/` |
| FCM | token `…-bad` / `…-payload` / `…-down` | `unregistered` (device pruned) / `payload_invalid` (message failed, device untouched) / `FcmUnavailable` (retry) |
| POS | `POS_API_KEYS=name:key,…` | seeded `PosDevice` rows (fake mode) |
| Clock | `FAKE_NOW` (local/test) | pinned `timeutils.now()` for expiry/grace/slot tests |

---

## 7. Settings and environment (P0 adds all; `.env.example` lines)

```dotenv
# ---- Customer API (M2) ---------------------------------------------------------
# Access JWT lifetime (seconds) and the sliding refresh window (days); cache window for concurrent refreshes (seconds).
#CUSTOMER_ACCESS_TTL=900
#CUSTOMER_REFRESH_TTL_DAYS=60
#CUSTOMER_REFRESH_GRACE_SECONDS=120
# OTP policy (A3): code TTL, resend wait, attempts, lockout, daily caps per phone / device / IP, wrong codes per day.
#CUSTOMER_OTP_TTL_SECONDS=120
#CUSTOMER_OTP_RESEND_SECONDS=60
#CUSTOMER_OTP_MAX_ATTEMPTS=5
#CUSTOMER_OTP_LOCKOUT_MINUTES=15
#CUSTOMER_OTP_DAILY_LIMIT=10
#CUSTOMER_OTP_DAILY_WRONG_LIMIT=10
#CUSTOMER_OTP_DEVICE_DAILY_LIMIT=10
#CUSTOMER_OTP_IP_DAILY_LIMIT=200
# Circuit breaker: SMS codes per hour across all phones (503 sms_unavailable + alert above it).
#SMS_GLOBAL_HOURLY_LIMIT=500
#CUSTOMER_VERIFICATION_TTL_SECONDS=600
#CUSTOMER_PASSWORD_MIN_LENGTH=8
# HMAC key for stored SMS codes and AES-256-GCM key for the code in transit to the SMS handler (32+ random bytes, base64url). Required in production.
SMS_CODE_HMAC_KEY=change-me-sms-code-hmac-key-at-least-32-bytes
SMS_PAYLOAD_KEY=change-me-sms-payload-aes-key-32-bytes-base64url
# HMAC key for pseudonymous phone/device references in logs and throttle keys. Required in production.
LOG_PII_KEY=change-me-log-pii-key-at-least-32-bytes
# Android SMS Retriever app hash (11 chars) appended to the OTP text for Android; empty = no hash line.
#ANDROID_SMS_APP_HASH=
#SOCIAL_TICKET_TTL_SECONDS=600
#SOCIAL_NONCE_TTL_SECONDS=600
# Google client ids the app's ID tokens are issued for (iOS client id, web/server client id used by Android), comma separated.
#GOOGLE_APP_CLIENT_IDS=change-me-ios.apps.googleusercontent.com,change-me-web.apps.googleusercontent.com
# Sign in with Apple: the iOS bundle id(s) Apple ID tokens are issued for; empty = provider "apple" disabled (404).
#APPLE_APP_BUNDLE_IDS=uz.glowderma.app
# Minimum app versions (426 app_update_required below them) and store links.
#MIN_APP_VERSION_IOS=0.0.0
#MIN_APP_VERSION_ANDROID=0.0.0
#APP_STORE_URL=https://apps.apple.com/app/id0000000000
#PLAY_STORE_URL=https://play.google.com/store/apps/details?id=uz.glowderma.app
#ADDRESS_MAX_PER_CUSTOMER=10
# ---- Eskiz SMS ----------------------------------------------------------------
# fake = in-process (phone ...0000 rejected, ...9999 down); live = notify.eskiz.uz (M3). Production refuses fake.
#ESKIZ_MODE=fake
#ESKIZ_BASE_URL=https://notify.eskiz.uz/api
#ESKIZ_EMAIL=change-me@example.uz
#ESKIZ_PASSWORD=change-me-eskiz-password
#ESKIZ_SENDER=4546
#ESKIZ_CALLBACK_URL=
# Fixed OTP for black-box e2e; read ONLY by config.settings.local / test (production ignores it).
#ESKIZ_FAKE_CODE=123456
# ---- Cart and checkout ---------------------------------------------------------
#CART_MAX_LINES=30
#CART_MAX_QTY_PER_LINE=10
#CHECKOUT_MAX_OPEN_ORDERS=1
#CHECKOUT_MIN_PAYABLE=1000
# Seconds after reservation_expires_at during which a transaction started before it may still complete.
#PAYMENT_IN_FLIGHT_GRACE_SECONDS=300
# HMAC key of the provider account reference "<order_id>-<ref>" (32+ random bytes). Required in production.
PAYMENT_REF_KEY=change-me-payment-ref-key-at-least-32-bytes
# Where the provider page sends the customer back (https app/universal link; order_id is appended) and the custom-scheme fallback.
#PAYMENT_RETURN_URL=https://glowderma.uz/pay/return
#PAYMENT_RETURN_FALLBACK_URL=glowderma://payment/return
# ---- Click (SHOP API) ----------------------------------------------------------
#CLICK_MODE=fake
#CLICK_SERVICE_ID=00000
#CLICK_MERCHANT_ID=00000
#CLICK_MERCHANT_USER_ID=00000
CLICK_SECRET_KEY=change-me-click-secret-key
#CLICK_PAY_URL=https://my.click.uz/services/pay
#CLICK_ALLOWED_IPS=
# A prepared Click payment counts as in flight (blocks switch/cancel/second prepare) for this many seconds.
#CLICK_INFLIGHT_SECONDS=600
# false (default): a Complete we cannot honour is accepted and refunded automatically; true (only after M3 proves
# Click reverses on an error answer): answer an error and re-check the payment status after the delay below.
#CLICK_REVERSES_ON_COMPLETE_ERROR=false
#CLICK_STATUS_CHECK_DELAY_SECONDS=900
# ---- Payme (Merchant API) ------------------------------------------------------
#PAYME_MODE=fake
#PAYME_MERCHANT_ID=change-me-payme-merchant-id
PAYME_KEY=change-me-payme-key
#PAYME_CHECKOUT_URL=https://checkout.paycom.uz
#PAYME_ACCOUNT_FIELD=order_id
#PAYME_ALLOWED_IPS=
#PAYME_FISCAL_DETAIL_ENABLED=false
# ---- Webhooks ------------------------------------------------------------------
# Stored rows per IP per minute for requests that fail provider authentication (the rest are only counted).
#WEBHOOK_REJECTED_PER_IP_PER_MIN=10
# ---- BTS ---------------------------------------------------------------------
#BTS_BASE_URL=https://api.bts.uz
#BTS_LOGIN=change-me
#BTS_PASSWORD=change-me
#BTS_SENDER_CITY_CODE=1001
#BTS_DEFAULT_PARCEL_WEIGHT_G=1000
BTS_WEBHOOK_SECRET=change-me-bts-webhook-secret
#BTS_POLL_STALE_MINUTES=30
# ---- FCM (live adapter M3: HTTP v1 over httpx with a service-account JWT) ------------
#FCM_PROJECT_ID=change-me-firebase-project
#FCM_CREDENTIALS_FILE=/run/secrets/fcm.json
# ---- Loyalty QR and POS ----------------------------------------------------------
QR_HMAC_KEY=change-me-qr-hmac-key-at-least-32-bytes
#QR_TOKEN_TTL_SECONDS=60
#QR_MAX_LIVE_TOKENS=2
#POS_FAILED_AUTH_PER_IP_PER_MIN=20
# Fake mode only: POS devices seeded as name:key pairs (production keys come from `manage.py createposkey`).
#POS_API_KEYS=Kassa 1:gdpos_demo0001_change-me-32-characters-key
# ---- Appointments ------------------------------------------------------------------
#BOOKING_MIN_LEAD_MINUTES=60
#BOOKING_HORIZON_DAYS=30
#BOOKING_MAX_OPEN_PER_CUSTOMER=3
#BOOKING_CANCEL_MIN_LEAD_MINUTES=0
# ---- Development switches (refused in production; require fake provider modes) ------
#FAKE_SCENARIO_HEADER_ENABLED=false
#FAKE_PAYMENTS_ENABLED=false
```
Validation (P0): positive integers where numeric; `ESKIZ_MODE`/`CLICK_MODE`/`PAYME_MODE`/`BTS_MODE` ∈ {fake, live}
(default `INTEGRATIONS_MODE`); `FAKE_PAYMENTS_ENABLED` requires `CLICK_MODE == PAYME_MODE == "fake"`;
`FAKE_SCENARIO_HEADER_ENABLED` requires every provider mode (`ESKIZ`, `CLICK`, `PAYME`, `BTS`, `FCM`) fake; production
requires `SMS_CODE_HMAC_KEY`, `SMS_PAYLOAD_KEY`, `LOG_PII_KEY`, `PAYMENT_REF_KEY`, `QR_HMAC_KEY` (≥32 bytes) always and
`CLICK_SECRET_KEY`, `PAYME_KEY`, `BTS_WEBHOOK_SECRET` when the matching mode is live, refuses `ESKIZ_MODE=fake`,
`FAKE_SCENARIO_HEADER_ENABLED`, `FAKE_PAYMENTS_ENABLED`, and never reads `ESKIZ_FAKE_CODE`; `THROTTLE_RATES` knows the new
scopes. Staging runs the production settings module.

## 8. Beat entries (`config/celery.py`, P0)

| Entry | Task | Schedule | Owner of the body |
|---|---|---|---|
| `accounts-cleanup-auth` | `accounts.cleanup_auth` | every 3600 s | P0 |
| `commerce-poll-bts` | `commerce.poll_bts` | every 600 s | P2 (P0 stub returns `[]`) |
| (exists, B5) `commerce-expire-reservations` | `commerce.expire_reservations` | every 60 s | B5; grace change by P2 |

No beat entry for `loyalty.services.expire_holds`: it must stay unscheduled while the in-flight grace exists (C§2.9.1).
The Click status check is an outbox message with a delay (`outbox.enqueue(…, delay=…)`), not a beat job.

---

## 9. Integration (lead) and cross-package e2e

1. Merge P2, P3, P1 into `feat/m2-integration`; run the full gate; regenerate, diff and commit `openapi/schema.yaml`.
2. Integration tests (lead, `glowderma/customers/tests/test_e2e_m2.py`):
   - register → login on a second device (`verify_device`) → first device gets `SESSION_REPLACED` and a
     `session_replaced` push carrying its `sid` → refresh rotation → lost-response retry;
   - catalog → cart (clamp) → calculate → checkout with cashback → pay (Click) → sandbox prepare/complete → order
     `processing`, hold captured, credit posted, push inbox row with the customer-language title, Billz push + BTS
     create outbox → BTS webhook `200` → `handed_to_courier` push → polling `300` → `delivered`;
   - checkout → expiry (pinned clock) → hold released, stock available again, Payme Perform -31008 → reorder refills
     the cart;
   - Click prepare → expiry job skips during the grace → complete after the grace → late-capture refund, timeline
     "To‘lov qaytarilmoqda";
   - admin cancel of a Payme-paid order → Payme `CancelTransaction` → refund succeeded → customer timeline "To‘lov
     qaytarildi";
   - `DELETE me/` with an unpaid order and an open appointment → both cancelled → re-register → same id, balance kept,
     old orders, appointments and inbox rows invisible;
   - `DELETE me/` while a Click payment is in flight → 409;
   - social link from settings → `account_security` push → reset-password → identity unlinked;
   - QR issue → POS resolve → second resolve `qr_invalid`;
   - booking → admin confirm (B6 endpoint) → push with `inbox_id` → ack → admin `delivered` counter.
3. Update `AGENTS.md` (customer base views, fixtures, i18n rule, fake switches, `pii_ref`; drop the "M2 exception" of
   §0.1 once the schema is committed on `feat/m2-integration`),
   `docs/decisions/open-questions.md` (the `M2-*` rows of C§7) and the spec's M2 section if a decision changed it
   (refused-capture default, device key for A2, history horizon for A1).
4. Ops: the website serves `apple-app-site-association` / `assetlinks.json` for `PAYMENT_RETURN_URL`; the edge proxy
   log format redacts `token=`; alerts on the ERROR events `otp.budget_tripped`, `payment.late_capture`,
   `payment.click_not_reversed`, `push.payload_invalid`; throttle rates reviewed after two weeks of production metrics.
5. Mobile handover: contract link, `openapi/schema.yaml`, the fake-mode recipe (`ESKIZ_FAKE_CODE`, dev simulate), the
   device-key and refresh rules (C§1.9, C§2.1.6), the system-browser payment hand-off (C§2.7.2).

## 10. Dependencies at a glance

| From | Needs | Why |
|---|---|---|
| P0 | `feat/m2-base` (B5 + followups merged) | commerce services/selectors, Click/Payme/BTS ports (the `AGENTS.md` schema rule is already amended) |
| P1 | P0 | auth class, device key, `accounts.0006`, catalog, i18n, settings, `secretbox`, `pii`, `qr_store`, `start_customer_session` |
| P1 (`DELETE me/` integration) | P2 `cancel_by_customer` + `customer_inflight_order`, P3 `appointments.cancel_by_customer` | stubs in P0 keep P1 independent until merge |
| P2 | P0, B5 | `create_order`, `mark_paid`, `fail_payment_effects`, `apply_bts_status`, `resolve_refund` (signatures changed by P2 itself) |
| P3 | P0 | auth class, FCM port extension, `qr_store`, `invalidate_fcm_token`, i18n; the `push.account_security` topic constant |
| Lead e2e | P1 + P2 + P3 | full flows |

---

## 11. Review log (revision 1)

Each finding of the revision-1 review (security and consistency lenses, merged where they overlap) and the decision.
"Adopted" means the contract and this plan now say what the finding asked; anything else is explained.

| # | Finding (section) | Decision |
|---|---|---|
| 1 | **Blocker** — late-capture refund unreachable: B5 expiry/switch/cancel fail the Click payment first, step 3 answers -9, `confirm_payment` cannot go failed→paid, a new row is impossible (C§2.13.1, plan §5.1) | Adopted, extended. One **refused-capture** rule (C§2.13.0) for payments failed by us, non-payable orders and double payments, evaluated after the honoured-capture check. `payments.mark_late_captured` is the only failed→paid transition. **Model change the finding did not foresee:** a double payment cannot be marked `paid` while the order's real payment is `paid` because of `payment_one_active_per_order`; `payments.0002` excludes `error_code = 'late_capture'` from that partial unique, and the selectors ignore such rows. Default flipped to `CLICK_REVERSES_ON_COMPLETE_ERROR=false` (deterministic: accept and refund); `true` adds the delayed status check by `click_paydoc_id`. Sandbox rows 21–28 cover expiry, abandoned, double payment and cancel with both flag values. |
| 2 | **Blocker** — per-transaction advisory locks only; services act on "the pending payment", so a concurrent switch can make `mark_paid` confirm the wrong row and two `CreateTransaction` can overwrite each other (C§2.13.0, C§2.7.2) | Adopted. Every handler takes `lock_order` before reading state; `mark_paid`, `fail_payment_effects`, `record_late_capture` take `payment_id` + provider and refuse otherwise; `attach_transaction` refuses a different existing txn id (`TransactionTaken`). Race tests added (pay switch vs Complete; two concurrent CreateTransaction). |
| 3 | **Blocker/major (both lenses)** — the raw OTP is in the `sms.send` outbox payload (C§2.1.1, C§6.4) | Adopted (option a). The code travels as AES-256-GCM ciphertext in Redis (`SMS_PAYLOAD_KEY`, 120 s); the payload has only `sms_code_id`, `lang`, `platform`; done rows are purged after 24 h; a test asserts no DB row or log holds the code. Design 03 §5's registry row is updated by the lead with the other doc updates (§9.3). |
| 4 | Major — in-flight Click payments unprotected; grace from `attached_at` lets repeated prepares keep an order alive; on-the-spot expiry ignores the grace and is rolled back by `@idempotent` (C§1.9, C§2.7.2, C§2.13.0) | Adopted. Grace anchored to `reservation_expires_at`, only for completing a transaction attached before it; attaching needs `now < R`; Click in flight for `CLICK_INFLIGHT_SECONDS` (600) blocks switch, cancel, delete, a second prepare and Payme create; the pay endpoint never writes on refusal. **Partly not adopted:** a Payme state-1 transaction does not block the customer's cancel, because Payme debits only on `PerformTransaction`, which a cancelled order refuses (P-2), so there is no money at risk and the customer is not stuck for up to 12 h. |
| 5 | Major — sequential order id as provider account value enables payment DoS (C§2.7.2) | Adopted: `"<order_id>-<10 hex HMAC>"`, verified in constant time before any lookup (Click -5, Payme -31050); no migration. |
| 6 | Major (both lenses) — Click duplicates by exact payload hash; re-signed retry of an applied Complete gets -8/-4 | Adopted: Prepare and Complete are idempotent from state (a captured or refunded payment of that `click_trans_id` answers 0); fingerprint over semantic fields; -8 only on conflict; sandbox rows 3, 16, 31. |
| 7 | Major — Payme details: account-error code for "another transaction", GetStatement by Payme time, non-integer amount, "Payme cancels" unverified (C§2.13.2) | Adopted with **-31051** (`ERR_ORDER_BUSY`, account range) for "another transaction / in flight"; paid, cancelled and expired keep -31008. The security lens's in-flight fix said -31008 for Payme create; the Payme-protocol finding is the more specific one, so -31051 wins, behind a constant (verify M3, `M2-P10`). GetStatement filters `provider_state.time`; the expression index is deferred to M3 (volumes are tiny; avoids a fourth migration now). Non-integer/≤0 amount → -31001. -32400 for internal errors behind `INTERNAL_ERROR_RESPONSE`. |
| 8 | Major (both lenses) — refresh: no row lock, no index on `previous_refresh_hash`, junk tokens bypass the per-token throttle, undefined grace-miss behaviour, 30 s grace logs out real users with a misleading message (C§2.1.6) | Adopted. Row lock on `refresh_hash OR previous_refresh_hash`; partial index in `accounts.0006`; `refresh_unknown` per-IP throttle on misses; check order revoked/expired → current → previous. Reuse = the old token after the **new one was used** (confirmation by the new refresh or the first access token carrying the current `rot`, recorded in `last_used_at` — no new column); before confirmation the old token replays or re-rotates with no time limit. Grace cache 120 s. New revoke reason `refresh_reused` → `SESSION_EXPIRED`; `SESSION_REPLACED` only for the single-device rule. |
| 9 | Major — send-code: no per-phone serialisation, `login_new_device` without password, no global/per-IP budget, victim cap burning, 50 guesses/day, capped-login oracle (C§2.1.1, C§2.1.4) | Adopted: advisory lock per phone; `login_new_device` only via `auth/login/` after the password; fid and IP day caps (Redis counters — they are cost controls, so a flush is acceptable; the phone caps stay DB-derived) and a global hourly breaker with an alert; 10 wrong codes per phone per day; login checks OTP availability **before** the password for unknown devices. Victim-cap burning remains a residual risk (C§6.3), App Attest / Play Integrity noted for M3. |
| 10 | Major (both lenses) — A2 "known device" rests on the non-secret fid (C§1.9, C§6.8) | Adopted: device key issued on every verified sign-in, SHA-256 on `Device.key_hash` (`accounts.0006`), header `X-GD-Device-Key`; rotated on reset; the fid no longer appears in `device_label`. |
| 11 | Major — social linking needs only an SMS code; no unlink; reset keeps identities (C§2.1.8–9) | Adopted: complete requires the password when the account has one; `POST/DELETE me/social/…`; reset unlinks every identity; `account_security` push on link/unlink/reset. **M2-A10 kept** (social sign-in skips the new-device SMS): with linking now requiring password + SMS (or a session + password), the provider sign-in is as strong as the A2 path. |
| 12 | Major — recycled numbers expose the previous holder's history after reactivation (C§2.2.3) | Adopted (history horizon at `reactivated_at`, new `M2-A15`); also no order-status push for pre-horizon orders. Scrubbing order snapshots at delete was not chosen: staff and accounting still need them. |
| 13 | Major (both lenses) — nothing serialises a customer's checkouts (C§2.7.1) | Adopted with the **Customer** row lock (not the Cart row): it also serialises checkout against `DELETE me/`, which the consistency lens raised. Lock order documented. **Backstop unique index not added:** the B5 seed creates six `awaiting_payment` orders (10238–10242 for generated customers plus a design order) whose customers may coincide, so an index migration could fail on seeded databases, `CHECKOUT_MAX_OPEN_ORDERS` is meant to stay configurable, and the lock plus the race tests are sufficient. The BTS quote now runs before any row lock and the text says it is inside the idempotency transaction. |
| 14 | Major (both lenses) — webhook rejects store 64 KiB rows unthrottled; Payme read-only calls store raw bodies | Adopted: bounded rejected rows (1 KiB, no raw body), ≤10 per IP per minute, sampled log beyond; authenticated deliveries never capped; Payme read-only methods without `raw_body`. |
| 15 | Major — BTS re-track trusts the payload's tracking number; `?token=` leaks into logs | Adopted: always re-track our stored tracking number, mismatch ignored + WARNING; query token stripped from `path`, proxy log redaction, header preferred (verify M3). |
| 16 | Major — dev simulate signs with live keys on a staging with live modes; fake Eskiz code on staging | Adopted: mounted only with fake payment modes; settings validation refuses the combinations; `ESKIZ_FAKE_CODE` only in local/test settings, and staging runs production settings. |
| 17 | Major (+ consistency minor) — FCM prunes on any `INVALID_ARGUMENT`; firebase-admin not pinned | Adopted: prune only on `UNREGISTERED`, `SENDER_ID_MISMATCH` and token-naming `INVALID_ARGUMENT`; other `INVALID_ARGUMENT` = `payload_invalid` (alert, device untouched); `PUT me/device/` on every cold start; live adapter on httpx + PyJWT, no new dependency. |
| 18 | Major — terms/slot conflicts share code `conflict` (both lenses) | Adopted: 409 `terms_update_required` (`details.current_version`) and 409 `slot_taken`. |
| 19 | Major — `public` Cache-Control on personalised catalog responses (both lenses) | Adopted: `private, no-cache` with a bearer, `public` only anonymous. |
| 20 | Major — per-IP throttles break behind CGNAT | Adopted: device-keyed scopes plus high IP backstops (new scopes so staff `login`/`oauth` rates stay unchanged); rates marked provisional (§9.4). |
| 21 | Major — cart add and address create double on retry | Adopted: Idempotency-Key required on `POST cart/items/` and `POST me/addresses/` (and `POST me/social/`); PATCH documented as absolute for steppers. |
| 22 | Major — the cart is emptied at checkout and never refilled | Adopted as `POST orders/{id}/reorder/` (new `M2-C16`); automatic refill on cancel/expiry was not chosen because the customer may already have a new cart. |
| 23 | Major — inbox/push titles use the admin Uzbek labels | Adopted: P3 renders the customer label in the customer's language; old rows keep their language. |
| 24 | Major — Apple guideline 4.8 | Adopted: `apple` provider in the contract and P1 (fake now, live with A7 credentials), recorded as `M2-A14` for the client to confirm. |
| 25 | Major — provider deep links and return behaviour undefined | Adopted: `app_url` always null in v1, system browser (never WebView), https return link with custom-scheme fallback, polling on resume, `payment.in_progress`; formats recorded as `M2-P9`. |
| 26 | Major — farewell push can sign out another account on the same install | Adopted: `sid` in the payload and the app's match rule; skipped when another active row holds the token; deactivated rows lose their token. |
| 27 | Major — P0's session-replaced push needs the FCM port change that P3 owned | Adopted: the port extension moved into P0's owned files. |
| 28 | Major — B5 `create_order` changes incomplete (visibility, min payable, per-line `OutOfStock`, typed errors, order) | Adopted: the seven changes are listed in the P2 table and C§2.7.1 rows follow the evaluation order. |
| 29 | Minor — Click refunds/status by `click_paydoc_id` | Adopted (verify M3); prepare without it → -8. |
| 30 | Minor — Click JSON bodies break the raw-string signature | Adopted: form only; JSON behind `CLICK_ACCEPT_JSON` with string parsing (verify M3). |
| 31 | Minor — read/read-all bypass the delivered counter | Adopted: both go through `acknowledge`. |
| 32 | Minor — social nonce optional, no replay cache, ticket not bound to the device or the verification token | Adopted: server-issued nonce (`auth/social/nonce/`) required for ID tokens, replay cache until `exp`, ticket carries `pii_ref(fid)`, verification token bound to the ticket through `gd:social:otp:{sms_code_id}`. **No `SmsCode.context` migration:** the binding lives in Redis; a cache flush makes completion fail safe (the user restarts), and durable single use comes from `verification_used_at` and the unique identity row. |
| 33 | Minor — `DELETE me/` password not guarded; social-only deletes with a bare access token | Adopted: `LoginGuard` scope `customer_delete`; social-only needs a `reset_password` verification token (reusing that purpose avoids a new `SmsCode.Purpose` choice). |
| 34 | Minor — SHA-256 phone hashes are brute-forceable | Adopted: `pii_ref` = keyed HMAC (`LOG_PII_KEY`). |
| 35 | Minor — QR minting unbounded, tokens survive logout, key prefix mismatch | Adopted: `qr` throttle, ≤2 live tokens, revoked on logout/replace/delete, both operations on the raw client with a literal prefix (`core.qr_store`). |
| 36 | Minor (both lenses) — POS: throttle after auth, no revoke/rotate, `customer_id` exposed, command has no actor | Adopted: failed-auth counter inside the authentication class, `revokeposkey` / `rotateposkey`, `--actor` required, opaque `customer_ref` instead of `customer_id`. |
| 37 | Minor — consent to an empty terms version | Adopted: 503 `terms_not_published` at the endpoint (a DB value cannot be validated by settings); `terms_version` versions both documents. |
| 38 | Minor — booking limit and own overlap race | Adopted: Customer row lock in `request_by_customer`; race test. |
| 39 | Minor — `create_order` ignores category visibility | Adopted (row 28). |
| 40 | Minor — hold `expires_at` in the past during the grace; `expire_holds` danger | Adopted: effective expiry shown; `expire_holds` must stay unscheduled (§8). |
| 41 | Minor — cart cashback cap understates the delivery case | Adopted: one formula; the cart shows `max_usable_pickup`; `delivery/calculate/` returns `cashback_max` and `expected_total`. |
| 42 | Minor — `previous_refresh_hash` index; "one migration" claim | Adopted (`accounts.0006`); the goal now says three migrations. |
| 43 | Minor — `PosAPIView` had no owner | Adopted: `loyalty/api/pos/base.py`, P3. |
| 44 | Minor — timeline subtitle keys and `TimelineType` literals | Adopted: `label.timeline.sub_amount`, `sub_amount_provider`, `sub_provider`; explicit literal list; new `ProgressStep` enum. |
| 45 | Minor — order item names are an Uzbek snapshot | Adopted: live product name in the resolved language, snapshot as fallback. |
| 46 | Minor — AI endpoints not scoped out | Adopted: "Out of scope (Phase 2)" header row. |
| 47 | Minor — the app cannot tell a QR was consumed | Adopted: refresh on focus + "Yangilash" button + documented re-scan flow; no status endpoint or push (one more route for a 50 s window was not worth it). |
| 48 | Minor — schema commit rule conflicts with AGENTS.md | Adopted as a lead prerequisite (§0.1): this task may not edit `AGENTS.md`, so the plan records the amendment and blocks P0 on it. |
| 49 | Minor — no change-password flow; no SMS autofill | Adopted: documented change-password path (C§2.1); `ANDROID_SMS_APP_HASH` line and iOS one-time-code note. |
| 50 | Minor — Payme state-1 abandonment leaves the customer stuck | Adopted: `payment.in_progress` + provider in `OrderDetail`, "To‘lov tekshirilmoqda", `payment_in_progress` message names the provider; the escape route is to finish or cancel in Payme or wait for the order to expire and reorder. |
