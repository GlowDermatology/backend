# Glow Derma admin API v1 (M1 contract)

| | |
|---|---|
| Version | 1.0.0 (2026-10-24), M1 admin vertical |
| Implemented by | Django 6.1 / DRF 3.18 backend (Codex) and the Next.js admin data layer (`frontend/src/data/api/*`, Claude) |
| Source of truth | `docs/specs/2026-10-24-backend-foundation-design.md`. Where this file and the spec disagree, the spec wins and this file is fixed. |
| Enforced by | drf-spectacular schema (`openapi/schema.yaml`, `--validate --fail-on-warn`), backend contract tests, frontend mapper tests (order №10248, Malika's ledger) |

Examples use `jsonc`: `//` comments give types and nullability and are not part of the payload. Example values are illustrative unless a test vector says otherwise.

---

## 1. Conventions

### 1.1 Base, routing, network
- Base URL `/api/v1/admin/`. Every path ends with `/`. Paths in §2 are relative to the base unless they start with `/api/v1/`.
- Only the Next server calls this API (server to server). There is no CORS, no cookie auth and no CSRF.
- Requests to `/api/v1/admin/`, `/api/v1/schema/` and `/api/v1/docs/` get 403 `permission_denied` before authentication runs when their network peer is outside `ADMIN_API_ALLOWED_NETS`, or (in production) when they lack a matching `X-GD-Internal-Secret`. The network peer is REMOTE_ADDR, or the rightmost untrusted `X-Forwarded-For` hop when REMOTE_ADDR is a trusted proxy.
- OpenAPI lives at `/api/v1/schema/` and `/api/v1/docs/` (staff-only in production). operationId is `admin_<resource>_<action>`.

### 1.2 Request headers

| Header | Required | Rule |
|---|---|---|
| `Authorization: Bearer <access JWT>` | all routes except `auth/login/`, `auth/refresh/`, `auth/logout/` (these identify the session by the refresh token) and `POST auth/oauth/{provider}/` | HS256, `kid` header, claims `iss, aud="staff", sub, sid, iat, exp, jti` (all required). Access TTL 15 min. |
| `X-GD-Internal-Secret` | sent by Next on every call | Compared in constant time on bytes; an empty configured secret never matches. Required on admin, schema and docs routes in production (403 `permission_denied`). Elsewhere a mismatch is not an error; `X-GD-Client-IP` and `X-Request-ID` are then ignored. |
| `X-GD-Client-IP` | sent by Next on every call | End-user IP. Honoured only when the internal secret matches; otherwise the client IP is REMOTE_ADDR, or (only when REMOTE_ADDR is in `TRUSTED_PROXIES`) the rightmost `X-Forwarded-For` hop not in `TRUSTED_PROXIES`. Used for throttles and `AuditEntry.ip`. |
| `Accept-Language` | optional, Next sends `uz` | M1 messages are always Uzbek (`Content-Language: uz`). ru/en catalogs arrive in M2. i18n data fields (`texts`, `name`) always return all three languages. |
| `Idempotency-Key` | see §1.7 | UUID string (36 chars). |
| `X-Request-ID` | optional | Accepted only with a valid internal secret, `^[A-Za-z0-9-]{8,64}$`. Otherwise Django generates one. |
| `Content-Type` | on bodies | `application/json` or `multipart/form-data` (where stated). Anything else → 415. |

**401 codes.** The client reacts to each code as follows.

| `code` | Cause | Client reaction |
|---|---|---|
| `not_authenticated` | Header missing or malformed; bad signature, `aud` or `iss`; access JWT expired; unknown refresh token on `auth/refresh/` | Refresh once, then retry — except that **any** 401 from `auth/refresh/` or `auth/logout/` ends the session (clear cookies, go to login, no retry) |
| `SESSION_EXPIRED` | Session revoked (`logout`, `password_changed`, `deactivated`, `admin_revoked`), past `absolute_expires_at`, or staff inactive; any invalid or unknown refresh token on `auth/logout/` | Clear cookies and go to login |
| `SESSION_REPLACED` | Session revoked with reason `replaced` (reserved; used by the customer API) | Clear cookies and go to login |
| `invalid_credentials` | `auth/login/` only | Show the form error |

### 1.3 Response headers
`X-Request-ID` (always) · `Cache-Control: no-store` (always) · `Retry-After` (on 429 and 409 `request_in_progress`) · `Idempotent-Replayed: true` (on a replayed response) · `Content-Disposition` (on exports).

### 1.4 Values

| Kind | Format | Example |
|---|---|---|
| Keys | snake_case | `created_at` |
| IDs | integer; Category is addressed by `slug` (`^[a-z0-9-]{2,40}$`). External refs are strings. | `10248`, `"serums"`, `"S-88213"` |
| Money | integer so'm, never tiyin; signed where stated | `784400`, `-48600` |
| Datetime in responses | ISO 8601, seconds precision, `+05:00` | `"2026-10-23T14:02:00+05:00"` |
| Datetime in requests | ISO 8601 with an offset (`Z` or `±HH:MM`); naive values → `invalid` | `"2026-11-01T00:00:00+05:00"` |
| Date | `YYYY-MM-DD`, a Tashkent calendar day | `"2026-10-26"` |
| Time of day | `"HH:MM"`, Tashkent | `"09:00"` |
| Percent | Decimal **string**. Responses always have 2 dp. Requests allow ≤2 dp; more → `max_decimal_places`; a JSON number → `invalid` | `"1.00"`, `"0.57"`, req `"1.5"` |
| Ratio | JSON number in 0..1, ≤4 dp | `0.679` |
| Phone | E.164 `^\+998\d{9}$` | `"+998901234567"` |
| Enums | literals from §3, identical to the frontend unless §7 lists a mapping | `"awaiting_payment"` |
| i18n text | `{"uz": str, "ru": str, "en": str}`; `""` means not filled, never `null` | |
| Absent value | Key always present with `null`; lists are `[]`; keys are never omitted | |
| Query booleans | `true` / `false` (also `1` / `0`) | `?promo=true` |
| Media URLs | Absolute, built from `PUBLIC_MEDIA_BASE_URL`, never from the request Host | `"https://media.example.uz/products/1/9f2c.webp"` |

Fields owned by slices that have not landed yet return `0`, `null` or `[]`, never an error.

### 1.5 Pagination
```jsonc
{"count": 1284, "page": 1, "page_size": 10, "page_count": 129, "results": [ /* items */ ]}
```
- `page_count = max(1, ceil(count / page_size))`.
- `page` is clamped to `[1, page_count]` and is never a 404. A non-integer `page` is read as 1.
- A `page_size` outside the endpoint's allow-list → 400 `validation_error`, field `page_size`, code `invalid_choice`.

| Endpoint | `page_size` allow-list | Default |
|---|---|---|
| `orders/` | 6, 10, 25, 50 | 10 |
| `products/` | 8 | 8 |
| `staff/` | 20, 50, 100 | 50 |
| Every other paginated list (`customers/`, `categories/`, `banners/`, `notifications/`, `segments/`, `segments/{id}/members/`, `payments/`, `audit-log/`) | 20, 50 | 20 |

Collections embedded in a resource (customer ledger and purchases, order items, history, refunds, cashback) are never paginated.

### 1.6 Errors
```jsonc
{"code": "validation_error",                      // machine code, table below
 "message": "Maydonlarni tekshiring.",            // Uzbek; ActionResult.error = the first errors[] entry with field null, else this message
 "errors": [{"field": "texts.uz.name",            // dotted snake_case path | null (non-field)
             "code": "min_length",
             "message": "O‘zbekcha nomni kiriting (kamida 2 belgi)."}],
 "request_id": "6f1c2a0e9b8d4c37"}
```
- `errors` is `[]` for non-validation errors.
- DRF nested errors are flattened server-side. Paths start at the body root; list indexes are numeric segments (`tiers.1.percent`); multipart names map 1:1 (`audience.phone`); query errors use the param name (`page_size`).
- Order: one entry per (field, code), in validation order. The FE mapper keeps the first message per field.
- `message` is `"Maydonlarni tekshiring."` unless the endpoint lists a specific message.

| HTTP | `code` | When |
|---|---|---|
| 400 | `validation_error` | Body, query or field errors (`errors[]` is non-empty) |
| 400 | `bad_request` | Malformed request rejected by Django (`SuspiciousOperation`, `BadRequest`, too many fields/files) |
| 400 | `parse_error` | Body cannot be parsed (malformed JSON) |
| 406 | `not_acceptable` | `Accept` header cannot be satisfied |
| 400 | `idempotency_key_required` | Missing or non-UUID `Idempotency-Key` on an endpoint that requires it |
| 401 | `not_authenticated` · `SESSION_EXPIRED` · `SESSION_REPLACED` · `invalid_credentials` | §1.2 |
| 403 | `permission_denied` | Outside the allowed networks (a token with an audience other than `staff` fails decoding → 401 `not_authenticated`) |
| 404 | `not_found` | Unknown id, slug or route |
| 405 | `method_not_allowed` | |
| 409 | `invalid_transition` | A state machine refuses (order transition, appointment action, retry-shipment precondition) |
| 409 | `not_cancellable` | Order cancel refused (§2.2) |
| 409 | `conflict` | Other state conflicts (category has products, BTS message in flight, last image of an active product, …) |
| 409 | `request_in_progress` | The same `Idempotency-Key` is still executing; `Retry-After: 1` |
| 413 | `payload_too_large` | Body > 6 MiB |
| 415 | `unsupported_media_type` | Wrong `Content-Type` |
| 422 | `idempotency_key_reused` | Same key, different request fingerprint |
| 429 | `throttled` | `Retry-After` in seconds |
| 500 | `server_error` | Unexpected; quote `request_id` |
| 503 | `provider_unavailable` | A synchronous provider call failed transiently (BTS cancel). Safe to retry with the same key. |

Default top-level messages: `not_authenticated` "Avtorizatsiya talab qilinadi." · `SESSION_EXPIRED` "Sessiya tugadi. Qayta kiring." · `SESSION_REPLACED` "Boshqa qurilmadan kirildi." · `invalid_credentials` "Login yoki parol noto‘g‘ri." · `permission_denied` "Ruxsat yo‘q." · `not_found` "Topilmadi." · `request_in_progress` "So‘rov bajarilmoqda. Birozdan so‘ng qayta urinib ko‘ring." · `idempotency_key_reused` "Bu kalit boshqa so‘rov uchun ishlatilgan." · `idempotency_key_required` "Idempotency-Key sarlavhasi kerak." · `throttled` "Juda ko‘p urinish. {n} soniyadan so‘ng qayta urinib ko‘ring." · `payload_too_large` "Fayl juda katta." · `provider_unavailable` "Tashqi xizmat javob bermadi. Qayta urinib ko‘ring." · `server_error` "Kutilmagan xato."

**Generic field codes** (endpoint tables override the message where the FE already has one):

| `code` | Default message |
|---|---|
| `required`, `null` | Majburiy maydon. |
| `blank` | Maydonni to‘ldiring. |
| `invalid` | Noto‘g‘ri qiymat. |
| `invalid_choice` | Ruxsat etilmagan qiymat. |
| `min_length` / `max_length` | Kamida {n} ta belgi kiriting. / {n} belgidan oshmasin. |
| `min_value` / `max_value` | Qiymat {n} dan kam bo‘lmasin. / Qiymat {n} dan oshmasin. |
| `max_decimal_places` | Ko‘pi bilan 2 ta kasr raqam. |
| `unique` | Bu qiymat band. |
| `does_not_exist` | Topilmadi. |
| `read_only` | Bu maydonni o‘zgartirib bo‘lmaydi. |
| `file_too_large` | Rasm 5 MB dan oshmasin. |
| `unsupported_image` | Faqat JPEG, PNG yoki WebP rasm. |
| `max_items` | Ko‘pi bilan {n} ta. |
| `mismatch` | Ro‘yxat joriy elementlarga mos emas. |

### 1.7 Idempotency (`Idempotency-Key`)
- **Required** (400 `idempotency_key_required` without it):
  - `orders/{id}/cancel/`, `orders/{id}/transition/`, `orders/{id}/retry-shipment/`;
  - `customers/{id}/cashback-adjustments/`;
  - `cashback/policy-versions/`;
  - `POST notifications/`;
  - `appointments/{id}/{confirm|reject|attend}/`.
- **Optional:** honoured when sent on any other *authenticated* POST, PUT, PATCH or DELETE. `auth/*` endpoints ignore the header: their responses carry credentials or end sessions, so they are never stored.
- **Scope:** (audience `staff`, actor staff id, method, resolved path including ids, key). The same key on two different orders is two separate records.
- **Fingerprint:** SHA-256 over the canonical body. JSON is parsed, keys sorted and serialised compactly. Multipart uses the sorted non-file fields plus the SHA-256 of each file.
- **Storage:** written in the same transaction as the effect. Only 2xx responses are stored; an error rolls back and stores nothing, so a retry runs again. Kept for at least 24 h; after that a key counts as new.

| Situation | Response |
|---|---|
| First request | Runs; a 2xx stores (status, body) |
| Same key, same fingerprint, completed | The stored status and body, plus `Idempotent-Replayed: true` |
| Same key, different fingerprint | 422 `idempotency_key_reused` |
| Same key, first request still running (until its response body is final, including the after-commit rebuild of §2.8) | 409 `request_in_progress`, `Retry-After: 1` |

The FE creates one key per form/dialog instance (`crypto.randomUUID()`), reuses it on retries and across re-opens until a success (including a replay), and renews it after a success or after 422 `idempotency_key_reused`. A replay counts as success.

### 1.8 Exports (`?export=csv|xlsx`)
- **Where:** available on every list marked **E** in §2.0.
- **Rows:** same filters and ordering as the JSON list; `page` and `page_size` are ignored. More than 50 000 rows → 400, field `export`, code `export_too_large` "Juda ko‘p qator. Filtrni toraytiring.".
- **Response:** 200 with `Content-Disposition: attachment; filename="<name>-YYYYMMDD-HHMM.<ext>"` (Tashkent time).
- **CSV:** `text/csv; charset=utf-8`; UTF-8 **BOM**; `;` separator; CRLF; every cell double-quoted with `"` doubled.
- **XLSX:** one sheet named `<name>`; text cells typed `s`; money and counts numeric.
- **Formatting:** Uzbek headers; enums as the Uzbek labels in §5.8; datetimes as text `YYYY-MM-DD HH:MM` (Tashkent); phones as text in E.164; money as plain integers.
- **Formula safety:** a cell that comes from a string field and starts with `=` `+` `-` `@` TAB or CR gets a leading `'`. Integer cells are exempt, and so is a value that is exactly an E.164 phone (`+` then 7–15 digits, no leading zero): it cannot carry a formula and stays plain E.164 text.
- **Audit:** every export writes the AuditEntry `export.downloaded` `{resource, filters, row_count, format}`.

| Resource | `<name>` | Columns |
|---|---|---|
| orders | `buyurtmalar` | `№; Mijoz; Telefon; Sana; Summa (so‘m); To‘lov; Usul; Olish usuli; Holat; BTS` (BTS = tracking number or empty) |
| products | `mahsulotlar` | `ID; Billz ID; Billz nomi; Nomi; Kategoriya; Narx (so‘m); Qoldiq; Aksiya; Holat; Yangi` |
| customers | `mijozlar` | `ID; Ism; Telefon; Ro‘yxatdan o‘tgan; Jami xaridlar (so‘m); Cashback foizi; Balans (so‘m); Holat` |
| payments | `tolovlar` | `ID; Buyurtma №; Mijoz; Telefon; Usul; Holat; Summa (so‘m); Tranzaksiya; Yaratilgan; Tasdiqlangan; Qaytarish` |
| notifications | `bildirishnomalar` | `ID; Sana; Turi; Sarlavha; Auditoriya; Holat; Yuborildi; Yetkazildi` |
| segments, members | `guruhlar`, `guruh-azolari` | `ID; Nomi; A’zolar; Push qurilmalar` / customer columns |
| categories, banners | `kategoriyalar`, `bannerlar` | `Slug; Nomi; Tartib; Faol; Mahsulotlar` / `ID; Sarlavha; Havola; Faol; Boshlanishi; Tugashi; Tartib` |
| audit-log | `harakatlar-jurnali` | `ID; Sana; Xodim; Amal; Obyekt; Obyekt ID; IP; So‘rov ID` |

### 1.9 Throttles (Redis cache; defaults, configurable)

| Scope | Default |
|---|---|
| `auth/login/` and `auth/change-password/` per normalized email | 5/min, then exponential lockout (1 → 15 min) |
| `auth/oauth/{provider}/` per client IP | 30/min |
| `auth/login/` per client IP | 30/min |
| `auth/refresh/` per session | 120/min (keyed on the token hash: Next prefetch bursts) |
| `auth/refresh/` and `auth/logout/` per client IP | 600/min (loose ceiling) |
| `?export=` per staff | 10/min |
| Everything else per staff | 1200/min |

---

## 2. Endpoints

### 2.0 Index
**I** = `Idempotency-Key` required · **E** = `?export=csv|xlsx` · **P** = paginated.

| Method | Path | Flags | Success |
|---|---|---|---|
| POST | `auth/login/`, `auth/refresh/` | | 200 |
| POST | `auth/logout/` (body `refresh`, `scope`), `auth/change-password/` | | 204 |
| GET | `auth/me/` | | 200 |
| POST | `auth/oauth/{provider}/` (no bearer; M0b) | | 200 |
| POST | `auth/oauth/{provider}/link/` (M0b) | | 200 |
| DELETE | `auth/oauth/{provider}/` (M0b) | | 204 |
| GET | `orders/` | P E | 200 |
| GET | `orders/status-counts/`, `orders/{id}/` | | 200 |
| POST | `orders/{id}/cancel/`, `orders/{id}/transition/` | I | 200 |
| POST | `orders/{id}/retry-shipment/` | I | 202 |
| GET | `products/` | P E | 200 |
| GET | `products/stats/`, `products/{id}/` | | 200 |
| POST | `products/` | | 201 |
| PATCH | `products/{id}/` | | 200 |
| POST | `products/{id}/images/` | multipart | 201 |
| DELETE | `products/{id}/images/{image_id}/` | | 204 |
| PUT | `products/{id}/images/order/` | | 200 |
| POST | `integrations/billz/import/` | | 202 |
| GET | `jobs/{id}/` | | 200 |
| GET / POST | `categories/` | P E / – | 200 / 201 |
| GET / PATCH / DELETE | `categories/{slug}/` | | 200 / 200 / 204 |
| PUT | `categories/order/` | | 200 |
| GET / POST | `banners/` | P E / multipart | 200 / 201 |
| GET / PATCH / DELETE | `banners/{id}/` | PATCH multipart or JSON | 200 / 200 / 204 |
| PUT | `banners/order/` | | 200 |
| GET | `customers/` | P E | 200 |
| GET | `customers/{id}/` | | 200 |
| POST | `customers/{id}/cashback-adjustments/` | I | 201 |
| GET | `cashback/policy/` | | 200 |
| POST | `cashback/policy-versions/` | I | 201 |
| GET | `notifications/` | P E | 200 |
| GET | `notifications/meta/` | | 200 |
| POST | `notifications/` | I, multipart | 201 |
| GET / PUT | `notification-templates/{status}/` | | 200 |
| GET / POST | `segments/` | P E / – | 200 / 201 |
| GET / PATCH / DELETE | `segments/{id}/` | | 200 / 200 / 204 |
| GET / POST | `segments/{id}/members/` | P E / – | 200 |
| DELETE | `segments/{id}/members/{customer_id}/` | | 204 |
| GET | `appointments/?date=`, `appointments/pending-summary/`, `appointments/meta/` | | 200 |
| POST | `appointments/{id}/confirm/`, `…/reject/`, `…/attend/` | I | 200 |
| POST | `appointments/` (admin booking, T10) | I optional | 201 |
| POST | `appointments/{id}/reschedule/`, `appointments/{id}/cancel/` (T10) | I optional | 200 |
| GET | `dashboard/`, `nav-counts/` | | 200 |
| GET / PATCH | `settings/` | | 200 |
| GET | `payments/`, `audit-log/` | P E | 200 |
| POST | `payments/{id}/refunds/{refund_id}/retry/`, `…/resolve/` (§2.12) | I optional | 200 |
| GET | `staff/` (§2.16) | P | 200 |
| GET | `/api/v1/health/` | no auth | 200 / 503 |

Shared shapes: `Actor` = `{"id": int, "display_name": str}`, where `display_name` is the staff full name, else the email. `null` actor means system. `CustomerRef` = `{"id": int, "name": str, "phone": str}`; `name` is `""` once the customer is deleted (scrubbed). Both are single OpenAPI components (`Actor`, `CustomerRef`) used by every module (orders, payments, appointments).

### 2.1 Auth
**`POST auth/login/`** (no bearer). Body `{"email": str (email, ≤254; trimmed and lower-cased before lookup), "password": str 1..128}`.
```jsonc
{"access": "eyJhbGciOiJIUzI1NiIsImtpZCI6...",          // JWT, 15 min
 "access_expires_at": "2026-10-24T12:15:00+05:00",
 "refresh": "Qm9n...43 chars",                          // opaque 256-bit base64url; stored as SHA-256; non-rotating
 "refresh_expires_at": "2026-10-25T00:00:00+05:00",     // absolute 12 h from login
 "user": { /* Me, below */ }}
```
- 401 `invalid_credentials`: unknown user, wrong password or inactive staff all give the same response and timing.
- 429 `throttled`.
- Audit `auth.login_succeeded` / `auth.login_failed` (`{email, method: "password"}`).

**`POST auth/refresh/`** (no bearer). Body `{"refresh": str}`.
- 200 `{"access", "access_expires_at", "refresh_expires_at"}`. The refresh token is unchanged. `access_expires_at ≤ refresh_expires_at` always (the access JWT `exp` is capped at the session's absolute expiry).
- 401 `SESSION_EXPIRED`: revoked, past absolute expiry, or staff inactive.
- 401 `not_authenticated`: unknown token.

**`POST auth/logout/`**. Body `{"refresh": str, "scope": "current"|"others"|"all"}`; `scope` defaults to `current`. The refresh token identifies the caller's session, so no bearer is needed (logout still works after the access JWT expired). → 204.

| `scope` | Revokes (reason `logout`) |
|---|---|
| `current` | The session that owns the refresh token |
| `others` | Every other session of that staff member; the caller's session stays active |
| `all` | Every session of that staff member, including the caller's |

- An invalid, unknown or already revoked refresh token → 401 `SESSION_EXPIRED`. An unknown `scope` → 400 `invalid_choice`.
- Each revoked session's `auth:sid:{sid}` cache entry is overwritten with a `revoked` tombstone on commit.
- Audit `auth.logout` `{scope, revoked_count}`.

**`POST auth/oauth/{provider}/`** (no bearer; milestone **M0b**). `provider` ∈ `google`, `facebook`.
Staff social sign-in, **allow-list only**: it never creates a staff account. The Next server runs the browser redirect (state + PKCE kept in an HttpOnly, SameSite=Lax, `Path=/admin/auth` cookie for ≤10 min) and posts the callback result here; Django holds the client secrets and does the code exchange.

Body `{"code": str, "code_verifier": str (43..128), "redirect_uri": str (must equal the configured value), "nonce": str (Google only)}`.

| Provider | Server-side verification |
|---|---|
| `google` | Exchange `code` at `https://oauth2.googleapis.com/token` (client id + secret + verifier). Verify the ID token against Google's JWKS: `iss` ∈ {`https://accounts.google.com`, `accounts.google.com`}, `aud` = our client id, `exp`, `nonce` matches, `email_verified == true`. Optional `GOOGLE_ALLOWED_HD` restricts the Workspace domain. |
| `facebook` | Exchange `code` at `https://graph.facebook.com/v{N}/oauth/access_token` (+ verifier). Check the token with `debug_token` (`app_id` = ours, `is_valid`), then `GET /me?fields=id,email` with `appsecret_proof`. The email is informational only (stored as `email_at_link`). |

Matching: an existing `SocialIdentity(provider, subject)` whose staff user is active → that user. Otherwise → 401 `social_account_not_allowed` "Bu hisob administrator sifatida ro‘yxatdan o‘tmagan." (same response whether or not the email exists). Identities are created only by **`POST auth/oauth/{provider}/link/`** (bearer + the same body; audit `auth.social_linked`), i.e. by a staff member already signed in with email + password. Optional Google-only first-use auto-link (`STAFF_GOOGLE_AUTOLINK`, default **off**) applies only when Google is authoritative for the address: `email_verified == true` and either the email ends with `@gmail.com` or the ID token's `hd` equals the email's domain (and matches `GOOGLE_ALLOWED_HD` when set). Facebook never auto-links.
- 200: same body as `auth/login/`. Audit `auth.login_succeeded` (`{email, method: provider}`) / `auth.login_failed`.
- 400 `oauth_exchange_failed` (bad/expired code, verifier mismatch), 401 `social_account_not_allowed`, 429 `throttled`, 503 `provider_unavailable`.
- Field errors: `nonce` `required` (Google only); `redirect_uri` `invalid` when it differs from the configured `OAUTH_REDIRECT_URI`; `code_verifier` 43..128. An unknown `provider` → 404. The nonce is compared byte-wise against the ID token's `nonce` claim (no Unicode normalisation), so the client sends exactly the string it put in the authorize URL.
- `link/` conflicts: 409 `conflict` "Bu provayder allaqachon bog‘langan." when the caller already has an identity for that provider, "Bu ijtimoiy hisob allaqachon bog‘langan." when the subject belongs to another staff member. `DELETE` without a linked identity → 404.
- Policy: an identity linked to an inactive staff member is refused like an unknown one; auto-link never creates a second Google identity for a staff member who already has one.

**`DELETE auth/oauth/{provider}/`** (bearer; M0b) → 204. Unlinks the caller's identity for that provider. Audit `auth.social_unlinked`.

**`POST auth/change-password/`**. Body `{"current_password": str, "new_password": str}` → 204.
- Revokes the user's **other** sessions (`password_changed`). Audit `auth.password_changed`.
- Field errors:
  - `current_password` `password_incorrect` "Joriy parol noto‘g‘ri.".
  - `new_password`: Django validators, min length 12 (`password_too_short` "Kamida 12 ta belgi.", `password_too_common` "Juda oddiy parol.", `password_entirely_numeric` "Faqat raqamlardan iborat bo‘lmasin.", `password_too_similar` "Login bilan juda o‘xshash."), plus `password_unchanged` "Yangi parol eskisidan farq qilsin.".

**`GET auth/me/`** → `Me`:
```jsonc
{"id": 1, "email": "admin@glowderma.uz", "display_name": "Administrator", "is_superuser": false,
 "linked_providers": ["google"],                       // social identities linked to this staff account
 "session_expires_at": "2026-10-25T00:00:00+05:00"}
```

### 2.2 Orders
**`GET orders/`** → `Page<OrderSummary>`

| Param | Values | Default | Rule |
|---|---|---|---|
| `status` | OrderStatus | – | |
| `search` | str ≤64, trimmed, case-insensitive | – | Matches if any holds: order id contains the term without a leading `№`; the term has ≥3 digits and the customer phone contains them; the customer name contains the term |
| `fulfilment` | `delivery`, `pickup` | – | |
| `payment` | PaymentStatus | – | |
| `period` | `today`, `week`, `month`, `all` | `all` | `today` = current Tashkent day; `week` = rolling `(now−7×24h, now]`; `month` = Tashkent calendar month to date |
| `date_from`, `date_to` | date | – | Inclusive Tashkent days on `created_at`, ANDed with `period`; `date_from > date_to` → 400, field `date_from`, code `invalid` |
| `customer` | int | – | Customer id |
| `ordering` | `-created_at`, `created_at` | `-created_at` | Ties broken by `id` in the same direction |
| `page`, `page_size`, `export` | §1.5, §1.8 | | |

```jsonc
// OrderSummary
{"id": 10248,
 "customer": {"id": 5, "name": "Malika Karimova", "phone": "+998901234567"},
 "created_at": "2026-10-23T14:02:00+05:00",
 "total": 784400,                         // items_subtotal + delivery_fee − cashback_used
 "status": "handed_to_courier",
 "payment": {"status": "paid", "provider": "click"},
 "fulfilment": "delivery",
 "shipment": {"state": "created", "tracking_number": "48213907", "tariff": 25000,
              "updated_at": "2026-10-23T16:40:00+05:00", "via": "webhook"},
 "cancellation": null}                    // | {"reason": CancellationReason, "note": str | null}
```

| `shipment.state` | Other keys |
|---|---|
| `not_required` | none (pickup) |
| `not_created` | `pending: bool` |
| `failed` | `last_attempt_at: datetime`, `attempts: int`, `pending: bool` |
| `created` | `tracking_number: str`, `tariff: int`, `updated_at: datetime`, `via: "webhook"\|"polling"` |

`pending` is true while a BTS create message for the order is queued or in flight.

**`GET orders/status-counts/`**: the same params as `orders/` except `status`, `ordering`, `page`, `page_size` and `export`.
```jsonc
{"all": 1284, "awaiting_payment": 3, "processing": 41, "handed_to_courier": 18, "delivered": 1150, "cancelled": 72}
```

**`GET orders/{id}/`** → `Order` (OrderSummary keys plus the ones below). 404 `not_found`.
```jsonc
{ /* …OrderSummary… */
 "channel": "ios",
 "address": "Toshkent, Chilonzor tumani, 9-kvartal, 12-uy, 45-xonadon",   // null for pickup
 "items": [                                          // snapshots taken at checkout
   {"product_id": 4, "name": "Seramidli namlantiruvchi krem", "billz_id": "4607012345",
    "image_url": "https://media.example.uz/products/4/a1b2.webp",          // null if the product had no photo
    "quantity": 1, "unit_price": 312000, "promo": false},
   {"product_id": 2, "name": "Quyoshdan himoya kremi SPF 50", "billz_id": "4607012401",
    "image_url": null, "quantity": 1, "unit_price": 240000, "promo": true}],  // + foam 2×128000
 "items_subtotal": 808000, "promo_subtotal": 240000,
 "delivery_fee": 25000, "cashback_used": 48600,
 "payment": {"status": "paid", "provider": "click", "amount": 784400,
             "transaction_id": "CLK-7741920385",                          // null until a provider transaction exists
             "confirmed_at": "2026-10-23T14:05:00+05:00"},                // null until paid
 "refunds": [],                                     // Refund[], newest first
 "cashback_hold": null,                             // | {"amount": int, "state": "active"|"captured"|"released", "expires_at": datetime}
 "cashback": [                                      // this order's ledger entries, id ascending
   {"ledger_entry_id": 120, "kind": "debit", "amount": -48600, "at": "2026-10-23T14:05:00+05:00",
    "base": null, "percent": null, "reverses_id": null},
   {"ledger_entry_id": 121, "kind": "credit", "amount": 5680, "at": "2026-10-23T14:05:00+05:00",
    "base": 568000, "percent": "1.00", "reverses_id": null}],
 "history": [ /* OrderEvent[], newest first (at desc, id desc); see §4 */ ],
 "available_actions": {"transitions": ["delivered"],   // handed_to_courier (delivery) → ["delivered"]
                       "cancel": true, "retry_shipment": false}}
```
```jsonc
// Refund
{"id": 7, "state": "requested", "provider": "click", "amount": 784400,
 "requested_at": "2026-10-24T12:01:00+05:00", "updated_at": "2026-10-24T12:01:00+05:00",
 "completed_at": null}                              // set on succeeded
```
- `payment.status` stays `paid` while a refund is not `succeeded`. It becomes `refunded` when refunds for the full amount succeed.
- `cashback[].kind` is `debit`, `credit` or `reversal` (adjustments are never linked to orders). `base` and `percent` are set on credits only.
- `available_actions`:
  - `transitions` = the manual transitions (`processing` → `handed_to_courier` for delivery, `delivered` for pickup; `handed_to_courier` → `delivered`).
  - `cancel` = status is `awaiting_payment`, `processing` or `handed_to_courier`.
  - `retry_shipment` = delivery ∧ `processing` ∧ shipment `failed` ∧ ¬`pending`.

**`POST orders/{id}/cancel/`** (I). Body `{"reason": str}`, trimmed, 3..500.

| Current status | Result |
|---|---|
| `awaiting_payment` | Cancelled. Reservations and the cashback hold are released; a `pending` payment becomes `failed`. |
| `processing` | Cancelled. If paid, a Refund is created. Reversals are posted. If a BTS shipment exists, BTS cancel runs first (synchronously) and must succeed. |
| `handed_to_courier` | Allowed only if the synchronous BTS cancel succeeds (BTS status 100); otherwise 409 `not_cancellable` |
| `delivered`, `cancelled` | 409 `not_cancellable` |

- **Effects** (one transaction; lock order Order → Payment → LoyaltyAccount):
  - `status=cancelled`; `cancellation={"reason":"admin","note":reason}`.
  - Refund: Click `requested`, submitted through the outbox; Payme `manual_required`. `payment.status` stays `paid`.
  - One `reversal` ledger entry per unreversed debit and credit of the order (unique `reverses_id`).
  - Push through the outbox (template `cancelled`).
  - Events `cancelled` and `refund_requested` (§4); audit `order.cancelled`.
- **Response:** 200 `Order`.
- **Errors:**
  - 400 `reason` (`min_length` "Kamida 3 ta belgi kiriting.", `max_length` "500 belgidan oshmasin."), top-level message "Sababni kiriting.".
  - 409 `not_cancellable` "Bu buyurtmani bekor qilib bo‘lmaydi." (BTS refusal: "BTS buyurtmani bekor qilmadi: posilka kuryerda.").
  - 503 `provider_unavailable`.

**`POST orders/{id}/transition/`** (I). Body `{"status": OrderStatus}`.
- The target must be in `available_actions.transitions`, else 409 `invalid_transition` "Bu holatga o‘tib bo‘lmaydi.". A non-OrderStatus value → 400 `invalid_choice`.
- Effects: status set; event `status_changed`; push through the outbox (status template); audit `order.status_changed`.
- 200 `Order`.

**`POST orders/{id}/retry-shipment/`** (I). Empty body.
- Precondition: delivery ∧ `processing` ∧ shipment `failed`. Otherwise 409 `invalid_transition` "BTS ga qayta yuborib bo‘lmaydi.".
- While `pending` → 409 `conflict` "BTS so‘rovi allaqachon yuborilmoqda.".
- Effect: an outbox BTS create; the handler looks the order up by `clientId` before creating. Audit `order.shipment_retry_requested`.
- 202 `Order` with `shipment.pending=true`. The outcome arrives later as `shipment_created` or `shipment_failed` with `manual=true`.

**Shared outcomes of the order actions** (`cancel`, `transition`, `retry-shipment`; the busy-order outcome also covers the refund `retry` / `resolve` of §2.12):
- **Busy order.** Every action locks the order row first and waits at most `ADMIN_LOCK_WAIT` (5 s; a BTS create holds the row across its bounded calls). When the row stays locked → 409 `conflict` "Buyurtma boshqa amal bilan band. Birozdan so‘ng qayta urinib ko‘ring.". Nothing is written; retry with the same `Idempotency-Key`.
- **Purchase conflict** (`cancel`, `transition`, `retry-shipment` only; the refund operations never touch the loyalty purchase). When the order's loyalty purchase belongs to another customer (data inconsistency surfaced by `loyalty`) → 409 `conflict` "Buyurtma xaridi boshqa mijozga tegishli. Ma’lumotlarni tekshiring.". Nothing is written.

### 2.3 Products and Billz
```jsonc
// Product
{"id": 1, "billz_id": "4607012301", "billz_name": "HYALURONIC SERUM 30ML", "volume": "30 ml",   // billz_* and volume: Billz-owned, read-only
 "category": "serums",                       // slug | null
 "price": 185000, "billz_price": 185000,     // price = admin (authoritative); billz_price read-only | null
 "stock": 12,                                // Billz-owned, read-only
 "promo": false, "status": "active", "is_new_from_billz": false,
 "texts": {"uz": {"name": "Gialuron kislotali serum", "description": "…", "ingredients": "…", "usage": "…"},
           "ru": {"name": "…", "description": "…", "ingredients": "…", "usage": "…"},
           "en": {"name": "", "description": "", "ingredients": "", "usage": ""}},
 "main_image_url": "https://media.example.uz/products/1/9f2c.webp",   // = images[0].url | null
 "images": [{"id": 31, "url": "https://media.example.uz/products/1/9f2c.webp", "position": 0}],
 "last_change": {"at": "2026-10-22T18:04:00+05:00", "actor": {"id": 1, "display_name": "Administrator"},
                 "kind": "price_changed", "old_price": 175000, "new_price": 185000},   // null if never edited
 "created_at": "2026-06-01T09:00:00+05:00", "updated_at": "2026-10-22T18:04:00+05:00"}
```

**`GET products/`** → `Page<Product>`

| Param | Values | Rule |
|---|---|---|
| `search` | str ≤64 (longer input is trimmed to 64) | Case-insensitive substring over `texts.{uz,ru,en}.name`, `billz_name` and `billz_id` (pg_trgm GIN). An ASCII-digit term of ≤18 digits also matches `id` exactly. |
| `category` | slug or `none` | `none` = no category (hence `none` is a reserved slug, §2.4); an unknown slug → 400 `invalid_choice` |
| `status` | `active`, `hidden` | |
| `promo`, `out_of_stock`, `is_new` | bool | `out_of_stock` = `stock ≤ 0`; `is_new` = `is_new_from_billz` |
| `ordering` | `default`, `name`, `-name` | `default` = `last_change.at` desc (falling back to `created_at`), then `id` desc. `name` = display name (uz name, else `billz_name`), case-insensitive ICU collation. |
| `page`, `page_size` (8), `export` | | |

**`GET products/stats/`**
```jsonc
{"total": 248, "active": 201, "hidden": 47, "new_from_billz": 6, "out_of_stock": 9,
 "billz": {"connected": true,                               // adapter configured (fake counts) ∧ last import job not failed
           "last_sync_at": "2026-10-24T11:58:00+05:00"}}    // null = never synced
```

**`POST products/`** (JSON) → 201 `Product`. **`PATCH products/{id}/`** (JSON, every field optional; `texts` merges at leaf level) → 200 `Product`. There is no PUT.

| Field | Rule and message |
|---|---|
| `billz_id` | Required on create. `^\d{6,14}$` (`invalid` "Billz ID yoki shtrix-kod 6–14 ta raqamdan iborat bo‘lsin."); `unique` "Bu Billz ID bilan mahsulot allaqachon bor."; must exist in Billz (`billz_not_found` "Billz’da bunday mahsulot topilmadi."). Setting or changing it refreshes `billz_name`, `volume`, `stock` and `billz_price`. |
| `texts.uz.name` | Trimmed 2..120 (`min_length` "O‘zbekcha nomni kiriting (kamida 2 belgi).", `max_length` "120 belgidan oshmasin.") |
| `texts.*.description`, `texts.*.ingredients` / `texts.*.usage` / `texts.ru.name`, `texts.en.name` | ≤2000 / ≤1000 / ≤120 ("{n} belgidan oshmasin."). Missing on create → `""`. |
| `price` | int 1000..100000000 (`min_value` "Narx kamida 1 000 so‘m bo‘lsin.", `max_value` "Narx juda katta.") |
| `category` | slug or `null`; an unknown slug → `does_not_exist` |
| `promo` | bool, default `false` |
| `status` | `active` or `hidden`, default `hidden` |
| Active rule | Whenever the resulting status is `active`: `category` must be set (`required_for_active` "Faol mahsulot uchun kategoriyani tanlang."), the product needs ≥1 image (field `images`, `required_for_active` "Faol mahsulotda kamida bitta rasm bo‘lsin.") and `price ≥ 1000` (field `price`, `min_value` "Narx kamida 1 000 so‘m bo‘lsin." — imported products may carry `price = 0` while hidden). A create with `active` therefore always fails; the FE creates `hidden`. |

- Every successful create or PATCH sets `is_new_from_billz=false`.
- `last_change.kind` is chosen by priority: `created` > `price_changed` > `activated`/`hidden` > `promo_added`/`promo_removed` > `updated`. Image calls do not change `last_change`.
- Audit `product.created` / `product.updated` (field diff).

- PATCH request shape: every field optional and `texts` merged leaf by leaf — any subset of languages and leaves (OpenAPI `PatchedProductPatchRequest`).

**`GET products/{id}/`** → `Product`; 404.

**`POST products/{id}/images/`** (multipart, one part `file`) → 201 `{"id": 32, "url": "…", "position": 1}`.
- Limits: ≤5 MiB (field `file`, `file_too_large`); must decode in Pillow as JPEG, PNG or WebP (field `file`, `unsupported_image`; a missing part → `file` `required`); ≤8 images per product (field `images`, `max_items` "Ko‘pi bilan 8 ta rasm.").
- The file is re-encoded in the same format, EXIF stripped, longest side ≤2400 px, and appended last.
- Audit `product.image_added`.

**`DELETE products/{id}/images/{image_id}/`** → 204; positions are compacted. 409 `conflict` "Faol mahsulotda kamida bitta rasm bo‘lsin." for the last image of an active product. 404 if the image belongs to another product.

**`PUT products/{id}/images/order/`**. Body `{"image_ids": [32, 31]}`, exactly the current set (`mismatch`; `[]` is the exact set of a product without images → 200) → 200 `{"images": [ProductImage…]}`. Audit `product.images_reordered`.

**`POST integrations/billz/import/`**. Body `{}` or `{"mode": "delta"|"full"}` (default `delta`) → 202 `Job`.
- Single-flight: if a `billz_import` job is queued or running, that job is returned. A `failed` job never blocks a new import.
- Fake adapter in M1 (`BILLZ_MODE`, default `INTEGRATIONS_MODE`). Products are upserted by `billz_id`:
  - new → `hidden` with `is_new_from_billz=true`;
  - existing → only the Billz-owned fields change (`billz_name`, `volume`, `billz_price`, stock); admin-owned fields and `last_change` never move.
- `Job.result` counts: `created` (new rows), `updated` (an existing product whose Billz-owned field or stock changed), `unchanged` (a `synced_at` refresh alone), `new_from_billz` (= `created`). `Job.progress` is `{"done", "total"}`.
- Sets `billz.last_sync_at`. Audit `billz.import_started` (actor = the staff who started it) / `billz.import_finished` (written by the worker, or by the stale-job reaper with `error.code = "worker_lost"`).
- Routes: the views live in `catalog` (an admin view sits above `integrations` in the layering); the paths are unchanged.

### 2.4 Categories (addressed by slug)
```jsonc
// Category
{"slug": "serums", "name": {"uz": "Serumlar", "ru": "Сыворотки", "en": "Serums"}, "position": 0,
 "is_active": true, "product_count": 42, "created_at": "…", "updated_at": "…"}
```
- `GET categories/` (P E; ordered by `position`; filter `is_active`).
- `POST categories/` → 201:
  - `slug`: `^[a-z0-9-]{2,40}$`, unique, not `order` or `none` (reserved → `slug` `invalid` "Bu slug band."), immutable afterwards; a lost unique race is reported as `slug` `unique` too;
  - `name.uz` 2..60 required; `name.ru`, `name.en` ≤60, optional (default `""`);
  - `is_active` default `true`;
  - appended last.
- `GET`/`PATCH categories/{slug}/`. Sending `slug` in PATCH → `read_only`.
- `DELETE categories/{slug}/` → 204, or 409 `conflict` "Kategoriyada mahsulotlar bor." when `product_count > 0`.
- `PUT categories/order/` `{"slugs": [...]}` (exact set, `mismatch`; `[]` only when there are no categories) → 200 `{"results": [Category…]}`.
- Audit `category.created|updated|deleted|reordered`.

### 2.5 Banners (API only in M1)
```jsonc
// Banner
{"id": 3, "title": {"uz": "Kuzgi chegirmalar", "ru": "", "en": ""},
 "image_url": "https://media.example.uz/banners/3/77aa.webp",
 "link": {"kind": "product", "product_id": 4, "category": null},   // kind none|product|category
 "is_active": true, "starts_at": null, "ends_at": null, "position": 0, "created_at": "…", "updated_at": "…"}
```
- `GET banners/` (P E; ordered by `position`; filter `is_active`).
- `POST banners/` (multipart): `image` (required; product-image rules), `title.uz` (1..80, required), `title.ru`, `title.en` (≤80, optional, default `""`), `link.kind`, `link.product_id`, `link.category`, `is_active`, `starts_at`, `ends_at` → 201.
  - Form encoding: nested keys are dotted form fields (`title.uz`, `link.kind`, `link.product_id`, `link.category`); `""` means `null` for the nullable keys (`link.product_id`, `link.category`, `starts_at`, `ends_at`); booleans are `true`/`false`. Only `multipart/form-data` (and JSON for PATCH) is accepted — never `application/x-www-form-urlencoded`.
  - `link.product_id` must be an active product, `link.category` an existing slug (`does_not_exist`).
  - `ends_at` must be after `starts_at` (`invalid`).
- `PATCH banners/{id}/` takes multipart or JSON; `image` is optional; `title` merges leaf by leaf (every leaf optional). An unchanged `link.product_id` is not re-checked against the active products, so a banner whose product was hidden later can still be edited or switched off; a different product must be active.
- `DELETE banners/{id}/` → 204.
- `PUT banners/order/` `{"ids": [...]}` (exact set, `mismatch`; `[]` only when there are no banners) → 200 `{"results": [Banner…]}`.
- Audit `banner.created|updated|deleted|reordered`.

### 2.6 Customers
**`GET customers/`** → `Page<CustomerListItem>`

| Param | Values | Rule |
|---|---|---|
| `search` | str ≤64 | Name contains the term, or (≥3 digits) the phone contains them |
| `status` | `active`, `deleted` | |
| `segment` | int | Segment membership (§2.8) |
| `ordering` | `-registered_at` (default), `registered_at`, `name`, `-name`, `lifetime_total`, `-lifetime_total`, `balance`, `-balance` | Ties by `id` |

```jsonc
// CustomerListItem
{"id": 5, "name": "Malika Karimova", "phone": "+998901234567", "status": "active",
 "registered_at": "2025-03-12T10:20:00+05:00", "lifetime_total": 13580000,
 "percent": "1.00",                      // active-policy rate for lifetime_total | null
 "balance": 48600, "online_orders_count": 12}
```

**`GET customers/{id}/`** → `CustomerCard`; 404.
```jsonc
{"id": 5, "name": "Malika Karimova", "phone": "+998901234567", "status": "active",
 "registered_at": "2025-03-12T10:20:00+05:00", "deleted_at": null,
 "language": "uz", "address": "Toshkent, Chilonzor, 9-kvartal",            // null if none
 "totals": {"store": 9940000, "online": 3640000, "lifetime": 13580000},    // non-reversed purchases
 "online_orders": {"count": 12, "last_order_id": 10248, "last_order_at": "2026-10-23T14:02:00+05:00"},  // ids/time null if none
 "tier": {"policy_version": 3, "percent": "1.00",                          // null if no tier covers lifetime
          "next": {"percent": "2.00", "from": 20000000, "remaining": 6420000, "progress": 0.679}},  // null if no next tier
 "balance": 48600, "held": 0, "available": 48600,                         // available = balance − active holds
 "groups": [{"id": 1, "name": "Doimiy mijozlar"}, {"id": 2, "name": "10 mln+ xarid"}],
 "upcoming_appointments": 1,                                               // confirmed|requested with start ≥ now
 "device": {"model": "iPhone 14", "os": "iOS 18.1", "app_version": "1.0.0",
            "last_active_at": "2026-10-24T09:12:00+05:00", "push_enabled": true},   // null if none
 "ledger": [ /* LedgerEntry[], full history, newest first (at desc, id desc) */ ],
 "purchases": [ /* Purchase[], full history, newest first */ ]}
```
```jsonc
// LedgerEntry
{"id": 112, "at": "2026-10-21T11:47:00+05:00", "kind": "credit", "amount": 2310, "balance_after": 48600,
 "source_type": "billz_sale", "source_ref": "S-88213",   // order → order number as string; billz_sale → Billz document no.; admin → null
 "order_id": null,                                        // int when source_type = order
 "base": 231000, "percent": "1.00", "promo_excluded": false, "policy_version": 3,   // credits only, else null/false
 "reverses_id": null,                                     // reversals only
 "reason": null, "actor": null}                           // adjustments only: reason str, actor Actor
// Purchase
{"id": 41, "at": "2026-10-23T14:05:00+05:00", "source": "online", "order_id": 10248, "billz_sale_ref": null,
 "amount": 808000, "promo_amount": 240000, "cashback": 5680, "policy_version": 3,
 "reversed": false}                                       // true once the order was cancelled/refunded
```

| `kind` | `source_type` | Populated |
|---|---|---|
| `credit` | `order` or `billz_sale` | `base`, `percent`, `policy_version`, `promo_excluded` (stored at credit time: promo items were excluded from the base), `order_id` or `source_ref` |
| `debit` | `order` | `order_id`, `source_ref` |
| `reversal` | `order` (or `billz_sale`) | `reverses_id`, `order_id` / `source_ref` |
| `adjustment` | `admin` | `reason`, `actor` |

**`POST customers/{id}/cashback-adjustments/`** (I). Body `{"direction": "add"|"subtract", "amount": int, "reason": str}`.

| Check, in order | Field and message | Top-level `message` |
|---|---|---|
| `amount` not an int in 1..10000000 | `amount` `invalid`/`min_value`/`max_value` "Summani kiriting." | "Summani kiriting." |
| `reason` trimmed outside 3..300 | `reason` `min_length` "Kamida 3 ta belgi kiriting." / `max_length` "300 belgidan oshmasin." | "Sababni kiriting." |
| `subtract` and `amount > available` (checked under the LoyaltyAccount lock) | `amount` `insufficient_balance` "Balansdan ko‘p ayirib bo‘lmaydi." | "Balans manfiy bo‘lib qoladi." |

- Allowed for deleted customers too: the balance is kept for reactivation.
- Effect: `loyalty.services.post_entry()` appends an `adjustment`. Audit `cashback.adjusted` `{direction, amount, reason, balance_after}`.
```jsonc
// 201
{"entry": { /* LedgerEntry, kind "adjustment", source_type "admin" */ }, "balance": 51550, "available": 51550}
```

### 2.7 Cashback policy
```jsonc
// PolicyVersion
{"version": 3, "state": "active",                         // active|scheduled|expired|superseded
 "effective_from": "2026-10-01T00:00:00+05:00",
 "effective_to": null,                                    // exclusive = effective_from of the next non-superseded version; null = open
 "basis": "before_purchase", "earn_on_cashback_paid": false,
 "tiers": [{"id": 7, "from": 0, "to": 20000000, "percent": "1.00"},          // from inclusive, to exclusive | null
           {"id": 8, "from": 20000000, "to": 30000000, "percent": "2.00"},
           {"id": 9, "from": 30000000, "to": 50000000, "percent": "3.00"}],  // sorted by from
 "author": {"id": 1, "display_name": "Administrator"},    // null = system / seed
 "created_at": "2026-09-20T10:00:00+05:00", "superseded_by": null}           // version int | null
```
**`GET cashback/policy/`**
```jsonc
{"active": { /* PolicyVersion */ }, "scheduled": null,   // PolicyVersion | null (at most one)
 "versions": [ /* every PolicyVersion incl. superseded, newest first by version */ ],
 "next_effective_from": "2026-11-01T00:00:00+05:00"}     // start of the next Tashkent calendar month
```

**`POST cashback/policy-versions/`** (I). Body `{"effective_from": datetime, "basis": RateBasis, "earn_on_cashback_paid": bool, "tiers": [{"from": int ≥0, "to": int|null, "percent": str}]}`.

| Rule | Field / code / message | Top-level `message` |
|---|---|---|
| No tiers | `tiers` `min_items` "Kamida bitta daraja kerak." | "Kamida bitta daraja kerak." |
| `to ≤ from` | `tiers.{i}.to` `empty_range` "Yuqori chegara quyi chegaradan katta bo‘lsin." | "Darajalar ustma-ust tushmoqda." |
| Overlap (sorted by `from`, `from_i < (to_prev ?? ∞)`) | `tiers.{i}.from` `overlap` "Darajalar ustma-ust tushmoqda." (`i` = submitted index) | "Darajalar ustma-ust tushmoqda." |
| `percent ∉ (0, 100]` | `tiers.{i}.percent` `out_of_range` "Foiz 0 dan katta va 100 dan oshmasligi kerak." | "Foiz 0 dan katta va 100 dan oshmasligi kerak." |
| `percent` > 2 dp | `tiers.{i}.percent` `max_decimal_places` | "Maydonlarni tekshiring." |
| `effective_from ≤ now` | `effective_from` `effective_from_in_past` "Kelajakdagi sanani tanlang." | "Maydonlarni tekshiring." |

- Gaps and an upper-bounded last tier do not block saving; they are returned as `warnings`.
- An existing `scheduled` version becomes `superseded` (`superseded_by` = new version; audit `cashback.policy_version_superseded`).
- Version numbers only grow. State follows time: `scheduled` until `effective_from`, then `active`; the previous version becomes `expired`.
- A purchase uses the version active at its confirmation time. Credit = `(eligible × basis_points) // 10000`, integer math only.
- Audit `cashback.policy_version_created`.
```jsonc
// 201
{"version": { /* PolicyVersion, state "scheduled" */ }, "superseded_version": null,   // int | null
 "warnings": [{"code": "gap", "from": 50000000, "to": 60000000},       // [from, to) uncovered
              {"code": "uncovered_above", "from": 80000000, "to": null}]}
```

### 2.8 Notifications, templates, segments
```jsonc
// NotificationRecord
{"id": 4, "kind": "general", "title": "Yangi dermatolog qabuli ochildi", "body": "…",
 "image_url": null,
 "link": {"kind": "none", "product_id": null, "label": null},          // product: {"kind":"product","product_id":4,"label":"Seramidli namlantiruvchi krem"}
 "audience": {"type": "segment", "segment_id": 1, "segment_name": "Doimiy mijozlar", "phone": null},  // name snapshot; phone for type customer
 "recipient_count": 612,          // devices with push on, snapshot at creation; recomputed when the send starts
 "state": "scheduled",            // scheduled|queued|sent
 "scheduled_at": "2026-10-26T10:00:00+05:00", "created_at": "2026-10-24T11:30:00+05:00", "sent_at": null,
 "sent": null, "failed": null,    // FCM accepted / failed counts, null until sent
 "delivered": null,               // null until M2 app acknowledgements
 "author": {"id": 1, "display_name": "Administrator"}}
```
**`GET notifications/`** (P E). Filters `state`, `kind`; ordered by `coalesce(scheduled_at, created_at)` desc.

**`GET notifications/meta/`**
```jsonc
{"total_devices": 4751,                                     // active customers with ≥1 push-enabled device (one push each)
 "segments": [{"id": 1, "name": "Doimiy mijozlar", "size": 612}, {"id": 2, "name": "10 mln+ xarid", "size": 318}],
 "templates": [{"status": "processing",
                "texts": {"uz": "To‘lov qabul qilindi. Buyurtma №{raqam} tayyorlanmoqda.", "ru": "…", "en": "…"},
                "updated_at": "…", "updated_by": null}],   // all 4 statuses
 "link_options": [{"product_id": 4, "label": "Seramidli namlantiruvchi krem"}]}   // active products, uz display name, sorted by label
```

**`POST notifications/`** (I, multipart). Top-level message "Maydonlarni tekshiring.".

| Field | Rule | Message |
|---|---|---|
| `kind` | `promo` or `general` | |
| `title` | trimmed 3..60 | `min_length` "Sarlavhani kiriting." / `max_length` "60 belgidan oshmasin." |
| `body` | trimmed 5..180 | `min_length` "Matnni kiriting." / `max_length` "180 belgidan oshmasin." |
| `image` | optional file, product-image rules | |
| `link.kind` | `none` (default) or `product` | |
| `link.product_id` | required for `product`; must be an active product | `does_not_exist` "Mahsulot topilmadi yoki yashirin." |
| `audience.type` | `all`, `segment` or `customer` | |
| `audience.segment_id` | required for `segment` | `does_not_exist` |
| `audience.phone` | required for `customer`; `^\+998\d{9}$`; must belong to an active customer | `invalid` "Telefon raqamini to‘g‘ri kiriting." / `customer_not_found` "Bu raqam bilan mijoz topilmadi." |
| `scheduled_at` | optional datetime, must be > now | `in_past` "Kelajakdagi vaqtni tanlang." |

- Without `scheduled_at` → state `queued`; recipients are materialised and a Celery batched send runs (fake FCM in M1). Each recipient also gets an inbox item.
- With `scheduled_at` → state `scheduled`; beat starts the send when due.
- Audit `notification.created`.
- 201 `NotificationRecord`. **The 201 body is the campaign as stored when the response is built**: it is read after the request's transaction committed and its on-commit dispatch ran. With an eager Celery (local settings) the send already finished inside the request, so the body says `state: "sent"` with `sent`, `failed` and `sent_at`; with a real worker it normally still says `queued` (a fast worker may already have advanced it). The key stays held until that body is stored: a retry racing the first response gets 409 `request_in_progress` (§1.7), and a replay afterwards returns that same body.

Templates are editable in the admin UI through `GET` / `PUT` below.

**`GET notification-templates/{status}/`**, where `status` ∈ `processing`, `handed_to_courier`, `delivered`, `cancelled` (else 404) → `{"status", "texts", "updated_at", "updated_by"}`.

**`PUT notification-templates/{status}/`**. Body `{"texts": {"uz": str 5..180, "ru": str ≤180, "en": str ≤180}}` → 200.
- The only placeholder allowed is `{raqam}` (`unknown_placeholder` "Faqat {raqam} o‘rinbosari ishlatiladi.").
- Audit `notification_template.updated`.

```jsonc
// Segment
{"id": 1, "name": "Doimiy mijozlar",
 "rules": {"min_online_orders": 3, "min_lifetime_amount": null},   // null = rule off; non-null rules are ANDed
 "manual_member_count": 0, "member_count": 640, "size": 612,       // size = members with ≥1 push-enabled device
 "created_at": "…", "updated_at": "…"}
```
- **Membership** = active customer ∧ (all set rules match ∨ manual member). With no rules set, membership is manual only.
  - `min_online_orders` counts paid, non-reversed online orders.
  - `min_lifetime_amount` compares `totals.lifetime`.
- `GET segments/` (P E).
- `POST segments/` `{"name": str 2..60 unique, "rules": {…}}` → 201.
- `GET`/`PATCH segments/{id}/`.
- `DELETE segments/{id}/` → 204, or 409 `conflict` "Rejalashtirilgan xabar bu guruhga yuboriladi." while a scheduled notification targets it.
- `GET segments/{id}/members/` (P E, CustomerListItem).
- `POST segments/{id}/members/` `{"customer_ids": [int] ≤500, "phones": [str] ≤500}` → 200 Segment. Either list or both may be sent; the members added are the union of the ids and the customers the phones resolve to.
- `DELETE segments/{id}/members/{customer_id}/` → 204. It removes manual membership only; a rule-only member → 409 `conflict` "Mijoz guruhga qoida orqali kiradi.".
- `POST segments/{id}/members/`: `customer_ids` ≤500, every id must exist (any status) — errors `customer_ids` `max_items` "Ko‘pi bilan 500 ta." / `does_not_exist`, a bad element `customer_ids.{i}` `invalid`. `phones` ≤500 E.164 numbers (`max_items`), deduplicated, each resolved to an **active** customer — a malformed element → `phones.{i}` `invalid` "Telefon raqamini to‘g‘ri kiriting.", no active customer with that number → `phones.{i}` `customer_not_found` "Bu raqam bilan mijoz topilmadi." (`i` = the first submitted index of a duplicated number). Neither list naming anybody (both absent or empty) → `customer_ids` `required`. Membership added this way is manual (the `DELETE` rules below apply); audit `segment.members_added` `{customer_ids, added}` carries the resolved ids. `DELETE …/members/{customer_id}/` for a customer who is neither a manual nor a rule member → 404.
- `size` (and `notifications/meta/` `total_devices`) count reachable customers — active members with at least one push-enabled device — because a campaign sends one push per customer (to the most recently active device); each figure equals the `recipient_count` a campaign to that audience would get. A non-list `customer_ids` or `phones` answers `invalid`.
- Audit `segment.*`.

### 2.9 Appointments
**`GET appointments/?date=YYYY-MM-DD`** (default: today in Tashkent; invalid → 400 `invalid`).
```jsonc
{"date": "2026-10-26",
 "specialists": [                                           // active professionals, by position then name
   {"id": 1, "name": "Nodira Rahimova", "role": "dermatologist",
    "working_hours": {"start": "09:00", "end": "18:00"},    // that weekday's hours (date override wins) | null = not working
    "blocks": [{"start": "16:00", "end": "16:30", "kind": "busy", "label": "Band qilingan"}]},   // label str | null; clipped to the day
   {"id": 2, "name": "Jasur Aliyev", "role": "dermatologist", "working_hours": {"start": "09:00", "end": "18:00"},
    "blocks": [{"start": "13:00", "end": "14:00", "kind": "off", "label": null}]}],
 "appointments": [                                          // Tashkent date of start = date; all statuses; by start
   {"id": 1, "specialist_id": 1, "customer": {"id": 8, "name": "Kamola Nazarova", "phone": "+998909741823"},
    "service_id": 1, "service_name": "Dermatolog konsultatsiyasi", "service_short_name": "Konsultatsiya",
    "duration_min": 30, "start": "2026-10-26T09:00:00+05:00", "end": "2026-10-26T09:30:00+05:00",
    "price": 150000,                                        // snapshot at booking
    "status": "requested", "requested_at": "2026-10-24T08:41:00+05:00", "requested_via": "app"}]}
```
**`GET appointments/pending-summary/`** → `{"count": 3, "next_at": "2026-10-26T09:00:00+05:00"}`. Counts `requested` appointments with `start ≥ now`; `next_at` is the earliest of them, or null.

**`POST appointments/{id}/confirm/` · `…/reject/` · `…/attend/`** (I). Empty body.
- Allowed: `requested` → confirm or reject; `confirmed` → attend. Otherwise 409 `invalid_transition` "Bu amal ushbu holatda mumkin emas.".
- Effects: an AppointmentEvent; a push through the outbox on confirm and reject; audit `appointment.confirmed|rejected|attended`.
- 200 with the appointment in the day-item shape above.
- `GET appointments/?date=` lists every status, so a day item may carry `status: "cancelled"` (T3).
- Route note: `confirm|reject|attend` share one route `appointments/{id}/{action}/`; any other action → 404.

**`GET appointments/meta/`** → what the admin booking dialog offers (M1d, for T10):
```jsonc
{"specialists": [                                   // active professionals, by position then name
   {"id": 2, "name": "Jasur Aliyev", "role": "dermatologist",
    "services": [                                   // active links to active services, by service position then name
      {"id": 1, "name": "Dermatolog konsultatsiyasi", "short_name": "Konsultatsiya",
       "duration_min": 30, "price": 120000}]}],     // what a booking with this specialist snapshots
 "slot_step_min": 5}
```

**Admin booking, rescheduling and cancellation (T10, M1d).** `Idempotency-Key` is optional on these three (honoured when sent, §1.7).

**`POST appointments/`** → 201 day-item (`status` `confirmed`, `requested_via` `admin`, `requested_at` = now).

| Field | Rule | Field errors (code · message) |
|---|---|---|
| `specialist_id` | int, an **active** professional | `required` · `does_not_exist` "Mutaxassis topilmadi." |
| `service_id` | int, an **active** service the specialist offers (duration and price are snapshotted from that link) | `required` · `does_not_exist` "Xizmat topilmadi." · `does_not_exist` "Mutaxassis bu xizmatni ko‘rsatmaydi." |
| `customer_id` \| `phone` | exactly one; the customer must be `active` | both missing → `customer_id` `required`; both sent → `customer_id` `invalid` "customer_id yoki phone dan faqat bittasini yuboring."; unknown/deleted id → `customer_id` `does_not_exist` "Mijoz topilmadi."; `phone` not E.164 → `invalid` "Telefon raqamini to‘g‘ri kiriting."; no active customer → `phone` `customer_not_found` "Bu raqam bilan mijoz topilmadi." |
| `start` | aware datetime, 5-minute grid, not in the past, inside that day's working hours (date override wins; `end = start + duration` must fit), outside blocked periods and the recurring break, not overlapping another `requested`/`confirmed` appointment of the specialist | `required` · `invalid` "Noto‘g‘ri qiymat." (naive) · `invalid` "Vaqt 5 daqiqalik qadamda bo‘lsin." · `in_past` "Kelajakdagi vaqtni tanlang." · `outside_working_hours` "Mutaxassis bu vaqtda ishlamaydi." · `slot_unavailable` "Bu vaqt band." |
| `note` | optional (omitted = `""`; not required and without a `default` in the OpenAPI schema), ≤300 (staff note) | `max_length` "300 belgidan oshmasin." |

- 409 `conflict` "Bu vaqt band." when a concurrent booking wins the slot (exclusion constraint; nothing is written).
- Effects: events `requested {via: "admin", …}` + `confirmed {push_sent: false}`; audit `appointment.created`. No push (the customer is on the phone).

**`POST appointments/{id}/reschedule/`** `{"start", "specialist_id"?}` → 200 day-item, status unchanged. Same `start` rules (the appointment itself is excluded from the overlap check). Changing the specialist re-snapshots duration and price from that specialist's link (`specialist_id` `does_not_exist`, or `service_id` `does_not_exist` "Mutaxassis bu xizmatni ko‘rsatmaydi."). 409 `invalid_transition` "Bu amal ushbu holatda mumkin emas." unless `requested`/`confirmed`; 409 `conflict` "Bu vaqt band." on the race. Effects: event `rescheduled {from_start, from_end, to_start, to_end, from_professional_id, to_professional_id, by: "admin", push_sent}`, push `rescheduled`, audit `appointment.rescheduled`.

**`POST appointments/{id}/cancel/`** `{"reason"?: str ≤300}` (omitted = `""`; optional in the OpenAPI schema too) → 200 day-item with `status: "cancelled"`. 409 `invalid_transition` unless `requested`/`confirmed`. Effects: event `cancelled {by: "admin", reason, push_sent}`, push `cancelled`, audit `appointment.cancelled`; the slot is free again.

`push_sent` on appointment events = the customer had an active push-enabled device at event time; the push itself is asynchronous (outbox `push.appointment`).

### 2.10 Dashboard and shell
**`GET dashboard/`**

| Param | Values | Rule |
|---|---|---|
| `range` | `today`, `week`, `month`, `custom` | Default `today` |
| `from`, `to` | date | Read only when `range=custom` (otherwise ignored, not even parsed). With `custom`: a missing `from` / `to` → `required` on that field; `from > to` → `invalid` on `from`; `to > today` (Tashkent) or an inclusive span of more than 366 days (`to − from + 1 > 366`) → `invalid` on `to` |

| `range` | `bucket` | Buckets (Tashkent) | Previous period |
|---|---|---|---|
| `today` | `hour` | 24, 00:00 … 23:00 of today | Yesterday |
| `week` | `day` | 7 days ending today | The 7 days before |
| `month` | `day` | 30 days ending today (rolling, not the calendar month) | The 30 days before |
| `custom` | `hour` if the span is ≤2 days, else `day` | Each hour or day in `[from, to]` | The same length just before `from` |

```jsonc
{"range": "today", "from": "2026-10-24", "to": "2026-10-24", "bucket": "hour",
 "buckets": [{"start": "2026-10-24T00:00:00+05:00", "revenue": 0, "orders": 0},
             {"start": "2026-10-24T09:00:00+05:00", "revenue": 180000, "orders": 1} /* … 24 total */],
 "previous_period_orders": 41,
 "stats": {"orders": 38, "revenue": 12480000,             // Σ buckets
           "new_customers": 14,                           // registered in [from, to], including since-deleted
           "total_customers": 4812, "deleted_customers": 61,   // all time, independent of range
           "out_of_stock": 9},                            // products with stock ≤ 0
 "attention": {"bts_failed": 1,                           // processing orders with shipment.state = failed
               "pending_appointments": 3, "next_appointment_at": "2026-10-26T09:00:00+05:00",  // = pending-summary
               "new_from_billz": 6,
               "refunds_needing_action": 0},              // refunds in failed|unknown|manual_required
 "generated_at": "2026-10-24T12:00:00+05:00"}
```
- `revenue` = Σ `payment.amount` of payments confirmed in the bucket, minus the succeeded refunds of those payments. Refunds are attributed to the payment's bucket, so revenue is never negative.
- `orders` = orders created in the bucket, any status.

**`GET nav-counts/`** → `{"orders_processing": 41, "appointments_pending": 3}`. These are all-time `processing` orders and the pending-summary count.

### 2.11 Settings (typed singleton)
**`GET settings/`**
```jsonc
{"reservation_minutes": 15, "support_phone": "+998712000000",   // str | null
 "pickup_address": {"uz": "Toshkent, …", "ru": "", "en": ""},
 "integrations": {"mode": "fake",                               // read-only, from INTEGRATIONS_MODE (every provider's default)
                  "providers": {"oauth": "fake", "billz": "fake", "fcm": "fake",
                                "click": "fake", "payme": "fake", "bts": "fake"}},   // read-only, each provider's *_MODE
 "updated_at": "…", "updated_by": null}                         // Actor | null
```
**`PATCH settings/`**: `reservation_minutes` int 5..60; `support_phone` E.164 or null; `pickup_address.*` ≤200 (each language optional: a leaf PATCH changes only the languages sent); `integrations` → `read_only`. → 200.
- Audit `settings.updated`: `data` is a diff of **only the changed leaves**, each `{"from", "to"}`, nested like the resource. A PATCH of `pickup_address.ru` alone logs `{"pickup_address": {"ru": {"from": "", "to": "Ташкент, …"}}}`; `{"reservation_minutes": {"from": 15, "to": 20}, "support_phone": {"from": "+998712000000", "to": null}}`. A PATCH that changes nothing writes no audit row and keeps `updated_at` / `updated_by`.
- `integrations` values are `IntegrationMode` literals. Fake providers are allowed in production (product owner decision 2026-10-26): the process logs one startup warning listing them, and never refuses to start.

### 2.12 Payments
**`GET payments/`** (P E)

| Param | Values |
|---|---|
| `status` | PaymentStatus |
| `provider` | PaymentProvider |
| `refund_state` | RefundState (any refund of the payment in that state) |
| `period`, `date_from`, `date_to` | As in `orders/`, applied to `created_at` |
| `search` | Order id, transaction id, or (≥3 digits) phone |
| `ordering` | `-created_at` (default) or `created_at` |

```jsonc
{"id": 301, "order_id": 10248, "customer": {"id": 5, "name": "Malika Karimova", "phone": "+998901234567"},
 "provider": "click", "status": "paid", "amount": 784400, "transaction_id": "CLK-7741920385",
 "created_at": "2026-10-23T14:02:30+05:00", "confirmed_at": "2026-10-23T14:05:00+05:00",
 "refunds": []}                                   // Refund[]
```

**Refund operations** (M1c, the Payments screen; declared by `commerce` because they write order events). `Idempotency-Key` is optional (honoured when sent, §1.7). Both answer **200 with the payment row above** (`refunds` included, newest first); 404 when the payment or the refund is unknown or the refund belongs to another payment.
- **`POST payments/{id}/refunds/{refund_id}/retry/`** (empty body): retries a `failed` refund as a **new** refund of the payment (Click `requested` and submitted through the outbox; Payme `manual_required`) and records its `refund_requested` order event (actor = caller); audit `refund.requested`. Refusals, checked under the order lock:
  - the refund is not `failed`, or the payment is no longer `paid` → 409 `invalid_transition` "Bu qaytarishni qayta yuborib bo‘lmaydi.";
  - another refund of the order is open (`requested`, `submitting`, `manual_required`, `unknown`) → 409 `conflict` "Bu to‘lov uchun qaytarish allaqachon jarayonda.".
- **`POST payments/{id}/refunds/{refund_id}/resolve/`** `{"state": "succeeded" | "failed"}` (any other value → 400 `state` `invalid_choice`): closes an `unknown` / `manual_required` refund after a manual check; audit `refund.state_changed` with the actor; order event `refund_succeeded` / `refund_failed`. A refund in any other state (also the loser of two concurrent resolves) → 409 `invalid_transition` "Bu qaytarishni yakunlab bo‘lmaydi.".
- Both share the busy-order outcome of §2.2.

### 2.13 Audit log
**`GET audit-log/`** (P E). Filters `actor` (staff id; the picker lists `GET staff/`, §2.16), `action` (§3.2), `object_type`, `object_id`, `date_from`, `date_to`; `ordering` `-at` (default) or `at`.
```jsonc
{"id": 9001, "at": "2026-10-24T12:03:00+05:00", "actor": {"id": 1, "display_name": "Administrator"},   // null = system
 "action": "cashback.adjusted", "object_type": "customer", "object_id": "5",   // object_id is a string (slugs)
 "data": {"direction": "add", "amount": 2950, "reason": "Kassada QR o‘qilmadi …", "balance_after": 51550},
 "ip": "203.0.113.7", "request_id": "6f1c2a0e9b8d4c37"}
```

### 2.14 Jobs
**`GET jobs/{id}/`**
```jsonc
{"id": 12, "kind": "billz_import", "state": "running",     // queued|running|succeeded|failed
 "params": {"mode": "delta"},
 "created_at": "…", "started_at": "…", "finished_at": null,
 "progress": {"done": 120, "total": 248},                  // null if unknown
 "result": null,                                           // succeeded: {"created": 3, "updated": 245, "unchanged": 0, "new_from_billz": 3}
 "error": null,                                            // failed: {"code": "provider_unavailable" | "worker_lost" | …, "message": "…"}
 "created_by": {"id": 1, "display_name": "Administrator"}}
```
- `progress` is always `{"done", "total"}` (or `null` before the first batch); `result` is the kind-specific summary (Billz import: §2.3).
- A `running` job whose worker died is re-claimed by a redelivery or, after 15 minutes without a heartbeat, failed by the beat task `integrations.reap_stale_jobs` with `error.code = "worker_lost"` ("Jarayon uzilib qoldi. Qayta urinib ko‘ring."). A failed job never blocks a new one of the same kind.
- 404 `not_found` for an unknown id.

### 2.15 Health
**`GET /api/v1/health/`**: no auth, no network gate, no throttle; not in the error envelope.
- Without params → 200 `{"status": "ok"}`.
- `?ready=1` checks the database, cache and broker → 200 `{"status": "ok", "checks": {"database": "ok", "cache": "ok", "broker": "ok"}, "version": "<git sha>", "integrations": {"oauth": "fake", "billz": "fake", "fcm": "fake", "click": "fake", "payme": "fake", "bts": "fake"}}`, or 503 with `"status": "error"` and the failing check set to `"error"`. `integrations` = `{provider: IntegrationMode}` for every provider with a `*_MODE` setting (informational; a fake provider never fails readiness). It is present **only when the caller would pass the admin network gate of §1** (peer in `ADMIN_API_ALLOWED_NETS` and, in production, a matching `X-GD-Internal-Secret`): which payment and shipping providers are fake is reconnaissance for an attacker. Other callers (load-balancer and container probes) get `status`, `checks` and `version` only.

### 2.16 Staff directory
**`GET staff/`** (P; read-only) → `Page<StaffListItem>`, the options of the audit-log `actor` filter.

| Param | Values | Default | Rule |
|---|---|---|---|
| `search` | str ≤64, trimmed, case-insensitive | – | Full name or email contains the term |
| `is_active` | query boolean | `true` | `false` lists the deactivated staff |
| `page`, `page_size` | §1.5 (20, 50, 100) | 50 | |

```jsonc
// StaffListItem — Actor plus email and is_active; ordered by display_name (case-insensitive), then id
{"id": 1, "display_name": "Administrator", "email": "admin@glowderma.uz", "is_active": true}
```

---

## 3. Enumerations

### 3.1 Value enums

| Enum | Literals | Used in |
|---|---|---|
| OrderStatus | `awaiting_payment`, `processing`, `handed_to_courier`, `delivered`, `cancelled` | orders, events |
| PaymentStatus | `pending`, `paid`, `failed`, `refunded` | order payment, payments |
| PaymentProvider | `click`, `payme` | |
| FulfilmentMethod | `delivery`, `pickup` | |
| OrderChannel | `ios`, `android` | |
| OrderPeriod | `today`, `week`, `month`, `all` | `orders/`, `payments/` |
| EventSource | `system`, `bts`, `admin` | OrderEvent |
| OrderEventType | 16 values, §4 | OrderEvent |
| CancellationReason | `reservation_expired`, `admin`, `customer_request` | |
| ShipmentState | `not_required`, `not_created`, `failed`, `created` | |
| ShipmentVia | `webhook`, `polling` | |
| RefundState | `requested`, `submitting`, `succeeded`, `failed`, `manual_required`, `unknown` | |
| CashbackHoldState | `active`, `captured`, `released` | `cashback_hold` |
| OrderCashbackKind | `debit`, `credit`, `reversal` | `Order.cashback` |
| LedgerKind | `credit`, `debit`, `adjustment`, `reversal` | LedgerEntry |
| LedgerSourceType | `order`, `billz_sale`, `admin` | LedgerEntry |
| AdjustmentDirection | `add`, `subtract` | |
| PurchaseSource | `online`, `store_qr` | Purchase |
| RateBasis | `before_purchase`, `with_purchase` | PolicyVersion |
| PolicyVersionState | `active`, `scheduled`, `expired`, `superseded` | |
| TierWarning | `gap`, `uncovered_above` | policy-versions 201 |
| ProductStatus | `active`, `hidden` | |
| ProductChangeKind | `created`, `price_changed`, `activated`, `hidden`, `promo_added`, `promo_removed`, `updated` | `last_change.kind` |
| ProductOrdering | `default`, `name`, `-name` | |
| ContentLanguage | `uz`, `ru`, `en` | texts, customer language |
| CustomerStatus | `active`, `deleted` | |
| SpecialistRole | `dermatologist`, `cosmetologist` | appointments |
| AppointmentStatus | `requested`, `confirmed`, `rejected`, `attended` | |
| AppointmentAction | `confirm`, `reject`, `attend` | path segment |
| RequestedVia | `app`, `admin` | |
| BlockKind | `busy`, `off` | |
| NotificationKind | `promo`, `general` | |
| NotificationState | `scheduled`, `queued`, `sent` | |
| AudienceType | `all`, `segment`, `customer` | |
| LinkKind | `none`, `product` | notifications |
| BannerLinkKind | `none`, `product`, `category` | banners |
| TemplateStatus | `processing`, `handed_to_courier`, `delivered`, `cancelled` | notification-templates |
| DashboardRange / Bucket | `today`, `week`, `month`, `custom` / `hour`, `day` | |
| JobKind / JobState | `billz_import` / `queued`, `running`, `succeeded`, `failed` | |
| BillzImportMode | `delta`, `full` | |
| SessionRevokeReason | `logout`, `expired`, `password_changed`, `deactivated`, `replaced`, `admin_revoked` | AuthSession, audit |
| LogoutScope | `current`, `others`, `all` | `auth/logout/` |
| ExportFormat | `csv`, `xlsx` | |
| IntegrationMode | `fake`, `live` | `settings/` `integrations`, `health/?ready=1` |

### 3.2 Audit actions (M1)

| Area | `action` values |
|---|---|
| Auth | `auth.login_succeeded`, `auth.login_failed`, `auth.logout` (`{scope, revoked_count}`), `auth.password_changed`, `auth.session_revoked` (`{sid, reason}`), `auth.staff_deactivated` (`{revoked_count}`), `auth.staff_reactivated`, `auth.social_linked`, `auth.social_unlinked` |
| System | `outbox.dead` (`{topic, dedupe_key, attempts, last_error}`) |
| Orders | `order.cancelled`, `order.status_changed`, `order.shipment_retry_requested`, `refund.requested`, `refund.state_changed` (system) |
| Catalog | `product.created`, `product.updated`, `product.image_added`, `product.image_deleted`, `product.images_reordered`, `billz.import_started`, `billz.import_finished`, `category.created`, `category.updated`, `category.deleted`, `category.reordered`, `banner.created`, `banner.updated`, `banner.deleted`, `banner.reordered` |
| Loyalty | `cashback.adjusted`, `cashback.policy_version_created`, `cashback.policy_version_superseded` |
| Notifications | `notification.created`, `notification_template.updated`, `segment.created`, `segment.updated`, `segment.deleted`, `segment.members_added`, `segment.member_removed`, `notification.push_dead` (system; a dead `push.order_status` / `push.appointment` / `campaign.send_batch` message: `{topic, dedupe_key, attempts, last_error, customer_id, campaign_id}`) |
| Appointments | `appointment.confirmed`, `appointment.rejected`, `appointment.attended`, `appointment.created`, `appointment.rescheduled` (`{…, from_start, from_end, reschedule_no}`), `appointment.cancelled` (`{…, reason}`) |
| Shell | `settings.updated` (changed leaves only, each `{from, to}`, §2.11), `export.downloaded` |
| Django admin (editable models) | `<model>.created`, `<model>.updated`, `<model>.deleted` for `location`, `professional`, `service`, `professional_service`, `working_hours`, `blocked_period`; `working_hours_override.created`, `working_hours_override.updated`, `working_hours_override.deleted` (the override is the seventh editable model, T13) |

---

## 4. OrderEvent types

```jsonc
// OrderEvent
{"id": 3, "type": "payment_confirmed", "at": "2026-10-23T14:05:00+05:00",
 "source": "system",                     // EventSource
 "status_after": "processing",           // OrderStatus | null (null = the event did not change the status)
 "actor": null,                          // Actor for source admin, else null
 "data": {"provider": "click", "transaction_id": "CLK-7741920385", "amount": 784400, "reservation_converted": true}}
```
Events are append-only. The API returns all of them; the FE mapper **folds** the "folded into" rows into their host row (§5.1).

| `type` | `source` | `status_after` | `data` keys | Display |
|---|---|---|---|---|
| `order_created` | system | `awaiting_payment` | `channel`, `item_count` (lines), `total` | row |
| `reservation_created` | system | null | `item_count`, `minutes`, `expires_at` | folded into `order_created` |
| `payment_confirmed` | system | `processing` | `provider`, `transaction_id`, `amount`, `reservation_converted` (bool) | row |
| `billz_sale_recorded` | system | null | `billz_order_id` (str) | folded into `payment_confirmed` |
| `cashback_credited` | system | null | `ledger_entry_id`, `amount`, `base`, `percent`, `policy_version` | folded into `payment_confirmed` |
| `payment_failed` | system | null | `provider`, `transaction_id` (str\|null), `error_code` (str\|null) | row |
| `shipment_created` | system or admin | null | `tracking_number`, `tariff`, `manual` (bool) | row |
| `shipment_failed` | system or admin | null | `attempt` (int), `manual` (bool), `error_code` (str\|null) | row |
| `status_changed` | admin | `to` | `from`, `to`, `push_sent` (bool) | row (manual transition) |
| `handed_to_courier` | bts | `handed_to_courier` | `via`, `bts_status` (str), `push_sent` | row |
| `delivered` | bts | `delivered` | `via`, `bts_status`, `push_sent` | row |
| `cancelled` | admin (or system for `customer_request`) | `cancelled` | `reason`, `note` (str\|null), `refund_id` (int\|null), `cashback_reversed` (bool), `stock_released` (bool), `bts_cancelled` (bool), `push_sent` | row |
| `reservation_expired` | system | `cancelled` | `released_item_count`, `payment_failed` (bool) | row |
| `refund_requested` | admin or system | null | `refund_id`, `amount`, `provider`, `manual` (bool, Payme) | folded into `cancelled` when `cancelled.data.refund_id` equals it, else a row |
| `refund_succeeded` | system | null | `refund_id`, `amount`, `provider` | row |
| `refund_failed` | system | null | `refund_id`, `amount`, `provider`, `state` (`failed`\|`unknown`), `error_code` | row |

**Order №10248 test vector** (API history, newest first) → the four design rows:
```jsonc
[{"id": 7, "type": "handed_to_courier", "at": "2026-10-23T16:40:00+05:00", "source": "bts", "status_after": "handed_to_courier", "actor": null, "data": {"via": "webhook", "bts_status": "200", "push_sent": true}},
 {"id": 6, "type": "shipment_created", "at": "2026-10-23T14:06:00+05:00", "source": "system", "status_after": null, "actor": null, "data": {"tracking_number": "48213907", "tariff": 25000, "manual": false}},
 {"id": 5, "type": "billz_sale_recorded", "at": "2026-10-23T14:05:20+05:00", "source": "system", "status_after": null, "actor": null, "data": {"billz_order_id": "0d6f6c1e-5a7b-4c1e-9a43-2f1d8e3b7a10"}},
 {"id": 4, "type": "cashback_credited", "at": "2026-10-23T14:05:00+05:00", "source": "system", "status_after": null, "actor": null, "data": {"ledger_entry_id": 121, "amount": 5680, "base": 568000, "percent": "1.00", "policy_version": 3}},
 {"id": 3, "type": "payment_confirmed", "at": "2026-10-23T14:05:00+05:00", "source": "system", "status_after": "processing", "actor": null, "data": {"provider": "click", "transaction_id": "CLK-7741920385", "amount": 784400, "reservation_converted": true}},
 {"id": 2, "type": "reservation_created", "at": "2026-10-23T14:02:00+05:00", "source": "system", "status_after": null, "actor": null, "data": {"item_count": 3, "minutes": 15, "expires_at": "2026-10-23T14:17:00+05:00"}},
 {"id": 1, "type": "order_created", "at": "2026-10-23T14:02:00+05:00", "source": "system", "status_after": "awaiting_payment", "actor": null, "data": {"channel": "ios", "item_count": 3, "total": 784400}}]
```
Expected display:
1. "Kuryerga topshirildi" — "Webhook · mijozga push yuborildi"
2. "BTS buyurtmasi yaratildi" — "Yetkazish raqami 48213907"
3. "Jarayonda" — "Click to‘lovi tasdiqlandi · band qilish savdoga aylantirildi · Billz’ga yozildi · cashback +5 680"
4. "To‘lov kutilmoqda" — "Qoldiq qayta tekshirildi · 3 ta mahsulot 15 daqiqaga band qilindi"

---

## 5. Display-field mapping (API → FE domain types)

**Helpers.** These are the existing `lib/format.ts` helpers; quoted templates are exact.

| Helper | Meaning |
|---|---|
| `N(x)` | `formatNumber`: NBSP thousands separator, `−` (U+2212) minus |
| `SOM(x)` | `formatSom`: `"784 400 so‘m"` |
| `MLN(x)` | `formatMillions`: `"20 mln"` with NBSP. `MLN0(x)` = the same without the `" mln"` suffix; strip the **NBSP** form (see §7) |
| `PCT(p)` | Percent string with trailing zeros and dot dropped and `.` → `,`: `"1.00"`→`1`, `"1.50"`→`1,5`, `"0.57"`→`0,57` |
| `DM(t)` | `formatDayMonthShort`: `"23-okt"` |
| `Y(t)` | Tashkent year |
| `P(provider)` | `PROVIDER_LABEL`: `Click` / `Payme` |
| `A(actor)` | `actor?.display_name ?? "Administrator"` |
| `VIA(v)` | `webhook`→`Webhook`, `polling`→`Polling` |

Generic mapping applies throughout: snake_case → camelCase; integer ids → `String(id)` for the FE string-id fields (customer, product, appointment, specialist, ledger, notification, segment, tier, image ids); order ids stay numbers; `null` → `undefined` for optional TS fields; the page envelope `count, page, page_size, page_count, results` → `total, page, pageSize, pageCount, items`.

### 5.1 Orders

| FE field | API source | Rule |
|---|---|---|
| `OrderSummary.customer` | `customer` | `{id: String(id), name, phone}` |
| `Shipment` | `shipment` | Camel-case the keys; `pending` is new (§7) |
| `OrderItem.art` / `imageUrl` (new) | `items[].image_url` | `art: null`; `imageUrl: image_url ?? undefined` |
| `Order.payment.transactionId` / `confirmedAt` | `payment.transaction_id` / `payment.confirmed_at` | `?? undefined` |
| `OrderEvent.status` | `status_after` | `?? undefined` |
| `OrderEvent.source` | `source` | As is |
| Folding | `history[]` | Drop `reservation_created`, `billz_sale_recorded` and `cashback_credited` rows; also drop a `refund_requested` row whose `refund_id` equals some `cancelled.data.refund_id`. Their data feeds the host rows below. Keep the API order (newest first). |
| `OrderEvent.title` | `type`, `data.to` | `order_created` "To‘lov kutilmoqda" · `payment_confirmed` "Jarayonda" · `payment_failed` "To‘lov o‘tmadi" · `shipment_created` "BTS buyurtmasi yaratildi" · `shipment_failed` "BTS ga yuborilmadi" · `status_changed` `STATUS_TITLE[data.to]` · `handed_to_courier` "Kuryerga topshirildi" · `delivered` "Yetkazildi" · `cancelled` / `reservation_expired` "Bekor qilindi" · `refund_requested` "To‘lov qaytarish so‘raldi" · `refund_succeeded` "To‘lov qaytarildi" · `refund_failed` "To‘lov qaytarilmadi" |
| note: `order_created` | + folded `reservation_created` r | With r: `` `Qoldiq qayta tekshirildi · ${r.item_count} ta mahsulot ${r.minutes} daqiqaga band qilindi` ``. Without: `` `Buyurtma yaratildi · ${data.item_count} ta mahsulot` `` |
| note: `payment_confirmed` | + folded billz b, credit c | Join with `" · "`: `` `${P(provider)} to‘lovi tasdiqlandi` ``; if `reservation_converted` "band qilish savdoga aylantirildi"; if b "Billz’ga yozildi"; if c `` `cashback +${N(c.amount)}` `` |
| note: `payment_failed` | `provider` | `` `${P(provider)} · to‘lov rad etildi` `` |
| note: `shipment_created` | `tracking_number`, `manual` | manual: `` `Qo‘lda yuborildi · yetkazish raqami ${tn}` ``; else `` `Yetkazish raqami ${tn}` `` |
| note: `shipment_failed` | `manual` | manual: "Qo‘lda urinish muvaffaqiyatsiz"; else "Avtomatik urinish muvaffaqiyatsiz · administratorga xabar berildi" |
| note: `status_changed` | `to`, order `fulfilment`, `push_sent`, `actor` | `to=delivered` ∧ pickup: `` `${A} · mijozga topshirildi` ``; else `` `${A} · holat qo‘lda o‘zgartirildi` `` + (push_sent ? " · mijozga push yuborildi" : "") |
| note: `handed_to_courier`, `delivered` | `via`, `push_sent` | `VIA(via)` + (push_sent ? " · mijozga push yuborildi" : "") |
| note: `cancelled` | `reason`, `refund_id`, `cashback_reversed`, `bts_cancelled`, `note`, `actor` | head = `customer_request` ? "Mijoz" : A. effects = [refund_id ? "to‘lov qaytarish so‘rovi yuborildi", cashback_reversed ? "cashback yozuvlari qaytarildi"] joined with `", "`. Parts = [head, effects (if any), bts_cancelled ? "BTS buyurtmasi bekor qilindi", note (if any)] joined with `" · "`. Example: "Administrator · to‘lov qaytarish so‘rovi yuborildi, cashback yozuvlari qaytarildi · Mijoz so‘rovi bilan" |
| note: `reservation_expired` | | "Band muddati tugadi · mahsulotlar bo‘shatildi" |
| note: `refund_requested` | `provider`, `amount`, `manual` | `` `${P} · ${SOM(amount)}` `` + (manual ? " · Payme kabinetida qo‘lda qaytariladi" : "") |
| note: `refund_succeeded` | `provider`, `amount` | `` `${P} · ${SOM(amount)}` `` |
| note: `refund_failed` | `provider`, `state` | `unknown`: `` `${P} · holat noma’lum, tekshirish kerak` ``; else `` `${P} · xato, administrator tekshiruvi kerak` `` |
| `OrderCashbackEntry` | `cashback[]` (+ `cashback_hold`) | `{kind, amount, label}`. label: debit "Ishlatildi"; credit `` `${PCT(percent)}% × ${N(base)}` `` ("1% × 568 000"); reversal "Qarama-qarshi yozuv". If there is no debit entry and `cashback_hold.state` is `active`, prepend `{kind: "debit", amount: -hold.amount, label: "Ishlatildi"}`. |
| Refund state (new UI line) | `refunds[0].state` | requested "Qaytarish so‘raldi" · submitting "Qaytarilmoqda" · succeeded "Qaytarildi" · failed "Qaytarib bo‘lmadi" · manual_required "Payme kabinetida qaytarish kerak" · unknown "Holat noma’lum · tekshiring" |
| `cancellation` | `cancellation` | `{reason, note: note ?? undefined}`; labels stay `CANCEL_LABEL` |

### 5.2 Products and catalog

| FE field | API source | Rule |
|---|---|---|
| `Product.category` | `category` | Slug string or null (`CategoryId` becomes `string`, §7) |
| `Product.images[]` | `images[]` | `{id: String(id), src: url, label: `Rasm ${position + 1}`}`; `art` omitted |
| `Product.art` | – | `null` |
| list thumbnail (new) | `main_image_url` | Rendered before `art` |
| `Product.lastChange.by` / `at` | `last_change.actor` / `.at` | `A(actor)` / `at` |
| `Product.lastChange.note` | `last_change.kind`, `old_price`, `new_price` | created "mahsulot yaratildi" · price_changed `` `narx ${N(old_price)} → ${N(new_price)}` `` · activated "faollashtirildi" · hidden "yashirildi" · promo_added "aksiyaga qo‘shildi" · promo_removed "aksiyadan olindi" · updated "ma’lumotlar yangilandi" |
| `CatalogStats.lastSyncLabel` | `billz.last_sync_at`, server `now` | null → "hali sinxronlanmagan"; <60 s → "hozirgina"; <60 min → `` `${m} daqiqa oldin` ``; <24 h → `` `${h} soat oldin` ``; else `formatDateTimeShort` |
| Sidebar Billz block | `billz.connected`, label above | "Billz ulangan" / "Billz ulanmagan"; second line `` `Sinxronlangan: ${lastSyncLabel}` `` |
| Category label | `categories/` `name.uz` | Replaces `CATEGORY_LABEL` |

### 5.3 Customers and ledger

| FE field | API source | Rule |
|---|---|---|
| `Customer.totals` | `totals.store`, `totals.online` | As is; lifetime = `totals.lifetime` |
| `Customer.onlineOrders` | `online_orders` | `{count, lastOrderId: last_order_id ?? undefined, lastOrderAt}` |
| `Customer.groups: string[]` | `groups[].name` | Names only |
| Balance card | `balance` | Server value (no longer the ledger sum) |
| Tier card (`TierInfo`) | `tier` | `{percent: percent && parseFloat(percent), next: next && {percent: parseFloat, remaining, progress}}` |
| `LedgerEntry.source` | `source_type`, `source_ref`, `actor` | order → `` `Buyurtma №${source_ref}` `` ("Buyurtma №10231"); billz_sale → `` `Billz savdo ${source_ref}` `` ("Billz savdo S-88213"); admin → `A(actor)` ("Administrator") |
| `LedgerEntry.note` | `kind`, `source_type`, `base`, `percent`, `promo_excluded`, `reason` | credit + billz_sale: `` `Chek ${N(base)} so‘m · ${PCT(percent)}%` `` ("Chek 231 000 so‘m · 1%"); credit + order: `` `${N(base)} so‘m · ${PCT(percent)}%` `` + (promo_excluded ? " · aksiyasiz qism" : "") ("314 000 so‘m · 1% · aksiyasiz qism"); debit "To‘lovning bir qismi"; reversal "Buyurtma bekor qilindi · cashback qaytarildi"; adjustment `reason` |
| Ledger BALANS column | `balance_after` | Replaces `withRunningBalance` |
| `CustomerPurchase.document` | `source`, `order_id`, `billz_sale_ref` | online → `` `№${order_id}` ``; store_qr → `billz_sale_ref` |
| `CustomerPurchase.promoAmount` | `promo_amount` | As is |
| `Customer.device.appVersion` | `device.app_version` | `device ?? undefined` |
| Adjustment result | 201 `entry`, `balance` | Mapped with the LedgerEntry rules; the view uses `balance` |

### 5.4 Cashback policy

| FE field | API source | Rule |
|---|---|---|
| `CashbackPolicy` | `active` | `{version, effectiveFrom, basis, earnOnCashbackPaid, tiers: tiers.map(t => ({id: String(id), from, to, percent: parseFloat(percent)}))}` |
| `nextEffectiveFrom` | `next_effective_from` | As is |
| `PolicyVersion.active` / `scheduled` | `state` | `state === "active"` / `state === "scheduled"` |
| `PolicyVersion.author` | `author` | `author?.display_name ?? "Tizim"` |
| `PolicyVersion.summary` | `tiers` of this version and of **prev** = the highest lower `version` whose state ≠ superseded | Key each tier as `(from, to, percent)`; added = after − before, removed = before − after. No prev: `` `Boshlang‘ich: ${tiers.map(t => t.from === 0 && t.to !== null ? `${MLN(t.to)} gacha ${PCT}%` : `${RANGE(t)}: ${PCT}%`).join("; ")}` `` ("Boshlang‘ich: 20 mln gacha 1%"). Only added: `` added.map(t => `${RANGE(t)}: ${PCT(t.percent)}% qo‘shildi`).join("; ") `` ("30–50 mln: 3% qo‘shildi"). Only removed: "Daraja olib tashlandi". Both: "Darajalar yangilandi". Neither: "Hisoblash qoidalari yangilandi". `RANGE(t)` = to null ? `` `${MLN(from)} dan` `` : `` `${MLN0(from)}–${MLN(to)}` `` |
| `PolicyVersion.period` | `effective_from`, `effective_to`, `state` | superseded → `` `${DM(from)} ${Y(from)} dan · almashtirildi` ``; `effective_to` null → `` `${DM(from)} ${Y(from)} dan` `` ("1-okt 2026 dan"); else last = `effective_to` − 1 day: same year `` `${DM(from)} – ${DM(last)} ${Y(last)}` `` ("15-avg – 30-sen 2026"), different years `` `${DM(from)} ${Y(from)} – ${DM(last)} ${Y(last)}` `` |
| Save result `{policy, version}` | 201 `version` | Policy and PolicyVersion via the rules above; `warnings` shown as a toast |

### 5.5 Notifications

| FE field | API source | Rule |
|---|---|---|
| `NotificationRecord.at` | `scheduled_at`, `created_at` | `scheduled_at ?? created_at` |
| `NotificationRecord.audienceLabel` | `audience`, `recipient_count` | all → `` `Barcha · ${N(recipient_count)}` `` ("Barcha · 4 751"); segment → `` `${segment_name} · ${N(recipient_count)}` `` ("Doimiy mijozlar · 612"); customer → `` `Bitta mijoz · ${formatPhone(phone)}` `` |
| `sent` / `delivered` / `state` | same | As is (`delivered` stays null in M1) |
| `totalDevices`, `segments` | meta `total_devices`, `segments` | `{id: String(id), name, size}` |
| `linkOptions` | meta `link_options` | `[{kind: "none"}, ...map(o => ({kind: "product", productId: String(o.product_id), label: o.label}))]` |
| `StatusTemplate` | meta `templates[]` | `text = texts.uz`; `translations[l] = texts[l].trim() !== ""` |
| `defaultSchedule` | – | Mapper: next Tashkent day 10:00 relative to the server `now` |
| `draft` | – | `null` (no draft persistence in M1) |
| `history` | `notifications/` page 1 (20) | `results` |

### 5.6 Appointments

| FE field | API source | Rule |
|---|---|---|
| `Specialist.role` | `role` | dermatologist → "Dermatolog"; cosmetologist → "Kosmetolog" |
| `Specialist.workingHours` / `blocks` | `working_hours`, `blocks` | If `working_hours` is null: `{start: "09:00", end: "18:00"}` plus a block `{start: "09:00", end: "18:00", kind: "off", label: "Ish kuni emas"}` |
| block `label` | `blocks[].label`, `kind` | `label ?? (kind === "busy" ? "Band qilingan" : "Ish vaqti emas")` |
| `Appointment.service` / `serviceShort` | `service_name` / `service_short_name` | As is |
| `Appointment.specialistId` | `specialist_id` | `String` |
| `Appointment.requestedVia` | `requested_via` | app → "ilova"; admin → "administrator" (rendered raw today) |
| Pending summary | `count`, `next_at` | `{count, nextAt: next_at}` |

### 5.7 Dashboard and shell

| FE field | API source | Rule |
|---|---|---|
| `series[]` window | `buckets`, `bucket` | Day buckets: all. Hour buckets of one day: keep the window `[min(09:00, first non-empty), max(21:00, last non-empty)]`, so it is 13 buckets 09:00–21:00 when nothing falls outside |
| `series[].label` | `buckets[].start` | hour → `formatTime(start)` ("09:00"); week → `` `${WEEKDAYS_SHORT[wd]}, ${DM(start)}` `` ("Ya, 18-okt"); month/custom day → `DM(start)` ("25-sen") |
| `series[].revenue` / `orders` | `revenue` / `orders` | As is |
| `axisNote` | `bucket` | hour "Soatlar bo‘yicha"; day "Kunlar bo‘yicha" |
| `axisLabels` | the windowed series | `[s[0], s[floor((n−1)/2)], s[n−1]]`. Hour → the label; day → `DM(start)` ("18-okt", "21-okt", "24-okt"; month "25-sen", "9-okt", "24-okt") |
| `stats.ordersNote` | `previous_period_orders`, `range` | today `` `Kecha: ${n} ta` ``; week `` `O‘tgan hafta: ${n} ta` ``; month `` `O‘tgan oy: ${n} ta` ``; custom `` `Oldingi davr: ${n} ta` `` |
| `stats.*` | `stats.new_customers`, `total_customers`, `deleted_customers`, `out_of_stock` | Camel-case |
| `attention.*` | `attention.*` | Camel-case; `refundsNeedingAction` is new; "Eng yaqini: …" stays `formatWeekdayShortDateTime` |
| Recent orders | `orders/?page_size=6` | `results` |
| Sidebar badges | `nav-counts/` | orders = `orders_processing`; appointments = `appointments_pending` |
| Sidebar user | `auth/me/` | name = `display_name`; secondary = `email`; initials = first letters of the first two words, else the first two letters, uppercased ("Administrator" → "AD") |

### 5.8 Uzbek labels shared by the UI and exports

| Enum | Labels |
|---|---|
| OrderStatus | awaiting_payment "To‘lov kutilmoqda" · processing "Jarayonda" · handed_to_courier "Kuryerga topshirildi" · delivered "Yetkazildi" · cancelled "Bekor qilindi" |
| PaymentStatus | pending "Kutilmoqda" · paid "To‘langan" · failed "Muvaffaqiyatsiz" · refunded "Qaytarilgan" |
| Provider / Fulfilment | click "Click" · payme "Payme" / delivery "Yetkazib berish" · pickup "Olib ketish" |
| CancellationReason | reservation_expired "Band muddati tugadi" · admin "Admin bekor qildi" · customer_request "Mijoz bekor qildi" |
| ProductStatus / CustomerStatus | active "Faol" · hidden "Yashirin" / active "Faol" · deleted "O‘chirilgan" |
| LedgerKind | credit "Hisoblandi" · debit "Ishlatildi" · adjustment "Tuzatish" · reversal "Qaytarildi" |
| NotificationState / Kind | scheduled "Rejalashtirilgan" · queued "Navbatda" · sent "Yuborildi" / promo "Aksiya" · general "Umumiy" |
| Booleans in exports | "Ha" / "Yo‘q" |

---

## 6. Cross-references: `docs/decisions/open-questions.md`
Provisional, configurable rules that shape payloads:
- **Admin cancel rule.** Allowed from `awaiting_payment` and `processing`. From `handed_to_courier` only if the BTS cancel succeeds (status 100); otherwise 409 `not_cancellable`.
  - This contract applies the same synchronous BTS-cancel check to `processing` orders whose BTS shipment exists.
- **Refund pending display.** Cancel never sets `payment.status=refunded`. The payment stays `paid` with a Refund in `requested`/`submitting`/`manual_required`/`unknown` until the provider confirms.
  - Payme is `manual_required`, done in the Payme cabinet and confirmed by inbound CancelTransaction.
  - Click `unknown` is never auto-retried.
- **Dashboard revenue.** Σ paid totals by payment confirmation time, net of refunds; orders = created in the bucket. Refunds are attributed to the payment's bucket.
- **Rate basis / cashback-paid earning.** `before_purchase`; the cashback-paid part does not earn. Both are policy fields (`basis`, `earn_on_cashback_paid`).
- **Seed tiers.** 0–20M 1%, 20–30M 2%, 30–50M 3%; open top.
- **Spendable timing.** Credits are spendable immediately (`LOYALTY_PENDING_DAYS=0`).
- **Refund larger than the balance.** The reversal is still posted; the balance may go negative and is flagged. `available` can be negative.
- **Reservation length.** 15 min (`settings.reservation_minutes`). Pickup completion reuses `delivered`.
- **Price authority.** Admin `price`; `billz_price` is stored for reference (`PRICE_SOURCE`).
- **Customer delete and re-register.** Phone is unique. Delete scrubs the name, image and address (`name: ""`). Re-registration reactivates the same id, so ledger and purchases persist.
- **Production media storage.** Open; URLs are always absolute from `PUBLIC_MEDIA_BASE_URL`.

---

## 7. Frontend deltas (code changes needed to consume this contract)
1. **Data layer plumbing.**
   - Add page and error mappers per §1.5 and §1.6: camel-case the field paths per segment and keep numeric segments.
   - Per-action field remaps:
     - `sendNotification`: `audience.phone`→`phone`, `audience.segment_id`→`segmentId`, `audience.type`→`audience`, `link.*`→`link`, `scheduled_at`→`scheduledAt`, `image`→ top-level error.
     - `saveProduct`: `billz_id`→`billzId`.
   - 404 → `null`.
2. **Mutation signatures.** Mutations take ids and an idempotency key:
   - `cancelOrder(orderId, reason, key)`, `changeOrderStatus(orderId, status, key)`, `retryShipment(orderId, key)`;
   - `adjustCashback(customerId, input, key)` (customer-view passes the id), `saveCashbackPolicy(draft, key)`;
   - `sendNotification(FormData, key)`, `updateAppointment(id, action, key)`.
3. **Order cancel no longer sets `refunded`.**
   - Remove the local `payment.status` flip.
   - `OrderPaymentCard` shows the refund state line (§5.1).
   - The cancel dialog text "… qaytariladi" stays.
4. **Use server actions, not local rules.** Use `available_actions` for the status, cancel and retry buttons instead of `manualTransitions`/`canCancel`. Domain functions stay for mock mode. 409 `not_cancellable` is possible from `handed_to_courier`.
5. **Retry shipment is asynchronous.**
   - `Shipment` gains `pending?: boolean` on `not_created`/`failed`, and `attempts` on `failed`.
   - Retry returns 202 with `pending=true`. Disable the button and show "Yuborilmoqda…"; refresh later.
   - Replace the misleading "BTS buyurtmasi allaqachon yaratilgan." with the API message.
6. **History gets new rows.** The `payment_failed` and `refund_*` titles and notes (§5.1) are new strings. The mapper owns the folding.
7. **Photos.**
   - `OrderItem` gains `imageUrl`; the order item and products-table thumbnails render the photo before `art`.
   - `ProductImage.src` = API url and `label` = "Rasm N".
   - Product save orchestration (as built in F2a): JSON `POST products/` (hidden, keyed) or a `PATCH` carrying only the fields changed against the editor's baseline (texts leaf by leaf, so a concurrent edit by another admin survives), then image deletes, then uploads (deletes first, so swapping a photo on a product with 8 images never reaches 9; an active product keeps one old image as an anchor until the first upload lands), then `PUT images/order` only when the order differs from the server's, then `PATCH status`. `POST products/{id}/images/` honours an optional `Idempotency-Key` (§1.7), so a client may key each picked file and retry a lost upload without a duplicate.
8. **Categories become data.** `CategoryId` → `string` (slug); add `getCategories()`; schema `category: z.string()`; pass categories to ProductEditor, ProductsToolbar and ProductsTable; wrap `/admin/products/new` in Suspense.
9. **Catalog stats.**
   - `getCatalogStats` returns `lastSyncAt` and `billzConnected`; `lastSyncLabel` is derived per §5.2.
   - The sidebar Billz and user blocks become async `<BillzStatus/>` and `<SidebarUser/>`.
   - Remove the always-on bell dot.
10. **Customer card.** The ledger table uses `balanceAfter` instead of `withRunningBalance`. CustomerView uses the server `balance`, and optionally the server `tier`. An adjustment returns `{entry, balance}`.
11. **Cashback editor.**
    - Percent ≤2 decimals, sent as a string.
    - The preview uses integer basis points: `(eligible × bp) // 10000`.
    - Add test vectors 0.57%, 2.3% and 1.5%.
    - `summarize` diffs by `(from, to, percent)`, not tier id.
    - Fix `range()`: `.replace(" mln", "")` uses a normal space while `formatMillions` emits NBSP, so the suffix is never stripped.
    - Show `warnings`.
12. **Notifications.**
    - Audience mapping: `group`→`segment`, `one`→`customer`; the phone is sent as `+998…`.
    - The view keeps the picked `File` and posts FormData.
    - `defaultSchedule` and `draft: null` are mapper-side. `history` is the first page.
    - Templates are editable in the UI through `GET` / `PUT notification-templates/{status}/` (as built by the frontend).
13. **Appointments.**
    - Map `requested_via`, `role`, and null `working_hours` (§5.6).
    - `DEFAULT_APPOINTMENT_DAY` → today (`parseDay`/`dayHref` take `today`).
    - The pending summary excludes past requests.
14. **Dashboard.**
    - Map buckets per §5.7.
    - The custom range ("Tanlash") can be wired to `range=custom&from=&to=`.
    - Fix the out-of-stock link `?stock=out` → `?out=1`.
    - `getRecentOrders(6)` → `orders/?page_size=6`.
15. **Export route.** Stream Django's file and forward `Content-Disposition`; the UI downloads through `fetch` + blob and shows the envelope message as a toast on any non-2xx (429, 400 `export_too_large`) instead of navigating to the route. Translate UI params to API params with the shared mapper:
    - Orders: `q`→`search`, `sort` `date_desc`/`date_asc`→`ordering` `-created_at`/`created_at`, `size`→`page_size`.
    - Products: `promo` `yes`/`no`→`true`/`false`, `out=1`→`out_of_stock=true`, `new=1`→`is_new=true`, `sort` `default`/`name_asc`/`name_desc`→`ordering` `default`/`name`/`-name`.
    - The CSV date format changes from "23-okt, 14:02" to "2026-10-23 14:02" (it now includes the year).
16. **Deleted customers.** `customer.name` can be `""`; render "O‘chirilgan mijoz" in orders, appointments and customer views.
17. **Auth.**
    - Handle the 401 codes per §1.2: `not_authenticated` → refresh once; `SESSION_*` → clear cookies and redirect.
18. **Decisions recorded while designing the M1 data model (2026-10-25, `docs/plans/2026-10-25-m1-models.md`).**
    - The seeded policy v3 carries `earn_on_cashback_paid = true` so order №10248's drawn credit (base 568 000 → 5 680) reproduces; the contract example in §2.7 stays illustrative.
    - `payment_failed` is emitted only on provider callbacks. A reservation expiry or an admin cancel of an unpaid order fails a `pending` payment silently (`reservation_expired.data.payment_failed` says whether one was failed); a Payme transaction in state 1 is never failed unilaterally and waits for Payme's `CancelTransaction`.
    - `online_orders.count` = paid, non-reversed online purchases; `online_orders.last_order_*` = the customer's newest order of any status.
    - A cancelled BTS parcel keeps `shipment.state = "created"` with its tracking number (no `cancelled` literal in M1); the `cancelled` order event carries `bts_cancelled`.
    - `cashback_used` never exceeds `items_subtotal` (cashback never pays the delivery fee) — a DB CHECK.
    - `Order.cashback_hold` renders only an `active` hold, else `null`.
    - Orders keep a recipient snapshot (`recipient_name`, `recipient_phone`, `address_text`) taken at checkout, so BTS parcels and exports survive a later customer scrub.
    - Admin appointment creation / rescheduling / cancellation (`requested_via = admin`) add audit actions `appointment.created`, `appointment.rescheduled`, `appointment.cancelled` and pushes on reschedule and cancel (M1d).
    - Refund operations for the Payments screen (M1c, consumed by F3): `POST payments/{id}/refunds/{refund_id}/retry/` (from `failed`) and `…/resolve/` `{state: succeeded|failed}` (from `unknown` / `manual_required`, audited `refund.state_changed` with the actor).
    - The logout action posts `{refresh: <gd_admin_refresh cookie>, scope: "current"}` and clears both cookies whatever the response (a 401 `SESSION_EXPIRED` counts as logged out). Settings (A1) can offer "others" and "all".
    - The login page, `auth.actions.ts`, cookies and `proxy.ts` follow the spec's "Auth" section.
