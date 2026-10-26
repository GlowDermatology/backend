# M2 customer API — implementation plan (slice B7)

**Contract:** `docs/api/customer-api-v1.md` v1.2.0-draft (this plan names its sections as "C§"). **Spec:**
`docs/specs/2026-10-24-backend-foundation-design.md` (Customer (M2), Financial integrity, Milestones → M2). **Shared
rules:** `docs/api/admin-api-v1.md` §1/§3/§4, `AGENTS.md`. Where this plan and the contract disagree, the contract
wins and this file is fixed. The review findings of revisions 1 and 2 and what was decided about each are in §11.

**Goal.** The iOS/Android app can sign up, sign in on one device, browse, fill a server cart, check out with stock
reservation and a cashback hold, pay with Click or Payme (callbacks verified, idempotent from state, refused captures
refunded), track BTS delivery, see cashback and show the personal QR, book specialists, and read its inbox — on the
built M1 models with **four** small migrations (`accounts.0006`, `notifications.0003`, `commerce.0004`,
`payments.0002`), reusing the
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
| **P1 accounts** | Customer auth (OTP, register, login, reset, refresh, logout, social incl. Apple and the nonce), `me/*` incl. delete + addresses + device + social identities + password change, public catalog, `settings/public/`, Eskiz port | `/home/user/wt/m2-p1`, `feat/m2-p1-accounts` | `test_gd_m2_p1` |
| **P2 commerce** | Cart, delivery regions/calculate, checkout, pay, customer cancel, reorder, Click/Payme callbacks, refused-capture reversals (accept and reverse), expiry grace, customer orders + timeline, BTS webhook + polling, dev simulate | `/home/user/wt/m2-p2`, `feat/m2-p2-commerce` | `test_gd_m2_p2` |
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
  order Customer → Cart → ProductStock → Order → Payment → LoyaltyAccount (C§2.7.1), where the customer-level lock is
  always `select_for_update(no_key=True)` (`FOR NO KEY UPDATE`; a plain `FOR UPDATE` deadlocks with a capture's
  `Purchase` insert, C§2.7.1); time via `timeutils.now()`.
- Logs and throttle keys never carry a raw phone or fid: `core.pii.pii_ref(value)` (C§6.4).
- Commits: small, imperative (`feat(customers): add the send-code endpoint`), never pushed.

### 0.2 Spec edits before P0 starts (lead)
The contract says the spec wins on conflict, so the spec must say what the contract now does before any package
builds it (the contract cannot be edited into compliance after the fact):
- Payme: `-31051` (account-error range) for "another active transaction / a Click transaction in flight"; `-31008`
  stays for paid, cancelled and non-payable orders (spec line "−31008 for a second active transaction").
- Customer refresh: "reuse after a short grace window revokes the session" made precise — identical replay for
  `CUSTOMER_REFRESH_GRACE_SECONDS`, replay or re-rotation while the rotation is unconfirmed and younger than
  `CUSTOMER_REFRESH_UNCONFIRMED_SECONDS`, reuse (revoke) after that or once an access token of the new rotation was used.
- Libraries: firebase-admin is **not** added; the FCM live adapter uses httpx + PyJWT (C§2.14.2).
- Customer social login: Google, Facebook **and Apple** (product-owner decision 2026-10-26), with Apple token
  revocation on unlink and deletion.
- Fake provider adapters are allowed under the production settings (product-owner decision 2026-10-26) with the
  guarantees of C§6.14 (no transaction started and no capture honoured through a fake — a Click Complete of a
  transaction attached while Click was live is accepted and reversed —, no payment offered, no fixed code, no simulate,
  no home delivery with fake BTS, no Billz sale recorded by a fake).
- Click: a Complete that cannot be honoured is accepted and reversed (Click SHOP API rule), never answered with an
  error.

---

## 1. P0 — seams (sequential, before P1–P3)

### Owns
| File | Change |
|---|---|
| `glowderma/core/enums.py` | the new enums of C§3 (`OtpPurpose`, `NextStep`, `CustomerSocialProvider` (`google`, `facebook`, `apple`; component `CustomerSocialProviderEnum` — the staff override `"SocialProviderEnum": ["google", "facebook"]` in `config/settings/base.py` stays as it is), `SocialTokenType`, `StockStatus`, `CustomerProductOrdering`, `CartLineStatus`, `OrderScope`, `CustomerRefundState`, `CashbackEarnedState`, `TimelineType`, `ProgressStep`, `ProgressState`, `LedgerSource`, `AppointmentScope`, `AppointmentCancelledBy`, `InboxKind`, `InboxLinkKind`, `PushDataType`, `AccountSecurityEvent` incl. `password_changed`); **no `DevicePlatform`**: the platform reuses `OrderChannel` (a second enum with the same literals makes drf-spectacular 0.30 report `ENUM_NAME_OVERRIDES` duplication, which `--fail-on-warn` turns into a failure); `SessionRevokeReason` += `refresh_reused` (admin §3, already updated) |
| `glowderma/core/i18n.py` (new) | `CATALOG` with every key of C§5 (incl. `push.*`), `t(key, lang, **params)`, `resolve_language(request, customer=None)` (Accept-Language q-parsing, fallback customer → `uz`), `format_number(n, lang)`, `format_percent(p, lang)` |
| `glowderma/core/secretbox.py` (new) | `seal(plaintext: bytes, *, aad: bytes) -> str` / `open_(token: str, *, aad: bytes) -> bytes`: AES-256-GCM (`cryptography`, already pinned through PyJWT[crypto]) under `SECRETBOX_KEY` (base64url decoding to exactly 32 bytes, §7), random 96-bit nonce; used for the OTP payload (C§2.1.1) and the Apple refresh token (C§2.1.8) |
| `glowderma/core/providers.py` (new) | `provider_modes() -> dict[str, str]` (`billz`, `click`, `payme`, `bts`, `eskiz`, `fcm`, `oauth` → `fake`/`live` from the `*_MODE` settings); `fake_in_production(provider) -> bool` (mode `fake` ∧ ¬`DEV_FEATURES_ALLOWED`, the one predicate of C§6.14); `callbacks_refused(provider) -> bool` (= `fake_in_production` for `click`/`payme`/`bts`, C§2.13.0); `payable_providers() -> list[str]` (C§2.4: Click, Payme, minus those whose callbacks are refused); `delivery_available() -> bool` (¬`fake_in_production("bts")`, C§2.6) and `fulfilment_methods() -> list[str]` (`delivery` only when available, then `pickup`); `warn_fake_providers()` (one WARNING `providers.fake_in_production {provider}` per fake provider when ¬`DEV_FEATURES_ALLOWED`) |
| `glowderma/core/apps.py` | `CoreConfig.ready()` calls `providers.warn_fake_providers()` (every process start: gunicorn, worker, beat) |
| `glowderma/core/api/health.py` | `?ready=1` adds `providers: provider_modes()` (admin §2.15); fake modes never fail the check |
| `glowderma/site_config/api/admin/serializers.py` | admin `GET settings/` `integrations.providers` = `provider_modes()` next to the existing `mode` (admin §2.11; the admin contract is already updated) |
| `glowderma/core/pii.py` (new) | `pii_ref(value: str) -> str` = first 16 hex of HMAC-SHA256(`LOG_PII_KEY`, value) |
| `glowderma/core/qr_store.py` (new) | raw-Redis store for QR keys (C§2.9.3): `put(key_hash, customer_id, ttl, max_live)`, `take(key_hash) -> str \| None` (`GETDEL`), `revoke_all(customer_id)`; literal prefix `gd:loyalty:qr:`; LocMem fallback with a process lock for tests. Lives in core so `accounts` (revocation) and `loyalty` (issue/resolve) can both use it |
| `glowderma/core/api/exceptions.py` | customer codes (C§1.6) as `CustomerError(code, status, *, key=None, details=None, params=None)` subclasses — `key` is the catalog key, default `error.<code>`, explicit where C§5.0 says so (`not_cancellable` per resource, `OUT_OF_STOCK` one/lines) — (incl. `terms_update_required`, `slot_taken`, `last_sign_in_method`, `sms_unavailable`, `terms_not_published`, `otp_network_limit`, 401 `password_changed`); the customer handler maps core `NotCancellable` / `InvalidTransition` from reused B5/B6 services to `error.not_cancellable` / `error.invalid_transition`; `field_error(code, message_key=…)` for endpoint-specific field keys; `ENDPOINT_FIELD_CODES` += `otp_invalid`, `password_too_short`, `password_too_common`, `password_entirely_numeric`, `password_too_similar`, `password_unchanged`, `password_incorrect`, `terms_outdated`, `push_token_missing`, `latlng_pair`, `address_incomplete`; `AUTH_FAILURE_CODES` unchanged; the handler adds `details` (null by default) and renders `message`/field messages through `core.i18n` **only** when the resolved view is a customer/public/POS view (`view.audience` attribute); admin bodies stay byte-identical (regression test) |
| `glowderma/core/api/schema.py` | `CUSTOMER_ERRORS`, `PUBLIC_CUSTOMER_ERRORS`, `DEVICE_HEADERS` (`X-GD-Device-Id`, `X-GD-Platform`, `X-GD-App-Version`), `DEVICE_KEY_HEADER`, `ACCEPT_LANGUAGE_HEADER` |
| `glowderma/core/throttling.py` | new scopes `sms_device: 10/min`, `customer_auth_device: 30/min`, `customer_auth_ip: 1200/min`, `refresh_unknown: 60/min`, `qr: 6/min`, `public: 300/min`, `public_ip: 3000/min`; `sms_ip` default `600/min` (used only by `auth/send-code/`) |
| `glowderma/core/audit.py` | `record()` accepts a `Customer` or `PosDevice` actor: `actor_type` `customer`/`integration`, `actor_label` = masked phone / device name (no `get_username` on non-staff) |
| `glowderma/accounts/models.py` + `migrations/0006_customer_auth_hardening.py` | `Device.key_hash = CharField(64, blank=True)`; partial index `accounts_authsession_prev_refresh` on `AuthSession.previous_refresh_hash` `WHERE previous_refresh_hash <> ''`; `AuthSession.revoke_reason` choice `refresh_reused`; `AuthSession.rotation = PositiveIntegerField(default=0)` and `AuthSession.rotation_confirmed = BooleanField(default=False)` (C§2.1.6: the `rot` claim is this counter, never a timestamp); `CustomerSocialIdentity.provider` choice `apple`; `CustomerSocialIdentity.provider_refresh_token = TextField(blank=True)` (Apple's refresh token sealed with `core.secretbox`, C§2.1.8); `Customer.history_from = DateTimeField(null=True)` (the history horizon, C§2.2.3; a data step back-fills it from `reactivated_at`); `Customer.is_authenticated = True`, `is_anonymous = False` (properties, no SQL) |
| `glowderma/accounts/tokens.py` | access TTL by audience (`STAFF_ACCESS_TTL` / `CUSTOMER_ACCESS_TTL`); customer access claim `rot` = `AuthSession.rotation` (integer) |
| `glowderma/accounts/authentication.py` | `CustomerJWTAuthentication` (aud `customer`; `auth:sid:{sid}` cache value `active:{abs_expiry}:{pii_ref(fid)}:{platform}`, filled with `cache.add` only and never rewritten, so the device and platform checks need no query and no write can overwrite a tombstone; tombstones `revoked:<reason>`; device/platform header match; `last_active_at`/`app_version` bump ≤ every 5 min; when the add-only flag `auth:sidrot:{sid}:{rot}` is absent → the conditional confirmation UPDATE of C§2.1.6 (`rotation = rot AND NOT rotation_confirmed`), then `cache.add` of the flag); `OptionalCustomerAuthentication` |
| `glowderma/accounts/permissions.py` | `IsActiveCustomer` |
| `glowderma/accounts/api/customer_base.py` (new) | `CustomerAPIView`, `CustomerGenericViewSet`, `PublicAPIView`, `WebhookAPIView` (CSRF-exempt, parsers set per view, no auth, provider-native responses), device-header parsing + 400 `device_required` + 426 `app_update_required` gate (a mixin used by the auth views too). **Not** `PosAPIView` (P3, `loyalty/api/pos/base.py`) |
| `glowderma/accounts/api/throttles.py` | `CustomerRateThrottle` (`customer`), `PublicDeviceThrottle` (`public`), `PublicIPThrottle` (`public_ip`), `CheckoutThrottle`, `PayThrottle`, `QrThrottle`, `SmsPhoneThrottle`, `SmsDeviceThrottle`, `SmsIPThrottle`, `CustomerAuthDeviceThrottle`, `CustomerAuthIPThrottle`, `RefreshUnknownThrottle` (counts only misses), `PosRateThrottle` |
| `glowderma/accounts/services.py` | `start_customer_session(customer_id, *, fid, platform, app_version, issue_device_key) -> SessionStart(session, raw_refresh, raw_device_key \| None, farewell_tokens: list[str])` (C§1.9 steps 1–6 under `select_for_update(no_key=True)` of the customer: deactivated devices lose their token — captured first into `farewell_tokens` —, farewell payload carries `sid` + token, skipped when the token is held by another active row, `core.qr_store.revoke_all`), `revoke_customer_sessions(customer_id, reason, except_sid=None)` (also `qr_store.revoke_all`), `move_history_horizon(customer_id, *, at)` (`Customer.history_from`), **`delete_customer` (the built B2 function, changed here; no other package owns it):** customer-level lock `no_key=True`, `key_hash=""` on every device, sessions revoked through `revoke_customer_sessions(…, "deactivated")` (customer tombstone TTL), `core.qr_store.revoke_all` on commit, `language="uz"`, `terms_version=""`, `consent_at`/`consent_ip`/`last_login_at` cleared, one `apple.revoke` outbox message per Apple identity holding a token (C§2.2.3 step 5), `register_device(customer_id, fid, …)` (a token taken from another row clears it there), `device_is_known(customer_id, fid, raw_device_key) -> bool` (`compare_digest`), `invalidate_fcm_token(device_id)`, tombstone TTL = max(staff, customer access TTL) + 60 |
| `glowderma/notifications/models.py` + `migrations/0003_inbox_account_source.py` | `InboxNotification.SourceType.ACCOUNT = "account"` (`source_id` = customer id) and the CHECK `inbox_kind_source_fields` extended with `Q(kind="general", source_type="account", template_status="")`, so the `account_security` notices (C§2.12) can be stored without a fake campaign id. P0 owns it because the topic constant is P0's and P3 owns no model change |
| `glowderma/accounts/topics.py` (new) | `SMS_SEND = "sms.send"`, `PUSH_SESSION_REPLACED = "push.session_replaced"`, `PUSH_ACCOUNT_SECURITY = "push.account_security"`, `APPLE_REVOKE = "apple.revoke"` + dedupe-key builders (`sms.send:{sms_code_id}`, `push.session_replaced:{session_id}`, `push.account_security:{customer_id}:{event}:{uuid}`, `apple.revoke:{identity_id}`) |
| `glowderma/accounts/tasks.py` (new) | handler `push.session_replaced` (data-only FCM message through the extended port; blanks the token in the payload after the send; `unregistered` → `invalidate_fcm_token`); beat task `accounts.cleanup_auth` (delete `SmsCode` older than 24 h, `done`/`dead` `sms.send` outbox rows older than 24 h, customer `AuthSession` rows revoked or expired for >90 days) |
| `glowderma/integrations/fcm/{ports,fake}.py` | `PushMessage(token, title, body, data, *, data_only, android_channel, android_priority, apns_push_type, apns_priority, image_url, collapse_key)`; `PushResult.error` ∈ {`unregistered`, `payload_invalid`, …}; the fake records every field **only when `DEV_FEATURES_ALLOWED`** (otherwise it keeps nothing and logs one INFO line with ids, C§2.14.2) and maps tokens `…-bad` → `unregistered`, `…-payload` → `payload_invalid`, `…-down` → `FcmUnavailable` (moved here from P3 so P0's session-replaced push can be built) |
| `glowderma/catalog/selectors.py` | `customer_visible_products()` (C§2.3 visibility) |
| `glowderma/commerce/services.py` | stub `cancel_by_customer(order_id: int, customer_id: int, *, at=None) -> int` raising `NotImplementedError("P2")` |
| `glowderma/commerce/selectors.py` | stub `customer_inflight_order(customer_id: int) -> int \| None` returning `None` (P2 implements C§2.2.3 step 1) |
| `glowderma/appointments/services.py` | stub `cancel_by_customer(appointment_id: int, customer_id: int, *, reason: str = "", at=None) -> Appointment` raising `NotImplementedError("P3")` |
| URL stubs | `glowderma/{customers,site_config,delivery,commerce,loyalty,appointments,notifications}/api/customer/{__init__,urls}.py`, `glowderma/commerce/api/webhooks/{__init__,urls}.py`, `glowderma/commerce/api/dev/{__init__,urls}.py`, `glowderma/loyalty/api/pos/{__init__,urls}.py` — each `urlpatterns = []` |
| `config/urls.py` | `CUSTOMER_API_MODULES = ("customers", "site_config", "delivery", "commerce", "loyalty", "appointments", "notifications")` mounted at `api/v1/`; `api/v1/webhooks/` → commerce webhooks (always mounted; fake-mode refusal in the views, C§2.13.0); `api/v1/pos/` → loyalty pos; `api/v1/dev/` only when `DEV_FEATURES_ALLOWED`, `FAKE_PAYMENTS_ENABLED` and `CLICK_MODE == PAYME_MODE == "fake"` |
| `config/settings/base.py`, `local.py`, `test.py`, `production.py` | every setting of §7 with validation (§7 rules); `DEV_FEATURES_ALLOWED` (a constant, not env: `False` in `base.py`, `True` in `local.py` and therefore `test.py`, forced `False` in `production.py`); `ESKIZ_FAKE_CODE` and `POS_API_KEYS` read only in `local.py`/`test.py` and refused by `production.py`; valid dev defaults for the new keys in `local.py` (added to the production placeholder list) |
| `config/celery.py` | beat entries of §8 (`commerce.poll_bts` points at a P0 stub task `return []`, implemented by P2) |
| `.env.example` | the lines of §7 |
| `conftest.py`, `glowderma/accounts/tests/factories.py` | fixtures `customer`, `customer_session`, `customer_token`, `customer_client` (bearer + device headers `fid-test-0001`, `ios`, `1.0.0` + a known device key), `public_client`, `pos_device` + `pos_key`, `pos_client`; helpers `device_headers(fid=…, platform=…, version=…, key=…)`, `raw_device_key(device)` |

### Tests (P0)
- Audience separation both ways; customer token on admin routes → 401; staff token on a dummy customer route → 401.
- Revocation codes through the cache (`replaced` → `SESSION_REPLACED`; `password_changed` → `password_changed`;
  `refresh_reused`, `logout`, … → `SESSION_EXPIRED`) without a DB hit; deleted customer → `SESSION_EXPIRED`.
- `delete_customer` (changed by P0): device key hashes cleared, QR tokens revoked on commit, sessions revoked with the
  customer tombstone TTL, language/consent/last login cleared, `apple.revoke` enqueued per Apple identity with a token;
  idempotent; it takes `FOR NO KEY UPDATE`, not `FOR UPDATE` (a concurrent `Purchase` insert for the customer does not
  wait). `start_customer_session` returns the deactivated devices' tokens in `farewell_tokens`; `move_history_horizon`.
- Device headers: missing → 400 `device_required`; fid mismatch → 401 + one audit row per hour; platform mismatch;
  `X-GD-App-Version` below the floor → 426 with `details`.
- `start_customer_session`: second device revokes the first (`replaced`), deactivates its device **and clears its
  token**, enqueues `push.session_replaced` with `sid`; no farewell push when the token now belongs to another active
  device; device key issued only with `issue_device_key`, stored as SHA-256; `device_is_known` false for fid-only and
  for a wrong key; **race:** two concurrent sign-ins of one customer on two devices (threads, `transaction=True`) end
  with exactly one open session and one active device.
- Access-token confirmation: the first request with the current `rot` sets `rotation_confirmed` once (one UPDATE);
  later requests and access tokens of an older rotation do not write; it works under a pinned `FAKE_NOW` (no timestamp
  comparison); **race:** a confirmation and a revocation (logout) run concurrently → the `revoked:` tombstone survives
  (the active entry is add-only) and the next request answers 401.
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
- Settings validation (boot tests import `config.settings.production` in a subprocess with a prepared env):
  `FAKE_PAYMENTS_ENABLED` with a live payment mode and `FAKE_SCENARIO_HEADER_ENABLED` with any live provider mode →
  `ImproperlyConfigured` in every module; production with `FAKE_PAYMENTS_ENABLED`, `FAKE_SCENARIO_HEADER_ENABLED`,
  `ESKIZ_FAKE_CODE` or `POS_API_KEYS` set → `ImproperlyConfigured`; production with **every** provider fake
  (`INTEGRATIONS_MODE=fake`) **boots**, logs one `providers.fake_in_production` WARNING per provider (caplog on
  `CoreConfig.ready()`) and reports the modes in `health/?ready=1` and admin `GET settings/`; production refuses each
  new key when missing, containing `change-me`, equal to a committed value (the `.env.example` lines and the `local.py`
  defaults are in `KNOWN_PLACEHOLDER_SECRETS`) or too short, and refuses a `SECRETBOX_KEY` that does not decode to
  exactly 32 bytes; `CLICK_SECRET_KEY`, `PAYME_KEY` and `BTS_WEBHOOK_SECRET` are refused as placeholders in **fake**
  mode too; `CLICK_INFLIGHT_SECONDS` > `PAYMENT_IN_FLIGHT_GRACE_SECONDS` → `ImproperlyConfigured`.
- `core.providers`: `callbacks_refused` / `payable_providers` matrix over (mode, `DEV_FEATURES_ALLOWED`); FCM fake
  under `override_settings(DEV_FEATURES_ALLOWED=False)` keeps no message (bounded memory, no text).
- Migrations `accounts.0006` and `notifications.0003` round trip (AGENTS.md rule: end with a plain `migrate`); an
  `account`-source `general` inbox row inserts, a `general` row with `source_type = "order"` is refused by the CHECK.
- Schema: `spectacular --validate --fail-on-warn` passes with every new enum registered in `CONTRACT_ENUMS` (no
  duplicate choice set, `CustomerSocialProviderEnum` next to the staff `SocialProviderEnum`, no `apple` on staff
  routes).
- Catalog: every code a customer view can raise maps to an existing key (C§5.0), including `not_cancellable` from a
  reused service and `invalid_transition`.
- The automatic URL-walk auth matrix is extended: every route under `api/v1/` that is not admin/health/schema is
  classified **C**/**O**/**S**/**K** from its base class and probed accordingly.

### Depends on
`feat/m2-base` green (the `AGENTS.md` "M2 exception" of §0.1 is already in place) and the spec edits of §0.2.

---

## 2. P1 — accounts, profile, public catalog, settings

### Owns
`glowderma/customers/{services.py, selectors.py, api/customer/*}` (new views: auth, me, social, catalog, delete),
`glowderma/accounts/services.py` (OTP and credential flows only — below the P0 block), `glowderma/accounts/tasks.py`
(the `sms.send` and `apple.revoke` handlers and the `apple.revoke` dead-letter hook, appended), `glowderma/integrations/eskiz/{__init__,ports,fake,registry}.py` (new),
`glowderma/integrations/oauth/{ports,fake,google,facebook,apple (new),registry}.py` (`verify_app_token`, Apple; the
registry keeps `PROVIDER_NAMES = STAFF_PROVIDERS = ("google", "facebook")` for the staff routes, which stay untouched,
and adds `CUSTOMER_PROVIDERS = ("google", "facebook", "apple")` — `apple` never reaches staff `auth/oauth/{provider}`),
`glowderma/site_config/{selectors.py, services.py (`update_settings` invalidates the `public_settings` cache on commit),
api/customer/*}`, `glowderma/catalog/selectors.py` (customer list helpers, after
P0's function), tests in `customers/tests/`, `accounts/tests/`, `integrations/tests/`, `site_config/tests/`.

### Endpoints
C§2.0 rows 1–23: `auth/send-code/`, `auth/verify-code/`, `auth/set-password/`, `auth/login/`, `auth/reset-password/`,
`auth/refresh/`, `auth/logout/`, `auth/social/nonce/`, `auth/social/`, `auth/social/complete/`, `me/`
(GET/PATCH/DELETE), `me/consent/`, `me/image/`, `me/device/`, `me/password/`, `me/social/` + `me/social/{provider}/`,
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
| `rotate_customer_refresh(raw, *, fid) -> RefreshResult` | `accounts.services` | C§2.1.6 steps 1–6: row lock on `refresh_hash OR previous_refresh_hash`, rotate / replay / re-rotate (bounded by `CUSTOMER_REFRESH_UNCONFIRMED_SECONDS`, dead hash remembered in `auth:refresh_dead:{hash}`, grace cache rewritten) / reuse → revoke `refresh_reused` + audit; grace cache written before commit and deleted on rollback |
| `logout_customer(raw)` | `accounts.services` | revoke + device inactive, token cleared, QR tokens revoked |
| `issue_social_nonce(fid)`, `consume_social_nonce(nonce, fid)`, `remember_id_token(provider, claims)` | `accounts.services` | Redis, single use, replay cache until `exp` |
| `register(...)` (reactivation → `move_history_horizon`), `login(...)` (device key evaluated first, guard scope by device knowledge, dummy hash whenever no stored hash is compared), `reset_password(...)` (unlinks identities, clears other device keys, dormant rule, `push.account_security` with `farewell_tokens`), `social_sign_in(...)`, `complete_social(...)` (fid + ticket binding, password for password accounts, dormant rule for SMS-only links, `farewell_tokens`), `link_social(...)` / `unlink_social(...)` (re-authentication: password, or a `reset_password` verification token when the account has none), `change_password(...)` (`me/password/`: current password or verification token; keeps session, device key and identities), `accept_consent(...)`, `update_profile(...)`, `set_image(...)`, `delete_account(customer_id, *, password=None, verification_token=None)`; `customers.selectors.last_activity_at(customer_id)` (newest of last login, session use, purchase, order) | `customers.services` | orchestrators (need `site_config` for the terms version and publication check and, for delete, `commerce`/`appointments`); `delete_account` takes the customer-level lock first (`select_for_update(no_key=True)`), refuses on `commerce.selectors.customer_inflight_order` |
| `public_settings()` | `site_config.selectors` | cached 60 s, invalidated on `update_settings` commit; `terms_version` null while unpublished; `payment_providers = core.providers.payable_providers()`, `fulfilment_methods = core.providers.fulfilment_methods()` (C§2.4) |
| `product_cards(qs, lang, customer_id=None)` | `customers.selectors` | one query for products + `available_stock` + main image + cart quantities |
| `verify_app_token(token, token_type, nonce)`, `exchange_apple_code(code, client_id) -> refresh_token`, `revoke_apple_token(refresh_token, client_id)` | `integrations.oauth` (Google, Facebook, Apple) | C§2.1.8: only the four (provider, token type) pairs; per-provider required claims (`email` optional, `email_verified` ignored — the staff Google adapter keeps its own list); Apple: RS256 against Apple's JWKS, `aud` ∈ `APPLE_BUNDLE_IDS`, `nonce == sha256(raw).hexdigest()`, ES256 client secret from `APPLE_TEAM_ID`/`APPLE_KEY_ID`/`APPLE_PRIVATE_KEY_FILE` for the code exchange and the revoke; Facebook `debug_token` with `input_token` in a POST form body (the staff `_debug_token` changed the same way); replay key `sha256(provider + ":" + raw token)`; ticket `jti` server-generated; customer social routes 404 while OAuth is fake under the production settings |

**Reused (do not redefine):** `accounts.services.{add_address, update_address, set_default_address, remove_address,
set_customer_password, check_customer_password, delete_customer (changed by P0), reactivate_customer,
start_customer_session (P0), register_device (P0), revoke_customer_sessions (P0), device_is_known (P0),
move_history_horizon (P0)}`, `accounts.api.guard.LoginGuard` (scope `customer_login` per phone for every password
check of the customer, `customer_login_device` per device row for a login with a valid device key), `catalog.selectors.{filter_products_search, customer_visible_products,
category_queryset, banner_queryset}`, `commerce.selectors.available_stock` (B5), `site_config.models.SiteSettings.load`,
`core.images.process_image`, `core.secretbox`, `core.pii`, `commerce.services.cancel_by_customer` (P2) and
`appointments.services.cancel_by_customer` (P3) from `delete_account` (called only when the customer has open items;
P1 tests cover the no-open-items path with the P0 stubs, the lead's integration test the rest).

### Models / migrations
None (P0 owns `accounts.0006`). The link-social ticket binding and the OTP fid/IP day counters live in Redis (§11).

### Fakes
- **FakeEskiz** (C§2.14.1): phone ending `0000` → `EskizRejected`, `9999` → `EskizUnavailable`, else accepted
  `fake-<sms_code_id>`; `FakeEskiz.sent` list (rendered texts, so tests can read the code without a DB lookup) **only
  when `DEV_FEATURES_ALLOWED`** — under the production settings the fake keeps nothing and logs `sms.fake_accepted
  {sms_code_id}`; the code generator returns `ESKIZ_FAKE_CODE` only under local/test settings.
- **OAuth `verify_app_token` fakes**: `fake:<subject>:<email>` valid (nonce must equal the issued nonce, or `*` only
  when `DEV_FEATURES_ALLOWED`), `fake-unverified:…` valid without verified email, `fake-fail` → `exchange_failed`,
  `fake-down` → `unavailable`, `fake-expired` → `invalid_token`; Apple `fake-apple:<subject>:<email>` (relay addresses
  allowed; available with `OAUTH_MODE=fake` even while `APPLE_BUNDLE_IDS` is empty); Apple authorization code
  `fake-code:<anything>` → refresh token `fake-apple-refresh-<subject>`, `fake-code-fail` → refused, `fake-code-down` →
  unavailable; the fake records every revoke. Under the production settings the routes answer 404 before any fake is
  called (C§2.1.8).

### Tests (P1)
- Every validation row of C§2.1–§2.4 and C§2.2.8 (one test per code, uz message; one ru and one en rendering per
  endpoint group).
- OTP: resend window (61 s ok, 59 s 429 with `Retry-After`), daily cap 10 (Tashkent day boundary), fid cap (`otp_daily_limit`), IP cap (`otp_network_limit`, its own message),
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
  token → tokens, new device key, the old session `SESSION_REPLACED`; timing parity: unknown phone, deleted customer,
  social-only (empty password) and wrong password each run exactly one hash (the dummy hash counted with a spy
  hasher); guard lockout; the unknown-device guard locked by 5 wrong passwords does not lock a login with a valid device
  key (`customer_login_device`), and vice versa; **oracle test:** with the phone's code cap exhausted, a right and a
  wrong password get the same 429 body.
- Refresh: rotation (counter +1, `rotated_at` = `last_used_at` from one instant); concurrent refresh threads →
  identical tokens and one chain; lost response → retry after 45 s with the old token → the grace body (identical
  tokens); after 5 min → 200 by re-rotation, nothing revoked, the grace cache rewritten so two concurrent retries get
  the same tokens; after 11 min still unconfirmed → reuse: 401 `SESSION_EXPIRED`, audit `customer.refresh_reused`; the
  token replaced by a re-rotation presented later → reuse (dead-hash entry); old token after the first access token of
  the new `rot` → 401 `SESSION_EXPIRED` + audit, session dead for the new token too; a token two rotations old → 401
  `not_authenticated`; sliding expiry; device mismatch; junk tokens hit `refresh_unknown`.
- Social: nonce issue/consume single use and fid binding; replayed ID token refused; linked → tokens; unknown →
  ticket bound to the fid (another fid → `social_ticket_invalid`); send-code with ticket; complete for active
  (password required when set, wrong → `password_incorrect`), social-only, no and deleted customer; a verification token
  from a code requested without/with another ticket → `verification_invalid`; `social_provider_linked`,
  `social_identity_linked` (unique-race); `push.account_security` enqueued on link. Pairs: google+`access_token` and
  apple+`access_token` → 400 `social_token_invalid`. Replay: the same raw token twice → refused; two different tokens
  with equal `sub` and `iat` → both accepted. Apple: happy path with nonce hashing; `authorization_code` missing → 400
  `required`; the code exchanged once and the sealed refresh token stored on the identity (at completion for an unknown
  identity, from `gd:social:apple:{jti}`; a missing cache entry → `social_ticket_invalid`); exchange refused → 400,
  Apple down → 503, nothing linked; `APPLE_BUNDLE_IDS` empty → 404 with `OAUTH_MODE=live`, fake adapter with
  `OAUTH_MODE=fake`; unlink, reset and `DELETE me/` enqueue `apple.revoke`; the handler revokes, retries on transport
  errors, treats Apple 400 as final, and the dead hook logs ERROR `apple.revoke_dead`. Fake OAuth under
  `override_settings(DEV_FEATURES_ALLOWED=False)` → 404 on the four routes and the `*` nonce refused. Facebook: the live
  adapter (recorded fixtures) sends `input_token` only in the POST body, never in a URL (staff path too).
- `me/social/`: link with password (wrong → `password_incorrect`, shares the `customer_login` budget); a social-only
  account linking without `verification_token` → 400 `verification_token` `required`, with a used or foreign token →
  400 `verification_invalid`, with a fresh `reset_password` token of its own phone → 200; unlink needs the same
  re-authentication (checked before the idempotent no-op); `last_sign_in_method`.
- `me/password/`: change with the current password (wrong → `password_incorrect`, guard); a social-only account sets a
  first password with a `reset_password` verification token; `password_unchanged`; requires an Idempotency-Key and a
  retry replays the 200; the current session and device key keep working, social identities stay linked, other
  sessions get 401 `password_changed`; `push.account_security password_changed` enqueued; audit.
- Reset: sessions revoked (the old device then gets 401 `password_changed`), identities unlinked with audit rows (Apple:
  `apple.revoke` enqueued), other device keys cleared, new device key returned, `push.account_security` enqueued with
  the old device's token in `farewell_tokens`; dormant rule: last activity 181 days ago → `history_from = now`, name,
  image and addresses scrubbed, balance and purchases kept, audit `customer.history_reset`; 179 days ago, or a purchase
  last week → untouched. Social completion on a social-only dormant account applies the same rule; completion on a new
  device puts the replaced device's token in `farewell_tokens`.
- `DELETE me/`: password check sharing the `customer_login` guard (5 failures on `DELETE me/` also lock `auth/login/` on
  an unknown device); social-only needs a `reset_password` verification token;
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
- Fake posture under `override_settings(DEV_FEATURES_ALLOWED=False)`: FakeEskiz keeps no text and no digits of the
  code (bounded memory); `ESKIZ_FAKE_CODE` is not used; `settings/public/` omits fake payment providers
  (`payment_providers: []` with both fake), and lists them under the test settings; with `BTS_MODE=fake` it publishes
  `fulfilment_methods: ["pickup"]` (both methods under the test settings).

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
`glowderma/integrations/services.py` (only the appended `set_state_data(provider, key, value)`: the BTS poll cursor needs
a writer of `IntegrationState.data`, which the built module lacks; nothing else in the file changes), tests in those
modules.

### Endpoints
C§2.0 rows 24–35 and 50–53: `cart/*`, `delivery/regions/`, `delivery/calculate/`, `orders/checkout/`,
`orders/{id}/pay/`, `orders/{id}/cancel/`, `orders/{id}/reorder/`, `me/orders/`, `me/orders/{id}/`,
`webhooks/click/prepare/`, `webhooks/click/complete/`, `webhooks/payme/`, `webhooks/bts/`,
`dev/payments/{order_id}/simulate/`.

### Services
| New / changed | Module | Notes |
|---|---|---|
| `cart_view(customer_id, lang)`, `add_to_cart`, `set_cart_quantity`, `remove_from_cart`, `clear_cart`, `reorder_into_cart(order_id, customer_id)` | `commerce.services` / `selectors` | Cart row lock; `price_at_add`; clamping to `max_quantity`; C§2.5/C§2.7.4 errors |
| `cashback_max(items_subtotal, delivery_fee, available)` | `commerce.selectors` | the one formula of C§2.7 (cart, calculate, checkout) |
| `create_order(…, delivery_fee: int, expected_total: int \| None = None, min_payable: int = 0, visible_only: bool = False)` | `commerce.services` (B5, changed) | **B5 changes, all of them:** (1) the BTS quote leaves `create_order` — the caller passes `delivery_fee` (no HTTP call under `ProductStock` locks; the seeder passes its fee); (2) visibility through `catalog.selectors.customer_visible_products()` when `visible_only` (status, price ≥ 1000, active category) under the stock locks; (3) `OutOfStock(lines=[{product_id, name, requested, available, status}])` with every problem line; (4) `CashbackAboveMax(max)` from `min_payable` evaluated on the live subtotal (replaces the bare `cashback_used ≤ items_subtotal` check); (5) `PriceChanged(totals)` when `expected_total` differs; (6) typed `AddressNotFound` / `AddressIncomplete` instead of the generic `CheckoutError`; (7) evaluation order stock → cashback max → expected total → rows → `create_hold` (`InsufficientCashback`); (8) `customer_note: str = ""` stored on `Order.customer_note` (C§2.7.1 body) |
| `checkout(customer_id, request) -> order_id` | `commerce.services` | quote first (no row lock), then `select_for_update(no_key=True)` on the Customer row, open-order rule and cart read under it, `create_order(…, customer_note=…)`, cart emptied, `Cart.last_checkout_order` |
| `is_payable(order, payment, now, *, attaching)`, `is_open(payment)`, `in_flight(payment, now)`, `customer_inflight_order(customer_id)` | `commerce.services` / `selectors` | C§2.7.2 definitions (open transaction vs in flight; `CLICK_INFLIGHT_SECONDS` = 120); the selector replaces the P0 stub |
| `ensure_payable_payment(order_id, provider, *, superseding: str \| None = None) -> PaymentRow` | `commerce.services` | same provider → reuse as is (the pay endpoint never writes for it); `superseding=<click_trans_id>` (Click Prepare only) → an active Click payment attached to another transaction is failed `abandoned` and a new row created (C§2.7.2 supersede rule); other provider in flight → `PaymentInProgress`; other provider not in flight → switch (`abandoned`); new row after a failure; updates `Order.payment_provider`; under `lock_order` |
| `cancel_by_customer(order_id, customer_id, *, at=None)` | `commerce.services` | replaces the P0 stub; state.py row 5; Click in flight → `PaymentInProgress` |
| `mark_paid(order_id, payment_id, provider, provider_txn_id, at=None)` | `commerce.services` (B5, **signature changed**) | refuses unless `payment_id` is the order's pending payment of `provider`; callers (seed, sandbox) updated |
| `fail_payment_effects(order_id, payment_id, error_code, at=None, *, provider_txn_id=None)` | `commerce.services` (B5, **signature changed**) | acts on that payment only |
| `record_late_capture(order_id, payment_id, provider, provider_txn_id, *, case, at=None)` | `commerce.services` | refused-capture rule (C§2.13.0): `payments.mark_late_captured` + `create_refund(origin=late_capture)` + `refund_requested` event + audit `payment.late_capture` + ERROR log |
| `expire_reservation` / `expire_due_reservations` | `commerce.services` (B5, changed) | skip an order whose active payment has an **open transaction** (`is_open`, never `in_flight`: Click is in flight for only 120 s after Prepare but open until its Complete) attached before `reservation_expires_at`, until `reservation_expires_at + PAYMENT_IN_FLIGHT_GRACE_SECONDS`; then expire it (C§2.7.2) |
| `customer_orders(customer_id, scope)`, `customer_order_detail(order_id, customer_id, lang)`, `customer_timeline(order, lang)`, `progress(order, lang)` | `commerce.selectors` | C§2.8, C§4; history horizon (`Customer.history_from`); live item names in the resolved language |
| `build(order_id)`, `verify(ref) -> order_id \| None` | `commerce.payment_ref` | C§1.4/C§2.7.2 account reference, `compare_digest` |
| `poll_bts()` | `commerce.tasks` | replaces the P0 stub; C§2.13.3: single flight (`cache.add` lock), round robin after `IntegrationState(bts).data["poll_cursor"]`, the cursor written after **every attempted parcel** through `integrations.services.set_state_data`; per-parcel failures (`BtsUnavailable` and `BtsRejected` from `track()`) logged and skipped; the run stops only after `BTS_POLL_MAX_CONSECUTIVE_FAILURES` transport failures in a row (`record_failure`), else `record_sync_success`; ≤ `BTS_POLL_BATCH` parcels and ≤ `BTS_POLL_MAX_SECONDS` per run, stable payload fields only (webhook re-track too; a `BtsRejected` re-track answers 200 `ignored`) |
| `set_state_data(provider, key, value)` | `integrations.services` | a single `UPDATE … SET data = data \|\| jsonb_build_object(key, value)` on the provider's `IntegrationState` row (no read-modify-write, so it cannot lose a concurrent `record_failure`) |
| webhook inbox helper (`store_delivery`, `store_rejected` with the per-IP cap and 1 KiB truncation, `semantic_fingerprint`) | `commerce/api/webhooks/inbox.py` | C§2.13.0 |
| fake-mode refusal: every webhook view first checks `core.providers.callbacks_refused(provider)` → Click Prepare `-1` / Payme `-32504` / BTS 401, bounded `rejected:` row with `error_code: "fake_mode"`, nothing else read or written; **exception:** a Click Complete is parsed and signature-checked, and when it names a payment attached while Click was live (`provider_state.prepare_at` set, `provider_txn_id = click_trans_id`) it runs the Complete steps with the honoured capture replaced by `record_late_capture(…, case="fake_mode")` (refund `manual_required` through the fake port, ERROR `payment.late_capture`); any other Complete → `-1` + `rejected:` row | `commerce/api/webhooks/*` | C§2.13.0 (production may run fake adapters; fake mode never pays an order and never strands a capture taken while Click was live) |
| checkout and pay accept only `core.providers.payable_providers()` (400 `invalid_choice` otherwise) | `commerce/api/customer/*` | C§2.4, C§2.7 |
| `attach_transaction(payment_id, provider_txn_id, provider_state)` (refuses a different existing txn id → `TransactionTaken`), `merge_provider_state(payment_id, patch)`, `mark_late_captured(payment_id, provider_txn_id, at, raw=None)` (pending or failed-by-us → `paid`, `error_code="late_capture"`, `failed_at=None`) | `payments.services` | under the Payment lock |
| one display rule for late captures (C§2.8): `display_payment`, `pick_display` and `display_status_subquery` rank only non-late rows and fall back to the newest late capture when every row is one; `active_payment` ignores late captures; `revenue_buckets` excludes them (admin §2.10); `payme_statement(from_ms, to_ms)` by `provider_state.time`; `payment_by_txn(provider, txn_id)` | `payments.selectors` (B5, changed); `commerce.selectors` order list/detail and the customer `OrderSummary` keep using `pick_display` / `display_status_subquery` | C§2.8, C§2.13.0 |
| the `already_paid` check of pay counts only non-late `paid` payments | `commerce.services.ensure_payable_payment` | C§2.7.2 |
| `delivery/calculate/` is served by `commerce/api/customer/urls.py` (it reads the cart, loyalty and `cashback_max`, which `delivery` may not import); `delivery/api/customer/` serves only `delivery/regions/` | `commerce/api/customer/*`, `delivery/api/customer/*` | C§2.6 |
| Payme protocol reads `account[PAYME_ACCOUNT_FIELD]` and answers `data = PAYME_ACCOUNT_FIELD` (the same setting builds the checkout URL's `ac.` key) | `integrations.payme.protocol`, `commerce/api/webhooks/payme.py` | C§2.13.2 |
| Click refunds keyed by `click_paydoc_id`: `RefundRow.provider_payment_ref` (= `Payment.provider_state["click_paydoc_id"]`, `""` when absent) in `payments.selectors.refund_row`; B5's `commerce.tasks.submit_refund`, `_resolve_after_failure` and `_reversed` pass it (never `provider_txn_id` = `click_trans_id`) to `ClickPort.refund` / `payment_status`; a Click refund whose payment has no `click_paydoc_id` is created `manual_required` and re-checked by `submit_refund` before any port call; the seeded Click payments of `commerce/seeds.py` get a `click_paydoc_id`. `resolve_refund` also accepts a `failed` Click refund → `succeeded` (staff payout after a refused reversal, admin §7 item 18), **guarded** under the Order → Payment locks: only the payment's newest refund, the payment still `paid`, no other open or `succeeded` refund of the payment — else `InvalidTransition(MESSAGE_REFUND_NOT_RESOLVABLE)` (409); `payments.services.mark_succeeded` accepts `failed` only through that path; `submit_refund` re-reads the payment before any port call and ends the refund `failed` (`payment_not_paid`, `refund_failed` event, no Click call) when it is no longer `paid` | `payments.selectors`, `payments.services`, `commerce.tasks`, `commerce.services` | C§2.13.0 (Click Merchant API reversal/status are keyed by the payment id; the resolve guard) |
| a Click refund is `manual_required` (created with `manual=True`, and re-checked by `submit_refund` before any port call) while the Click port is fake under the production settings (`core.providers.callbacks_refused("click")`): the fake would report a reversal that moved no money | `commerce.services` (refund creation call sites), `commerce.tasks.submit_refund` | C§2.13.0, C§6.14 |
| `region_catalog()` | `delivery.selectors` | BTS `regions()` cached 24 h (`gd:bts:regions`); `[]` (nothing cached) while `core.providers.delivery_available()` is false |
| delivery offered only while `core.providers.delivery_available()`: `delivery/calculate/` and checkout answer 409 `delivery_unavailable` (`key="error.delivery_not_offered"`) for `fulfilment = delivery` before any BTS call; `delivery/regions/` answers `{"regions": [], "updated_at": null}` | `commerce/api/customer/*`, `delivery/api/customer/*` | C§2.6, C§2.7.1 row 3 |
| `FakeBts` under the production settings (`core.providers.fake_in_production("bts")`): `regions()` `[]`, `quote()`/`create()` → `BtsRejected("fake_mode")` (shipment `failed`, `shipment_failed {error_code: "fake_mode"}`), `find_by_client_id()` → `None`, `cancel()`/`track()` → `BtsUnavailable`; `_parcels` stays empty; `poll_bts` returns at once | `integrations/bts/fake.py`, `commerce.tasks` | C§2.14.3 |
| `push_sale` (B5 `billz.push_sale` handler) skips the port while `core.providers.fake_in_production("billz")`: no `link_billz_sale`, no `billz_sale_recorded` event, WARNING `billz.sale_not_pushed {order_id, purchase_id}`, the message ends `done` | `commerce.tasks` | C§6.14 |
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
  1-so'm remainder → `cashback_max`); snapshots (incl. `customer_note`) and events equal to B5's admin expectations; cart emptied; replay returns
  the stored 201 with an empty cart; consent outdated → 409 `terms_update_required`; no HTTP call while a row lock is
  held (fake BTS asserts no open `select_for_update`).
- **Race — two checkouts for the last unit** (threads, `transaction=True`): exactly one order, one reservation; the
  other gets `OUT_OF_STOCK` with `available: 0`.
- **Race — two checkouts of one customer with different keys**: one order, the other `open_order_exists` (the second
  blocks on the Customer row lock).
- **Race — checkout vs the Customer lock held by a delete** (a thread holding `select_for_update` on the customer and
  then deactivating it): checkout ends with 401 `SESSION_EXPIRED`, no order.
- Pay: same provider reuses the row with no write (also while a Click transaction is open or in flight), switch
  Click↔Payme (`abandoned`), switch while Click is in flight (< 120 s) → `payment_in_progress` and allowed after it,
  Payme state-1 refusal, `actions.pay` true while Click is in flight and false while Payme is in state 1, not payable → 409 `order_expired` **with no write** (the order stays `awaiting_payment`
  until the job), already paid, URL format (Click query with `account_ref`, Payme base64 with tiyin, language and
  `account_ref`), `return_url` https.
- Customer cancel: from `awaiting_payment` (Click pending failed; Payme state-1 left pending), Click in flight → 409,
  refused otherwise.
- Customer orders: scope filters, ownership 404, history horizon (orders before `history_from` invisible and 404),
  №10248 timeline vector (C§4.1) in uz/ru/en, progress for delivery, pickup and cancelled orders, refund mapping,
  `payment.in_progress`, `actions`, item names in ru/en.
- **Click sandbox suite (offline, §5.1)** and **Payme sandbox suite (offline, §5.2)** — every row.
- **Race — expiry vs late callback:** (a) Click complete holding the Order lock while the expiry job runs → the job
  skips (`skip_locked`), the payment wins; (b) the job wins first → complete is a refused capture: `0` + late-capture
  refund; (c) prepare at T−30 s, expiry at T, complete at T+200 s (Click no longer in flight, still open) → the job
  skipped the order until T+300 s → paid (grace); (c′) prepare at T−30 s, expiry at T, complete at T+60 s →
  paid (grace); (d) complete after the grace → refused capture; (e) prepare at T+10 s → `-9` (attaching after
  `reservation_expires_at`).
- **Race — pay switch vs Click complete** (threads): the switch to Payme and the Complete of the prepared Click payment
  run concurrently → either the Complete wins (Click paid, the switch answers `already_paid`) or the switch is refused
  `payment_in_progress`; never a Payme row confirmed with a Click transaction id.
- **Race — duplicate concurrent Click complete** (two threads, same payload): one effect, both answers `0`.
- **Race — superseding Prepare vs Complete of the old transaction** (threads): exactly one of the two Click payments
  ends `paid` normally; the other is either never captured or a refused capture (`late_capture` + refund); never two
  normal captures.
- Click refunds by `click_paydoc_id`: an admin cancel of a Click-paid order and a late-capture refund call the fake
  port's `refund` / `payment_status` with the payment's `click_paydoc_id` (asserted on the fake's recorded calls, not on
  the outbox payload); a payment without one → refund `manual_required`, no port call; a refused reversal → `failed`,
  then `…/resolve/ {state: succeeded}` closes it. **Resolve guard:** a refused reversal → `failed` → `…/retry/` (new
  refund, still open) → `…/resolve/ {state: succeeded}` of the **old** failed refund → 409 `invalid_transition`, the
  payment stays `paid`, the retry still submits; the same after the retry `succeeded` (409, no second succeeded refund,
  `revenue_buckets` never negative); a `submit_refund` that finds the payment no longer `paid` ends the refund `failed`
  `payment_not_paid` with no port call (asserted on the fake's recorded calls).
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
  stale shipments only; regions cache. Polling: with 250 stale parcels and an unchanged status, three runs poll all
  250 (cursor wraps), none twice; an unchanged status with a changed volatile field in the tracked document appends no
  `ShipmentEvent`; a second run while the lock is held returns at once; a run stops at `BTS_POLL_MAX_SECONDS` (pinned
  clock). **Per-parcel failures:** one parcel whose `track()` always raises `BtsUnavailable` (scenario `bts.track:
  "down"`) and another that raises `BtsRejected` (`"rejected"`) → every other stale parcel is still polled within two
  runs, the cursor moves past both, WARNING `bts.poll_parcel_failed` per failure, `last_sync_at` set; three parcels in
  a row down → the run stops after the third (`consecutive_failures` + 1) and the next run starts after it; a
  `BtsRejected` between transport failures resets the streak; the webhook re-track answering `BtsRejected` → 200
  `ignored`, nothing applied.
- Display rule (C§2.8): an order with a Payme `paid` payment and a Click `late_capture` row shows Payme in the admin
  list, the admin `payment` filter (`paid` matches), the admin detail, `me/orders/` and `me/orders/{id}/`; an expired
  order whose only payment is a late capture shows it as `paid` + `refund.in_progress` (never `pending`) and `refunded`
  after the refund succeeds; `revenue_buckets` excludes the late capture; pay on a cancelled order being refunded answers
  `not_payable`, never `already_paid`.
- **Race — checkout vs a capture of another open order** (`CHECKOUT_MAX_OPEN_ORDERS=2`, threads, `transaction=True`): a
  Click Complete holds Order → Payment → LoyaltyAccount and inserts its `Purchase` while a checkout of the same customer
  holds the customer-level lock and waits in `create_hold` → both finish, no `40P01` deadlock.
- Dev simulate: mounted only with `DEV_FEATURES_ALLOWED`, the flag and fake payment modes; runs the real views; absent
  under the production settings.
- **Fake providers under the production settings** (`override_settings(DEV_FEATURES_ALLOWED=False)`, `CLICK_MODE =
  PAYME_MODE = BTS_MODE = "fake"`): a correctly signed Click Prepare, a correctly signed Complete naming an unattached
  or unknown payment and a Basic-auth Payme `CreateTransaction` + `PerformTransaction` made with the configured keys are
  refused (`-1` / `-32504`), the BTS webhook answers 401, the order stays `awaiting_payment`, only bounded `rejected:`
  rows are written; **switch while a Click transaction is open:** Prepare under the test settings (processed
  there as a live Click's would be), then `override_settings(DEV_FEATURES_ALLOWED=False, CLICK_MODE="fake")` and the correctly signed Complete
  → `0`, payment `paid` `late_capture`, refund `manual_required` (no port call), audit `payment.late_capture {case:
  "fake_mode"}`, ERROR log, order not `processing`; the same Complete with a wrong signature → `-1`; checkout and pay refuse
  a fake provider with 400 `invalid_choice`; a Click refund created or submitted while the Click port is fake becomes
  `manual_required` with no port call. **Mixed posture** (`override_settings(DEV_FEATURES_ALLOWED=False, BTS_MODE=
  "fake", BILLZ_MODE="fake")`, `core.providers.payable_providers` patched to `["click"]` as with a live Click): `delivery/calculate/` and checkout with
  `fulfilment = delivery` → 409 `delivery_unavailable` with the `error.delivery_not_offered` message and no BTS call,
  pickup checkout works, `delivery/regions/` empty; a `bts.create_shipment` of an order placed before the switch →
  shipment `failed` `fake_mode`, no tracking number, `FakeBts._parcels` empty; an admin cancel of a `created`
  shipment → 503, no `bts_cancelled: true`; `poll_bts` polls nothing; `billz.push_sale` → `billz_pushed_at` null, no
  `billz_sale_recorded` event, WARNING logged.
- Migrations `commerce.0004` and `payments.0002` round trip.

### Depends on
P0; B5 merged (`feat/m2-base`).

---

## 4. P3 — loyalty, QR/POS, appointments, inbox

### Owns
`glowderma/loyalty/{qr.py (new), selectors.py, api/customer/*, api/pos/* (incl. base.py: PosAPIView), authentication.py
(new: PosKeyAuthentication with the failed-auth counter, IsActivePosDevice), services.py (PosDevice key creation,
revoke, rotate), management/commands/{createposkey,revokeposkey,rotateposkey}.py, seeds.py (the `POS_API_KEYS` seeding,
local/test settings only)}`,
`glowderma/appointments/{services.py, selectors.py, api/customer/*}`, `glowderma/notifications/{services.py,
selectors.py, tasks.py (the `push.account_security` handler), topics.py (`HANDLED_TOPICS` += `push.account_security`, so
its dead letters are audited by the existing `on_push_dead` hook), texts.py, api/customer/*}`, tests in those modules.

### Endpoints
C§2.0 rows 36–49: `me/cashback/`, `me/cashback/ledger/`, `me/qr/`, `pos/cashback/resolve-qr/`, `specialists/`,
`specialists/{id}/`, `services/`, `specialists/{id}/slots/`, `appointments/`, `me/appointments/`,
`me/appointments/{id}/`, `appointments/{id}/cancel/`, `me/notifications/`, `me/notifications/unread-count/`,
`me/notifications/{id}/read/`, `me/notifications/read-all/`, `me/notifications/ack/`.

### Services
| New / changed | Module | Notes |
|---|---|---|
| `issue_qr(customer_id) -> QrToken`, `resolve_qr(token) -> customer_id \| None` | `loyalty.qr` | 128-bit token, HMAC key, `core.qr_store.put(…, ttl=60, max_live=2)` / `take` (`GETDEL`) |
| `create_pos_device(name, location_label, actor_email) -> (PosDevice, raw_key)`, `revoke_pos_device(prefix, actor_email)`, `rotate_pos_key(prefix, actor_email) -> raw_key`, `authenticate_pos_key(raw)` | `loyalty.services` / `loyalty.authentication` | SHA-256, `compare_digest`, `last_used_at` ≤ once per minute; failed-auth Redis counter per IP; audits `pos.device_created/revoked/rotated` with the staff actor; `POS_API_KEYS` seeding by the loyalty seeder only when `DEV_FEATURES_ALLOWED` (the production settings refuse the variable, C§2.10) |
| `pos_customer_ref(device_id, customer_id)` | `loyalty.qr` | opaque `c_…` (C§2.10) |
| `customer_ledger_page(customer_id)`, `ledger_item_view(row, lang, horizon)` | `loyalty.selectors` | paginated in SQL over the `_ledger_queryset()` filtered by the customer's account (`ledger_for_customer` returns a full Python list and is not used for pages); before-horizon rows without order links |
| `effective_holds(customer_id, now)` | `loyalty.selectors` | C§2.9.1: an **active** hold whose `expires_at` has passed exists only while the expiry job skips its order for an open transaction attached in time (otherwise expiry would have released it), so the selector shows `expires_at + PAYMENT_IN_FLIGHT_GRACE_SECONDS` for such holds and `expires_at` otherwise — no read of commerce or payments (layering) |
| `free_slots(professional_id, service_id, date, now)`, `check_customer_slot(...)` | `appointments.selectors` / `services` | C§2.11 algorithm (grid relative to the hours start, lead time, horizon, blocks, break, open appointments) |
| `request_by_customer(customer_id, professional_id, service_id, start, note)` | `appointments.services` | customer-level lock first (`select_for_update(no_key=True)`), then count + own overlap; exclusion race → 409 `slot_taken` (reuse `_lost_the_slot`); event `requested` source `customer`; audit |
| `cancel_by_customer(appointment_id, customer_id, *, reason="", at=None)` | `appointments.services` | replaces the P0 stub |
| `customer_appointments(customer_id, scope)` | `appointments.selectors` | history horizon |
| `inbox_page(customer_id)`, `unread_count(customer_id)` | `notifications.selectors` | `inbox_customer_unread_idx`; history horizon |
| `acknowledge(customer_id, inbox_ids)`, `mark_read(customer_id, inbox_id)`, `mark_all_read(customer_id)` | `notifications.services` | read and read-all call `acknowledge` first; campaign `delivered` increment only on a real transition |
| `deliver_order_status` (changed): title = `t('label.order_status[_pickup].<status>', customer.language)`; skip orders created before `customer.history_from`; `deliver_appointment` unchanged texts; `deliver_account_security(payload)` (new, handler of `push.account_security`: inbox row `kind=general`, `source_type=account`, `source_id=customer_id`, `link={"kind": "none"}`, dedupe `account:{customer_id}:{event}:{event_id}`; push to the current device and to each `farewell_token`, then blanks them in the payload) | `notifications.services` / `tasks` | C§2.14.2, C§4.3 |
| push payloads with `inbox_id`; channel/priority per `data.type`; `unregistered` → `accounts.services.invalidate_fcm_token`; `payload_invalid` → inbox `push_state=failed`, `push_error="payload_invalid"`, ERROR log, the handler returns normally (outbox `done`, no retry; `core/tasks.py` is unchanged), device untouched | `notifications.services` (`_push_messages`, `_deliver`, `_settle`) | C§2.14.2, using the P0 port |

**Reused (do not redefine):** `loyalty.selectors.{account_summary, customer_totals, tier_for, active_policy,
rules_of, ledger_for_customer, held_amount}`, `professionals.selectors.{day_schedules, booking_catalogue}`,
`appointments.services.{active_link, record_event, _lost_the_slot}`, `appointments.selectors.get_appointment`,
`notifications.services.{_deliver, deliver_order_status, deliver_appointment, send_batch}`,
`notifications.selectors.customer_push_device`, `accounts.services.invalidate_fcm_token` (P0), `core.qr_store` (P0),
`integrations.fcm` port (P0).

### Models / migrations
None of its own (`PosDevice`, `InboxNotification.delivered_at`, `CampaignRecipient.delivered_at`, `Campaign.delivered`,
`Appointment.CancelledBy.CUSTOMER`, `AppointmentEvent.Source.CUSTOMER` exist); the `account` inbox source comes from
P0's `notifications.0003`.

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
| 1 | prepare | valid | 0, `merchant_prepare_id` 301 | payment pending, txn attached, `prepare_at`, `click_paydoc_id` stored; one inbox row |
| 2 | prepare | same payload again | 0 (replayed) | unchanged; one effect; inbox rows: the original + one `dup:` row |
| 3 | prepare | same `click_trans_id`, re-signed (new `sign_time`) | 0, same prepare id | unchanged; one more `dup:` row |
| 4 | prepare | wrong `sign_string` | -1 | nothing; bounded `rejected:` row |
| 5 | prepare | `action=1` on prepare URL | -3 | nothing |
| 6 | prepare | unknown order / wrong HMAC part / bare order id | -5 | nothing |
| 7 | prepare | `amount=784401` / `"784400.50"` / `"abc"` | -2 | nothing |
| 8 | prepare | `service_id` other / missing param / missing `click_paydoc_id` / JSON body / same `click_trans_id` with another amount (conflicting fingerprint) | -8 | nothing; `rejected:` row for the conflict |
| 9 | prepare | order cancelled, or `now ≥ R` | -9 | nothing |
| 10 | prepare | order paid | -4 | nothing |
| 11 | prepare | new `click_trans_id` while 301 is attached without a Complete (prepared 30 s ago, i.e. still in flight, and again 10 min ago; both before `R`) | 0, new prepare id | 301 failed `abandoned` (no `payment_failed` event), new pending row attached (supersede, C§2.7.2) |
| 12 | complete | of the superseded 301 after row 11, `error=0` (money taken) | 0, `merchant_confirm_id` 301 | refused capture: 301 `paid` `late_capture`, refund `{case: failed_by_us}`; the new payment untouched and still payable |
| 13 | prepare | while a Payme transaction is in state 1 | -9 | nothing |
| 14 | complete | valid | 0, `merchant_confirm_id` 301 | paid, order `processing`, events, outbox |
| 15 | complete | same payload again | 0 (replayed) | one purchase, one credit; inbox rows: the original + one `dup:` row |
| 16 | complete | re-signed retry after success | 0 (from state) | one purchase, one credit; one more `dup:` row |
| 17 | complete | unknown `merchant_prepare_id` | -6 | nothing |
| 18 | complete | `error=-5017` | -9 | payment failed, `payment_failed` event, order still payable |
| 19 | complete | amount differs | -2 | pending |
| 20 | complete | prepared before `R`, complete within the grace (also after Click left the 120 s in-flight window) | 0 | paid normally |
| 21 | complete | after expiry beyond the grace (order cancelled by the job, payment failed `expired`) | 0 | payment `paid` `late_capture`, `Refund(late_capture)` requested + `refund.submit` + `refund_requested` event + audit `payment.late_capture {case: failed_by_us}`; the refund handler calls the fake port's `refund` with `click_paydoc_id` (asserted on the fake's recorded call, not on the payload) |
| 22–24 | — | removed in revision 2 (PAY-2: no error-answer mode, no delayed status check) | — | — |
| 25 | complete | 301 failed `abandoned` by a switch to Payme | 0 | late-capture refund `{case: failed_by_us}`; the Payme payment untouched |
| 26 | complete | the order was meanwhile paid by Payme (double payment) | 0 | 301 `paid` `late_capture` next to the Payme paid row (index allows it), refund `{case: double_payment}`, order unchanged; order list/filter/detail show the Payme payment (C§2.8) |
| 27 | — | removed in revision 2 (PAY-2) | — | — |
| 28 | complete | after a customer cancel (only possible once Click left the in-flight window) | 0 | refund `{case: failed_by_us}` |
| 29 | complete | bad signature (incl. `merchant_prepare_id` tampered) | -1 | nothing |
| 30 | complete | payment failed by Click earlier (`error_code "-5017"`) | -9 | nothing |
| 31 | complete | payment already refunded after a capture | 0 (from state) | nothing new but a `dup:` row |
| 32 | complete | refused capture of a payment whose reversal the fake refuses (`click.refund: failed`) | 0 | refund `failed`, counted in `attention.refunds_needing_action`; `…/resolve/ {state: succeeded}` closes it |
| 33 | prepare / complete | with `CLICK_MODE=fake` under `override_settings(DEV_FEATURES_ALLOWED=False)`: any Prepare, correctly signed; a correctly signed Complete whose payment was never attached | -1 | order and payments unchanged; bounded `rejected:` row `error_code: "fake_mode"` |
| 34 | complete | row 1 ran before the switch (301 attached while Click was live), then `CLICK_MODE=fake` under `override_settings(DEV_FEATURES_ALLOWED=False)`, correctly signed Complete with `error=0` | 0, `merchant_confirm_id` 301 | refused capture `{case: fake_mode}`: 301 `paid` `late_capture`, refund `manual_required` (no port call), ERROR `payment.late_capture`; the order is not paid and expires as usual |

### 5.2 Payme (mirrors the `test.paycom.uz` checklist; amounts in tiyin 78 440 000; codes marked † verify M3)

| # | Method | Variation | Expected |
|---|---|---|---|
| 1 | any | no/wrong Basic auth | -32504 (bounded `rejected:` row) |
| 2 | – | body not JSON / no `method` / unknown method / `ChangePassword` / GET | -32700 / -32600 / -32601 / -32601 / -32300 |
| 3 | CheckPerformTransaction | unknown order / wrong HMAC part / missing `order_id` | -31050 (`data: "order_id"`; with `PAYME_ACCOUNT_FIELD=invoice` the account key and `data` are `invoice`) |
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
| 27 | any | correct Basic auth with `PAYME_MODE=fake` under `override_settings(DEV_FEATURES_ALLOWED=False)` | -32504, order unchanged, bounded `rejected:` row `error_code: "fake_mode"` |

---

## 6. Fake provider behaviour (deterministic switches for e2e)

Black-box e2e drives every branch with `X-GD-Fake-Scenario` (copied into `Order.metadata["fake"]` at checkout; read
directly by `delivery/calculate/`, which has no order yet; honoured only under the local/test settings with every
provider mode fake and the flag on) or with the phone/token grammar below. Everything in this table is local/test
behaviour: under the production settings the fakes run without scenarios, record nothing, refuse callbacks and report
no effect that did not happen (fake BTS: no delivery offered, no parcel made up, C§2.14.3; fake Billz: no sale
recorded; C§6.14).

| Provider | Switch | Effect |
|---|---|---|
| Eskiz | phone `…0000` / `…9999` / other | rejected / transport down / accepted; code = `ESKIZ_FAKE_CODE` (local/test settings only) |
| OAuth | token `fake:<sub>:<email>`, `fake-unverified:…`, `fake-apple:<sub>:<email>`, `fake-fail`, `fake-down`, `fake-expired`; Apple code `fake-code:…` / `fake-code-fail` / `fake-code-down` | valid / valid unverified / Apple valid / `social_token_invalid` / 503 / `social_token_invalid`; code exchanged / 400 / 503 |
| BTS | `bts.quote`: `"rejected"` / `"down"`; `bts.tariff`: int | 409 `delivery_unavailable` / 503 / fixed fee |
| BTS | `bts.fail_attempts`, `bts.down`, `bts.existing`, `bts.tracking_number`, `bts.status`, `bts.cancel` | B5 rules (create/cancel) |
| BTS | `bts.track_status`: `"100"`/`"200"`/`"300"`/other; `bts.track`: `"rejected"` / `"down"` | what `track()` answers (webhook re-track, polling); `track()` raises `BtsRejected` / `BtsUnavailable` |
| Click | `click.refund`: `succeeded`/`failed`/`unknown`/`down` | B5 refund rules (also used by the late-capture reversal); the fake records the `click_paydoc_id` it was called with |
| Click | `click.transaction_id`, `click.paydoc_id` | fixed ids for `dev/…/simulate/` |
| Payme | `payme.transaction_id` | fixed Payme id for `dev/…/simulate/` |
| FCM | token `…-bad` / `…-payload` / `…-down` | `unregistered` (device pruned) / `payload_invalid` (message failed, device untouched) / `FcmUnavailable` (retry) |
| POS | `POS_API_KEYS=name:key,…` | seeded `PosDevice` rows (local/test settings only; refused by the production settings) |
| Clock | `FAKE_NOW` (local/test) | pinned `timeutils.now()` for expiry/grace/slot tests |

---

## 7. Settings and environment (P0 adds all; `.env.example` lines)

```dotenv
# ---- Customer API (M2) ---------------------------------------------------------
# Access JWT lifetime (seconds) and the sliding refresh window (days); cache window for concurrent refreshes (seconds).
#CUSTOMER_ACCESS_TTL=900
#CUSTOMER_REFRESH_TTL_DAYS=60
#CUSTOMER_REFRESH_GRACE_SECONDS=120
# How long an unconfirmed rotation lets the previous refresh token replay or re-rotate; after it (or once an access
# token of the new rotation was used) the previous token counts as reuse and revokes the session.
#CUSTOMER_REFRESH_UNCONFIRMED_SECONDS=600
# OTP policy (A3): code TTL, resend wait, attempts, lockout, daily caps per phone / device / IP, wrong codes per day.
#CUSTOMER_OTP_TTL_SECONDS=120
#CUSTOMER_OTP_RESEND_SECONDS=60
#CUSTOMER_OTP_MAX_ATTEMPTS=5
#CUSTOMER_OTP_LOCKOUT_MINUTES=15
#CUSTOMER_OTP_DAILY_LIMIT=10
#CUSTOMER_OTP_DAILY_WRONG_LIMIT=10
#CUSTOMER_OTP_DEVICE_DAILY_LIMIT=10
# Per-IP day cap: a cost control that also meets carrier-grade NAT, so it answers its own code (otp_network_limit);
# provisional, tuned from metrics.
#CUSTOMER_OTP_IP_DAILY_LIMIT=1000
# Circuit breaker: SMS codes per hour across all phones (503 sms_unavailable + alert above it).
#SMS_GLOBAL_HOURLY_LIMIT=500
#CUSTOMER_VERIFICATION_TTL_SECONDS=600
#CUSTOMER_PASSWORD_MIN_LENGTH=8
# Dormant-account rule (C§2.2.3, M2-A16): an SMS recovery of an account inactive for longer moves the history horizon
# to now and scrubs name, image and addresses.
#CUSTOMER_DORMANT_DAYS=180
# Keys of the customer API. Local/test settings ship valid development defaults, so these stay commented here;
# production requires real values and refuses "change-me", committed values (these lines, the local defaults) and
# short keys. Generate each with: python -c "import secrets; print(secrets.token_urlsafe(32))"
# HMAC key for stored SMS codes (>= 32 characters).
#SMS_CODE_HMAC_KEY=
# AES-256-GCM key of core.secretbox (OTP payload in Redis, Apple refresh tokens): base64url of EXACTLY 32 bytes
# (secrets.token_urlsafe(32) produces one).
#SECRETBOX_KEY=
# HMAC key for pseudonymous phone/device references in logs and throttle keys (>= 32 characters).
#LOG_PII_KEY=
# Android SMS Retriever app hash (11 chars) appended to the OTP text for Android; empty = no hash line.
#ANDROID_SMS_APP_HASH=
#SOCIAL_TICKET_TTL_SECONDS=600
#SOCIAL_NONCE_TTL_SECONDS=600
# Google client ids the app's ID tokens are issued for (iOS client id, web/server client id used by Android), comma separated.
#GOOGLE_APP_CLIENT_IDS=change-me-ios.apps.googleusercontent.com,change-me-web.apps.googleusercontent.com
# Sign in with Apple (product-owner decision 2026-10-26): the iOS bundle id(s) Apple ID tokens are issued for, comma
# separated; empty = provider "apple" disabled (404) with OAUTH_MODE=live, the fake Apple adapter with OAUTH_MODE=fake
# (local/test only: production answers 404 on customer social routes while OAuth is fake).
#APPLE_BUNDLE_IDS=uz.glowderma.app
# Apple developer team id, Sign in with Apple key id and the .p8 private key file (ES256 client secret for the code
# exchange and token revocation). Required in production when OAUTH_MODE=live and APPLE_BUNDLE_IDS is set.
#APPLE_TEAM_ID=change-me-team-id
#APPLE_KEY_ID=change-me-key-id
#APPLE_PRIVATE_KEY_FILE=/run/secrets/apple-signin.p8
# Minimum app versions (426 app_update_required below them) and store links.
#MIN_APP_VERSION_IOS=0.0.0
#MIN_APP_VERSION_ANDROID=0.0.0
#APP_STORE_URL=https://apps.apple.com/app/id0000000000
#PLAY_STORE_URL=https://play.google.com/store/apps/details?id=uz.glowderma.app
#ADDRESS_MAX_PER_CUSTOMER=10
# ---- Eskiz SMS ----------------------------------------------------------------
# fake = in-process (phone ...0000 rejected, ...9999 down); live = notify.eskiz.uz (M3). Production accepts fake
# (startup WARNING, reported in health/?ready=1): no SMS is delivered, so nobody can register, reset or sign in on a
# new device. That is a demo/staging posture only.
#ESKIZ_MODE=fake
#ESKIZ_BASE_URL=https://notify.eskiz.uz/api
#ESKIZ_EMAIL=change-me@example.uz
#ESKIZ_PASSWORD=change-me-eskiz-password
#ESKIZ_SENDER=4546
#ESKIZ_CALLBACK_URL=
# Fixed OTP for black-box e2e; read ONLY by config.settings.local / test (production refuses the variable).
#ESKIZ_FAKE_CODE=123456
# ---- Cart and checkout ---------------------------------------------------------
#CART_MAX_LINES=30
#CART_MAX_QTY_PER_LINE=10
#CHECKOUT_MAX_OPEN_ORDERS=1
#CHECKOUT_MIN_PAYABLE=1000
# Seconds after reservation_expires_at during which a transaction started before it may still complete.
#PAYMENT_IN_FLIGHT_GRACE_SECONDS=300
# HMAC key of the provider account reference "<order_id>-<ref>" (>= 32 characters; local default, required in production).
#PAYMENT_REF_KEY=
# Where the provider page sends the customer back (https app/universal link; order_id is appended) and the custom-scheme fallback.
#PAYMENT_RETURN_URL=https://glowderma.uz/pay/return
#PAYMENT_RETURN_FALLBACK_URL=glowderma://payment/return
# ---- Click (SHOP API) ----------------------------------------------------------
# fake under the production settings: Prepares are refused and Click is not offered to customers (no free orders); a
# Complete of a transaction prepared while Click was live is accepted and reversed by hand (manual refund), so a
# switch from live to fake never strands a customer's money.
#CLICK_MODE=fake
#CLICK_SERVICE_ID=00000
#CLICK_MERCHANT_ID=00000
#CLICK_MERCHANT_USER_ID=00000
# Issued by Click (merchant cabinet). Production requires a real value in every mode (a placeholder is refused).
#CLICK_SECRET_KEY=
#CLICK_PAY_URL=https://my.click.uz/services/pay
#CLICK_ALLOWED_IPS=
# A prepared Click payment counts as in flight (blocks a switch to Payme, the customer cancel and DELETE me) for
# this many seconds; must not exceed PAYMENT_IN_FLIGHT_GRACE_SECONDS. Paying again with Click is never blocked: a new
# Prepare supersedes the open Click transaction. A Complete we cannot honour is always accepted and reversed (Click
# SHOP API rule), so there is no switch for it.
#CLICK_INFLIGHT_SECONDS=120
# ---- Payme (Merchant API) ------------------------------------------------------
# fake under the production settings: callbacks are refused and Payme is not offered to customers.
#PAYME_MODE=fake
#PAYME_MERCHANT_ID=change-me-payme-merchant-id
# Issued by Payme (merchant cabinet; the test key in the sandbox). Production requires a real value in every mode.
#PAYME_KEY=
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
# Shared secret of the BTS webhook (>= 32 characters; ours). Production requires a real value in every mode.
#BTS_WEBHOOK_SECRET=
# Polling fallback (C§2.13.3): parcels whose last status is older than this are polled round robin, at most
# BTS_POLL_BATCH parcels and BTS_POLL_MAX_SECONDS per run (single flight). A failing parcel is skipped (the cursor
# moves past it); the run stops only after BTS_POLL_MAX_CONSECUTIVE_FAILURES transport failures in a row (BTS down).
#BTS_POLL_STALE_MINUTES=30
#BTS_POLL_BATCH=100
#BTS_POLL_MAX_SECONDS=300
#BTS_POLL_MAX_CONSECUTIVE_FAILURES=3
# ---- FCM (live adapter M3: HTTP v1 over httpx with a service-account JWT) ------------
#FCM_PROJECT_ID=change-me-firebase-project
#FCM_CREDENTIALS_FILE=/run/secrets/fcm.json
# ---- Loyalty QR and POS ----------------------------------------------------------
# HMAC key of the QR token store and the POS customer_ref (>= 32 characters; local default, required in production).
#QR_HMAC_KEY=
#QR_TOKEN_TTL_SECONDS=60
#QR_MAX_LIVE_TOKENS=2
#POS_FAILED_AUTH_PER_IP_PER_MIN=20
# Local/test settings only (production refuses it): POS devices seeded as name:key pairs; production keys come from
# `manage.py createposkey`. Key format gdpos_<8-char prefix>_<32 base64url chars>.
#POS_API_KEYS=Kassa 1:gdpos_demo0001_local-dev-only-pos-key-000000000
# ---- Appointments ------------------------------------------------------------------
#BOOKING_MIN_LEAD_MINUTES=60
#BOOKING_HORIZON_DAYS=30
#BOOKING_MAX_OPEN_PER_CUSTOMER=3
#BOOKING_CANCEL_MIN_LEAD_MINUTES=0
# ---- Development switches (local/test settings only: production refuses them; they need fake provider modes) ----
#FAKE_SCENARIO_HEADER_ENABLED=false
#FAKE_PAYMENTS_ENABLED=false
```
Validation (P0):
- positive integers where numeric; `ESKIZ_MODE`/`CLICK_MODE`/`PAYME_MODE`/`BTS_MODE` ∈ {fake, live} (default
  `INTEGRATIONS_MODE`); `FAKE_PAYMENTS_ENABLED` requires `CLICK_MODE == PAYME_MODE == "fake"`;
  `FAKE_SCENARIO_HEADER_ENABLED` requires every provider mode (`ESKIZ`, `CLICK`, `PAYME`, `BTS`, `FCM`) fake;
  `THROTTLE_RATES` knows the new scopes.
- **Fake providers are allowed in production** (product-owner decision 2026-10-26): the production settings accept
  `fake` for every `*_MODE`; `CoreConfig.ready()` logs one WARNING `providers.fake_in_production {provider}` per fake
  provider and `health/?ready=1` / admin `GET settings/` report the modes (C§6.14). What fake means there is C§6.14:
  callbacks refused, fake payment providers not offered, no home delivery with fake BTS (pickup only), no Billz sale
  recorded by fake Billz, no SMS, no push, no social sign-in with fake OAuth.
- `DEV_FEATURES_ALLOWED` is a settings-module constant (`True` only in `local.py`/`test.py`). The production settings
  refuse at boot `FAKE_PAYMENTS_ENABLED`, `FAKE_SCENARIO_HEADER_ENABLED`, `ESKIZ_FAKE_CODE`, `POS_API_KEYS` and (as
  today) `FAKE_NOW`, whatever the provider modes.
- **Keys** (production; `local.py` ships valid development defaults, which are added to `KNOWN_PLACEHOLDER_SECRETS`):
  `SMS_CODE_HMAC_KEY`, `LOG_PII_KEY`, `PAYMENT_REF_KEY`, `QR_HMAC_KEY` and `BTS_WEBHOOK_SECRET` are required, ≥32
  characters (used as their UTF-8 bytes); `SECRETBOX_KEY` is required and must decode (base64url, padding optional) to
  **exactly** 32 bytes (AES-256); `CLICK_SECRET_KEY` and `PAYME_KEY` are required in **every** mode (in fake mode Payme
  callbacks are refused anyway and the check is a second layer; `CLICK_SECRET_KEY` still verifies the Completes of
  transactions attached while Click was live, C§2.13.0), with no length floor because the provider issues them.
  Every one of these is refused when it contains `change-me` (case-insensitive) or equals a committed value (the
  existing `production.py` rule for `SECRET_KEY`/`INTERNAL_API_SECRET`, extended).
- `OAUTH_MODE=live` with a non-empty `APPLE_BUNDLE_IDS` requires `APPLE_TEAM_ID`, `APPLE_KEY_ID` (no `change-me`) and a
  readable `APPLE_PRIVATE_KEY_FILE` that loads as an EC P-256 key.
- Staging runs the production settings module.

## 8. Beat entries (`config/celery.py`, P0)

| Entry | Task | Schedule | Owner of the body |
|---|---|---|---|
| `accounts-cleanup-auth` | `accounts.cleanup_auth` | every 3600 s | P0 |
| `commerce-poll-bts` | `commerce.poll_bts` | every 600 s | P2 (P0 stub returns `[]`) |
| (exists, B5) `commerce-expire-reservations` | `commerce.expire_reservations` | every 60 s | B5; grace change by P2 |

No beat entry for `loyalty.services.expire_holds`: it must stay unscheduled while the in-flight grace exists (C§2.9.1).

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
   - **race — `DELETE me/` vs a concurrent Click Complete / Payme Perform of the same customer's order** (threads,
     `transaction=True`): the capture holds Order → Payment → LoyaltyAccount and inserts its `Purchase` while the delete
     holds the customer-level lock and waits for the Order lock → no `40P01` deadlock; the capture commits, the delete
     then sees the order paid and continues (processing orders are kept);
   - social link from settings (password re-authentication) → `account_security` push → reset-password on a second
     device → identity unlinked, the first device gets the visible `password_reset` notice (farewell token) and then 401
     `password_changed`;
   - social-only account → `me/password/` with an SMS code → identity kept, password sign-in works;
   - QR issue → POS resolve → second resolve `qr_invalid`;
   - booking → admin confirm (B6 endpoint) → push with `inbox_id` → ack → admin `delivered` counter.
3. Update `AGENTS.md` (customer base views, fixtures, i18n rule, fake switches, `pii_ref`; drop the "M2 exception" of
   §0.1 once the schema is committed on `feat/m2-integration`),
   `docs/decisions/open-questions.md` (the `M2-*` rows of C§7 are already recorded as provisional defaults decided
   2026-10-26; update them only if a decision changes) and the spec's M2 section (§0.2 lists the edits that must land
   before P0; afterwards: refused-capture rule, device key for A2, history horizon for A1 and A16). `admin-api-v1.md`
   already carries the M2 cross-references (§2.10, §2.11, §2.15, §3.1, §3.2, §7 item 18).
4. Ops: the website serves `apple-app-site-association` / `assetlinks.json` for `PAYMENT_RETURN_URL`; the edge proxy
   log format redacts `token=`; alerts on the ERROR events `otp.budget_tripped`, `payment.late_capture`,
   `push.payload_invalid`, `apple.revoke_dead`, and on refunds `failed` after a refused Click reversal (staff payout); throttle rates reviewed after two weeks of production metrics.
   A deployment that runs fake adapters shows them in the startup WARNINGs `providers.fake_in_production` and in
   `health/?ready=1` `providers`; the runbook lists what that posture cannot do (C§6.14: no online payment through a
   fake provider, no home delivery with fake BTS — shipments of orders placed before a switch to fake are shipped by
   hand from `attention.bts_failed` —, Billz sales entered by hand with fake Billz, no SMS, no push, no social sign-in
   with fake OAuth).
5. Mobile handover: contract link, `openapi/schema.yaml`, the local fake-mode recipe (`ESKIZ_FAKE_CODE`, dev simulate —
   local/test settings only; a deployed staging needs live Eskiz and payment providers to sign up and pay), the
   device-key and refresh rules (C§1.9, C§2.1.6), the system-browser payment hand-off (C§2.7.2).

## 10. Dependencies at a glance

| From | Needs | Why |
|---|---|---|
| P0 | `feat/m2-base` (B5 + followups merged), the spec edits of §0.2 | commerce services/selectors, Click/Payme/BTS ports (the `AGENTS.md` schema rule is already amended) |
| P1 | P0 | auth class, device key, `accounts.0006`, catalog, i18n, settings, `secretbox`, `pii`, `qr_store`, `start_customer_session` |
| P1 (`DELETE me/` integration) | P2 `cancel_by_customer` + `customer_inflight_order`, P3 `appointments.cancel_by_customer` | stubs in P0 keep P1 independent until merge |
| P2 | P0, B5 | `create_order`, `mark_paid`, `fail_payment_effects`, `apply_bts_status`, `resolve_refund` (signatures changed by P2 itself) |
| P3 | P0 | auth class, FCM port extension, `qr_store`, `invalidate_fcm_token`, i18n; the `push.account_security` topic constant |
| Lead e2e | P1 + P2 + P3 | full flows |

---

## 11. Review log

### 11.1 Revision 1

Each finding of the revision-1 review (security and consistency lenses, merged where they overlap) and the decision.
"Adopted" means the contract and this plan now say what the finding asked; anything else is explained. Rows changed
again by revision 2 say so in italics.

| # | Finding (section) | Decision |
|---|---|---|
| 1 | **Blocker** — late-capture refund unreachable: B5 expiry/switch/cancel fail the Click payment first, step 3 answers -9, `confirm_payment` cannot go failed→paid, a new row is impossible (C§2.13.1, plan §5.1) | Adopted, extended. One **refused-capture** rule (C§2.13.0) for payments failed by us, non-payable orders and double payments, evaluated after the honoured-capture check. `payments.mark_late_captured` is the only failed→paid transition. **Model change the finding did not foresee:** a double payment cannot be marked `paid` while the order's real payment is `paid` because of `payment_one_active_per_order`; `payments.0002` excludes `error_code = 'late_capture'` from that partial unique, and the selectors ignore such rows. Default flipped to `CLICK_REVERSES_ON_COMPLETE_ERROR=false` (deterministic: accept and refund); `true` adds the delayed status check by `click_paydoc_id`. Sandbox rows 21–28 cover expiry, abandoned, double payment and cancel with both flag values. *Revision 2 (PAY-2): the `true` mode, its delayed status check and sandbox rows 22–24/27 are removed; accept and reverse is the only rule.* |
| 2 | **Blocker** — per-transaction advisory locks only; services act on "the pending payment", so a concurrent switch can make `mark_paid` confirm the wrong row and two `CreateTransaction` can overwrite each other (C§2.13.0, C§2.7.2) | Adopted. Every handler takes `lock_order` before reading state; `mark_paid`, `fail_payment_effects`, `record_late_capture` take `payment_id` + provider and refuse otherwise; `attach_transaction` refuses a different existing txn id (`TransactionTaken`). Race tests added (pay switch vs Complete; two concurrent CreateTransaction). |
| 3 | **Blocker/major (both lenses)** — the raw OTP is in the `sms.send` outbox payload (C§2.1.1, C§6.4) | Adopted (option a). The code travels as AES-256-GCM ciphertext in Redis (`SMS_PAYLOAD_KEY`, 120 s); the payload has only `sms_code_id`, `lang`, `platform`; done rows are purged after 24 h; a test asserts no DB row or log holds the code. Design 03 §5's registry row is updated by the lead with the other doc updates (§9.3). |
| 4 | Major — in-flight Click payments unprotected; grace from `attached_at` lets repeated prepares keep an order alive; on-the-spot expiry ignores the grace and is rolled back by `@idempotent` (C§1.9, C§2.7.2, C§2.13.0) | Adopted. Grace anchored to `reservation_expires_at`, only for completing a transaction attached before it; attaching needs `now < R`; Click in flight for `CLICK_INFLIGHT_SECONDS` (600) blocks switch, cancel, delete, a second prepare and Payme create; the pay endpoint never writes on refusal. **Partly not adopted:** a Payme state-1 transaction does not block the customer's cancel, because Payme debits only on `PerformTransaction`, which a cancelled order refuses (P-2), so there is no money at risk and the customer is not stuck for up to 12 h. *Revision 2 (PAY-5): in flight is 120 s and no longer refuses a second Click Prepare, which supersedes the open attachment.* |
| 5 | Major — sequential order id as provider account value enables payment DoS (C§2.7.2) | Adopted: `"<order_id>-<10 hex HMAC>"`, verified in constant time before any lookup (Click -5, Payme -31050); no migration. |
| 6 | Major (both lenses) — Click duplicates by exact payload hash; re-signed retry of an applied Complete gets -8/-4 | Adopted: Prepare and Complete are idempotent from state (a captured or refunded payment of that `click_trans_id` answers 0); fingerprint over semantic fields; -8 only on conflict; sandbox rows 3, 16, 31. |
| 7 | Major — Payme details: account-error code for "another transaction", GetStatement by Payme time, non-integer amount, "Payme cancels" unverified (C§2.13.2) | Adopted with **-31051** (`ERR_ORDER_BUSY`, account range) for "another transaction / in flight"; paid, cancelled and expired keep -31008. The security lens's in-flight fix said -31008 for Payme create; the Payme-protocol finding is the more specific one, so -31051 wins, behind a constant (verify M3, `M2-P10`). GetStatement filters `provider_state.time`; the expression index is deferred to M3 (volumes are tiny; avoids a fourth migration now). Non-integer/≤0 amount → -31001. -32400 for internal errors behind `INTERNAL_ERROR_RESPONSE`. |
| 8 | Major (both lenses) — refresh: no row lock, no index on `previous_refresh_hash`, junk tokens bypass the per-token throttle, undefined grace-miss behaviour, 30 s grace logs out real users with a misleading message (C§2.1.6) | Adopted. Row lock on `refresh_hash OR previous_refresh_hash`; partial index in `accounts.0006`; `refresh_unknown` per-IP throttle on misses; check order revoked/expired → current → previous. Reuse = the old token after the **new one was used** (confirmation by the new refresh or the first access token carrying the current `rot`, recorded in `last_used_at` — no new column); before confirmation the old token replays or re-rotates with no time limit. Grace cache 120 s. New revoke reason `refresh_reused` → `SESSION_EXPIRED`; `SESSION_REPLACED` only for the single-device rule. *Revision 2 (SEC-08, SEC-09): the replay window is bounded (`CUSTOMER_REFRESH_UNCONFIRMED_SECONDS`) and confirmation uses an integer rotation counter — two new columns in `accounts.0006`.* |
| 9 | Major — send-code: no per-phone serialisation, `login_new_device` without password, no global/per-IP budget, victim cap burning, 50 guesses/day, capped-login oracle (C§2.1.1, C§2.1.4) | Adopted: advisory lock per phone; `login_new_device` only via `auth/login/` after the password; fid and IP day caps (Redis counters — they are cost controls, so a flush is acceptable; the phone caps stay DB-derived) and a global hourly breaker with an alert; 10 wrong codes per phone per day; login checks OTP availability **before** the password for unknown devices. Victim-cap burning remains a residual risk (C§6.3), App Attest / Play Integrity noted for M3. |
| 10 | Major (both lenses) — A2 "known device" rests on the non-secret fid (C§1.9, C§6.8) | Adopted: device key issued on every verified sign-in, SHA-256 on `Device.key_hash` (`accounts.0006`), header `X-GD-Device-Key`; rotated on reset; the fid no longer appears in `device_label`. |
| 11 | Major — social linking needs only an SMS code; no unlink; reset keeps identities (C§2.1.8–9) | Adopted: complete requires the password when the account has one; `POST/DELETE me/social/…`; reset unlinks every identity; `account_security` push on link/unlink/reset. **M2-A10 kept** (social sign-in skips the new-device SMS): with linking now requiring password + SMS (or a session + password), the provider sign-in is as strong as the A2 path. *Revision 2 (SEC-05): linking from a session now needs the password, or an SMS code when the account has none.* |
| 12 | Major — recycled numbers expose the previous holder's history after reactivation (C§2.2.3) | Adopted (history horizon at `reactivated_at`, new `M2-A15`); also no order-status push for pre-horizon orders. Scrubbing order snapshots at delete was not chosen: staff and accounting still need them. *Revision 2 (SEC-10): the horizon is `Customer.history_from`, also moved by the dormant-account rule `M2-A16`.* |
| 13 | Major (both lenses) — nothing serialises a customer's checkouts (C§2.7.1) | Adopted with the **Customer** row lock (not the Cart row): it also serialises checkout against `DELETE me/`, which the consistency lens raised. Lock order documented. **Backstop unique index not added:** the B5 seed creates six `awaiting_payment` orders (10238–10242 for generated customers plus a design order) whose customers may coincide, so an index migration could fail on seeded databases, `CHECKOUT_MAX_OPEN_ORDERS` is meant to stay configurable, and the lock plus the race tests are sufficient. The BTS quote now runs before any row lock and the text says it is inside the idempotency transaction. *Revision 2 (PAY-4): the customer-level lock is `FOR NO KEY UPDATE`.* |
| 14 | Major (both lenses) — webhook rejects store 64 KiB rows unthrottled; Payme read-only calls store raw bodies | Adopted: bounded rejected rows (1 KiB, no raw body), ≤10 per IP per minute, sampled log beyond; authenticated deliveries never capped; Payme read-only methods without `raw_body`. |
| 15 | Major — BTS re-track trusts the payload's tracking number; `?token=` leaks into logs | Adopted: always re-track our stored tracking number, mismatch ignored + WARNING; query token stripped from `path`, proxy log redaction, header preferred (verify M3). |
| 16 | Major — dev simulate signs with live keys on a staging with live modes; fake Eskiz code on staging | Adopted: mounted only with fake payment modes; settings validation refuses the combinations; `ESKIZ_FAKE_CODE` only in local/test settings, and staging runs production settings. *Revision 2 (PAY-1, SEC-02, decision 4): production now accepts fake provider modes; the dev switches stay local/test-only and fake callbacks are refused.* |
| 17 | Major (+ consistency minor) — FCM prunes on any `INVALID_ARGUMENT`; firebase-admin not pinned | Adopted: prune only on `UNREGISTERED`, `SENDER_ID_MISMATCH` and token-naming `INVALID_ARGUMENT`; other `INVALID_ARGUMENT` = `payload_invalid` (alert, device untouched); `PUT me/device/` on every cold start; live adapter on httpx + PyJWT, no new dependency. |
| 18 | Major — terms/slot conflicts share code `conflict` (both lenses) | Adopted: 409 `terms_update_required` (`details.current_version`) and 409 `slot_taken`. |
| 19 | Major — `public` Cache-Control on personalised catalog responses (both lenses) | Adopted: `private, no-cache` with a bearer, `public` only anonymous. |
| 20 | Major — per-IP throttles break behind CGNAT | Adopted: device-keyed scopes plus high IP backstops (new scopes so staff `login`/`oauth` rates stay unchanged); rates marked provisional (§9.4). |
| 21 | Major — cart add and address create double on retry | Adopted: Idempotency-Key required on `POST cart/items/` and `POST me/addresses/` (and `POST me/social/`); PATCH documented as absolute for steppers. |
| 22 | Major — the cart is emptied at checkout and never refilled | Adopted as `POST orders/{id}/reorder/` (new `M2-C16`); automatic refill on cancel/expiry was not chosen because the customer may already have a new cart. |
| 23 | Major — inbox/push titles use the admin Uzbek labels | Adopted: P3 renders the customer label in the customer's language; old rows keep their language. |
| 24 | Major — Apple guideline 4.8 | Adopted: `apple` provider in the contract and P1 (fake now, live with A7 credentials), recorded as `M2-A14` for the client to confirm. *Revision 2: resolved by the product owner on 2026-10-26 (`M2-A14` decided, `APPLE_BUNDLE_IDS`).* |
| 25 | Major — provider deep links and return behaviour undefined | Adopted: `app_url` always null in v1, system browser (never WebView), https return link with custom-scheme fallback, polling on resume, `payment.in_progress`; formats recorded as `M2-P9`. |
| 26 | Major — farewell push can sign out another account on the same install | Adopted: `sid` in the payload and the app's match rule; skipped when another active row holds the token; deactivated rows lose their token. |
| 27 | Major — P0's session-replaced push needs the FCM port change that P3 owned | Adopted: the port extension moved into P0's owned files. |
| 28 | Major — B5 `create_order` changes incomplete (visibility, min payable, per-line `OutOfStock`, typed errors, order) | Adopted: the seven changes are listed in the P2 table and C§2.7.1 rows follow the evaluation order. |
| 29 | Minor — Click refunds/status by `click_paydoc_id` | Adopted (verify M3); prepare without it → -8. *Revision 2 (PAY-3): the built refund handler is changed to use it; a payment without one gets a `manual_required` refund.* |
| 30 | Minor — Click JSON bodies break the raw-string signature | Adopted: form only; JSON behind `CLICK_ACCEPT_JSON` with string parsing (verify M3). |
| 31 | Minor — read/read-all bypass the delivered counter | Adopted: both go through `acknowledge`. |
| 32 | Minor — social nonce optional, no replay cache, ticket not bound to the device or the verification token | Adopted: server-issued nonce (`auth/social/nonce/`) required for ID tokens, replay cache until `exp`, ticket carries `pii_ref(fid)`, verification token bound to the ticket through `gd:social:otp:{sms_code_id}`. **No `SmsCode.context` migration:** the binding lives in Redis; a cache flush makes completion fail safe (the user restarts), and durable single use comes from `verification_used_at` and the unique identity row. |
| 33 | Minor — `DELETE me/` password not guarded; social-only deletes with a bare access token | Adopted: `LoginGuard` scope `customer_delete`; social-only needs a `reset_password` verification token (reusing that purpose avoids a new `SmsCode.Purpose` choice). *Revision 2 (SEC-18): the `customer_delete` scope is dropped; `DELETE me/` shares `customer_login`.* |
| 34 | Minor — SHA-256 phone hashes are brute-forceable | Adopted: `pii_ref` = keyed HMAC (`LOG_PII_KEY`). |
| 35 | Minor — QR minting unbounded, tokens survive logout, key prefix mismatch | Adopted: `qr` throttle, ≤2 live tokens, revoked on logout/replace/delete, both operations on the raw client with a literal prefix (`core.qr_store`). |
| 36 | Minor (both lenses) — POS: throttle after auth, no revoke/rotate, `customer_id` exposed, command has no actor | Adopted: failed-auth counter inside the authentication class, `revokeposkey` / `rotateposkey`, `--actor` required, opaque `customer_ref` instead of `customer_id`. |
| 37 | Minor — consent to an empty terms version | Adopted: 503 `terms_not_published` at the endpoint (a DB value cannot be validated by settings); `terms_version` versions both documents. |
| 38 | Minor — booking limit and own overlap race | Adopted: Customer row lock in `request_by_customer`; race test. |
| 39 | Minor — `create_order` ignores category visibility | Adopted (row 28). |
| 40 | Minor — hold `expires_at` in the past during the grace; `expire_holds` danger | Adopted: effective expiry shown; `expire_holds` must stay unscheduled (§8). |
| 41 | Minor — cart cashback cap understates the delivery case | Adopted: one formula; the cart shows `max_usable_pickup`; `delivery/calculate/` returns `cashback_max` and `expected_total`. |
| 42 | Minor — `previous_refresh_hash` index; "one migration" claim | Adopted (`accounts.0006`); the goal now says three migrations. *Revision 2 (B2): four migrations (`notifications.0003`).* |
| 43 | Minor — `PosAPIView` had no owner | Adopted: `loyalty/api/pos/base.py`, P3. |
| 44 | Minor — timeline subtitle keys and `TimelineType` literals | Adopted: `label.timeline.sub_amount`, `sub_amount_provider`, `sub_provider`; explicit literal list; new `ProgressStep` enum. |
| 45 | Minor — order item names are an Uzbek snapshot | Adopted: live product name in the resolved language, snapshot as fallback. |
| 46 | Minor — AI endpoints not scoped out | Adopted: "Out of scope (Phase 2)" header row. |
| 47 | Minor — the app cannot tell a QR was consumed | Adopted: refresh on focus + "Yangilash" button + documented re-scan flow; no status endpoint or push (one more route for a 50 s window was not worth it). |
| 48 | Minor — schema commit rule conflicts with AGENTS.md | Adopted as a lead prerequisite (§0.1): this task may not edit `AGENTS.md`, so the plan records the amendment and blocks P0 on it. *Revision 2: the amendment is now in `AGENTS.md` (Dev loop, "M2 exception").* |
| 49 | Minor — no change-password flow; no SMS autofill | Adopted: documented change-password path (C§2.1); `ANDROID_SMS_APP_HASH` line and iOS one-time-code note. *Revision 2 (SEC-06): the signed-in path is now `POST me/password/`, which keeps the identities.* |
| 50 | Minor — Payme state-1 abandonment leaves the customer stuck | Adopted: `payment.in_progress` + provider in `OrderDetail`, "To‘lov tekshirilmoqda", `payment_in_progress` message names the provider; the escape route is to finish or cancel in Payme or wait for the order to expire and reorder. |

### 11.2 Revision 2 (review round of 2026-10-26: money, auth and consistency lenses)

Findings are named by their review ids; overlapping findings share a row. The product-owner decisions of 2026-10-26
(M2 defaults recorded as provisional, Sign in with Apple, the Billz live-adapter scope, fake adapters allowed in
production) were applied in the same revision. No finding was rejected outright; the parts not adopted are explained
in their rows.

| # | Finding (section) | Decision |
|---|---|---|
| PAY-1 · SEC-01 · B1 | **Blocker** — with fake Click/Payme allowed in production, callbacks verify against keys required only in live mode and public placeholders, so anyone could sign a capture and get a free order; fake Click refunds "succeed" without moving money (C§2.13, C§6.14, plan §7) | Adopted. Under the production settings a fake provider accepts **no** callback (Click `-1`, Payme `-32504`, BTS 401; bounded `rejected:` row, checked before parsing); fake payment providers are omitted from `payment_providers` and refused by checkout/pay; a Click refund through a fake port becomes `manual_required`; production requires real `CLICK_SECRET_KEY`, `PAYME_KEY`, `BTS_WEBHOOK_SECRET` in every mode and refuses `change-me` and committed values. Tests: plan §3 (fake posture), sandbox Click 33 and Payme 27. **Not adopted:** a ≥32-byte floor for `CLICK_SECRET_KEY`/`PAYME_KEY` — Click and Payme issue those keys and their length is theirs (a floor could make a real key unusable); the fake-mode refusal is the protection, the placeholder check the second layer. Our own `BTS_WEBHOOK_SECRET` gets the ≥32 floor. |
| SEC-02 · M1 | Major — "production refuses fake" contradicts decision 4; no startup warning, no per-provider mode report, no fake-Eskiz posture; gates keyed on provider mode (C§2.14.1, C§6.14, plan §1/§7) | Adopted. The wording is gone everywhere (C§2.14.1, C§6.14, plan §7 and its `.env` comments, P0 tests; revision-1 row 16 annotated). One WARNING per fake provider at start (`CoreConfig.ready()`), `providers` in `health/?ready=1` and `integrations.providers` in admin `GET settings/` (P0 owns `core/api/health.py` and the admin serializer; admin §2.11/§2.15 updated). The fake-Eskiz demo/staging posture is documented (C§2.14.1). The fixed OTP, simulate, the scenario header, `POS_API_KEYS` seeding and the `*` nonce wildcard are gated on `DEV_FEATURES_ALLOWED` (a settings-module constant) and refused by production; the contract states that staging cannot sign up or pay without live providers. |
| SEC-03 · m4 | Major — placeholders pass "≥32 bytes"; the AES key placeholder decodes to 36 bytes (plan §7) | Adopted. `SMS_PAYLOAD_KEY` becomes `SECRETBOX_KEY` (it now also seals Apple refresh tokens): base64url of exactly 32 bytes; HMAC keys ≥32 characters; the `.env.example` key lines are commented, `local.py` ships valid dev defaults, and those plus every committed value are in `KNOWN_PLACEHOLDER_SECRETS`; `change-me` refused for every key. Tests in P0. |
| SEC-11 | Major — fakes keep every message (plain OTPs, campaign tokens) in process memory in production (C§2.14.1, C§6.4) | Adopted: FakeEskiz and FakeFcm record only under the local/test settings; under production they keep nothing and log one INFO line with ids. Tests in P0/P1. |
| M2 · SEC-24 | Major — `OAUTH_MODE=fake` in production lets anyone assert any subject (C§2.1.8) | Adopted: the customer social routes answer 404 while OAuth is fake under the production settings; the `*` wildcard is local/test only; the oauth mode is reported with the others. **For the lead (outside this contract):** the staff OAuth fake of M0b has the same exposure under production with `OAUTH_MODE=fake` (fake codes carry any email); not changed here. |
| PAY-2 | Major — Click's SHOP API allows no error answer to a Complete after withdrawal; the `true` branch is wasted and its message has no dead-letter rule (C§2.13.0, M2-P7, plan §5.1) | Adopted: `CLICK_REVERSES_ON_COMPLETE_ERROR`, `CLICK_STATUS_CHECK_DELAY_SECONDS`, the `payments.click_status_check` topic/handler and sandbox rows 22–24/27 are removed; accept and reverse is the only rule, citing the Click documentation; `M2-P7` is decided by the protocol. A reversal Click refuses ends as refund `failed` (attention). **Added:** B5's `…/resolve/` accepted only `unknown`/`manual_required`, so staff could not close a refund they paid out by hand; P2 extends it to `failed` Click refunds (admin §7 item 18). |
| PAY-3 | Major — the refund handler sends `click_trans_id` to Click's reversal/status endpoints, which are keyed by the payment id (C§2.13.0, plan §3) | Adopted: `RefundRow.provider_payment_ref` (= `click_paydoc_id`), used by `submit_refund`, `_resolve_after_failure` and `_reversed`; a payment without one gets a `manual_required` refund; sandbox row 21 asserts the id the fake port received; seeded Click payments get a paydoc id. |
| PAY-4 | Major — `FOR UPDATE` on the customer deadlocks with a capture's `Purchase` FK insert (`FOR KEY SHARE`) under `DELETE me/` and multi-order checkout (C§2.2.3, C§2.7.1, C§6.12) | Adopted: the customer-level lock is `select_for_update(no_key=True)` everywhere (checkout, `DELETE me/`, `delete_customer`, `start_customer_session`, booking), explained in C§2.7.1; race tests (P2: checkout vs a capture with two open orders; lead e2e: `DELETE me/` vs Click Complete / Payme Perform). |
| PAY-5 | Major — re-opening Click starts a new transaction that Prepare refused for 600 s, so a customer who left Click's page loses the order; 600 s exceeded the grace (C§2.7.2, C§2.13.1) | Adopted: a new Prepare supersedes an open Click attachment (old payment `abandoned`, a late Complete of it is a refused capture); in flight is 120 s (validated ≤ the grace) and blocks only a provider switch, cancel and delete; `actions.pay` stays true while Click is in flight; the expiry job skips orders with an **open** transaction attached before `R`, so a Complete inside the grace after the 120 s is honoured; the "re-opens the same provider page" sentence is fixed; sandbox rows 11/12 rewritten. Payme state 1 still blocks (`-31051` is a Payme checklist requirement and Payme debits only on Perform). |
| PAY-6 · M7 | Major — only `active_payment`/`display_payment` ignore late captures; lists, the filter and revenue do not; `already_paid` counts them (C§2.8, plan §3) | Adopted: one display rule for `display_payment`, `pick_display`, `display_status_subquery` and the customer lists, with a defined fallback for orders whose only payment is a late capture (`paid` → `refunded`, never `pending`); `already_paid` and `revenue_buckets` exclude late captures (admin §2.10 cross-reference); tests for Payme paid + Click late capture across list, filter and detail. |
| PAY-7 | Major — polling by `last_status_at` starves newer parcels; volatile payload fields append events; runs can overlap (C§2.13.3) | Adopted: single flight (`cache.add` lock), round robin from `IntegrationState(bts).data["poll_cursor"]` (no migration), `BTS_POLL_BATCH`/`BTS_POLL_MAX_SECONDS`, stable payload fields for polling and the webhook re-track; tests. |
| PAY-8 | Minor — contract call forms bind arguments wrongly against the plan signatures | Adopted: keyword `provider_txn_id=` and `case=` everywhere; the delayed payload that could not carry `case` is gone with PAY-2. |
| PAY-9 | Minor — a replay cannot insert a second inbox row under the same unique key | Adopted: duplicates are stored as `dup:{event_key}:{uuid4}` rows (outcome `duplicate`, no raw body); a conflicting Click fingerprint is checked first; sandbox rows 2/3/15/16/31 assert the row counts. |
| PAY-10 | Minor — `PAYME_ACCOUNT_FIELD` builds the URL but callbacks hard-code `order_id` | Adopted: callbacks read `account[PAYME_ACCOUNT_FIELD]` and answer `data` with that name (GetStatement too); Payme row 3 covers a custom name. |
| SEC-04 | Major — no Apple authorization code, so tokens cannot be revoked on deletion as Apple requires (C§2.1.8, C§2.2.3) | Adopted: `authorization_code` required for `apple` on `auth/social/` and `me/social/`, exchanged with an ES256 client secret (`APPLE_TEAM_ID`, `APPLE_KEY_ID`, `APPLE_PRIVATE_KEY_FILE`), refresh token sealed on `CustomerSocialIdentity.provider_refresh_token` (`accounts.0006`), revoked through `apple.revoke` on unlink, reset and deletion; fake records the revoke. Because Apple's code lives 5 minutes, an unknown identity's code is exchanged at `auth/social/` and the sealed token waits for completion in Redis (a flush → `social_ticket_invalid`). Server-to-server notifications: M3, default stated. |
| SEC-05 | Major — a bearer alone could link a new identity to a social-only account (C§2.2.8, M2-A10, C§6.16) | Adopted: `POST me/social/` needs the password, or a `reset_password` verification token when the account has none; the same re-authentication for `DELETE me/social/{provider}/`; M2-A10 and C§6.16 corrected; tests. |
| SEC-06 · M6 | Major — reset is the only password change and unlinks every identity, including the one the customer adds a password for (C§2.1, C§2.1.5, C§2.1.9) | Adopted: `POST me/password/` (**C**, **I**; current password or an SMS token) keeps the session, device key and identities; reset is recovery only; the `last_sign_in_method` path and C§2.1.9 point to it; index row 15 added (rows renumbered; package rows here updated); `M2-A17`. |
| SEC-07 | Major — reset and social completion notify only the new device; the old one is silently logged out (C§2.1.5, C§1.2, M2-N3) | Adopted: `start_customer_session` returns the deactivated devices' tokens (`farewell_tokens`), which get the visible `account_security` notice; a `password_changed` revocation answers its own 401 with a message. **Not adopted:** an SMS notice (M2-N3 re-evaluated and kept) — a reset or SMS-only link needs the SMS code, so whoever did it already receives that SIM's SMS; the previous device is the channel the owner still has. |
| SEC-08 | Major — an unconfirmed rotation keeps the old token valid without limit, against the spec; re-rotation forks (C§2.1.6) | Adopted: `CUSTOMER_REFRESH_UNCONFIRMED_SECONDS` (600, anchored to the rotation), dead-hash memory for tokens replaced by a re-rotation (their return revokes), grace cache rewritten on re-rotation; the spec wording is in the §0.2 edits. |
| SEC-09 · M5 | Major — `rot` in epoch seconds never equals a microsecond `rotated_at`; two clock reads; FAKE_NOW; cache staleness (C§1.2, C§2.1.6) | Adopted: integer `AuthSession.rotation` + `rotation_confirmed` (`accounts.0006`; revision-1 row 8 annotated), one instant in step 4, the "refresh with the new token confirms" sentence replaced, platform in the cache value; tests incl. pinned clock. |
| SEC-17 | Minor — writing the confirmed flag can overwrite a tombstone | Adopted: `auth:sid:{sid}` stays add-only and immutable; confirmation uses its own add-only key `auth:sidrot:{sid}:{rot}`; race test. |
| SEC-10 | Major — recycled numbers of never-deleted accounts expose the previous holder after an SMS recovery (C§2.1.5, C§6.17) | Adopted as provisional `M2-A16`: after a reset or an SMS-only link on an account inactive > `CUSTOMER_DORMANT_DAYS` (180) the horizon (`Customer.history_from`, `accounts.0006`) moves to now and name, image and addresses are scrubbed. Last activity includes purchases and orders, so an in-store customer activating the app is not scrubbed. |
| SEC-12 · M9 | Major/minor — the `delete_customer` change (key hashes, QR tokens) had no owner | Adopted: P0 owns the change (plus revocation through `revoke_customer_sessions`, `no_key` lock, SEC-21 scrub, `apple.revoke`); P1 keeps the `DELETE me/` tests. |
| SEC-13 | Minor — deleted/social-only phones answer login faster | Adopted: a dummy hash whenever no stored hash is compared, in the login service; timing-parity test over the four cases. |
| SEC-14 | Minor — Apple token details (no `jti`, nonce encoding, name, email claims) | Adopted: replay key `sha256(provider + ":" + raw token)`; server-generated ticket `jti`; Apple nonce `sha256(raw).hexdigest()` lowercase; the app keeps Apple's `fullName` until completion; per-provider required claims (email optional, `email_verified` ignored). |
| SEC-15 · M3 | Minor/major — decisions 1–2 not reflected (`APPLE_BUNDLE_IDS`, M2-A14, open-questions) | Adopted: renamed to `APPLE_BUNDLE_IDS` (empty: 404 live, fake adapter local/test); `M2-A14` decided; every M2 row recorded now in `open-questions.md` as a provisional default (decided 2026-10-26), A2/A3/A6/A7 and P1–P3 updated. |
| SEC-16 | Minor — undefined (provider, token type) pairs | Adopted: only four pairs; any other → 400 `social_token_invalid`; tests for Google and Apple access tokens. |
| SEC-18 | Minor — phone-keyed lockout before the device check; separate delete budget | Adopted: the device key is evaluated first and a known device has its own guard (`customer_login_device`); `DELETE me/`, `me/social/` and `me/password/` share `customer_login`; the remaining lockout DoS is a documented residual (C§6.3). |
| SEC-19 | Minor — the IP day cap hits CGNAT users with a misleading message; breaker outage undocumented | Adopted: own code `otp_network_limit` (uz/ru/en) and default 1000; the fid cap is documented as a buggy-client guard; the global-breaker outage is a C§6.3 residual with alert and runbook. **Deferred:** a per-/24 or per-ASN counter is noted for M3 (no metrics yet to size it). |
| SEC-20 | Minor — the Facebook user token goes into the `debug_token` URL | Adopted: POST form body (verify M3) with a documented fallback (request loggers silenced); the staff adapter is changed the same way (P1 owns `facebook.py`). |
| SEC-21 | Minor — a reactivated account shows the old `registered_at` and keeps the old consent | Adopted: `Me.registered_at` shows the horizon; `delete_customer` resets language and clears consent fields and `last_login_at` (audit keeps the history). |
| SEC-22 · M4 | Major/minor — `DevicePlatform` duplicates `OrderChannel` (spectacular error); `SocialProviderEnum` collision; Apple on staff routes; `refresh_reused` missing in the enum | Adopted: `OrderChannel` reused, `CustomerSocialProvider` named separately, the staff override kept; the OAuth registry keeps `PROVIDER_NAMES` (staff) and adds `CUSTOMER_PROVIDERS`, so the staff views need no change; `SessionRevokeReason` += `refresh_reused` (P0, admin §3.1); P0 runs the schema validation with every new enum. |
| SEC-23 · m8 | Minor — device-key format inconsistent; header listed for social sign-in | Adopted: unprefixed 43 base64url characters with a regex, malformed → unknown (never 400); `auth/social/` removed from the header row; verification-token and nonce examples unprefixed too. |
| B2 | **Blocker** — the inbox model admits `general` only for campaigns, so account-security rows need a migration | Adopted: `notifications.0003_inbox_account_source` (source `account`, CHECK extended), owned by P0; four migrations; dedupe key and `link` defined (C§2.12). |
| M8 | Major — `customer_note` dropped by `create_order` | Adopted: change (8) of `create_order`, passed by checkout, asserted in the snapshot test. |
| m1 | Minor — "marked failed (no retry)" is impossible in the outbox | Adopted: inbox `push_state=failed`, the handler returns normally (outbox `done`). |
| m2 | Minor — `delivery/calculate/` cannot live in `delivery` (layering) | Adopted: served by `commerce/api/customer/urls.py`; `delivery/api/customer` serves only regions. |
| m3 | Minor — codes without catalog keys; codes with two keys | Adopted: `field.otp_invalid`, `error.not_cancellable`, `error.invalid_transition` (uz/ru/en), `CustomerError(…, key=…)`, and the code → key table C§5.0. |
| m5 | Minor — contract deviates from the spec it says wins | Adopted as §0.2: the spec edits land before P0 starts (Payme `-31051`, the refresh rule, no firebase-admin, Apple, fake providers in production, accept-and-reverse). |
| m6 | Minor — `refresh_reused` and the M2 audit actions missing from the admin side | Adopted: P0 enum; admin §3.1 gains `refresh_reused` and §3.2 a cross-reference row (the cross-reference edits this revision may make). |
| m7 | Minor — ownership and implementability gaps | Adopted: `site_config/services.py` (P1), `notifications/topics.py` and `loyalty/seeds.py` (P3) in the Owns lists; POS seeding local/test only; the sample key has 32 random characters; the ledger page is built from the queryset. |
