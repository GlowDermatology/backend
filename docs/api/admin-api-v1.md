# Glow Derma admin API v1 (M1 contract)

| | |
|---|---|
| Version | 1.0.2 (2026-10-26), M1 admin vertical; M2 cross-references from `docs/api/customer-api-v1.md` in §2.1 (staff OAuth under fake OAuth in production), §2.2 (manual Click refunds on cancel), §2.3 (Billz import and product create refused while Billz is fake in production), §2.10 (revenue, `attention.bts_polling_failing`), §2.11 (`integrations.providers`), §2.14 (`billz_fake_mode`), §2.15 (`providers`), §3.1 (`refresh_reused`), §3.2, §4 (`refund_requested.manual_reason`), §5.1/§5.7 (refund lines by provider, BTS polling attention), §6 (refund pending display, live-to-fake switch) and §7 items 3 and 18 (refund state line, refund resolve with a fresh Click status check). Gap closure, settings package (2026-10-27): §1.7, §2.0, §2.10 (`attention.cashback_policy_missing`), §2.11 (contacts, legal texts, `POST settings/terms/publish/`), §2.15 (`cashback_policy_missing`), §3.2 (`settings.terms_published`, the bootstrap policy), §5.7 and §7 item 19. Gap closure, staff package (2026-10-27): §1.6, §1.7 (required keys of the staff writes, stored credentials), §2.0, §2.1 (`Me.is_manager`), §2.6 (`devices`, `device.platform`, `online_orders.all_count`), §2.16 (staff management), §3.2 (`staff.*`), §5.3, §5.8, §6 and §7 items 20–21. Gap closure, catalog package (2026-10-26): §1.4 (folded name search, link URLs), §1.8 (export columns), §2.2–§2.5 (category image, banner `link.url`), §2.6/§2.16 (folded search), §2.8 (notification `link.url`), §2.14 (delta interval), §3 and §7 item 22. Gap closure, specialists and services package (2026-10-27): §1.5, §1.8, §2.0, §2.9 (day board `specialists[].is_active`, booking locks), §2.17 (specialists, services, schedules, clinic location), §3.1 (`ServiceCategory`, `DirectoryOrdering`), §3.2 (`professional.*`, `schedule.*`, `service.*`, `location.*`), §5.6, §5.8 and §6. Settings moved to the ops console (2026-11-07): `settings/` and `settings/terms/publish/` retired; §1.6, §1.7, §2.0, §2.11, §3.1, §3.2 and §7 item 19. Yandex Delivery (2026-11-15, `feat/yandex-delivery`; `docs/integrations/yandex-delivery.md`): §1.5, §1.6, §1.7, §2.0, §2.2 (filters `provider` / `stage` / `attention`, `delivery`, `available_actions.dispatch` / `confirm_return`, the carrier-aware cancel), §2.10 (`fulfilment`, `attention.yandex_failing`, `nav-counts.delivery_issues_open`), §2.18 (dispatch, confirm return, the delivery exception queue, dispatch suspension, the dev courier route), §3.1, §3.2 and §4 (`delivery_failed`, `shipment_returned`, source `yandex`) |
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
| `Accept-Language` | optional; Next sends the admin panel's language (`gd_admin_language`) | The language of error messages and exports (feat/admin-ux, §1.6, §1.8): the first supported tag in q-order, region ignored (`ru-RU` → `ru`); `q=0`, a malformed `q`, `*`, an unsupported tag or no header → `uz`. Only this header counts — never `StaffUser.language` or a cookie. i18n data fields (`texts`, `name`) always return all three languages. |
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
`X-Request-ID` (always) · `Cache-Control: no-store` (always) · `Content-Language: uz|ru|en` (always: the language chosen from `Accept-Language`, §1.2 — one of the three, never the raw header) with `Vary: Accept-Language` · `Retry-After` (on 429 and 409 `request_in_progress`) · `Idempotent-Replayed: true` (on a replayed response) · `Content-Disposition` (on exports). Other `/api/` paths keep `Content-Language: uz` (customer views set their own).

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
| Phone | E.164 `^\+998\d{9}$` in every response and stored value. **Requests (feat/admin-ux, phone leniency):** the admin fields that take a phone — `segments/{id}/members/` `phones[]`, `appointments/` `phone`, `notifications/` `audience.phone`, `notifications/test-devices/` `phone`, `clinics/` and `clinics/{id}/` `phone` — also accept what staff paste: ASCII digits with spaces (any Unicode space), `-` / Unicode dashes, `.`, `(`, `)`, `/`, an optional `+998`, `998` or `00998` before exactly 9 national digits (`+998 90 123-45-67`, `998901234567`, `(90) 123 45 67`, `90 123 45 67`); they are normalised to E.164 before lookup, deduplication and storage. Anything else (letters, another country code, a wrong digit count, > 32 characters, a JSON number) → `invalid` "Telefon raqamini to‘g‘ri kiriting." (`core.phones.normalize_uz_phone`). The customer API keeps the strict E.164 rule. | `"+998901234567"` |
| Enums | literals from §3, identical to the frontend unless §7 lists a mapping | `"awaiting_payment"` |
| i18n text | `{"uz": str, "ru": str, "en": str}`; `""` means not filled, never `null` | |
| Absent value | Key always present with `null`; lists are `[]`; keys are never omitted | |
| Query booleans | `true` / `false` (also `1` / `0`) | `?promo=true` |
| Media URLs | Absolute, built from `PUBLIC_MEDIA_BASE_URL`, never from the request Host | `"https://media.example.uz/products/1/9f2c.webp"` |
| Image variants | Every image URL field has two siblings with the same nullability: `thumbnail_` / `medium_` inserted before `url` (`image_url` → `image_thumbnail_url`, `image_medium_url`; `main_image_url` → `main_image_thumbnail_url`, `main_image_medium_url`; `photo_url` → `photo_thumbnail_url`, `photo_medium_url`; a gallery item's `url` → `thumbnail_url`, `medium_url`). They are WebP copies whose longest side is 400 px (thumbnail) and 1000 px (medium), never upscaled, written at upload; the name is the original's with the extension replaced by `_400.webp` / `_1000.webp` (`docs/specs/2026-11-13-image-variants-design.md`). Lists and cards should use them; the original stays for a single large image. | `"https://media.example.uz/products/1/9f2c_400.webp"` |
| Link URLs (banner / campaign destinations) | Absolute `https` URL ≤500, trimmed, no credentials, port 443 only, no Unicode control/format characters; the scheme is matched case-insensitively and stored lower-cased (`HTTPS://x.uz/A` → `https://x.uz/A`); else `invalid` "Havolani https:// bilan to‘liq kiriting." (open questions GC-3, GC-9) | `"https://glowderma.uz/aksiya"` |
| Name search folding | Every name `search` compares case-insensitively **and folded** on both sides: the Uzbek o‘/g‘ mark in any of its six encodings (U+0027 `'`, U+0060 `` ` ``, U+2018 `‘`, U+2019 `’`, U+02BB `ʻ`, U+02BC `ʼ`) is one character, and `ё` = `е` (`core.search.SearchFold`, GC-1) | `ko'pik` finds "Yumshoq tozalovchi ko‘pik" |

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
| Every other paginated list (`customers/`, `categories/`, `banners/`, `notifications/`, `segments/`, `segments/{id}/members/`, `payments/`, `audit-log/`, `specialists/`, `specialists/{id}/overrides/`, `specialists/{id}/blocks/`, `services/`, `delivery/issues/`) | 20, 50 | 20 |

Collections embedded in a resource (customer ledger and purchases, order items, history, refunds, cashback) are never paginated.

### 1.6 Errors
```jsonc
{"code": "validation_error",                      // machine code, table below
 "message": "Maydonlarni tekshiring.",            // in the request language (below); ActionResult.error = the first errors[] entry with field null, else this message
 "errors": [{"field": "texts.uz.name",            // dotted snake_case path | null (non-field)
             "code": "min_length",
             "message": "O‘zbekcha nomni kiriting (kamida 2 belgi)."}],
 "request_id": "6f1c2a0e9b8d4c37"}
```
- `errors` is `[]` for non-validation errors.
- DRF nested errors are flattened server-side. Paths start at the body root; list indexes are numeric segments (`tiers.1.percent`); multipart names map 1:1 (`audience.phone`); query errors use the param name (`page_size`).
- Order: one entry per (field, code), in validation order. The FE mapper keeps the first message per field.
- `message` is `"Maydonlarni tekshiring."` unless the endpoint lists a specific message.
- **Language (feat/admin-ux):** the messages in this document are the Uzbek source texts. The envelope has the same shape and the same `code`s in every language; `message` and each `errors[].message` are rendered in the request's language (§1.2: `uz` / `ru` / `en`). A text the catalog does not know (free text, a provider's own message) stays Uzbek; values inside a message (names, ids, amounts) are never translated, only regrouped (`12 000` → `12,000` in English). This holds for the DRF handler, the admin 404/500 pages, the 413 `payload_too_large` and the 403 of the admin network gate (`/api/v1/schema/` and `/api/v1/docs/` stay Uzbek). The client must branch on `code` and `errors[].code`, never on a message text.
- **Stored and sent texts never change language:** the API request itself runs in Uzbek; audit entries, outbox payloads, push and inbox notifications, order / appointment events, ledger entries, idempotency records and e-mails are written exactly as before whatever `Accept-Language` says (staff e-mails use the recipient's `StaffUser.language`).

| HTTP | `code` | When |
|---|---|---|
| 400 | `validation_error` | Body, query or field errors (`errors[]` is non-empty) |
| 400 | `bad_request` | Malformed request rejected by Django (`SuspiciousOperation`, `BadRequest`, too many fields/files) |
| 400 | `parse_error` | Body cannot be parsed (malformed JSON) |
| 406 | `not_acceptable` | `Accept` header cannot be satisfied |
| 400 | `idempotency_key_required` | Missing or non-UUID `Idempotency-Key` on an endpoint that requires it |
| 401 | `not_authenticated` · `SESSION_EXPIRED` · `SESSION_REPLACED` · `invalid_credentials` | §1.2 |
| 403 | `permission_denied` | Outside the allowed networks (a token with an audience other than `staff` fails decoding → 401 `not_authenticated`); a staff management call (§2.16) by a staff member who is not a manager |
| 404 | `not_found` | Unknown id, slug or route |
| 405 | `method_not_allowed` | |
| 409 | `invalid_transition` | A state machine refuses (order transition, appointment action, retry-shipment precondition) |
| 409 | `click_not_reversed` | M3: `payments/{id}/refunds/{refund_id}/resolve/` of a failed Click refund without `manual_payout` while Click still shows the payment paid (§7 item 18); the UI then offers "Qo‘lda to‘landi" |
| 409 | `click_no_paydoc` | feat/admin-ux (`conflict` before): `…/resolve/` of a failed Click refund without `manual_payout` whose payment has no Click payment id — Click cannot be asked; the UI offers "Qo‘lda to‘landi" (§7 item 18) |
| 409 | `click_fake_mode` | feat/admin-ux (`invalid_transition` before): `…/resolve/` of a failed Click refund, or `…/retry/` of a failed Click refund, while the Click port is fake under the production settings; nothing written (§7 item 18) |
| 409 | `order_busy` | feat/admin-ux (`conflict` before): the order row is held by another change for longer than the admin lock wait (5 s; e.g. a BTS / Yandex call in flight) — every admin endpoint that locks an order (cancel, transitions, dispatch, retry-shipment, confirm-return, refund resolve/retry). Transient: retry with the same `Idempotency-Key` "Buyurtma boshqa amal bilan band. Birozdan so‘ng qayta urinib ko‘ring." |
| 409 | `not_cancellable` | Order cancel refused (§2.2; a Yandex Delivery order also when Yandex refuses, the parcel is delivered or out of sight, §2.18) |
| 409 | `return_requested` | Yandex Delivery: the cancel of a picked-up order asked Yandex to bring the parcel back (or the request's answer was lost) and Yandex has not shown the return yet; the order is unchanged, retry later — a retry only re-reads, the return is never requested twice (§2.18) |
| 409 | `coordinates_required` · `method_disabled` · `too_heavy` · `not_configured` · `suspended` · `closed` · `after_cutoff` · `returns_unavailable` | `orders/{id}/dispatch/` refusals: no map pin, the chosen method is off, the parcel is too heavy for the class, the warehouse calendar refuses a courier now (§2.18) |
| 409 | `product_changed` | feat/ai-assistant, server-internal: applying AI suggestions found a product value changed since the suggestions were generated (§2.3); nothing written — `assistant/jobs/{id}/apply/` never answers it, it lists such keys in `conflicts` (§2.19) "Mahsulot boshqa joyda o‘zgartirildi: sahifani yangilab, qayta urinib ko‘ring." |
| 409 | `assistant_disabled` | feat/ai-assistant (§2.19): the AI assistant is switched off "AI yordamchi o‘chirilgan." |
| 409 | `ai_not_configured` | §2.19: the AI provider is disabled or has no active key "AI yordamchi hali sozlanmagan. Administratorga murojaat qiling." |
| 409 | `assistant_budget_exceeded` | §2.19: today's or this month's AI budget is spent "AI uchun ajratilgan byudjet tugadi. Administratorga murojaat qiling." |
| 409 | `assistant_job_not_ready` | §2.19: the suggestions are not ready or were already applied / discarded "Bu takliflar hali tayyor emas yoki allaqachon ko‘rib chiqilgan." |
| 429 | `assistant_daily_limit` | §2.19: the staff member's AI jobs today reached the limit "Bugungi AI so‘rovlari chegarasiga yetdingiz. Ertaga qayta urinib ko‘ring." |
| 409 | `conflict` | Other state conflicts (category has products, BTS message in flight, last image of an active product, a manager acting on their own account or removing the last active manager (§2.16), …) |
| 409 | `request_in_progress` | The same `Idempotency-Key` is still executing; `Retry-After: 1` |
| 413 | `payload_too_large` | Body > 6 MiB |
| 415 | `unsupported_media_type` | Wrong `Content-Type` |
| 422 | `idempotency_key_reused` | Same key, different request fingerprint |
| 429 | `throttled` | `Retry-After` in seconds |
| 500 | `server_error` | Unexpected; quote `request_id` |
| 503 | `provider_unavailable` | A synchronous provider call failed transiently (BTS cancel; the Yandex carrier cancel of `orders/{id}/cancel/` when nothing can have reached Yandex — a call that may have reached it answers 409 instead, §2.18; the Billz lookup of `POST products/` and of a `PATCH products/{id}/` that changes `billz_id`, §2.3). Safe to retry with the same key. |

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
  - `appointments/{id}/{confirm|reject|attend}/`;
  - `POST staff/`, `staff/{id}/deactivate/`, `staff/{id}/reset-password/` (§2.16);
  - Yandex Delivery (§2.18): `orders/{id}/dispatch/`, `orders/{id}/confirm-return/`, `orders/{id}/issues/`, `delivery/issues/{id}/resolve/`, `POST delivery/suspension/`.
- **Optional:** honoured when sent on any other *authenticated* POST, PUT, PATCH or DELETE. `auth/*` endpoints ignore the header: their responses carry credentials or end sessions, so they are never stored.
- **Scope:** (audience `staff`, actor staff id, method, resolved path including ids, key). The same key on two different orders is two separate records.
- **Stored credentials:** a response field that carries a credential — `temporary_password` of `POST staff/` and `staff/{id}/reset-password/` (§2.16) — is stored **sealed** (AES-256-GCM under `SECRETBOX_KEY`, bound to the key's scope and the field) and opened for the replay, so the replay shows the same password and the database never holds it in clear. A replay after a `SECRETBOX_KEY` rotation within the key's lifetime carries `null` there (reset the password again). A replay returns the password **as it was issued**: if a later reset replaced it, or the person already changed it, the replayed one no longer signs in (reset again).
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
- **Response:** 200 with `Content-Disposition: attachment; filename="<name>-YYYYMMDD-HHMM.<ext>"` (Tashkent time) and `Content-Language`. `<name>` is the table's name in the request language (§1.2): `categories-20261117-0412.csv` in English, `kategoriyalar-…` in Uzbek. A name that is not ASCII (Russian) is sent as RFC 6266 / 5987 `attachment; filename="kategorii-….xlsx"; filename*=UTF-8''%D0%BA%D0%B0…-….xlsx`: the `filename` fallback is the Russian name transliterated to ASCII (`товары` → `tovary`, `журнал-действий` → `zhurnal-deystviy`; the Uzbek name only if a character has no Latin spelling), so a client that reads only `filename` still saves a Russian name.
- **CSV:** `text/csv; charset=utf-8`; UTF-8 **BOM**; `;` separator; CRLF; every cell double-quoted with `"` doubled.
- **XLSX:** one sheet named `<name>` in the request language (Excel-forbidden `\ / ? * [ ] :` removed, at most 31 characters); text cells typed `s`; money and counts numeric.
- **Formatting:** headers in the request language (the Uzbek ones below are the source; a header or label that reads differently on one list has that list's own rendering — msgctxt `export <resource>`: `Nomi` is "Название"/"Name" on products, categories, services and segments, and a Russian `Faol` / `Nofaol` agrees with the list's noun: «Активна» for categories and services, «Активен» elsewhere); enums as the labels of §5.8 in the request language (also `Ha` / `Yo‘q`, `Faol` / `Nofaol`, the refund state, the weekday short names, `mahsulot #<id>`); data — names, ids, amounts, dates, phones, free text — is never translated; datetimes as text `YYYY-MM-DD HH:MM` (Tashkent); phones as text in E.164; money as plain integers.
- **Formula safety:** a cell that comes from a string field and starts with `=` `+` `-` `@` TAB or CR gets a leading `'`. Integer cells are exempt, and so is a value that is exactly an E.164 phone (`+` then 7–15 digits, no leading zero): it cannot carry a formula and stays plain E.164 text.
- **Audit:** every export writes the AuditEntry `export.downloaded` `{resource, filters, row_count, format}` (in Uzbek, outside the export's language: the audit text never changes language).

| Resource | `<name>` | Columns |
|---|---|---|
| orders | `buyurtmalar` | `№; Mijoz; Telefon; Sana; Summa (so‘m); To‘lov; Usul; Olish usuli; Holat; BTS` (BTS = tracking number or empty) |
| products | `mahsulotlar` | `ID; Billz ID; Billz nomi; Nomi; Kategoriya; Narx (so‘m); Qoldiq; Aksiya; Holat; Yangi` |
| customers | `mijozlar` | `ID; Ism; Telefon; Ro‘yxatdan o‘tgan; Jami xaridlar (so‘m); Cashback foizi; Balans (so‘m); Holat` |
| payments | `tolovlar` | `ID; Buyurtma №; Mijoz; Telefon; Usul; Holat; Summa (so‘m); Tranzaksiya; Yaratilgan; Tasdiqlangan; Qaytarish` |
| notifications | `bildirishnomalar` | `ID; Sana; Turi; Sarlavha; Auditoriya; Holat; Yuborildi; Yetkazildi; Havola` (`Havola`: `mahsulot #<id>`, the URL for kind `url`, else empty) |
| segments, members | `guruhlar`, `guruh-azolari` | `ID; Nomi; A’zolar; Push qurilmalar` / customer columns |
| categories, banners | `kategoriyalar`, `bannerlar` | `Slug; Nomi; Tartib; Faol; Mahsulotlar; Rasm` (`Rasm`: `image_url`, else empty) / `ID; Sarlavha; Havola; Faol; Boshlanishi; Tugashi; Tartib` |
| audit-log | `harakatlar-jurnali` | `ID; Sana; Xodim; Amal; Obyekt; Obyekt ID; IP; So‘rov ID` |
| specialists (§2.17) | `mutaxassislar` | `ID; Ism; Mutaxassislik; Tajriba (yil); Xizmatlar; Ish kunlari; Kelgusi qabullar; Holat` (Xizmatlar = short names of the active offers joined with `, `; Ish kunlari = `Du, Se, Ch, Pa, Ju, Sh, Ya` of the working weekdays; Kelgusi qabullar = `upcoming_appointments`; Holat = `Faol` / `Nofaol`) |
| services (§2.17) | `xizmatlar` | `ID; Nomi; Qisqa nomi; Kategoriya; Davomiyligi (daq.); Narx (so‘m); Mutaxassislar; Holat` (Narx = `default_price`, empty when null; Mutaxassislar = `specialist_count`) |

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
| `auth/password-token/check/` and `set/` per client IP (feat/admin-ux, `staff_password_token`) | 30/min |
| `staff/{id}/send-password-link/` per manager and account, `auth/password-link/` per caller (`staff_password_link`) | 5/hour |
| `auth/mfa/verify/` per client IP (feat/admin-ux, `mfa_verify`) | 30/min |
| `products/{id}/assistant/jobs/` per staff (feat/ai-assistant, `assistant_generate`) | 30/hour |
| `products/{id}/images/{image_id}/scenes/` per staff (product scenes, `scene_generate`) | 20/hour |
| Admin panel MFA code checks per account (`verify/`, `confirm/`, `disable/`, `recovery-codes/`; LoginGuard scope `panel_otp`) | 5/min, then exponential lockout (1 → 15 min) |

---

## 2. Endpoints

### 2.0 Index
**I** = `Idempotency-Key` required · **E** = `?export=csv|xlsx` · **P** = paginated. Every write of §2.17 honours an optional `Idempotency-Key`.

| Method | Path | Flags | Success |
|---|---|---|---|
| POST | `auth/login/`, `auth/refresh/` | | 200 |
| POST | `auth/logout/` (body `refresh`, `scope`), `auth/change-password/` | | 204 |
| GET / PATCH | `auth/me/` (PATCH `language`, feat/admin-ux) | | 200 |
| POST | `auth/password-link/` (feat/admin-ux; while a change is pending) | | 200 |
| POST | `auth/password-token/check/` (no bearer), `auth/password-token/set/` (no bearer; 204) (feat/admin-ux) | | 200 / 204 |
| POST | `auth/oauth/{provider}/` (no bearer; M0b) | | 200 |
| GET | `auth/mfa/` (feat/admin-ux) | | 200 |
| POST | `auth/mfa/setup/`, `auth/mfa/confirm/`, `auth/mfa/disable/`, `auth/mfa/recovery-codes/` (feat/admin-ux) | | 200 |
| POST | `auth/mfa/verify/` (no bearer; feat/admin-ux) | | 200 |
| POST | `auth/oauth/{provider}/link/` (M0b) | | 200 |
| DELETE | `auth/oauth/{provider}/` (M0b) | | 204 |
| GET | `orders/` | P E | 200 |
| GET | `orders/status-counts/`, `orders/{id}/` | | 200 |
| POST | `orders/{id}/cancel/`, `orders/{id}/transition/` | I | 200 |
| POST | `orders/{id}/retry-shipment/` | I | 202 |
| POST | `orders/{id}/dispatch/` (Yandex Delivery, §2.18) | I | 202 |
| POST | `orders/{id}/confirm-return/` (§2.18) | I | 200 |
| POST | `orders/{id}/issues/` (§2.18) | I | 201 / 200 |
| GET | `delivery/issues/` (§2.18) | P | 200 |
| POST | `delivery/issues/{id}/resolve/` (§2.18) | I | 200 |
| GET / POST | `delivery/suspension/` (§2.18) | – / I | 200 |
| GET | `products/` | P E | 200 |
| GET | `products/stats/`, `products/{id}/` | | 200 |
| POST | `products/` | | 201 |
| PATCH | `products/{id}/` | | 200 |
| POST | `products/{id}/images/` | multipart | 201 |
| PATCH | `products/{id}/images/{image_id}/` (feat/ai-assistant: alt text) | | 200 |
| DELETE | `products/{id}/images/{image_id}/` | | 204 |
| PUT | `products/{id}/images/order/` | | 200 |
| POST | `integrations/billz/import/` | | 202 |
| GET | `jobs/{id}/` | | 200 |
| GET / POST | `categories/` | P E / JSON or multipart (gap ADM-5.1) | 200 / 201 |
| GET / PATCH / DELETE | `categories/{slug}/` | PATCH JSON or multipart | 200 / 200 / 204 |
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
| POST | `notifications/` | I, multipart | 201 (`draft`) |
| GET / PATCH / DELETE | `notifications/{id}/` | – / multipart, author or superuser / author or superuser | 200 / 200 / 204 |
| POST | `notifications/{id}/test/` | superuser | 200 |
| POST | `notifications/{id}/tests/{test_id}/confirm/` | superuser | 200 |
| POST | `notifications/{id}/approve/` | superuser | 200 |
| POST | `notifications/{id}/return-to-draft/` | superuser | 200 |
| POST | `notifications/{id}/cancel/` (M3) | superuser | 200 |
| POST | `notifications/{id}/stop/` | superuser | 200 |
| GET / POST | `notifications/test-devices/` | superuser | 200 / 201 |
| DELETE | `notifications/test-devices/{id}/` | superuser | 204 |
| GET / PUT | `notification-templates/{status}/` | | 200 |
| GET / POST | `segments/` | P E / – | 200 / 201 |
| GET / PATCH / DELETE | `segments/{id}/` | | 200 / 200 / 204 |
| GET / POST | `segments/{id}/members/` | P E / – | 200 |
| DELETE | `segments/{id}/members/{customer_id}/` | | 204 |
| GET | `appointments/?date=`, `appointments/pending-summary/`, `appointments/meta/`, `appointments/range/` (feat/admin-ux) | | 200 |
| POST | `appointments/{id}/confirm/`, `…/reject/`, `…/attend/` | I | 200 |
| POST | `appointments/` (admin booking, T10) | I optional | 201 |
| POST | `appointments/{id}/reschedule/`, `appointments/{id}/cancel/` (T10) | I optional | 200 |
| GET | `dashboard/`, `nav-counts/` | | 200 |
| GET | `payments/`, `audit-log/` (tiered, §2.13) | P E | 200 |
| GET | `audit-log/facets/` (feat/admin-ux, §2.13) | | 200 |
| POST | `payments/{id}/refunds/{refund_id}/retry/`, `…/resolve/` (§2.12) | I optional | 200 |
| GET / POST | `staff/` (§2.16; POST manager only) | P / I | 200 / 201 |
| GET / PATCH | `staff/{id}/` (manager only) | – / I optional | 200 |
| POST | `staff/{id}/deactivate/`, `staff/{id}/reset-password/` (manager only) | I | 200 |
| POST | `staff/{id}/reactivate/` (manager only) | I optional | 200 |
| POST | `staff/{id}/send-password-link/` (manager only; feat/admin-ux) | I optional | 200 |
| POST | `staff/{id}/reset-mfa/` (manager only; feat/admin-ux) | I optional | 200 |
| GET / POST | `specialists/` (§2.17) | P E / – | 200 / 201 |
| GET / PATCH | `specialists/{id}/` | | 200 |
| PUT / DELETE | `specialists/{id}/photo/` | multipart / – | 200 / 204 |
| PUT | `specialists/{id}/services/`, `specialists/{id}/working-hours/` | | 200 |
| GET / POST | `specialists/{id}/overrides/`, `specialists/{id}/blocks/` | P / – | 200 / 201 |
| DELETE | `specialists/{id}/overrides/{override_id}/`, `specialists/{id}/blocks/{block_id}/` | | 204 |
| GET | `specialists/{id}/conflicts/` | | 200 |
| GET / POST | `services/` | P E / – | 200 / 201 |
| GET / PATCH | `services/{id}/` | | 200 |
| GET / POST | `clinics/` | | 200 / 201 |
| GET / PATCH | `clinics/{id}/` | | 200 |
| GET | `assistant/status/` (feat/ai-assistant, §2.19) | | 200 |
| POST | `products/{id}/assistant/jobs/` (§2.19; throttle `assistant_generate`) | I optional | 202 / 200 |
| GET | `assistant/jobs/{id}/` (§2.19) | | 200 |
| POST | `assistant/jobs/{id}/apply/`, `assistant/jobs/{id}/discard/` (§2.19) | I optional | 200 |
| GET | `/api/v1/health/` | no auth | 200 / 503 |

Shared shapes: `Actor` = `{"id": int, "display_name": str}`, where `display_name` is the staff full name, else the email. `null` actor means system. `CustomerRef` = `{"id": int, "name": str, "phone": str | null, "email": str | null}`; `name` is `""` once the customer is deleted (scrubbed); `phone` is null only for an account without a phone yet (hybrid identity: never on orders and payments, which are phone-gated; a staff booking may name one); `email` (additive, `feat/customer-web`) is the verified e-mail address, null without one or once deleted — show it (or the name) instead of "—" for a phoneless customer. Both are single OpenAPI components (`Actor`, `CustomerRef`) used by every module (orders, payments, appointments).

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
- feat/admin-ux: for an account with the **admin panel MFA** on (below) a correct password opens **no session**; the 200 is `{"mfa_required": true, "challenge": str, "challenge_expires_at": datetime}` instead (OpenAPI `AdminLoginResult` = one of the two shapes; tell them apart by `mfa_required`) and `auth/mfa/verify/` opens the session. Audit `auth.mfa_challenged` `{email, method}` instead of `auth.login_succeeded`.

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

Matching: an existing `SocialIdentity(provider, subject)` whose staff user is active → that user. Otherwise → 401 `social_account_not_allowed` "Bu hisob administrator sifatida ro‘yxatdan o‘tmagan." (same response whether or not the email exists). Identities are created only by **`POST auth/oauth/{provider}/link/`** (bearer + the same body; audit `auth.social_linked`), i.e. by a staff member already signed in with email + password: the session records how its sign-in was proven (`AuthSession.auth_method`: `password` for `auth/login/`, the provider for a social sign-in), and `link/` with a bearer of a session opened by a social sign-in (or of a session from before the field existed) → **403 `permission_denied`** "Ruxsat yo‘q." before the provider is called, so a social sign-in can never attach a further identity. Optional Google-only first-use auto-link (`STAFF_GOOGLE_AUTOLINK`, default **off**) applies only when Google is authoritative for the address: `email_verified == true` and either the email ends with `@gmail.com` or the ID token's `hd` equals the email's domain (and matches `GOOGLE_ALLOWED_HD` when set). Facebook never auto-links.
- 200: same body as `auth/login/`. Audit `auth.login_succeeded` (`{email, method: provider}`) / `auth.login_failed`. With the admin panel MFA on: the challenge answer of `auth/login/` (a first-use Google auto-link is kept), `auth/mfa/verify/` opens the session with `auth_method` = the provider.
- 400 `oauth_exchange_failed` (bad/expired code, verifier mismatch), 401 `social_account_not_allowed`, 429 `throttled`, 503 `provider_unavailable`.
- Field errors: `nonce` `required` (Google only); `redirect_uri` `invalid` when it differs from the provider's configured callback — `GOOGLE_OAUTH_REDIRECT_URI` / `FACEBOOK_OAUTH_REDIRECT_URI`, the Next BFF callbacks `/admin/auth/oauth/{provider}/callback` (production with `OAUTH_MODE=live` requires both); the single `OAUTH_REDIRECT_URI` of M0b is **deprecated**, kept for one release as the fallback of a provider whose own value is empty —; `code_verifier` 43..128. An unknown `provider` → 404. The nonce is compared byte-wise against the ID token's `nonce` claim (no Unicode normalisation), so the client sends exactly the string it put in the authorize URL.
- **Single use across instances:** before the provider is called, the login and `link/` views claim the body's `code_verifier` with an atomic `cache.add("oauth:verifier:<sha256(code_verifier)>", 1, 660)` in the shared Redis; a verifier presented a second time (another tab, a replayed request, another API instance) → 401 `social_account_not_allowed` (the uniform social error) without a provider call. The Next server generates a fresh verifier per authorize redirect, so a legitimate flow never repeats one.
- `link/` conflicts: 409 `conflict` "Bu provayder allaqachon bog‘langan." when the caller already has an identity for that provider, "Bu ijtimoiy hisob allaqachon bog‘langan." when the subject belongs to another staff member. `DELETE` without a linked identity → 404.
- Policy: an identity linked to an inactive staff member is refused like an unknown one; auto-link never creates a second Google identity for a staff member who already has one.
- M2 (customer contract §6.14): under the production settings (`config.settings.production`, staging included) with `OAUTH_MODE=fake`, `POST auth/oauth/{provider}/` and `POST auth/oauth/{provider}/link/` answer 404 before the body is read (the OAuth registry refuses the provider), because the fake accepts any `fake:<subject>:<email>` code with a caller-chosen nonce; `DELETE auth/oauth/{provider}/` and email + password sign-in keep working. Under the local/test settings the fake works as above.

**`DELETE auth/oauth/{provider}/`** (bearer; M0b) → 204. Unlinks the caller's identity for that provider. Audit `auth.social_unlinked`.

**`POST auth/change-password/`**. Body `{"current_password": str, "new_password": str}` → 204.
- Revokes the user's **other** sessions (`password_changed`). Audit `auth.password_changed` `{forced}` (feat/admin-ux: `true` when the change cleared a pending `must_change_password`).
- feat/admin-ux: also clears `must_change_password`, stamps `password_changed_at` and revokes every open password link. Allowed while a change is pending (below).
- Field errors:
  - `current_password` `password_incorrect` "Joriy parol noto‘g‘ri.".
  - `new_password`: Django validators, min length 12 (`password_too_short` "Kamida 12 ta belgi.", `password_too_common` "Juda oddiy parol.", `password_entirely_numeric` "Faqat raqamlardan iborat bo‘lmasin.", `password_too_similar` "Login bilan juda o‘xshash."), plus `password_unchanged` "Yangi parol eskisidan farq qilsin.".

**`GET auth/me/`** → `Me`:
```jsonc
{"id": 1, "email": "admin@glowderma.uz", "display_name": "Administrator", "is_superuser": false,
 "is_manager": true,                                   // gap closure: may manage staff (§2.16)
 "linked_providers": ["google"],                       // social identities linked to this staff account
 "session_expires_at": "2026-10-25T00:00:00+05:00",
 "auth_method": "password"}                            // M3: how the current session's sign-in was proven
```
- `is_manager` (gap closure, §2.16) = the caller may manage staff; the UI shows the staff screen ("Xodimlar") only then. It is read from the database on every request, so a removed role applies at once.
- `auth_method` (M3) = `AuthSession.auth_method` of the caller's session: `password` (`auth/login/`) or the provider of a social sign-in (`google`, `facebook`); `null` for a session opened before the method was recorded (`accounts.0007`). The same `Me` is the `user` of the `auth/login/` and `auth/oauth/{provider}/` answers. Only a `password` session may link an identity (`link/` → 403 otherwise, above), so the UI hides "Bog‘lash" unless `auth_method` is `password`.
- feat/admin-ux adds `"must_change_password": bool`, `"language": "uz"|"ru"|"en"` (`StaffUser.language`), `"mfa_enabled": bool` (a confirmed admin-panel TOTP device; the ops console's devices do not count) and `"capabilities": {"staff_email_invite": bool}` — `true` when password links can be e-mailed: `ADMIN_PANEL_URL` is set and e-mail does not run its fake adapter under the production settings. When `false` the UI shows the e-mail delivery options disabled with the reason.

**`PATCH auth/me/`** (feat/admin-ux). Body `{"language": "uz"|"ru"|"en"}` → 200 `Me`. Missing or another value → 400 `required` / `invalid_choice`. Not audited (a preference). Allowed while a change is pending.

**Forced password change** (feat/admin-ux, overrides GST-3). `StaffUser.must_change_password` is set for every account whose password someone else chose: a generated temporary password (`POST staff/`, `reset-password/`, `resetstaffpassword` without `--password-stdin`), an account created or reset by e-mail link only, `createstaff` / `resetstaffpassword --password-stdin` with `--must-change`. While it is set, **every** admin route answers **403 `password_change_required`** "Avval parolingizni o‘zgartiring." (checked in `IsActiveStaff`, so it holds for password and social sessions on every device, managers and superusers included) except the allow-list: `GET`/`PATCH auth/me/`, `POST auth/change-password/`, `POST auth/password-link/` (and `auth/logout/`, `auth/refresh/`, which need no bearer). Sign-in itself still succeeds (the UI reads `user.must_change_password` and shows the change screen). The ops console refuses the password step of such an account ("Avval admin panelda o‘z parolingizni o‘rnating, so‘ng konsolga qayta kiring.", audit `console.login_failed` with `reason` `password_change_required`; no lockout counted). Existing accounts are not forced.

**`POST auth/password-link/`** (feat/admin-ux; bearer, allowed while pending). No body → 200 `{"email_masked": "a***@glowderma.uz", "activation": {"sent": true, "expires_at": "…"}}`: e-mails the caller a `reset` link — for an account that must change its password but cannot type the current one (a social sign-in, an e-mail-link account). 409 `conflict` "Parolingiz o‘rnatilgan: …" when no change is pending, "E-pochta orqali yuborish sozlanmagan." when `capabilities.staff_email_invite` is false; 429 after 5/hour (`staff_password_link`). Audit `staff.password_link_sent` `{purpose: "reset", via: "self", expires_at, link_id}`.

**`POST auth/password-token/check/`** (feat/admin-ux; **no bearer**). Body `{"token": str ≤128}` → 200 `{"email_masked", "full_name", "purpose": "invite"|"reset", "expires_at"}` for the admin panel's `/set-password?token=…` page. An unknown, used, revoked or expired link, or one of an inactive account → one uniform **400 `token_invalid`** "Havola yaroqsiz yoki muddati o‘tgan. Menejerdan yangi havola so‘rang." (`errors: []`). 429 after 30/min per client IP (`staff_password_token`).

**`POST auth/password-token/set/`** (feat/admin-ux; **no bearer**). Body `{"token": str, "new_password": str}` → **204** (no session is opened: the person signs in next). Under the staff row lock the link must still be open (else 400 `token_invalid`); `new_password` gets the change-password field errors (validated for the link's account) and `password_unchanged` when it is the current password (the link stays usable after a refused password). On success: the password is set, `must_change_password` cleared, `password_changed_at` stamped, the link used and every other link revoked, **every** session revoked (`password_changed`), the ops console sessions ended and, on commit, the login lockouts lifted. Audit `staff.password_set` `{purpose, link_id, revoked_count[, console_sessions]}` (actor: the person) plus `auth.session_revoked` per session. Same throttle as `check/`.

**Password links** (`StaffPasswordToken`): 32 random bytes (base64url, 43 characters), stored as SHA-256 only, single use, valid `STAFF_INVITE_TTL_HOURS` (default 72, 1..168); issuing one revokes the earlier open links of the account, and a password set through a link or `change-password/`, a temporary-password reset and a deactivation revoke them too. The link is `{ADMIN_PANEL_URL}/set-password?token=<token>` and travels only in the e-mail: outbox topic `email.staff_password_link` with payload `{link_id, lang, purpose, to, link, name}` where `to`, `link` and `name` are sealed (`core.secretbox`, AAD bound to the token id) and blanked once the message is final (sent, refused, superseded, dead). The handler sends only while the link is still open, claims `StaffPasswordToken.sent_at` before the relay call (never sent twice; a relay that accepted nothing un-claims and retries; a refusal clears it), in the account's `language` (texts `email.staff_link.*`, templates `accounts/email/staff_link.{txt,html}`). Done/dead messages of the topic are deleted after 24 h by `accounts.cleanup_auth`.

**Admin panel MFA** (feat/admin-ux; optional TOTP, off by default, each person's own choice — nobody can mandate it). The panel's authenticator entry ("Glow Derma Admin") is **separate from the ops console's** ("Glow Derma Ops"): its devices are namespaced by name (`Admin panel`, recovery codes `Admin panel zaxira kodlar`), so enabling, using, disabling or resetting it never touches a console device and a panel code never opens the console (the console's enrolment window and four-eyes rule stay). TOTP: 30 s steps, 6 digits, ±1 step; a code verifies once (django-otp `last_t`, under the device row lock), an older or repeated code is refused. Recovery codes: ten `xxxxx-xxxxx` codes (case, spaces and dashes ignored), stored only as keyed hashes, each single use. Every code check (sign-in, `confirm/`, `disable/`, `recovery-codes/`) shares the per-account lockout `panel_otp` (5 wrong codes a minute lock 1 → 15 min; and, whatever the pace, every 10th wrong code in a row — no correct code between, none more than 48 h apart — locks 15 min, then 1 h, 4 h, 16 h, 24 h; 429 with `Retry-After`; audit `auth.mfa_locked` `{wait}` and alert on a locked attempt, `{wait, failures}` and alert when the 10-in-a-row lock starts); every password re-check shares the `change_password` lockout. Every route below except `verify/` needs a bearer and answers 403 `password_change_required` while a change is pending. Error codes: **400 `mfa_code_invalid`** "Kod noto‘g‘ri yoki allaqachon ishlatilgan. Ilovadagi yangi kodni kiriting." (wrong, used, malformed, or not this account's panel device; `errors: []`), **400 `mfa_challenge_invalid`** "Kirish muddati tugadi yoki bekor qilindi. Email va parolni qaytadan kiriting." (`verify/` only), `current_password` `password_incorrect` "Joriy parol noto‘g‘ri.".

**`GET auth/mfa/`** → 200 `{"enabled": bool, "recovery_codes_left": int}` (0 while off).

**`POST auth/mfa/setup/`** Body `{"current_password": str}` → 200 `{"device_id": int, "otpauth_uri": "otpauth://totp/Glow%20Derma%20Admin%3A<email>?secret=…&algorithm=SHA1&digits=6&period=30&issuer=Glow%20Derma%20Admin", "manual_key": "ABCD EFGH …"}`: a new **unconfirmed** device (an earlier unconfirmed panel device is replaced); the UI draws the QR code from `otpauth_uri` and shows `manual_key`. 409 `conflict` "Ikki bosqichli himoya allaqachon yoqilgan." when already on. Not audited (nothing is on yet).

**`POST auth/mfa/confirm/`** Body `{"device_id": int, "code": str}` → 200 `{"recovery_codes": [10 × "xxxxx-xxxxx"]}` — **shown once**. Confirms the pending device, removes the account's other panel TOTP devices, issues the recovery codes and marks the current session verified (`AuthSession.mfa_verified_at`). 400 `mfa_code_invalid` for a wrong code or a `device_id` that is not the caller's pending panel device; 409 when already on. Audit `auth.mfa_enabled` `{recovery_codes: 10}`.

**`POST auth/mfa/disable/`** Body `{"current_password": str, "code_or_recovery": str}` (a 6-digit code or one unused recovery code) → 200 `{"enabled": false, "recovery_codes_left": 0}`; the panel devices and codes are deleted (console devices stay). 409 `conflict` "Ikki bosqichli himoya yoqilmagan." when off. Audit `auth.mfa_disabled` `{factor: "totp"|"recovery", totp_devices, recovery_codes}`.

**`POST auth/mfa/recovery-codes/`** Body `{"current_password": str, "code": str}` (a TOTP code) → 200 `{"recovery_codes": [10]}` (shown once; the old codes stop working). 409 when off. Audit `auth.mfa_recovery_codes_regenerated` `{recovery_codes: 10}`.

**`POST auth/mfa/verify/`** (**no bearer**). Body `{"challenge": str ≤128, "code": str}` or `{"challenge", "recovery_code": str}` (exactly one; neither → `code` `required`, both → `code` `invalid`) → 200 the `auth/login/` session answer; the session's `auth_method` is the first step's (`password` or the provider) and `mfa_verified_at` is stamped. The challenge (32 random bytes, base64url; the cache keeps only its SHA-256) lives **5 minutes**, is **single use**, allows **5 attempts**, and dies when the account's password changes, the account is deactivated, its panel MFA is reset or disabled, or (social first step) the provider identity is unlinked → 400 `mfa_challenge_invalid` (sign in again). A wrong code → 400 `mfa_code_invalid` (the challenge stays for its remaining attempts). 429 for the account lockout or after 30/min per client IP (`mfa_verify`). Audit `auth.mfa_succeeded` `{factor, method}`, `auth.login_succeeded` `{email, method, mfa: factor}`, plus `auth.mfa_recovery_used` `{remaining}` for a recovery code; a wrong code `auth.mfa_failed` `{factor, context: login|setup|disable|recovery_codes}`.

**Lost phone:** sign in with a recovery code (then disable and re-enable, or ask for new codes), else a manager's `POST staff/{id}/reset-mfa/` (§2.16) or the operator's `manage.py resetstaffmfa <email>`.

### 2.2 Orders
**`GET orders/`** → `Page<OrderSummary>`

| Param | Values | Default | Rule |
|---|---|---|---|
| `status` | OrderStatus | – | |
| `search` | str ≤64, trimmed, case-insensitive | – | Matches if any holds: order id contains the term without a leading `№`; the term has ≥3 digits and the customer phone contains them; the customer name contains the term (folded, §1.4) |
| `fulfilment` | `delivery`, `pickup` | – | |
| `payment` | PaymentStatus | – | |
| `period` | `today`, `week`, `month`, `all` | `all` | `today` = current Tashkent day; `week` = rolling `(now−7×24h, now]`; `month` = Tashkent calendar month to date |
| `date_from`, `date_to` | date | – | Inclusive Tashkent days on `created_at`, ANDed with `period`; `date_from > date_to` → 400, field `date_from`, code `invalid` |
| `customer` | int | – | Customer id |
| `provider` | `bts`, `yandex` | – | The shipment's carrier (Yandex Delivery, §2.18); pickup orders never match |
| `stage` | ShipmentStage | – | A Yandex shipment's stage (§2.18) |
| `attention` | `open`, `urgent` | – | The order has an open delivery issue / an open urgent one (§2.18) |
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
 "cancellation": null,                    // | {"reason": CancellationReason, "note": str | null}
 "delivery": {"provider": "bts", "method": null, /* … every Yandex key null … */ "open_issues": []}}   // OrderDelivery | null (pickup), §2.18
```

| `shipment.state` | Other keys |
|---|---|
| `not_required` | none (pickup) |
| `not_created` | `pending: bool` |
| `failed` | `last_attempt_at: datetime`, `attempts: int`, `pending: bool` |
| `created` | `tracking_number: str`, `tariff: int`, `updated_at: datetime`, `via: "webhook"\|"polling"` |

`pending` is true while a BTS create message for the order is queued or in flight.

A Yandex Delivery shipment (§2.18) keeps this union BTS-shaped: `not_created` until a courier claim is accepted, `failed` when a dispatch failed before any acceptance, then `created` with `tracking_number: ""` (the claim id is never a tracking number) and `tariff` = the accepted offer; its progress is `delivery.stage`.

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
    "image_thumbnail_url": "https://media.example.uz/products/4/a1b2_400.webp",  // §1.4 image variants
    "image_medium_url": "https://media.example.uz/products/4/a1b2_1000.webp",
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
                       "cancel": true, "retry_shipment": false,
                       "dispatch": false, "confirm_return": false}}   // Yandex Delivery, §2.18
```
```jsonc
// Refund
{"id": 7, "state": "requested", "provider": "click", "amount": 784400,
 "requested_at": "2026-10-24T12:01:00+05:00", "updated_at": "2026-10-24T12:01:00+05:00",
 "completed_at": null,                              // set on succeeded
 "manual_reason": null}                             // M3: payme | no_paydoc | fake_mode | null
```
- `Refund.manual_reason` (M3): why the refund is (or was) `manual_required` — `payme` (every Payme refund: a cabinet refund), `no_paydoc` (a Click payment without Click's payment id), `fake_mode` (Click fake under the production settings); `null` for a refund that was never manual. It is the reason recorded when the refund became manual (at creation — the `refund_requested.manual_reason` of §4 — or by the outbox re-check before anything is sent to Click, which records no event) and stays after the refund is resolved. The same object appears in `orders/{id}/` `refunds[]` and in the `payments/` rows (§2.12); §5.1 picks the refund-state line by it.
- `payment.status` stays `paid` while a refund is not `succeeded`. It becomes `refunded` when refunds for the full amount succeed.
- `cashback[].kind` is `debit`, `credit` or `reversal` (adjustments are never linked to orders). `base` and `percent` are set on credits only.
- `available_actions`:
  - `transitions` = the manual transitions (`processing` → `handed_to_courier` for delivery, `delivered` for pickup; `handed_to_courier` → `delivered`).
  - `cancel` = status is `awaiting_payment`, `processing` or `handed_to_courier`.
  - `retry_shipment` = delivery ∧ `processing` ∧ BTS shipment `failed` ∧ ¬`pending` (never for a Yandex shipment).
  - `dispatch` (Yandex Delivery) = `POST orders/{id}/dispatch/` would be accepted now (§2.18); false for BTS and pickup.
  - `confirm_return` (Yandex Delivery) = the parcel is `returning` / `returned` and the return of the claim that carried it is not confirmed yet (§2.18).
  - A Yandex order has **no manual transition**, except `delivered` while a `parcel_unaccounted` issue is open (§2.18).

**`POST orders/{id}/cancel/`** (I). Body `{"reason": str}`, trimmed, 3..500.

| Current status | Result |
|---|---|
| `awaiting_payment` | Cancelled. Reservations and the cashback hold are released; a `pending` payment becomes `failed`. |
| `processing` | Cancelled. If paid, a Refund is created. Reversals are posted. If a BTS shipment exists, BTS cancel runs first (synchronously) and must succeed. |
| `handed_to_courier` | Allowed only if the synchronous BTS cancel succeeds (BTS status 100); otherwise 409 `not_cancellable` |
| `delivered`, `cancelled` | 409 `not_cancellable` |
| any status, Yandex Delivery shipment | carrier-aware: Yandex is asked first, outside the transaction (§2.18 "Cancel of a Yandex order") |

- **Effects** (one transaction; lock order Order → Payment → LoyaltyAccount):
  - `status=cancelled`; `cancellation={"reason":"admin","note":reason}`.
  - Refund: Click `requested`, submitted through the outbox; Payme `manual_required`. M2 (customer contract §2.13.0): a Click refund is `manual_required` too — never sent to Click — when the payment has no Click payment id (`click_paydoc_id`: seeded or legacy rows) or the Click port is fake under the production settings; staff pay those out by hand and close them with `…/resolve/` (§7 item 18). `payment.status` stays `paid`.
  - One `reversal` ledger entry per unreversed debit and credit of the order (unique `reverses_id`).
  - Push through the outbox (template `cancelled`).
  - Events `cancelled` and `refund_requested` (§4); audit `order.cancelled`.
- **Response:** 200 `Order`.
- **Errors:**
  - 400 `reason` (`min_length` "Kamida 3 ta belgi kiriting.", `max_length` "500 belgidan oshmasin."), top-level message "Sababni kiriting.".
  - 409 `not_cancellable` "Bu buyurtmani bekor qilib bo‘lmaydi." (BTS refusal: "BTS buyurtmani bekor qilmadi: posilka kuryerda."; the Yandex messages are in §2.18).
  - Yandex Delivery only: 409 `return_requested`, 409 `conflict` (another Yandex operation of the order is running, Yandex has not shown the cancel yet — its answer lost included —, or the order stayed busy right after Yandex answered) (§2.18).
  - 503 `provider_unavailable` (a Yandex order: only when nothing can have reached Yandex, §2.18).

**`POST orders/{id}/transition/`** (I). Body `{"status": OrderStatus}`.
- The target must be in `available_actions.transitions`, else 409 `invalid_transition` "Bu holatga o‘tib bo‘lmaydi.". A non-OrderStatus value → 400 `invalid_choice`.
- Effects: status set; event `status_changed`; push through the outbox (status template); audit `order.status_changed`.
- 200 `Order`.

**`POST orders/{id}/retry-shipment/`** (I). Empty body.
- Precondition: delivery ∧ `processing` ∧ BTS shipment `failed`. Otherwise (a Yandex shipment too) 409 `invalid_transition` "BTS ga qayta yuborib bo‘lmaydi.".
- While `pending` → 409 `conflict` "BTS so‘rovi allaqachon yuborilmoqda.".
- Effect: an outbox BTS create; the handler looks the order up by `clientId` before creating. Audit `order.shipment_retry_requested`.
- 202 `Order` with `shipment.pending=true`. The outcome arrives later as `shipment_created` or `shipment_failed` with `manual=true`.

**Shared outcomes of the order actions** (`cancel`, `transition`, `retry-shipment`; the busy-order outcome also covers the refund `retry` / `resolve` of §2.12):
- **Busy order.** Every action locks the order row first and waits at most `ADMIN_LOCK_WAIT` (5 s; a BTS create holds the row across its bounded calls). A Yandex cancel waits longer (§2.18): recording what Yandex answered waits up to 15 s for the row, then up to 5 s more to write that record without it (answering its own 409), and the Yandex snapshots the cancel applies wait for the row without a limit. When the row stays locked → 409 `order_busy` (feat/admin-ux; `conflict` before) "Buyurtma boshqa amal bilan band. Birozdan so‘ng qayta urinib ko‘ring.". Nothing is written; retry with the same `Idempotency-Key`.
- **Purchase conflict** (`cancel`, `transition`, `retry-shipment` only; the refund operations never touch the loyalty purchase). When the order's loyalty purchase belongs to another customer (data inconsistency surfaced by `loyalty`) → 409 `conflict` "Buyurtma xaridi boshqa mijozga tegishli. Ma’lumotlarni tekshiring.". Nothing is written.

### 2.3 Products and Billz
```jsonc
// Product
{"id": 1, "billz_id": "4607012301", "billz_name": "HYALURONIC SERUM 30ML", "volume": "30 ml",   // billz_* and volume: Billz-owned, read-only
 "category": "serums",                       // slug | null
 "price": 185000, "billz_price": 185000,     // price = admin (authoritative); billz_price read-only | null
 "stock": 12,                                // Billz-owned, read-only
 "promo": false, "status": "active", "is_new_from_billz": false,
 "texts": {"uz": {"name": "Gialuron kislotali serum", "short_description": "…",   // feat/ai-assistant: short_description
                  "description": "…", "ingredients": "…", "usage": "…"},
           "ru": {"name": "…", "short_description": "…", "description": "…", "ingredients": "…", "usage": "…"},
           "en": {"name": "", "short_description": "", "description": "", "ingredients": "", "usage": ""}},
 "main_image_url": "https://media.example.uz/products/1/9f2c.webp",   // = images[0].url | null
 "main_image_thumbnail_url": "https://media.example.uz/products/1/9f2c_400.webp",   // §1.4 image variants
 "main_image_medium_url": "https://media.example.uz/products/1/9f2c_1000.webp",
 "images": [{"id": 31, "url": "https://media.example.uz/products/1/9f2c.webp",
             "thumbnail_url": "https://media.example.uz/products/1/9f2c_400.webp",
             "medium_url": "https://media.example.uz/products/1/9f2c_1000.webp", "position": 0,
             "alt": {"uz": "", "ru": "", "en": ""}}],   // feat/ai-assistant: alt text ≤160 per language; "" = the product name
 "last_change": {"at": "2026-10-22T18:04:00+05:00", "actor": {"id": 1, "display_name": "Administrator"},
                 "kind": "price_changed", "old_price": 175000, "new_price": 185000},   // null if never edited
 "slug": "gialuron-kislotali-serum",          // B-W1: the website URL segment (unique, never only digits)
 "seo": {"uz": {"title": "", "description": ""},   // B-W1: optional page title (≤120) / meta description (≤300)
         "ru": {"title": "", "description": ""},   // per language; "" = the website builds one
         "en": {"title": "", "description": ""}},
 "seo_status": {"ok": false,                       // B-W1 «SEO holati»: ok = no error and no warning
                "issues": [{"code": "description_missing", "severity": "error", "language": "uz",
                            "message": "Tavsif (o‘zbekcha) yo‘q."}]},   // SeoIssueCode / SeoSeverity, errors first
 "created_at": "2026-06-01T09:00:00+05:00", "updated_at": "2026-10-22T18:04:00+05:00"}   // updated_at moves on gallery changes too (B-W1)
```

**`GET products/`** → `Page<Product>`

| Param | Values | Rule |
|---|---|---|
| `search` | str ≤64 (longer input is trimmed to 64) | Case-insensitive substring over `texts.{uz,ru,en}.name`, `billz_name` and `billz_id` (pg_trgm GIN), folded on both sides (§1.4: apostrophe variants, `ё` = `е`; the stored `search_text` column is folded since `catalog.0002`, and since `catalog.0006` every apostrophe is removed from it and from the term, so `kopik` finds "ko‘pik"). An ASCII-digit term of ≤18 digits also matches `id` exactly. |
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
| `billz_id` | Required on create. `^\d{6,14}$` (`invalid` "Billz ID yoki shtrix-kod 6–14 ta raqamdan iborat bo‘lsin."); `unique` "Bu Billz ID bilan mahsulot allaqachon bor."; must exist in Billz (`billz_not_found` "Billz’da bunday mahsulot topilmadi."). Setting or changing it refreshes `billz_name`, `volume` (only when Billz provides one: with live Billz and no `BILLZ_VOLUME_FIELD` the stored volume is kept), `stock` and `billz_price`. M3: the create, and a PATCH that sets or changes `billz_id`, ask live Billz for the product (bounded to about 25 s); when Billz, its shared cache or the answer fails → 503 `provider_unavailable` "Tashqi xizmat javob bermadi. Qayta urinib ko‘ring." and nothing is written (safe to retry with the same `Idempotency-Key`); every other PATCH never calls Billz. M2: while the Billz port is fake under the production settings (customer contract §2.3, §6.14) a create, or a PATCH that sets or changes `billz_id`, answers 409 `conflict` "Billz sinov rejimida: Billz ma’lumotlarini olib bo‘lmaydi. Billz ulanganidan keyin qayta urinib ko‘ring." (nothing written; a fake must never invent Billz fields or stock); every other PATCH works as usual. |
| `texts.uz.name` | Trimmed 2..120 (`min_length` "O‘zbekcha nomni kiriting (kamida 2 belgi).", `max_length` "120 belgidan oshmasin.") |
| `texts.*.description`, `texts.*.ingredients` / `texts.*.usage` / `texts.ru.name`, `texts.en.name` | ≤2000 / ≤1000 / ≤120 ("{n} belgidan oshmasin."). Missing on create → `""`. |
| `texts.*.short_description` (feat/ai-assistant) | ≤300 (`max_length`); missing on create → `""`. A one-paragraph summary under the name; the customer API shows it in `ProductDetail.short_description` |
| `price` | int 1000..100000000 (`min_value` "Narx kamida 1 000 so‘m bo‘lsin.", `max_value` "Narx juda katta.") |
| `category` | slug or `null`; an unknown slug → `does_not_exist` |
| `promo` | bool, default `false` |
| `status` | `active` or `hidden`, default `hidden` |
| `slug` (B-W1) | optional; on create derived from `texts.uz.name` (else `billz_name`, else `mahsulot-<billz_id>`; Uzbek apostrophes dropped, Cyrillic transliterated, `-2`, `-3`… when taken); never changes on its own. When sent: `^[a-z0-9]+(-[a-z0-9]+)*$`, not only digits, ≤140 (`invalid` "Faqat lotin kichik harflari, raqamlar va chiziqcha (faqat raqam emas).", `max_length`), unique (`unique` "Bu manzil (slug) boshqa mahsulotda bor.", a lost race too). Changing it changes the website URL (no redirect history kept) |
| `seo.{uz,ru,en}.{title,description}` (B-W1) | optional, merged leaf by leaf; title ≤120, description ≤300 (`max_length`); trimmed. The customer API shows each only in its own language |
| Active rule | Whenever the resulting status is `active`: `category` must be set (`required_for_active` "Faol mahsulot uchun kategoriyani tanlang."), the product needs ≥1 image (field `images`, `required_for_active` "Faol mahsulotda kamida bitta rasm bo‘lsin.") and `price ≥ 1000` (field `price`, `min_value` "Narx kamida 1 000 so‘m bo‘lsin." — imported products may carry `price = 0` while hidden). A create with `active` therefore always fails; the FE creates `hidden`. |

- Every successful create or PATCH sets `is_new_from_billz=false`.
- Responses of `POST products/` and `PATCH products/{id}/`: 201/200 `Product`, 400 `validation_error`, 404 (PATCH), 409 `conflict` (fake Billz in production), 503 `provider_unavailable` (the Billz lookup above).
- `last_change.kind` is chosen by priority: `created` > `price_changed` > `activated`/`hidden` > `promo_added`/`promo_removed` > `updated`. Image calls do not change `last_change`.
- Audit `product.created` / `product.updated` (field diff; B-W1 adds `slug` and `seo.<lang>.<title|description>` leaves; feat/ai-assistant adds `texts.<lang>.short_description` and `images.<image_id>.alt.<lang>` leaves).
- **Concurrent change (feat/ai-assistant, server-internal):** the AI assistant's apply (§2.19) writes through the same service with the values it read at generation time; under the product row lock any of them that changed meanwhile → 409 `product_changed` "Mahsulot boshqa joyda o‘zgartirildi: sahifani yangilab, qayta urinib ko‘ring." and nothing is written. A plain `PATCH products/{id}/` never answers it.
- **«SEO holati» checklist** (B-W1, `catalog.seo`, server-computed on every Product, no extra query): errors — `name_missing` (uz), `description_missing` (uz), `images_missing`; warnings — `name_translation_missing` (ru/en), `description_missing` (ru/en), `description_short` (<80), `alt_text_fallback` (an image without its own uz alt text and no uz name: alt text falls back to the Billz name), `category_missing`, `seo_title_short` (<15) / `seo_title_long` (>60), `seo_description_short` (<50) / `seo_description_long` (>160); hints (`info`) — `seo_title_missing`, `seo_description_missing` (the website builds a fallback). Messages are Uzbek; ordered errors → warnings → hints.

- PATCH request shape: every field optional and `texts` merged leaf by leaf — any subset of languages and leaves (OpenAPI `PatchedProductPatchRequest`).

**`GET products/{id}/`** → `Product`; 404.

**`POST products/{id}/images/`** (multipart, one part `file`) → 201 `{"id": 32, "url": "…", "thumbnail_url": "…", "medium_url": "…", "position": 1}`; the two variants (§1.4) are written with the file, and a failure to write them fails the upload.
- Limits: ≤5 MiB (field `file`, `file_too_large`); must decode in Pillow as JPEG, PNG or WebP (field `file`, `unsupported_image`; a missing part → `file` `required`); ≤8 images per product (field `images`, `max_items` "Ko‘pi bilan 8 ta rasm.").
- The file is re-encoded in the same format, EXIF stripped, longest side ≤2400 px, and appended last.
- Audit `product.image_added`.

**`PATCH products/{id}/images/{image_id}/`** (feat/ai-assistant; JSON) Body `{"alt": {"uz"?: str, "ru"?: str, "en"?: str}}` — each leaf ≤160 (`max_length`), trimmed, merged leaf by leaf; `""` clears it (the website then uses the product name) → 200 `ProductImage`. 404 if the image belongs to another product. Like the other image calls it moves `updated_at` but not `last_change`. Audit `product.image_updated` (changed `images.<image_id>.alt.<lang>` leaves; nothing changed → no row).

**`DELETE products/{id}/images/{image_id}/`** → 204; positions are compacted. 409 `conflict` "Faol mahsulotda kamida bitta rasm bo‘lsin." for the last image of an active product. 404 if the image belongs to another product.

**Image shape** (product scenes): `ProductImage` also carries `width`, `height`, `source` (`upload` | `studio` | `ai_scene`), `derived_from_id` (the image a scene was built from, or null), `product_box` (`[x0, y0, x1, y1]`, 0-1, or null) and `renditions = {"portrait": R | null, "square": R | null}` with `R = {crop: [x, y, w, h] (0-1), mode: "auto" | "manual", url, medium_url, thumbnail_url}` — screen-shaped WebP copies (4:5 for cards, the product page and the cart; 1:1 for rails, feeds, app cards and thumbnails). The image itself never changes.

**`PUT products/{id}/images/{image_id}/renditions/`** (optional `Idempotency-Key`; JSON) Body `{"portrait"?: [x, y, w, h] | "auto" | null, "square"?: …}`: a crop (0-1 of the image, the shape's ratio in pixels ±2 %, inside the image, ≥ 200 px on its short side), `"auto"` (framed on `product_box` with a 14 % margin, else the largest centred crop; when the product cannot fit the shape inside the photo, an automatic crop reaches up to half a side past the edges, filled with a blurred copy of the photo, so the product is never cut) or `null` (removed); a shape left out is kept → 200 `ProductImage`. Field errors per shape: `invalid_crop`, `crop_too_small`. 404 if the image belongs to another product. Replaced files are deleted after commit; deleting an image deletes its renditions. Audit `product.image_renditions` (`{image_id, renditions: {ratio: crop}}` before/after).

**`PUT products/{id}/images/order/`**. Body `{"image_ids": [32, 31]}`, exactly the current set (`mismatch`; `[]` is the exact set of a product without images → 200) → 200 `{"images": [ProductImage…]}`. Audit `product.images_reordered`.

**`POST integrations/billz/import/`**. Body `{}` or `{"mode": "delta"|"full"}` (default `delta`) → 202 `Job`.
- Single-flight: if a `billz_import` job is queued or running, that job is returned. A `failed` job never blocks a new import.
- M2: while the Billz port is fake under the production settings (`DEV_FEATURES_ALLOWED` false; customer contract §2.3, §6.14) the import is **refused**: 409 `conflict` "Billz sinov rejimida: Billz ma’lumotlarini olib bo‘lmaydi. Billz ulanganidan keyin qayta urinib ko‘ring." and no job is created, because a fake would invent products and stock that customers then pay against (online selling needs live Billz; the customer catalog shows every product unavailable in that posture). A job queued before such a switch fails when the worker claims it (`error.code = "billz_fake_mode"`, same message) without calling the port; `billz.last_sync_at` does not move. Under the local/test settings the fake import works as below.
- Fake adapter in M1 (`BILLZ_MODE`, default `INTEGRATIONS_MODE`). Products are upserted by `billz_id`:
  - new → `hidden` with `is_new_from_billz=true`;
  - existing → only the Billz-owned fields change (`billz_name`, `volume`, `billz_price`, stock); the upsert never moves an admin-owned field or `last_change`.
- M3, live Billz, `mode = "full"` (the reconciliation; the daily beat runs one): the listing is used only when it is complete and plausible (every page, the count stable while paging, as many distinct products as Billz counts, at least one product of the configured shop, not fewer than `BILLZ_FULL_SYNC_MIN_RATIO` of the previous full count); otherwise the job fails with `error.code = "billz_listing_incomplete"` and nothing is applied or hidden. After the upserts, **the system hides** every `active` product Billz no longer lists: `status = hidden`, `last_change = {"kind": "hidden", "actor": null, …}` (the system), audit `product.updated` (`status` diff, actor `system`, reason `billz_missing: …`). Never more than `max(3, active − ceil(ratio × active))` products in one run (else the job fails `billz_listing_incomplete` and nothing is hidden), never a product created, edited or looked up in Billz after the listing started, and never deleted (texts, images and history stay; staff re-activate it). A delta never hides.
- `Job.result` counts: `created` (new rows), `updated` (an existing product whose Billz-owned field or stock changed), `unchanged` (a `synced_at` refresh alone), `new_from_billz` (= `created`); a `full` run adds `hidden` (products the reconciliation hid). `Job.progress` is `{"done", "total"}`.
- Sets `billz.last_sync_at`. Audit `billz.import_started` (actor = the staff who started it) / `billz.import_finished` (written by the worker, or by the stale-job reaper with `error.code = "worker_lost"`).
- Error codes of a failed import job — `Job.error` `{code, message}`, the full table in §2.14: `provider_unavailable` (Billz did not answer or the run ran out of time), `billz_listing_incomplete` (the full listing above) and `billz_auth_failed` (Billz refused the integration key: `BILLZ_SECRET_KEY` must be checked) carry the generic message "Tashqi xizmat javob bermadi. Qayta urinib ko‘ring." (the code tells them apart); `billz_fake_mode` (M2) carries "Billz sinov rejimida: Billz ma’lumotlarini olib bo‘lmaydi. Billz ulanganidan keyin qayta urinib ko‘ring."; `worker_lost` "Jarayon uzilib qoldi. Qayta urinib ko‘ring."; `server_error` "Kutilmagan xato.".
- Routes: the views live in `catalog` (an admin view sits above `integrations` in the layering); the paths are unchanged.

### 2.4 Categories (addressed by slug)
```jsonc
// Category
{"slug": "serums", "name": {"uz": "Serumlar", "ru": "Сыворотки", "en": "Serums"},
 "image_url": "https://media.example.uz/categories/2/77aa1b2c3d4e5f60.webp",   // gap ADM-5.1 | null
 "image_thumbnail_url": "…", "image_medium_url": "…",                          // §1.4 image variants | null
 "position": 0, "is_active": true, "product_count": 42,
 "seo": {"uz": {"title": "", "description": ""}, "ru": {…}, "en": {…}},   // B-W1, as products
 "seo_status": {"ok": false, "issues": […]},   // B-W1: name_translation_missing, image_missing, no_active_products, seo_* bounds/hints
 "created_at": "…", "updated_at": "…"}
```
- `GET categories/` (P E; ordered by `position`; filter `is_active`).
- **Bodies** (gap ADM-5.1, open question GC-4): `POST` and `PATCH` take JSON **or** `multipart/form-data` (anything else → 415). Multipart names are dotted like the banners' (`slug`, `name.uz`, `name.ru`, `name.en`, `is_active`) plus the optional file part `image`:
  - `image`: the product-image rules of §2.3 — ≤5 MiB (`image` `file_too_large`), decoded by content as JPEG, PNG or WebP (`image` `unsupported_image`), re-encoded in the same format, EXIF stripped, longest side ≤2400 px; stored as `categories/{id}/<sha256 prefix>.<ext>` and served as `image_url` (absolute, §1.4); the customer catalogue's `catalog/categories/` `image_url` is the same file.
  - `PATCH` with an `image` part replaces the stored image (the previous file is removed after the commit; the same bytes again change nothing); `remove_image: true` (JSON boolean, or `true`/`false`/`1`/`0` in multipart; any other value such as `"yes"` or `1` in JSON → `invalid`) drops it (`image_url: null`, file removed after commit; nothing to remove → 200 unchanged); both at once → 400 `remove_image` `invalid`. A JSON body never carries a file.
- `POST categories/` → 201:
  - `slug`: `^[a-z0-9-]{2,40}$`, unique, not `order` or `none` (reserved → `slug` `invalid` "Bu slug band."), immutable afterwards; a lost unique race is reported as `slug` `unique` too;
  - `name.uz` 2..60 required; `name.ru`, `name.en` ≤60, optional (default `""`);
  - `is_active` default `true`;
  - `seo.{uz,ru,en}.{title,description}` (B-W1): optional, as products (JSON nested, multipart dotted `seo.uz.title`); PATCH merges leaf by leaf; the audit snapshot carries the leaves;
  - appended last.
- `GET`/`PATCH categories/{slug}/`. Sending `slug` in PATCH → `read_only`.
- `DELETE categories/{slug}/` → 204, or 409 `conflict` "Kategoriyada mahsulotlar bor." when `product_count > 0`.
- `PUT categories/order/` `{"slugs": [...]}` (exact set, `mismatch`; `[]` only when there are no categories) → 200 `{"results": [Category…]}`.
- Audit `category.created|updated|deleted|reordered`; the snapshot carries `image` (the stored file name or `null`), so an image upload, replacement or removal shows in `category.updated` as `{image: old}` → `{image: new}`.

### 2.5 Banners (API only in M1)
```jsonc
// Banner
{"id": 3, "title": {"uz": "Kuzgi chegirmalar", "ru": "", "en": ""},
 "image_url": "https://media.example.uz/banners/3/77aa.webp",
 "image_thumbnail_url": "https://media.example.uz/banners/3/77aa_400.webp", "image_medium_url": "…",   // §1.4
 "link": {"kind": "product", "product_id": 4, "category": null, "url": null},   // kind none|product|category|url
 // gap X-BANNER-URL: {"kind": "url", "product_id": null, "category": null, "url": "https://glowderma.uz/aksiya"}
 "is_active": true, "starts_at": null, "ends_at": null, "position": 0, "created_at": "…", "updated_at": "…"}
```
- `GET banners/` (P E; ordered by `position`; filter `is_active`).
- `POST banners/` (multipart): `image` (required; product-image rules), `title.uz` (1..80, required), `title.ru`, `title.en` (≤80, optional, default `""`), `link.kind`, `link.product_id`, `link.category`, `link.url`, `is_active`, `starts_at`, `ends_at` → 201.
  - Form encoding: nested keys are dotted form fields (`title.uz`, `link.kind`, `link.product_id`, `link.category`, `link.url`); `""` means `null` for the nullable keys (`link.product_id`, `link.category`, `link.url`, `starts_at`, `ends_at`); booleans are `true`/`false`. Only `multipart/form-data` (and JSON for PATCH) is accepted — never `application/x-www-form-urlencoded`.
  - `link.product_id` must be an active product, `link.category` an existing slug (`does_not_exist`).
  - Gap X-BANNER-URL (open question GC-3): `link.kind = "url"` opens `link.url` in the app — required for that kind (`link.url` `required`; multipart `""` is null, JSON `""` is `blank`), an absolute `https` URL ≤500 without credentials and with no port other than 443 (`link.url` `invalid` "Havolani https:// bilan to‘liq kiriting.", `max_length` "500 belgidan oshmasin."). Ignored for the other kinds; switching a banner to another kind clears it. A `PATCH` that keeps the kind `url` without sending `link.url` keeps the stored URL. The database check `banner_link_kind_targets` admits a URL only with kind `url` and only when it starts with `https://` (`catalog.0004`).
  - `ends_at` must be after `starts_at` (`invalid`).
- `PATCH banners/{id}/` takes multipart or JSON; `image` is optional; `title` merges leaf by leaf (every leaf optional). An unchanged `link.product_id` is not re-checked against the active products, so a banner whose product was hidden later can still be edited or switched off; a different product must be active.
- Export column `Havola`: `mahsulot #<id>`, `kategoriya <slug>`, the URL itself for kind `url`, else empty. The audit snapshot carries `link.url`.
- `DELETE banners/{id}/` → 204.
- `PUT banners/order/` `{"ids": [...]}` (exact set, `mismatch`; `[]` only when there are no banners) → 200 `{"results": [Banner…]}`.
- Audit `banner.created|updated|deleted|reordered`.

### 2.6 Customers
**`GET customers/`** → `Page<CustomerListItem>`

| Param | Values | Rule |
|---|---|---|
| `search` | str ≤64 | Name contains the term (case-insensitive and folded, §1.4; index `accounts_customer_fold_trgm`, `accounts.0008`), or (≥3 digits) the phone contains them, or (≥3 characters, hybrid identity) the e-mail address contains the term (case-insensitive) |
| `status` | `active`, `deleted` | |
| `segment` | int | Segment membership (§2.8) |
| `ordering` | `-registered_at` (default), `registered_at`, `name`, `-name`, `lifetime_total`, `-lifetime_total`, `balance`, `-balance` | Ties by `id` |

```jsonc
// CustomerListItem
{"id": 5, "name": "Malika Karimova",
 "phone": "+998901234567",              // null for an account without a phone yet (hybrid identity)
 "email": null,                         // verified e-mail address | null; show it when phone is null
 "status": "active",
 "registered_at": "2025-03-12T10:20:00+05:00", "lifetime_total": 13580000,
 "percent": "1.00",                      // active-policy rate for lifetime_total | null
 "balance": 48600, "online_orders_count": 12}
```

**Hybrid identity (customer contract §2.2.10).** A customer may sign up by e-mail or a social identity and attach
the phone at the first checkout, payment, booking or QR. Such an account has `phone: null` and, usually, an `email`;
lists, the card and segment members show the e-mail then, and the CSV/XLSX exports of customers and segment members
put the e-mail in the "Telefon" column. Phone-only actions skip it: a one-customer notification (§2.8
`audience.phone`) and an admin booking by phone cannot name it (a booking by `customer_id` can). Deletion scrubs the
e-mail (the phone stays, A1).

**`GET customers/{id}/`** → `CustomerCard`; 404.
```jsonc
{"id": 5, "name": "Malika Karimova", "phone": "+998901234567",   // phone null until attached
 "email": null,                                                   // verified e-mail address | null
 "status": "active",
 "registered_at": "2025-03-12T10:20:00+05:00", "deleted_at": null,
 "language": "uz", "address": "Toshkent, Chilonzor, 9-kvartal",            // null if none
 "totals": {"store": 9940000, "online": 3640000, "lifetime": 13580000},    // non-reversed purchases
 "online_orders": {"count": 12, "last_order_id": 10248, "last_order_at": "2026-10-23T14:02:00+05:00",  // ids/time null if none
                   "all_count": 13},                                        // gap closure: every order of any status
 "tier": {"policy_version": 3, "percent": "1.00",                          // null if no tier covers lifetime
          "next": {"percent": "2.00", "from": 20000000, "remaining": 6420000, "progress": 0.679}},  // null if no next tier
 "balance": 48600, "held": 0, "available": 48600,                         // available = balance − active holds
 "groups": [{"id": 1, "name": "Doimiy mijozlar"}, {"id": 2, "name": "10 mln+ xarid"}],
 "upcoming_appointments": 1,                                               // confirmed|requested with start ≥ now
 "device": {"platform": "ios", "model": "iPhone 14", "os": "iOS 18.1", "app_version": "1.0.0",
            "last_active_at": "2026-10-24T09:12:00+05:00", "push_enabled": true},   // null if none
 "devices": [                                                              // gap closure (ADM-8.1); [] if none
   {"id": 5, "platform": "ios", "model": "iPhone 14", "os": "iOS 18.1", "app_version": "1.0.0",
    "push_enabled": true, "is_active": true, "last_active_at": "2026-10-24T09:12:00+05:00",
    "created_at": "2025-03-12T10:20:00+05:00"}],
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
{"id": 41, "at": "2026-10-23T14:05:00+05:00", "source": "online", "order_id": 10248, "billz_sale_ref": "5632631379",   // see below
 "amount": 808000, "promo_amount": 240000, "cashback": 5680, "policy_version": 3,
 "reversed": false}                                       // true once the order was cancelled/refunded
```

- `online_orders.all_count` (gap closure, ADM-8.1) = every order of the customer, any status — the rows `orders/?customer={id}` lists, so the card's "Buyurtmalari" link can show it; `count` stays the paid, non-reversed online purchases the loyalty programme counts.
- `device` = the most recently active of the customer's **active** devices (`null` when none: logged out everywhere, or deleted); gap closure: it carries `platform` (`OrderChannel`: `ios` | `android` | `web`).
- `devices` (gap closure, ADM-8.1) = the customer's app installations and (B-W1) website browsers (`platform` `web`), active ones first, then by `last_active_at` (newest first), ties by `id` desc, at most **20**. Several can be active at once (B-W1: the single-device rule is retired, customer contract §1.9). `is_active` turns `false` once no session is left on the device (a logout, a session signed out from the list, a password change or reset elsewhere) or the account deletion; `push_enabled` is whether pushes reach it now; `created_at` is the installation's first sign-in. Never carries the FCM token, the device key (hash) or the Firebase installation id. A deleted customer keeps the list (every entry inactive). One query with `device`, so the card's query count does not grow.
- `billz_sale_ref` (Purchase): the Billz document number (≤ 32), `null` when none. A `store_qr` purchase always has one. An `online` purchase gets one the moment Billz created its sale (M3 live push), **while that sale may still be a draft**: it is the number staff look up in Billz when the push is deferred (closed shift) or dead (the sale is then completed, or deleted, by hand). The order history's `billz_sale_recorded` event marks the sale as paid in Billz; while Billz is fake (or before the push ran) it is `null`.

| `kind` | `source_type` | Populated |
|---|---|---|
| `credit` | `order` or `billz_sale` | `base`, `percent`, `policy_version`, `promo_excluded` (stored at credit time: promo items were excluded from the base), `order_id` or `source_ref` |
| `debit` | `order` | `order_id`, `source_ref` |
| `reversal` | `order` (or `billz_sale`) | `reverses_id`, `order_id` / `source_ref` |
| `adjustment` | `admin` | `reason`, `actor` |

**`GET customers/{id}/sessions/`** (B-W1; P, `page_size` 20 | 50) → `Page<AdminCustomerSession>`: the customer's open sessions — each signed-in phone and browser — most recently active first, exactly as the customer's «Faol seanslar» list (customer contract §2.2.10) without `current`. 404 for an unknown customer; a deleted customer has none.
```jsonc
// AdminCustomerSession
{"id": "6f0c1d2e-…", "device": "Chrome (Windows)",   // "Glow Derma iOS (iPhone 15)" for the app
 "platform": "web",                                   // OrderChannel | null
 "location": "Tashkent, Tashkent, UZ",               // local GeoIP City database; "—" when unknown
 "created_at": "…", "updated_at": "…"}                // sign-in; last activity (≤ every 5 min)
```
**`DELETE customers/{id}/sessions/{session_id}/`** (B-W1; optional `Idempotency-Key`) → 204: signs that session out (reason `admin_revoked`; the customer's client answers 401 `SESSION_EXPIRED`; the device stops receiving pushes when no other session is left on it). 404 for an unknown customer, or a session that is unknown, already ended or another customer's. Audit `customer.session_revoked` (`{sid, current: false}`, actor the staff member, entity the customer).

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

**`POST cashback/policy-versions/`** (I). Body `{"effective_from": datetime | "now", "basis": RateBasis, "earn_on_cashback_paid": bool, "tiers": [{"from": int ≥0, "to": int|null, "percent": str}]}`. `"now"` (exactly that string) applies the rules at once: the server sets `effective_from` one microsecond after the version's `created_at` (shown as the same second; any read after the save finds it `active`) (so both the past-date rule and the `effective_from > created_at` check hold) and the audit entry carries `"immediately": true`. Any future moment may be given (not only month starts; `next_effective_from` is a suggestion).

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

**Approval workflow.** Ordinary staff (`is_staff`) only prepare drafts; superusers (`StaffUser.is_superuser`)
test a draft on a registered real device, confirm the test, approve the send (dispatched no earlier than
`approval_delay_minutes` later, cancellable until then), return it to draft and emergency-stop a running send.
The backend enforces every rule; the UI hides the privileged controls from non-superusers. All staff see all
campaigns. States: `draft` → `tested` → `scheduled` (approved, waiting for `dispatch_after`) → `sending` →
`sent`; `sending` → `stopping` → `partially_sent` | `cancelled` (emergency stop); a dead `campaign.send_batch`
→ `partially_sent` | `failed`. Tests and approvals are bound to `revision`, which bumps on every change of
kind, title, body, image, link or audience: a stale approval is impossible.

```jsonc
// NotificationRecord
{"id": 4, "kind": "general", "title": "Yangi dermatolog qabuli ochildi", "body": "…",
 "image_url": null, "image_thumbnail_url": null, "image_medium_url": null,   // §1.4 image variants
 "link": {"kind": "none", "product_id": null, "label": null, "url": null},   // product: {"kind":"product","product_id":4,"label":"Seramidli namlantiruvchi krem","url":null}
                                                                       // url (gap ADM-10.1): {"kind":"url","product_id":null,"label":null,"url":"https://glowderma.uz/aksiya"}
 "audience": {"type": "segment", "segment_id": 1, "segment_name": "Doimiy mijozlar", "phone": null},  // name snapshot; phone for type customer
 "recipient_count": 612,          // customers with a pushable device, estimated at creation / approval; recomputed when the send starts
 "state": "scheduled",            // draft|tested|scheduled|sending|stopping|sent|partially_sent|cancelled|failed
 "scheduled_at": "2026-10-26T10:00:00+05:00",   // the author's requested send time (optional); the dispatch waits for it
 "created_at": "2026-10-24T11:30:00+05:00", "updated_at": "2026-10-24T11:45:00+05:00",
 "revision": 1,                   // bumps on every content change
 "tested_revision": 1, "test_confirmed_at": "2026-10-24T11:40:00+05:00", "test_confirmed_by": {"id": 1, "display_name": "Administrator"},
 "approved_at": "2026-10-24T11:45:00+05:00", "approved_by": {"id": 1, "display_name": "Administrator"}, "approved_revision": 1,
 "dispatch_after": "2026-10-26T10:00:00+05:00",   // state scheduled: the beat starts the send after this instant = max(approved_at + delay, scheduled_at)
 "started_at": null, "sent_at": null,
 "cancelled_at": null, "cancelled_by": null,      // set when a superuser cancelled the approved campaign, or a stop sent nothing
 "stop_requested_at": null, "stop_requested_by": null, "stopped_at": null, "failed_at": null, "last_error": "",
 "sent": null, "failed": null,    // FCM accepted / failed counts: live tallies from the first batch on (null before the send started)
 "attempted": null,               // sent + failed (stopped recipients count in neither) | null
 "delivered": null,               // null until M2 app acknowledgements
 "author": {"id": 1, "display_name": "Administrator"},
 "tests": [{"id": 7, "revision": 1, "test_device_id": 1, "device_label": "Malika — iPhone 14",   // no phone: the tester's number stays in the superuser-only registry
            "sent_at": "2026-10-24T11:38:00+05:00", "sent_by": {"id": 1, "display_name": "Administrator"},
            "fcm_ok": true, "fcm_error": "",       // fcm_ok null while in flight; false with fcm_error unregistered|no_device|unavailable|…
            "confirmed_at": "2026-10-24T11:40:00+05:00", "confirmed_by": {"id": 1, "display_name": "Administrator"}}]}
```
**`GET notifications/`** (P E). Filters `state`, `kind`; ordered by `coalesce(scheduled_at, created_at)` desc.
**`GET notifications/{id}/`** → the record (with `tests`).

**`GET notifications/meta/`**
```jsonc
{"total_devices": 4751,                                     // active customers with ≥1 push-enabled device (one push each)
 "segments": [{"id": 1, "name": "Doimiy mijozlar", "size": 612}, {"id": 2, "name": "10 mln+ xarid", "size": 318}],
 "templates": [{"status": "processing",
                "texts": {"uz": "To‘lov qabul qilindi. Buyurtma №{raqam} tayyorlanmoqda.", "ru": "…", "en": "…"},
                "updated_at": "…", "updated_by": null}],   // all 4 statuses
 "link_options": [{"product_id": 4, "label": "Seramidli namlantiruvchi krem"}],   // active products, uz display name, sorted by label
 "approval_delay_minutes": 5}                                // CAMPAIGN_APPROVAL_DELAY_MINUTES: the cancel window after an approval
```

**`POST notifications/`** (I, multipart). Top-level message "Maydonlarni tekshiring.".

| Field | Rule | Message |
|---|---|---|
| `kind` | `promo` or `general` | |
| `title` | trimmed 3..60 | `min_length` "Sarlavhani kiriting." / `max_length` "60 belgidan oshmasin." |
| `body` | trimmed 5..180 | `min_length` "Matnni kiriting." / `max_length` "180 belgidan oshmasin." |
| `image` | optional file, product-image rules | |
| `link.kind` | `none` (default), `product` or `url` (gap ADM-10.1) | |
| `link.product_id` | required for `product`; must be an active product | `does_not_exist` "Mahsulot topilmadi yoki yashirin." |
| `link.url` | required for `url` (`""` counts as missing); an absolute `https` URL ≤500, no credentials, port 443 only (§1.4, GC-3); ignored — and not validated — for the other kinds | `required` "Majburiy maydon." / `invalid` "Havolani https:// bilan to‘liq kiriting." / `max_length` "500 belgidan oshmasin." |
| `audience.type` | `all`, `segment` or `customer` | |
| `audience.segment_id` | required for `segment` | `does_not_exist` |
| `audience.phone` | required for `customer`; `^\+998\d{9}$`; must belong to an active customer | `invalid` "Telefon raqamini to‘g‘ri kiriting." / `customer_not_found` "Bu raqam bilan mijoz topilmadi." |
| `scheduled_at` | optional datetime, must be > now | `in_past` "Kelajakdagi vaqtni tanlang." |

- Always → state `draft`, `revision` 1; **nothing is materialised, enqueued or pushed** until a superuser tested, confirmed and approved the campaign. `scheduled_at` is the author's *requested* send time: the dispatch waits for it (`dispatch_after = max(approved_at + delay, scheduled_at)`).
- A `url` campaign (gap ADM-10.1) gives every inbox item `link {"kind": "url", "url": …}` and every push the FCM data key `link_url` (customer contract §2.12, §2.14.2); `link_url` is `""` for the other kinds. The audit row `notification.created` carries `link_url`. The database check `campaign_link_fields` admits a URL only with kind `url` and only when it starts with `https://` (`notifications.0005`). The UI's "Bosilganda ochiladigan sahifa" offers a URL next to the products (`link_options` stays the product list).
- Audit `notification.created`. 201 `NotificationRecord`.

**`PATCH notifications/{id}/`** (multipart; `Idempotency-Key` optional; the campaign's author or a superuser, else 403 `permission_denied`) → 200 `NotificationRecord`. `draft` / `tested` only (else 409 `invalid_transition` "Faqat qoralamani o‘zgartirish mumkin."). The same keys as the create form, every content key optional (a missing key keeps the stored value; a submitted `link.*` / `audience.*` replaces the stored link / audience as a whole; `scheduled_at` `""` clears the requested time), plus `expected_revision` (required; ≠ the current `revision` → 409 `conflict` "Xabar o‘zgartirilgan: sahifani yangilab, qayta urinib ko‘ring.") and `remove_image` (`true` drops the image). A change of kind, title, body, image, link or audience bumps `revision`, clears the test confirmation and puts a `tested` campaign back to `draft`; a change of `scheduled_at` alone keeps the state. `in_past` applies to a **submitted** `scheduled_at` only: a kept requested time that went by does not block an edit of another field (the dispatch waits for `max(now + delay, scheduled_at)` anyway). Audit `notification.draft_updated` (changed fields before/after).

**`DELETE notifications/{id}/`** (author or superuser; `draft` / `tested` only, else 409 `invalid_transition`) → 204; the image file and the tests go with it. Audit `notification.draft_deleted`.

**`POST notifications/{id}/test/`** (superuser) `{"device_ids": [int] 1..10}` → 200 `NotificationRecord`. Pushes the current revision of a `draft` / `tested` campaign to the chosen active test devices (`device_ids` must be active registry ids, else `does_not_exist`). Each test is a real inbox item of the tester (dedupe `campaign-test:{campaign}:{test}`), so the deep link works on the device, and is recorded in `tests[]` with the FCM outcome (`fcm_ok`, `fcm_error`); a dead token (`unregistered`) prunes the device like a campaign push does; a device without a token records `fcm_ok: false` `no_device`. No chosen device pushable → 409 `conflict` "Push qabul qila oladigan test qurilmasi tanlanmadi.". FCM unreachable → every test `fcm_ok: false` `unavailable` and 503 `provider_unavailable`. Audit `notification.test_sent`.

**`POST notifications/{id}/tests/{test_id}/confirm/`** (superuser; empty body) → 200 `NotificationRecord`. The superuser saw the test on the device: the campaign becomes `tested` with `tested_revision = revision`, `test_confirmed_at/by`. The test must be the current revision's (409 `conflict` "Bu test eski matn uchun yuborilgan: qayta test yuboring.") and delivered (`fcm_ok` true; 409 `conflict` "Bu test qurilmaga yetib bormagan: tasdiqlab bo‘lmaydi."); a test of another campaign → 404. Idempotent. Audit `notification.test_confirmed`.

**`POST notifications/{id}/approve/`** (superuser) `{"expected_revision": int}` → 200 `NotificationRecord`. A `tested` campaign whose confirmed test is the current revision becomes `scheduled`: `approved_at/by/revision`, `dispatch_after = max(now + approval_delay_minutes, scheduled_at)`, `recipient_count` re-estimated. **The approval dispatches nothing** (no outbox row, no FCM call): the beat `notifications.start_due_campaigns` (every minute) claims the campaign once `dispatch_after` has passed; `cancel/` works until then. `expected_revision` ≠ `revision` → 409 `conflict` (stale page); `draft` → 409 `invalid_transition` "Avval test yuboring va qurilmada ko‘rganingizni tasdiqlang.", any other wrong state → 409 `invalid_transition` "Faqat test tasdiqlangan xabarni yuborishga tasdiqlash mumkin."; a segment audience whose segment was deleted → 409 `conflict`. A repeat on the approved revision answers 200 unchanged (no second audit). Audit `notification.approved`.

**`POST notifications/{id}/return-to-draft/`** (superuser; empty body) → 200 `NotificationRecord`. A `scheduled` campaign the beat has not claimed, or a `cancelled` one that never started, goes back to `draft`: approval, cancellation and test confirmation cleared, `revision` bumped, so it must be tested and approved again. A `draft` answers unchanged; anything else → 409 `invalid_transition`. Audit `notification.returned_to_draft`.

**`POST notifications/{id}/cancel/`** (M3, now **superuser**; empty body; `Idempotency-Key` optional, §1.7) → 200 `NotificationRecord`. Cancels an approved **`scheduled`** campaign before the beat starts it: state `cancelled`, `cancelled_at` = now, `cancelled_by`, nothing is materialised or sent (no recipients, no inbox items) and the beat never starts it; audit `notification.cancelled`. Idempotent: an already `cancelled` campaign answers 200 with the same record and no new audit row. A `draft` / `tested` campaign → 409 `invalid_transition` "Qoralamani bekor qilish shart emas: uni o‘chiring yoki o‘zgartiring." (delete or edit it instead); once the send started (`sending`, `stopping`, `sent`, …) → 409 `conflict` "Yuborish boshlangan: xabarni bekor qilib bo‘lmaydi." (use `stop/`); an unknown id → 404. The row lock orders it against the beat: a campaign the beat is starting at that moment is answered after it, with the 409. `GET notifications/?state=cancelled` lists cancelled campaigns; a cancelled campaign no longer holds its segment (`DELETE segments/{id}/` checks `scheduled` ones only). The UI offers «Yuborishni bekor qilish» on `scheduled` rows only, with the countdown to `dispatch_after`.

**`POST notifications/{id}/stop/`** (superuser; empty body; emergency stop) → 200 `NotificationRecord`. `sending` → `stopping` (`stop_requested_at/by`; audit `notification.stop_requested`). The running `campaign.send_batch` checks the state **before claiming** its batch (nothing more goes out) and **before enqueueing the next batch** (the pushes already in flight are settled, claims FCM did not take become `stopped`); it then finalises the campaign: pending recipients `stopped` and their never-pushed in-app rows **withdrawn** (deleted: the customer never got the push, so the stopped campaign does not sit in the app's feed either; a row FCM accepted stays), `stopped_at`, state `partially_sent` when at least one push went out, else `cancelled` (`cancelled_by` = who asked for the stop); audit `notification.stopped` (system). A `stopping` campaign with no open batch left (a dead worker) is finalised by the beat `notifications.finalise_stopping_campaigns` (every minute). **A push FCM already accepted is never recalled.** Idempotent on `stopping` and on a campaign that ended by a stop; anything else → 409 `invalid_transition` "Xabar hozir yuborilmayapti: to‘xtatadigan narsa yo‘q.".

**Dead batch** (open question M3-N1, decided): a `campaign.send_batch` message that dies (ten failures, or deferred past the 6 h window) closes its campaign — `partially_sent` when a push went out, else `failed`; `failed_at`, `last_error` = the message's, pending recipients `stopped`; audit `notification.failed` (system) next to `notification.push_dead` / `outbox.dead`.

**Test devices** (superuser; the registry is global). `GET notifications/test-devices/` → `[{"id", "phone", "label", "device_id", "platform", "model", "is_pushable", "registered_by", "created_at"}]` (active rows; `is_pushable` = the device still takes pushes and its customer is active). `POST notifications/test-devices/` `{"phone": "+998…", "label": str 1..60}` → 201: registers the most recently active pushable device of the active customer with that phone (a real customer-app install of a staff member). Field errors `phone`: `invalid`, `customer_not_found` "Bu raqam bilan mijoz topilmadi.", `device_not_pushable` "Bu mijozning push qabul qiladigan qurilmasi yo‘q.", `unique` "Bu qurilma allaqachon ro‘yxatda."; `label` `min_length` "Qurilma nomini kiriting." / `max_length` "60 belgidan oshmasin.". `DELETE notifications/test-devices/{id}/` → 204 (soft, idempotent; unknown id → 404). Audit `test_device.registered` / `test_device.removed` (the audit log is readable by every staff member, so the `phone` in those rows is masked `+998 90 *** ** 67`).

Settings: `CAMPAIGN_APPROVAL_DELAY_MINUTES` (default 5; 0 allowed locally for e2e; **production refuses < 5**) is the minimum wait between an approval and the dispatch, shown as `approval_delay_minutes` in `meta`. Deployment: `notifications.0010` turns every pre-existing `scheduled` campaign into a `draft` (it needs a test and an approval again) and lets a `queued` one continue as `sending`.

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
- `GET`/`PATCH segments/{id}/`. While an approved `scheduled` notification targets the segment its audience is pinned: a `rules` change → 409 `conflict` "Rejalashtirilgan xabar bu guruhga yuboriladi." (a rename is always allowed).
- `DELETE segments/{id}/` → 204, or the same 409 `conflict` while a scheduled notification targets it.
- `GET segments/{id}/members/` (P E, CustomerListItem).
- `POST segments/{id}/members/` `{"customer_ids": [int] ≤500, "phones": [str] ≤500}` → 200 Segment. Either list or both may be sent; the members added are the union of the ids and the customers the phones resolve to. 409 `conflict` "Rejalashtirilgan xabar bu guruhga yuboriladi." while a scheduled notification targets the segment (removing a member stays allowed: the audience may only shrink).
- `DELETE segments/{id}/members/{customer_id}/` → 204. It removes manual membership only; a rule-only member → 409 `conflict` "Mijoz guruhga qoida orqali kiradi.".
- `POST segments/{id}/members/`: `customer_ids` ≤500, every id must exist (any status) — errors `customer_ids` `max_items` "Ko‘pi bilan 500 ta." / `does_not_exist`, a bad element `customer_ids.{i}` `invalid`. `phones` ≤500 E.164 numbers (`max_items`), deduplicated, each resolved to an **active** customer — a malformed element → `phones.{i}` `invalid` "Telefon raqamini to‘g‘ri kiriting.", no active customer with that number → `phones.{i}` `customer_not_found` "Bu raqam bilan mijoz topilmadi." (`i` = the first submitted index of a duplicated number). Neither list naming anybody (both absent or empty) → `customer_ids` `required`. Membership added this way is manual (the `DELETE` rules below apply); audit `segment.members_added` `{customer_ids, added}` carries the resolved ids. `DELETE …/members/{customer_id}/` for a customer who is neither a manual nor a rule member → 404.
- `size` (and `notifications/meta/` `total_devices`) count reachable customers — active members with at least one push-enabled device — because a campaign sends one push per customer (to the most recently active device); each figure equals the `recipient_count` a campaign to that audience would get. A non-list `customer_ids` or `phones` answers `invalid`.
- Audit `segment.*`.

### 2.9 Appointments
**`GET appointments/?date=YYYY-MM-DD`** (default: today in Tashkent; invalid → 400 `invalid`).
```jsonc
{"date": "2026-10-26",
 "specialists": [                                           // active professionals (+ inactive ones with an appointment that day), by position then name
   {"id": 1, "name": "Nodira Rahimova", "role": "dermatologist",
    "is_active": true,                                      // false: deactivated (§2.17) but still has appointments that day
    "location": {"id": 1, "name": "Yunusobod", "address": "…"}, // scheduled clinic | null
    "working_hours": {"start": "09:00", "end": "18:00"},    // that weekday's hours (date override wins) | null = not working
    "blocks": [{"start": "16:00", "end": "16:30", "kind": "busy", "label": "Band qilingan"}]},   // label str | null; clipped to the day
   {"id": 2, "name": "Jasur Aliyev", "role": "dermatologist", "is_active": true, "working_hours": {"start": "09:00", "end": "18:00"},
    "blocks": [{"start": "13:00", "end": "14:00", "kind": "off", "label": null}]}],
 "appointments": [                                          // Tashkent date of start = date; all statuses; by start
   {"id": 1, "specialist_id": 1, "location": {"id": 1, "name": "Yunusobod", "address": "…"}, "customer": {"id": 8, "name": "Kamola Nazarova", "phone": "+998909741823"},
    "service_id": 1, "service_name": "Dermatolog konsultatsiyasi", "service_short_name": "Konsultatsiya",
    "duration_min": 30, "start": "2026-10-26T09:00:00+05:00", "end": "2026-10-26T09:30:00+05:00",
    "price": 150000,                                        // snapshot at booking
    "status": "requested", "requested_at": "2026-10-24T08:41:00+05:00", "requested_via": "app"}]}
```
**`GET appointments/pending-summary/`** → `{"count": 3, "next_at": "2026-10-26T09:00:00+05:00"}`. Counts the `requested` appointments that still await an answer: `start ≥` the start of **today** (Tashkent midnight) — M3: today's requests whose time has already passed count too, so the header never says every request was reviewed while one of today's is unanswered; requests of earlier days do not count. `next_at` is the earliest of them (it may lie earlier today), or null. The dashboard's `pending_appointments` / `next_appointment_at` and `nav-counts/` `appointments_pending` are the same figures.

**`GET appointments/range/?from=&to=&specialist_id=&detail=counts|full`** (feat/admin-ux: the week and month boards).

| Param | Rule | Field errors (code · message) |
|---|---|---|
| `from`, `to` | required dates (Tashkent days, inclusive); `from ≤ to`; span ≤ **42** days with `detail=counts`, ≤ **7** with `detail=full` | `required` · `invalid` · `to` `empty_range` "Oxirgi sana boshlang‘ich sanadan oldin bo‘lmasin." · `to` `out_of_range` "Ko‘pi bilan 42 kunlik davr." / "To‘liq ko‘rinish ko‘pi bilan 7 kunlik davr uchun." |
| `specialist_id` | optional int ≥ 1, any existing specialist (active or not); narrows `specialists`, counts and `appointments` | `min_value` · `invalid` · `does_not_exist` "Mutaxassis topilmadi." |
| `detail` | `counts` (default) or `full` (enum `AppointmentRangeDetail`) | `invalid_choice` |

```jsonc
{"from": "2026-10-26", "to": "2026-11-01",
 "specialists": [{"id": 1, "name": "Nodira Rahimova", "role": "dermatologist", "is_active": true}],   // the day board's rule over the whole range: active ones + inactive ones with an appointment in it; by position, name
 "days": [                                                  // one per day from..to, in order
   {"date": "2026-10-26",
    "counts": {"requested": 1, "confirmed": 3, "attended": 0, "rejected": 0, "cancelled": 1},   // appointments whose Tashkent start day is this date, every status
    "total": 5,
    "working": true,                                        // at least one listed specialist has hours that day (override wins)
    "hours": {"start": "09:00", "end": "20:00"}}],          // earliest start / latest end of those specialists | null
 "schedules": null,                                         // detail=full: [{"specialist_id", "date", "working_hours": {start, end} | null, "location": {id, name, address} | null, "blocks": [DayBlock]}] for every listed specialist × day (the day board's specialist shape)
 "appointments": null}                                      // detail=full: the day items (§2.9 shape above) of the range, by start, id
```
At most 8 queries whatever the span or the number of specialists (one grouped count `(Tashkent day, status, specialist)`, the specialists, the three schedule tables, the appointments with `detail=full`, plus authentication).

**`POST appointments/{id}/confirm/` · `…/reject/` · `…/attend/`** (I). Empty body.
- Allowed: `requested` → confirm or reject; `confirmed` → attend. Otherwise 409 `invalid_transition` "Bu amal ushbu holatda mumkin emas.".
- Effects: an AppointmentEvent; a push through the outbox on confirm and reject; audit `appointment.confirmed|rejected|attended`.
- 200 with the appointment in the day-item shape above.
- `GET appointments/?date=` lists every status, so a day item may carry `status: "cancelled"` (T3).
- Route note: `confirm|reject|attend` share one route `appointments/{id}/{action}/`; any other action → 404.
- FND-1: a specialist deactivated with `force` (§2.17) keeps its column (`is_active: false`) on every day it still has appointments (any status), so the appointments it left behind stay visible and actionable; it has no column on other days. Its open appointments ahead are listed by `specialists/{id}/conflicts/` only when the schedule no longer covers them; reschedule them to another specialist or cancel them here.

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
| `location_id` | required; active clinic scheduled for the specialist on the selected Tashkent date | An unavailable or mismatched clinic returns a field error. |
| `customer_id` \| `phone` | exactly one; the customer must be `active` | both missing → `customer_id` `required`; both sent → `customer_id` `invalid` "customer_id yoki phone dan faqat bittasini yuboring."; unknown/deleted id → `customer_id` `does_not_exist` "Mijoz topilmadi."; `phone` not E.164 → `invalid` "Telefon raqamini to‘g‘ri kiriting."; no active customer → `phone` `customer_not_found` "Bu raqam bilan mijoz topilmadi." |
| `start` | aware datetime, 5-minute grid, not in the past, inside that day's working hours (date override wins; `end = start + duration` must fit), outside blocked periods and the recurring break, not overlapping another `requested`/`confirmed` appointment of the specialist | `required` · `invalid` "Noto‘g‘ri qiymat." (naive) · `invalid` "Vaqt 5 daqiqalik qadamda bo‘lsin." · `in_past` "Kelajakdagi vaqtni tanlang." · `outside_working_hours` "Mutaxassis bu vaqtda ishlamaydi." · `slot_unavailable` "Bu vaqt band." |
| `note` | optional (omitted = `""`; not required and without a `default` in the OpenAPI schema), ≤300 (staff note) | `max_length` "300 belgidan oshmasin." |

- 409 `conflict` "Bu vaqt band." when a concurrent booking wins the slot (exclusion constraint; nothing is written).
- FND-1: the booking locks the specialist's row `FOR KEY SHARE` and re-reads `is_active`, so a specialist deactivated meanwhile (§2.17) answers `specialist_id` `does_not_exist` "Mutaxassis topilmadi." with nothing written; the same holds for `reschedule/` to another specialist.
- Effects: events `requested {via: "admin", …}` + `confirmed {push_sent: false}`; audit `appointment.created`. No push (the customer is on the phone).

**`POST appointments/{id}/reschedule/`** `{"start", "location_id", "specialist_id"?}` → 200 day-item, status unchanged. Same `start` rules (the appointment itself is excluded from the overlap check). Changing the specialist re-snapshots duration and price from that specialist's link (`specialist_id` `does_not_exist`, or `service_id` `does_not_exist` "Mutaxassis bu xizmatni ko‘rsatmaydi."). 409 `invalid_transition` "Bu amal ushbu holatda mumkin emas." unless `requested`/`confirmed`; 409 `conflict` "Bu vaqt band." on the race. Effects: event `rescheduled {from_start, from_end, to_start, to_end, from_professional_id, to_professional_id, from_location_id, to_location_id, by: "admin", push_sent}`, push `rescheduled`, audit `appointment.rescheduled`.

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
               "refunds_needing_action": 0,               // refunds in failed|unknown|manual_required (a superseded failed one excluded, below)
               "bts_polling_failing": false,              // M2: the last BTS polling run tracked no parcel or stopped on transport failures (IntegrationState(bts).consecutive_failures > 0)
               "cashback_policy_missing": false,          // no cashback policy version is in force now: purchases earn nothing
               "yandex_failing": false},                  // Yandex Delivery: live and its last poll / price check failed
 "fulfilment": {"paid_to_prepare": 4, "to_hand_to_courier": 2, "in_delivery": 2,   // Yandex Delivery: order-state counters, now (not windowed)
                "returning": 1, "completed_today": 12, "needs_action": 3, "urgent": 1},
 "generated_at": "2026-10-24T12:00:00+05:00"}
```
- `revenue` = Σ `payment.amount` of payments confirmed in the bucket, minus the succeeded refunds of those payments. Refunds are attributed to the payment's bucket, so revenue is never negative. Late-captured payments (M2, customer contract §2.13.0: money taken for an order we did not honour, always refunded) are excluded.
- `orders` = orders created in the bucket, any status.
- `attention.refunds_needing_action` counts refunds in `unknown` or `manual_required`, and `failed` refunds that still need staff: a `failed` refund is **not** counted when a later refund of the same payment `succeeded` (a staff retry returned the money) or when its payment is `refunded`. `payments/?refund_state=failed` (§2.12) applies the same rule.
- `attention.bts_polling_failing` (M2, customer contract §2.13.3) = `IntegrationState(bts).consecutive_failures > 0`: a polling run stopped after consecutive transport failures, or attempted parcels and tracked none (every parcel refused). It turns false at the next run that tracks a parcel; BTS webhooks never change it.

- `attention.cashback_policy_missing` (gap CB-9.2.4) = no cashback policy version is in force at `generated_at`: none exists, or the only versions are scheduled for later (`state` `scheduled`). Purchases confirmed meanwhile earn no cashback (§2.7, the engine finds no tier). A fresh install is never in this state: migration `loyalty.0006` creates the first version (§3.2) when the policy table is empty; the flag catches a database where it was skipped. Staff fix it in "Cashback sozlamalari" (a new version starts at its `effective_from`, never in the past).

- `attention.yandex_failing` (Yandex Delivery, §2.18) = Yandex is live and `IntegrationState(yandex).consecutive_failures > 0`: the last status poll (`claims/bulk_info`) failed, or a checkout price check met an account failure (401, 403, a payment refusal). It turns false at the next answered poll or price check; always `false` in fake mode. The third failure in a row also alerts `delivery.provider_failing` (`docs/ops/monitoring.md`).
- `fulfilment` (Yandex Delivery) counts the orders **as they stand now**, whatever `range` says: `paid_to_prepare` = `processing` orders still to prepare (Yandex at `awaiting_dispatch`, or `failed` / `returned` waiting for a new dispatch; BTS orders; pickup orders); `to_hand_to_courier` = `processing` Yandex orders whose courier is requested, assigned or at the warehouse; `in_delivery` = `handed_to_courier` orders not coming back; `returning` = open Yandex orders at `returning` / `returned`; `completed_today` = delivered today (Tashkent), pickup hand-overs included; `needs_action` / `urgent` = open / open urgent delivery issues (§2.18). Each tile links to the filtered order list (`status`, `provider`, `stage`, `attention`) or the issue queue.
- K4 (`attention.bts_failed`, the nightly `k4_*` checks) stays BTS-only: a Yandex order waiting for its dispatch is never a finding.

**`GET nav-counts/`** → `{"orders_processing": 41, "appointments_pending": 3, "delivery_issues_open": 2}`. These are all-time `processing` orders, the pending-summary count and (Yandex Delivery) the open delivery issues (§2.18, the queue's badge).

### 2.11 Settings (ops console only)
**Retired 2026-11-07:** `GET` / `PATCH settings/` and `POST settings/terms/publish/` are no longer routes of this API (an unknown route, 404 `not_found`), and the Next.js settings cards that used them are gone (§7 item 19). The store settings, the contacts and the legal texts are sensitive business data, so staff edit them **only** on the ops console page **`/django-admin/settings/`** («Sozlamalar» in the «Katalog» group; `glowderma/site_config/admin_views.py`, a custom Unfold page, not a ModelAdmin): `is_staff` with TOTP 2FA, the console's network gate and session limits (`docs/ops/staff-access.md`). The console is mounted only with `DJANGO_ADMIN_ENABLED=true` (staging and production also need `OPS_DOMAIN` and `OPS_ALLOWED_IPS`; open questions, decision log 2026-11-07). `SiteSettings` stays a read-only registration in the console (raw inspection). The customer side is unchanged: `GET /api/v1/settings/public/` (customer contract §2.4).

**The page** keeps the Next screen's shape: three cards, each saved on its own (POST, then a redirect back with an Uzbek message; a refused card is shown again with the input kept and its first error focused), every write through `site_config.services` (row lock; the public-settings cache is dropped on commit).
- «Do‘kon sozlamalari»: `reservation_minutes`, `support_phone` (the seller, «Sotuvchi bilan bog‘lanish»), `pickup_address` (uz/ru/en); the provider modes («Integratsiyalar: jonli | sinov (fake)», each provider's `IntegrationMode` on hover) and the last change (`updated_at` · `updated_by`, «Tizim» for the system).
- «Aloqa ma’lumotlari»: `store_phone` (the app's Settings section), `cosmetologist_phone` (the AI section, Phase 2), `telegram_url`, `instagram_url`, `website_url`.
- «Huquqiy matnlar»: the `terms` and `privacy_policy` drafts and `booking_policy` per language (character counters); `terms_version` · `terms_updated_at` («E’lon qilinmagan» before the first publication), «E’lon qilinmagan o‘zgarishlar bor» while a draft differs from its published copy, and «E’lon qilish».

**Drafts and published copies (GS-2).** `terms` and `privacy_policy` exist twice: the **draft** that the «Huquqiy matnlar» card shows and saves, and the **published copy** that only «E’lon qilish» writes (it copies every language of both drafts). `settings/public/` (customer contract §2.4), registration and the consent checks use the published copy only, so a `terms_version` always names exactly one text. `booking_policy` is not versioned: it has one copy, served as soon as it is saved.

**Only what the staff user changed is written.** Every input is rendered with a digest of its stored value (hidden `<name>__was`); a save writes only the fields whose cleaned value differs from it, so a page opened before a colleague's save never writes the colleague's values back. A save that changes nothing writes nothing ("O‘zgarish yo‘q: saqlanadigan narsa topilmadi.") and keeps `updated_at` / `updated_by`.

Field rules of the console forms (the limits and Uzbek messages of the retired API):

| Field | Rule | Error message |
|---|---|---|
| `reservation_minutes` | whole minutes 5..60 | "5 dan 60 gacha daqiqa kiriting." (empty, not a number) · "Qiymat 5 dan kam bo‘lmasin." · "Qiymat 60 dan oshmasin." |
| `support_phone`, `store_phone`, `cosmetologist_phone` | stored E.164 `^\+998\d{9}$`; typed with spaces, `-`, `(`, `)` or `.` (`+998 71 200 00 00`), as `998712000000` or as the nine local digits; shown as `+998 71 200 00 00`; empty clears (`null` in the audit and `settings/public/`) | "Telefon raqamini to‘g‘ri kiriting." |
| `telegram_url` | absolute `https` URL, ≤200, trimmed, on a public host name; host `t.me`, `telegram.me` or `www.telegram.me` and the path one account handle (`/name`, `/name/` or an invite `/+code`: letters, digits, `_`, `.`, ≤64, not only dots); empty clears; scheme and host stored lowercase | "Havolani https:// bilan to‘liq kiriting." (not https, not a URL, credentials in it, a port other than 443, an IP or single-label host, or any of `"` `<` `>` `` ` `` `\`, whitespace or an invisible control/format character such as a bidi override) · "Telegram havolasi https://t.me/… ko‘rinishida bo‘lsin." (another host, no handle, a deeper path, `/..`) · "200 belgidan oshmasin." |
| `instagram_url` | as `telegram_url`; host `instagram.com` or `www.instagram.com` with an account handle (a query such as `?igsh=…` is kept) | … · "Instagram havolasi https://instagram.com/… ko‘rinishida bo‘lsin." · "200 belgidan oshmasin." |
| `website_url` | absolute `https` URL, ≤200, any public host name (not an IP literal, not a single label such as `localhost`), no credentials, port 443 only, same forbidden characters; empty clears; scheme and host stored lowercase | "Havolani https:// bilan to‘liq kiriting." · "200 belgidan oshmasin." |
| `pickup_address.{uz,ru,en}` | ≤200, trimmed | "200 belgidan oshmasin." |
| `terms.{uz,ru,en}`, `privacy_policy.{uz,ru,en}` (drafts) | text ≤50 000 characters; trimmed, line breaks stored as `\n`; a text with no visible character (only whitespace, zero-width spaces, BOM, bidi marks or other format characters) is stored `""`; an emptied language empties the draft only (the published copy is untouched) | "50000 belgidan oshmasin." |
| `booking_policy.{uz,ru,en}` | text ≤5 000 characters, same normalisation | "5000 belgidan oshmasin." |

- **Saving a text never publishes it.** A saved terms / privacy draft is invisible to customers until «E’lon qilish», which is the only way `terms_version` changes: the version is the unit a customer consents to. Publish after any change customers must accept.
- Audit `settings.updated`: `data` is a diff of **only the changed leaves**, each `{"from", "to"}`, nested by field (a contact `null` when empty; `pickup_address` and the texts as `{uz, ru, en}` objects). Saving `pickup_address.ru` alone logs `{"pickup_address": {"ru": {"from": "", "to": "Ташкент, …"}}}`; `{"reservation_minutes": {"from": 15, "to": 20}, "support_phone": {"from": "+998712000000", "to": null}}`. A changed leaf of `terms`, `privacy_policy` or `booking_policy` is logged as a digest, **never the text**: `{"terms": {"uz": {"changed": true, "old_len": 1830, "new_len": 1912, "sha256": "9f2c4a1b7e3d5c60"}}}` (`old_len` / `new_len` in characters; `sha256` = the first 16 hex characters of the SHA-256 of the new text in UTF-8, `null` when the new text is `""`). A save that changes nothing writes no audit row.
- The provider modes are `IntegrationMode` literals. Fake providers are allowed in production (product owner decision 2026-10-26): the process logs one startup warning listing them, and never refuses to start.

**«E’lon qilish»** opens a confirmation ("Shartlarning yangi versiyasini e’lon qilasizmi?", "Barcha mijozlardan shartlarni qayta qabul qilish so‘raladi.") and then calls `site_config.services.publish_terms`, which copies the `terms` and `privacy_policy` drafts (every language) into the published copies and publishes them as a new version ("Shartlar e’lon qilindi: 20261027-2. Mijozlar ilovada yangi versiyani qabul qiladi."):
- `terms_version` = `YYYYMMDD-n`: the Tashkent calendar day of the publication and its count on that day from 1 (`20261027-1`, then `20261027-2`; the next day `20261028-1`). A version is never reused (should the stored version carry a later day — a clock set back — that day is kept and `n` grows). `terms_updated_at` = now; `updated_at` / `updated_by` follow.
- From the moment it commits: registration (`auth/set-password/`, social completion) accepts only the new version (`terms_outdated` otherwise); every existing customer whose consent names another version gets `consent.update_required: true` in `me/` and **409 `terms_update_required`** at checkout and booking until the app sends `POST me/consent/` with the new version (customer contract §2.1.3, §2.2.4). `settings/public/` serves the new `terms_version` at once (its cache starts a new generation on commit).
- Publishing again without a text change still issues a new version and asks every customer again: publish only for a change customers must accept.
- **Stale page, double click.** The button carries the page's publish fingerprint (`services.publish_fingerprint`: SHA-256 over the current `terms_version` and every terms and privacy draft); `publish_terms(expected_fingerprint=…)` compares it under the row lock and publishes nothing when it moved — a second click (the version already moved on) or a page opened before a colleague's draft save or publication: "Matnlar yoki versiya siz sahifani ochganingizdan keyin o‘zgargan. Sahifani yangilang, matnlarni tekshiring va qayta e’lon qiling." This replaces the API's `Idempotency-Key`.
- Refusal: the uz `terms` or the uz `privacy_policy` draft has no visible character (empty, whitespace, or only invisible format characters) → "Foydalanish shartlari va maxfiylik siyosatining o‘zbekcha matnini kiriting, so‘ng e’lon qiling." (nothing written). Both refusals are shown in the reopened confirmation; the stale check runs first.
- Concurrent publications are serialised on the settings row; of two made from the same page only the first publishes (the second is stale).
- Audit `settings.terms_published` `{"from": "20261027-1" | null, "to": "20261027-2", "sha256": {"terms": {"uz": "…", "ru": "…", "en": null}, "privacy_policy": {…}}}` — the digest (as above) of every published text. It matches the published copy served under that version until the next publication (drafts never touch it); only the current version's full text is kept (no archive of older versions, GS-2).

### 2.12 Payments
**`GET payments/`** (P E)

| Param | Values |
|---|---|
| `status` | PaymentStatus |
| `provider` | PaymentProvider |
| `refund_state` | RefundState (any refund of the payment in that state; for `failed`, only a failed refund that still needs staff — not one a later `succeeded` refund of the same payment superseded, and none of a `refunded` payment, the rule of §2.10 `attention.refunds_needing_action`) |
| `period`, `date_from`, `date_to` | As in `orders/`, applied to `created_at` |
| `search` | Order id, transaction id, or (≥3 digits) phone |
| `ordering` | `-created_at` (default) or `created_at` |

```jsonc
{"id": 301, "order_id": 10248, "customer": {"id": 5, "name": "Malika Karimova", "phone": "+998901234567"},
 "provider": "click", "status": "paid", "amount": 784400, "transaction_id": "CLK-7741920385",
 "created_at": "2026-10-23T14:02:30+05:00", "confirmed_at": "2026-10-23T14:05:00+05:00",
 "refunds": []}                                   // Refund[] (§2.2, with manual_reason)
```
Export `tolovlar` (§1.8): the `Qaytarish` column is the §5.1 refund-state line of the payment's newest refund (for `manual_required` by `manual_reason` and provider — the Click line for a manual Click refund, never the Payme one), empty without a refund.

**Refund operations** (M1c, the Payments screen; declared by `commerce` because they write order events). `Idempotency-Key` is optional (honoured when sent, §1.7). Both answer **200 with the payment row above** (`refunds` included, newest first); 404 when the payment or the refund is unknown or the refund belongs to another payment.
- **`POST payments/{id}/refunds/{refund_id}/retry/`** (empty body): retries a `failed` refund as a **new** refund of the payment (Click `requested` and submitted through the outbox; Payme `manual_required`) and records its `refund_requested` order event (actor = caller); audit `refund.requested`. Refusals, checked under the order lock:
  - the refund is not `failed`, or the payment is no longer `paid` → 409 `invalid_transition` "Bu qaytarishni qayta yuborib bo‘lmaydi.";
  - another refund of the order is open (`requested`, `submitting`, `manual_required`, `unknown`) → 409 `conflict` "Bu to‘lov uchun qaytarish allaqachon jarayonda.".
- **`POST payments/{id}/refunds/{refund_id}/resolve/`** `{"state": "succeeded" | "failed"}` (any other value → 400 `state` `invalid_choice`): closes an `unknown` / `manual_required` refund after a manual check; audit `refund.state_changed` with the actor; order event `refund_succeeded` / `refund_failed`. A refund in any other state (also the loser of two concurrent resolves) → 409 `invalid_transition` "Bu qaytarishni yakunlab bo‘lmaydi.".
- Both share the busy-order outcome of §2.2.

### 2.13 Audit log
**`GET audit-log/`** (P E). Filters `actor` (staff id; the picker lists `GET staff/`, §2.16), `action` (§3.2), `object_type`, `object_id`, `request_id` (feat/admin-ux: 8–64 characters `[A-Za-z0-9-]`, a **prefix** of the stored request id, case-sensitive; anything else → `invalid` "Noto‘g‘ri qiymat."), `date_from`, `date_to`; `ordering` `-at` (default) or `at`.
```jsonc
{"id": 9001, "at": "2026-10-24T12:03:00+05:00", "actor": {"id": 1, "display_name": "Administrator"},   // null = system
 "action": "cashback.adjusted", "object_type": "customer", "object_id": "5",   // object_id is a string (slugs)
 "data": {"direction": "add", "amount": 2950, "reason": "Kassada QR o‘qilmadi …", "balance_after": 51550},
 "ip": "203.0.113.7", "request_id": "6f1c2a0e9b8d4c37"}   // ip: null for ordinary staff (below)
```
**Tiers (feat/admin-ux, `core/audit_scopes.py`).** Each entry gets a scope from its action prefix (the part before the first dot), actor type and object type; the first rule that matches wins:

| # | Rule | Scope | Who sees it |
|---|---|---|---|
| 1 | prefixes `console`, `ops`, `ops_access`, `integrations`, `outbox`, `report`, `pos`; actions `billz.sale_recorded_by_hand`, `billz.payment_attempt_forgotten`, `loyalty.reconciliation_completed`, `loyalty.reconciliation_mismatch` | system | superusers |
| 2 | prefix `staff` | staff admin | managers, superusers |
| 3 | prefix `auth` written by a staff actor or about object type `staff` / `auth_session`; prefix `export` | staff security | ordinary staff: **their own** only (actor = them, or object `staff` with their id); managers, superusers: all |
| 4 | prefixes `appointment`, `banner`, `billz`, `blocked_period`, `cashback`, `category`, `customer`, `delivery`, `location`, `loyalty`, `notification`, `notification_template`, `order`, `payment`, `product`, `professional`, `professional_service`, `refund`, `schedule`, `segment`, `service`, `settings`, `test_device`, `working_hours`, `working_hours_override`; `auth.*` written by a customer | business | every staff member |
| 5 | anything else | system (fail closed) | superusers |

- The list **and** its export apply the tier; a filter on something outside it (an action, object, actor or request id) answers an empty list, never 403. `export.downloaded` rows follow rule 3.
- Ordinary staff never see network details: `ip` is `null` (and the export's `IP` column is empty) and the `ip`, `last_ip`, `user_agent`, `location` keys of `data` (at any depth) are `null`. Managers and superusers see them. Stored rows are unchanged; the ops console reads the table without tiers.
- The tier is re-read from the account on every request (removing `is_manager` takes effect at once).
- A test scans every `audit.record(action=…)` of the code base (wrappers and carried `audit_action`s followed) and fails on an action no rule 1–4 names.

**`GET audit-log/facets/`** (feat/admin-ux) → what the audit screen offers the signed-in member:
```jsonc
{"tier": "staff",                      // AuditViewerTier: staff | manager | superuser
 "ip_visible": false,                  // true for manager and superuser
 "visible_prefixes": ["appointment", "auth", "banner", "…", "working_hours_override"],   // prefixes with entries this member may see (sorted)
 "own_only_prefixes": ["auth", "export"],   // subset limited to their own entries (customer-written auth.* stays visible); [] for manager / superuser
 "hidden_actions": ["billz.payment_attempt_forgotten", "billz.sale_recorded_by_hand", "loyalty.reconciliation_completed", "loyalty.reconciliation_mismatch"]}   // hidden although the prefix is visible; [] for superusers
```

### 2.14 Jobs
**`GET jobs/{id}/`**
```jsonc
{"id": 12, "kind": "billz_import", "state": "running",     // queued|running|succeeded|failed
 "params": {"mode": "delta"},
 "created_at": "…", "started_at": "…", "finished_at": null,
 "progress": {"done": 120, "total": 248},                  // null if unknown
 "result": null,                                           // succeeded: {"created": 3, "updated": 245, "unchanged": 0, "new_from_billz": 3} (+ "hidden": 2 for a full import, M3)
 "error": null,                                            // failed: {"code": "provider_unavailable" | "billz_listing_incomplete" | "billz_auth_failed" (M3) | "worker_lost" | "billz_fake_mode" (M2) | "server_error", "message": "…"}
 "created_by": {"id": 1, "display_name": "Administrator"}}
```
- `progress` is always `{"done", "total"}` (or `null` before the first batch); `result` is the kind-specific summary (Billz import: §2.3).
- A `running` job whose worker died is re-claimed by a redelivery or, after 15 minutes without a heartbeat, failed by the beat task `integrations.reap_stale_jobs` with `error.code = "worker_lost"` ("Jarayon uzilib qoldi. Qayta urinib ko‘ring."); M3: so is a job still `queued` 15 minutes after it was created (no worker ever claimed it). A failed job never blocks a new one of the same kind.
- `error` of a failed job (the same codes and messages as §2.3; the UI shows `message` as is and may branch on `code`):

| `error.code` | `error.message` | When |
|---|---|---|
| `provider_unavailable` | "Tashqi xizmat javob bermadi. Qayta urinib ko‘ring." | Billz did not answer, or the run ran out of time |
| `billz_listing_incomplete` (M3) | "Tashqi xizmat javob bermadi. Qayta urinib ko‘ring." | A full listing was incomplete or implausible (§2.3); nothing applied or hidden |
| `billz_auth_failed` (M3) | "Tashqi xizmat javob bermadi. Qayta urinib ko‘ring." | Billz refused the integration key (`BILLZ_SECRET_KEY`) |
| `billz_fake_mode` (M2) | "Billz sinov rejimida: Billz ma’lumotlarini olib bo‘lmaydi. Billz ulanganidan keyin qayta urinib ko‘ring." | Queued before Billz was switched to fake under the production settings |
| `worker_lost` | "Jarayon uzilib qoldi. Qayta urinib ko‘ring." | No heartbeat for 15 minutes, or still `queued` 15 minutes after creation (M3) |
| `server_error` | "Kutilmagan xato." | An unexpected error in the worker |

- 404 `not_found` for an unknown id.

### 2.15 Health
**`GET /api/v1/health/`**: no auth, no network gate, no throttle; not in the error envelope.
- Without params → 200 `{"status": "ok"}`.
- `?ready=1` checks the database, cache and broker → 200 `{"status": "ok", "checks": {"database": "ok", "cache": "ok", "broker": "ok"}, "version": "<git sha>", "integrations": {"oauth": "fake", "billz": "fake", "fcm": "fake", "click": "fake", "payme": "fake", "bts": "fake"}}`, or 503 with `"status": "error"` and the failing check set to `"error"`. `integrations` = `{provider: IntegrationMode}` for every provider with a `*_MODE` setting (informational; a fake provider never fails readiness; M2 adds `eskiz`, customer contract §6.14). It is present **only when the caller would pass the admin network gate of §1** (peer in `ADMIN_API_ALLOWED_NETS` and, in production, a matching `X-GD-Internal-Secret`): which payment and shipping providers are fake is reconnaissance for an attacker. Other callers (load-balancer and container probes) get `status`, `checks` and `version` only.
- **M3 — live providers.** Callers past the same gate also get `providers`: one entry per provider whose mode is `live` (none while every provider is fake), `{"configured": bool, "last_sync_at": datetime | null, "last_sync_age_seconds": int | null}`. `configured` = the live adapter can be built from the settings and its `is_configured()` says yes (settings only, nothing is sent — Billz: integration key, shop, cash box and payment type; Payme: merchant id and key; Click: the three numeric ids, not `00000`, and the secret key; BTS: login, password, sender city code and, for a branch pickup, the branch code; Eskiz: email, password and sender; FCM: project id and an existing key file). `last_sync_at` is the last successful Billz catalog sync or BTS polling run (`IntegrationState.last_sync_at`, `null` for the other providers and while the database check fails), `last_sync_age_seconds` the whole seconds since then (`null` with it). Example (inside the gate): `"providers": {"billz": {"configured": true, "last_sync_at": "2026-10-27T03:07:12+05:00", "last_sync_age_seconds": 1980}}`.
- **M3 — sandboxes.** Callers past the same gate also get `sandbox`: the live providers configured against a sandbox — `bts` (BTS's test host as `BTS_BASE_URL`), `payme` (`PAYME_TEST_MODE=true` or Payme's sandbox checkout host), `eskiz` (Eskiz's test sender `4546`) — in that order, `[]` when none. Informational (a sandbox never fails readiness; each one also logs a startup WARNING); it must be empty in production, where money and parcels are real.
- **Gap CB-9.2.4 — cashback policy.** Callers past the same gate also get `cashback_policy_missing`: `true` while no cashback policy version is in force (the rule of §2.10 `attention.cashback_policy_missing`), `null` while the database check fails or the check itself fails (logged WARNING "health check cashback policy failed"). Informational: it **never** fails readiness.
- **The 503 rule.** `?ready=1` answers **503** (`"status": "error"`) when any of `database`, `cache`, `broker` is `error` **or** any live provider is not configured — for **every** caller. Outside the gate the body then carries no reason: `"status": "error"` with every check `ok` means a live provider is misconfigured (ask from inside the gate, or see docs/ops/monitoring.md §4). A fake provider never fails readiness. A failure of the provider probe itself is logged (WARNING) and does not fail readiness. Without `?ready=1` the answer is always 200 (liveness).

### 2.16 Staff (directory and management)
Gap closure FND-4 (decisions GST-1..GST-6 in `docs/decisions/open-questions.md`). The client has one role, admin: every staff member has the same rights everywhere else. **Managers** (`StaffUser.is_manager`, `Me.is_manager`) additionally manage staff accounts: every route below except `GET staff/` answers **403 `permission_denied`** "Ruxsat yo‘q." to anyone else (checked before the id is looked up). A superuser is always a manager (removing the role → 409 below; `IsManager` and `Me.is_manager` also treat a superuser as one); migration `accounts.0008` made every superuser a manager and, when that left none, the first active account; `createstaff` makes the first account of an install a manager; the seeded `admin@glowderma.uz` is one. At least one **active** manager always remains (below).

**`GET staff/`** (P; every staff member) → `Page<StaffListItem>`: the options of the audit-log `actor` filter, and the staff screen's rows for a manager.

| Param | Values | Default | Rule |
|---|---|---|---|
| `search` | str ≤64, trimmed, case-insensitive | – | Full name (folded, §1.4) or email contains the term |
| `is_active` | query boolean | `true` | `false` lists the deactivated staff |
| `page`, `page_size` | §1.5 (20, 50, 100) | 50 | |

```jsonc
// StaffListItem — ordered by display_name (case-insensitive), then id
{"id": 7, "display_name": "Nodira Rahimova", "email": "nodira@glowderma.uz", "is_active": true,   // every caller
 "full_name": "Nodira Rahimova", "is_manager": false,                                            // manager only, else null
 "last_login_at": "2026-10-24T18:30:00+05:00", "linked_providers": ["google"],                   // manager only, else null
 "created_at": "2026-10-20T09:00:00+05:00",                                                     // manager only, else null
 "must_change_password": false, "mfa_enabled": false,                                             // feat/admin-ux; manager only, else null
 "invite": {"state": "none", "sent_at": null, "expires_at": null}}                               // feat/admin-ux; manager only, else null
```
- The first four keys are the directory every caller gets (the audit-log filter uses `id` and `display_name`). The other eight are filled only when the **caller** is a manager; for anyone else they are `null` (`linked_providers` too, so "not shown" differs from "none linked", `[]`).
- `last_login_at` = the start of the member's newest session (every password or social sign-in opens one; staff sessions are never purged); `null` = never signed in. `linked_providers` = the linked social identities (`google`, `facebook`), by provider. `created_at` = when the account was created. `mfa_enabled` (feat/admin-ux) = a confirmed admin-panel TOTP device, as on `StaffMember` (the staff screen's badge and its «MFA ni o‘chirish» action).
- The query count does not grow with the rows (one correlated subquery for `last_login_at`, one prefetch for the providers).

```jsonc
// StaffMember — the manager's view of one account; every write below answers with it
{"id": 7, "display_name": "Nodira Rahimova", "email": "nodira@glowderma.uz", "full_name": "Nodira Rahimova",
 "is_active": true, "is_manager": false, "last_login_at": null,            // null: never signed in
 "linked_providers": [], "created_at": "2026-10-27T10:15:00+05:00",
 // feat/admin-ux
 "must_change_password": true, "password_changed_at": null, "mfa_enabled": false,
 "invite": {"state": "pending", "sent_at": "2026-10-27T10:15:02+05:00", "expires_at": "2026-10-30T10:15:00+05:00"}}
// StaffCredentials — 201 of POST staff/, 200 of …/reset-password/
{"staff": { /* StaffMember */ }, "temporary_password": "Kp7m-Qx3r-Wt9z-Hn2v",   // null for delivery email_link
 "activation": {"sent": true, "expires_at": "2026-10-30T10:15:00+05:00"}}      // null for delivery temporary_password
```
- `invite` (feat/admin-ux) = the newest password link of the account (§2.1 "Password links"): `state` `none` (never e-mailed one), `pending` (open; `sent_at` = when the handler handed it to the relay, `null` while queued or when the address was refused), `expired`, `used` (the person set a password with it) or `revoked` (replaced by a newer link, a password change, a temporary-password reset or a deactivation). `password_changed_at` = when the person last set their own password (link or `change-password/`). `mfa_enabled` = a confirmed admin-panel TOTP device. `activation.sent` = the e-mail was queued (the outbox delivers it).

**`POST staff/`** (manager; **I**) body `{"email": str, "full_name": str, "is_manager": bool = false, "delivery": "temporary_password"|"email_link"|"both" = "temporary_password", "language": "uz"|"ru"|"en" = "uz"}` → **201 `StaffCredentials`**. Creates an active account; `delivery` (feat/admin-ux, overrides GST-3): `temporary_password` — a generated password shown once; `email_link` — no usable password, an `invite` link is e-mailed (in `language`); `both`. Either way `must_change_password` is set (§2.1 "Forced password change").

| Check | Field, code and message |
|---|---|
| `email` missing / empty / not an email / > 254 | `email` `required` / `blank` / `invalid` / `max_length` (generic messages) |
| `email` (trimmed, lower-cased) already belongs to an account, active or not (case-insensitive) | `email` `unique` "Bu email bilan xodim allaqachon bor (nofaol bo‘lsa, uni qayta faollashtiring)." |
| `full_name` missing, or trimmed outside 2..150 | `full_name` `required` / `min_length` "Kamida 2 ta belgi kiriting." / `max_length` "150 belgidan oshmasin." |
| `is_manager` not a boolean | `is_manager` `invalid` |
| `delivery` / `language` not one of the values | `delivery` / `language` `invalid_choice` |
| `delivery` `email_link` or `both` while `Me.capabilities.staff_email_invite` is false | `delivery` `invalid_choice` "E-pochta orqali yuborish sozlanmagan: vaqtinchalik parolni tanlang." |

- **Temporary password**: generated by the server, `xxxx-xxxx-xxxx-xxxx` from letters and digits without look-alikes (no `0 O 1 l I`; ~92 bits), passing the password validators. It is **shown once**: never logged or audited; a replay of the same `Idempotency-Key` returns it again (stored sealed, §1.7 "Stored credentials"). The person signs in with it and must set their own password first (feat/admin-ux, overrides GST-3: 403 `password_change_required` everywhere else until `auth/change-password/` or a link); the UI shows the password once with a copy button and tells the manager to hand it over in person.
- Audit `staff.created` `{email, full_name, is_manager, delivery, must_change_password}`, plus `staff.password_link_sent` `{purpose: "invite", via: "create", expires_at, link_id}` for an e-mail delivery (never the link).

**`GET staff/{id}/`** (manager) → `StaffMember`; unknown id → 404.

**`PATCH staff/{id}/`** (manager; I optional) body any of `{"full_name": str (trimmed 2..150), "is_manager": bool}` → 200 `StaffMember`.
- `email`, `is_active`, `password` in the body → 400 `read_only` "Bu maydonni o‘zgartirib bo‘lmaydi." on each (the email is the sign-in identity: create a new account instead; the state and the password have the actions below).
- Removing the manager role of the caller's own account → 409 `conflict` "O‘zingizni menejerlikdan chiqara olmaysiz."; of a superuser → 409 `conflict` "Superadmin doim menejer bo‘ladi: uni menejerlikdan chiqarib bo‘lmaydi."; of the last active manager → 409 `conflict` "Kamida bitta faol menejer qolishi kerak. Avval boshqa xodimni menejer qiling." (The active managers are locked before the check, so two managers removing each other at the same time cannot both succeed.)
- Only changed values are written; audit `staff.updated` with `before` / `after` of the changed fields (none changed → nothing written, 200).

**`POST staff/{id}/deactivate/`** (manager; **I**; empty body) → 200 `StaffMember` (`is_active: false`).
- Every open session of the account is revoked (`deactivated`; tombstones on commit, so its access tokens get 401 `SESSION_EXPIRED` on the next request on every API worker) and it can no longer sign in (password or social). Linked identities and the manager role are kept for a reactivation.
- The caller's own account → 409 `conflict` "O‘z hisobingizni faolsizlantira olmaysiz."; the last active manager → 409 `conflict` (message above). An inactive account → 200 unchanged, nothing written.
- **Racing sign-ins** (deactivation and reset alike): a sign-in (password or social) opens its session under the same account row lock and checks again that the account is active and, for a password sign-in, that the password it proved is still the stored one. A sign-in that passed the password check just before a reset or a deactivation committed therefore answers 401 `invalid_credentials` (`social_account_not_allowed` for a social one) instead of opening a session the revocation missed (decision GST-8).
- Audit `staff.deactivated` (before `{is_active: true}`, after `{is_active: false, revoked_count}`) and one `auth.session_revoked` `{sid, reason}` per revoked session.

**`POST staff/{id}/reactivate/`** (manager; I optional; empty body) → 200 `StaffMember`. The account signs in again with its password or a linked identity; the sessions revoked at deactivation stay revoked. An active account → 200 unchanged. Audit `staff.reactivated`.

**`POST staff/{id}/reset-password/`** (manager; **I**; optional body `{"delivery": … = "temporary_password"}`) → 200 `StaffCredentials`.
- Replaces the password with a new temporary one (rules above) — or, for `email_link`, makes it unusable and e-mails a `reset` link (`both`: both) — sets `must_change_password`, revokes every earlier link and every open session (`password_changed`); linked identities stay. A running login lockout of the account (§1.9; the API and the Django-admin login scopes) ends once the reset commits, from the API and from `resetstaffpassword` alike. Works on an inactive account too.
- The caller's own account → 409 `conflict` "O‘z parolingizni «Parolni o‘zgartirish» orqali almashtiring." (`auth/change-password/` asks for the current password, so a stolen session cannot take the account over).
- A **superuser's** account with `temporary_password` or `both` (the answer would hand the password to the caller), unless the caller is a superuser too → 409 `conflict` "Superadminga vaqtinchalik parolni faqat superadmin bera oladi. Havolani uning e-pochtasiga yuboring yoki superadmindan so‘rang." (`email_link` stays allowed: the link goes to the superuser's own address). The operator's `manage.py resetstaffpassword` is the break-glass path.
- Audit `staff.password_reset` `{revoked_count, generated, delivery, must_change_password}` (`generated` false for `email_link`) and one `auth.session_revoked` per revoked session (plus `staff.password_link_sent` `{via: "reset"}` for an e-mail delivery); the password is never recorded.
- `delivery` e-mail options while `capabilities.staff_email_invite` is false → `delivery` `invalid_choice` (as for `POST staff/`).

**`POST staff/{id}/send-password-link/`** (feat/admin-ux; manager; I optional; no body) → 200 `{"staff": StaffMember, "activation": {"sent": true, "expires_at"}}`. E-mails a new single-use link (`invite` for an account that never signed in nor set a password, else `reset`); the password and the sessions are **not** changed; earlier links are revoked. 409 `conflict` for the caller's own account ("O‘zingizga havola «Parolni o‘zgartirish» sahifasidan so‘raladi."), an inactive account ("Nofaol hisobga havola yuborilmaydi. Avval uni qayta faollashtiring.") or when e-mail links are not available ("E-pochta orqali yuborish sozlanmagan."); 404 unknown id; 429 after 5/hour per manager and account (`staff_password_link`). Audit `staff.password_link_sent` `{purpose, via: "resend", expires_at, link_id}`.

**`POST staff/{id}/reset-mfa/`** (feat/admin-ux; manager; I optional; optional body `{"reason": str}`: trimmed, ≤200, blank allowed, default `""`; longer → 400 `validation_error`, field `reason`, code `max_length`) → 200 `StaffMember` (`mfa_enabled: false`). A lost phone: removes the account's **admin panel** TOTP device and recovery codes (its ops console devices stay — that is `ops_access reset-2fa`), revokes every open session (`admin_revoked`) and ends a running `panel_otp` lockout once it commits; a pending sign-in challenge dies with it. The next sign-in needs the password only and the person may enable MFA again. 409 `conflict` for the caller's own account ("O‘z ikki bosqichli himoyangizni «Sozlamalar»da o‘chiring."), an account without panel MFA ("Bu xodimda ikki bosqichli himoya yoqilmagan.") or a **superuser's** account unless the caller is a superuser too ("Superadminning ikki bosqichli himoyasini faqat superadmin o‘chira oladi."; the operator's `manage.py resetstaffmfa` is the break-glass path); 404 unknown id. Audit `auth.mfa_reset` `{totp_devices, recovery_codes, revoked_count, via: "api"|"cli"}` (actor: the manager; `reason`: the body's `reason`) plus `auth.session_revoked` per session (same `reason`); alerted (`auth.mfa_reset`, docs/ops/monitoring.md), because a manager who also resets the person's password could sign in as them.

**Operator commands** (server shell; decision GST-6) run the same audited services, for when no manager can sign in: `createstaff <email> [--manager] [--superuser] [--full-name] [--password-stdin]` (audited `staff.created` now; `--full-name` trimmed, 2..150 when given, else the display name is the email), `deactivatestaff <email>`, `reactivatestaff <email>`, `resetstaffpassword <email> [--password-stdin]` (prints a temporary password once unless one is read from stdin), `resetstaffmfa <email>` (feat/admin-ux: the admin panel MFA reset above, `via: "cli"`; refused without panel MFA) and `setstaffmanager <email> [--off]`. Each takes `--actor <email>` (an active staff member who asked for it; default: the system) and audits with `reason` "manage.py <command>". They may act on the actor's own account; the last-active-manager and superuser rules still hold. Runbook: `docs/ops/staff-access.md`.

### 2.17 Mutaxassislar va xizmatlar (specialists, services, schedules, the clinic; FND-1, FND-7)
Staff manage the bookable side of the clinic here (until now only the disabled Django admin could). Every write is audited (§3.2 "Specialists and services"), honours an optional `Idempotency-Key` (§1.7) and answers the error envelope of §1.6; nothing is ever deleted except a date override, a blocked period, an offer left out of a specialist's set and the photo. The customer catalogue and the free slots (customer contract §2.11) are computed from the same rows on every request — there is no slot cache — so a change is bookable, or no longer offered, at once. Bookings (customer `POST appointments/`, admin `POST appointments/` and `reschedule/`) lock the specialist and, for a new booking, the service `FOR KEY SHARE` before they read the offer (price, duration) and check the slot; every write here holds the specialist (or the service) `FOR UPDATE`, so a booking racing an edit either waits and then sees it (a withdrawn offer, a retired service or a new block is refused; a new price or duration is snapshotted) or is made first and kept like any other booked appointment (PS-1, PS-3, PS-14).

**What a change does to existing appointments** (decisions PS-1 … PS-3): nothing. Booked appointments keep their time and their `price` / `duration_min` snapshots whatever the weekly hours, overrides, blocks, offered services, prices or service status become; only new bookings follow the new rules. `GET specialists/{id}/conflicts/` lists the open appointments the schedule no longer covers so staff can move or cancel them (§2.9). Deactivating a specialist who still has `requested` / `confirmed` appointments ahead is refused unless confirmed with `force` (below).

**Grid.** Durations and schedule times are multiples of **5 minutes** (the admin booking grid, `appointments/meta/` `slot_step_min`); the customer grid is the specialist's `slot_step_min` (10, 15, 20, 30 or 60) counted from the start of that day's hours, and a duration need not be a multiple of it.

```jsonc
// ClinicSpecialist
{"id": 3, "name": "Dilnoza Yusupova", "role": "cosmetologist",                 // SpecialistRole (the professional category)
 "qualifications": {"uz": "…", "ru": "", "en": ""},                            // i18n, ≤2000 each
 "bio": {"uz": "…", "ru": "", "en": ""},                                       // i18n, ≤2000 each
 "experience_years": 6,                                                        // int | null
 "photo_url": "https://media.example.uz/professionals/3/9f2c1b7a0d4e8c61.webp", // | null
 "photo_thumbnail_url": "…", "photo_medium_url": "…",                           // §1.4 image variants | null
 "location_id": 1,
 "location_ids": [1, 2],
 "staff_user": {"id": 4, "display_name": "Dilnoza Yusupova"},                  // Actor | null: the linked staff account (informational)
 "slot_step_min": 30, "position": 2, "is_active": true,
 "services": [                                                                 // OfferedService[], by service position then Uzbek name
   {"service_id": 3, "name": "Yuzni chuqur tozalash", "short_name": "Yuzni chuqur tozalash", "category": "cosmetology",
    "price": 320000, "duration_min": 75,                                       // what a booking snapshots
    "is_active": true,                                                         // the offer (a paused offer is kept with its price)
    "service_is_active": true}],                                               // the service; bookable only when both are true
 "working_hours": [                                                            // WorkingDay[], by weekday (0 = Monday … 6 = Sunday); a missing weekday = not working
   {"weekday": 0, "start": "09:00", "end": "18:00", "break_start": "13:00", "break_end": "14:00"}],  // break: both or null
 "upcoming_appointments": 4,                                                   // requested|confirmed appointments starting now or later
 "slug": "dilnoza-yusupova",                                                   // B-W1: the website URL segment
 "created_at": "…", "updated_at": "…"}
```

**`GET specialists/`** (P E) → `Page<ClinicSpecialist>`; page sizes 20, 50 (§1.5); four queries for any page (count, rows with the staff link, offers, hours) plus one for the `upcoming_appointments` of the page.

| Param | Values | Rule |
|---|---|---|
| `search` | str (trimmed, cut to 64) | Substring of `name`, case-insensitive and folded (§1.4) |
| `is_active` | query boolean | |
| `role` | SpecialistRole | an unknown value → 400 `invalid_choice` |
| `ordering` | DirectoryOrdering, default `position` | `position` = position, name, id; `name` / `-name` case-insensitive (ICU collation); `created_at` / `-created_at` |
| `page`, `page_size`, `export` | | Export `mutaxassislar` (§1.8) |

**`POST specialists/`** → 201 `ClinicSpecialist`. **`PATCH specialists/{id}/`** → 200 `ClinicSpecialist` (every field optional; `qualifications` and `bio` merge leaf by leaf; OpenAPI `PatchedClinicSpecialistPatchRequest`). **`GET specialists/{id}/`** → `ClinicSpecialist`; 404 for an unknown id (an inactive specialist is still served).

| Field | Rule | Field errors (code · message) |
|---|---|---|
| `name` | str, trimmed, 2..150, required on create (shown as is in every language) | `required` · `blank` · `min_length` "Kamida 2 ta belgi kiriting." · `max_length` "150 belgidan oshmasin." |
| `role` | SpecialistRole, required on create | `invalid_choice` |
| `qualifications.*`, `bio.*` | ≤2000 each; missing on create → `""` | `max_length` "2000 belgidan oshmasin." |
| `experience_years` | int 0..80 or null | `min_value` · `max_value` "Qiymat 80 dan oshmasin." |
| `slot_step_min` | 10, 15, 20, 30 or 60; default 30 | `invalid_choice` "Ruxsat etilgan qadamlar: 10, 15, 20, 30 yoki 60 daqiqa." |
| `location_id`, `location_ids` | Primary clinic and all assigned clinic IDs. Both are required on create. The set must contain the primary clinic. Update the schedule before removing a clinic used by recurring hours or a current or future override. | Field validation rejects unavailable or inconsistent assignments. |
| `staff_user_id` | an **active** staff user not linked to another specialist, or null; the account already linked to this specialist is accepted back unchanged even once deactivated (a resent form) | `does_not_exist` "Xodim topilmadi." · `unique` "Bu xodim boshqa mutaxassisga bog‘langan." (also when a concurrent request linked it first) |
| `position` | int 0..100000; default: appended last | `min_value` · `max_value` |
| `is_active` | bool; **default `false` on create** (decision PS-6: add the offer and the hours, then activate) | |
| `slug` (B-W1) | optional; on create derived from `name` (transliterated; `mutaxassis` when empty; `-2`… when taken); never changes on its own; when sent `^[a-z0-9]+(-[a-z0-9]+)*$`, not only digits, ≤160 | `invalid` "Faqat lotin kichik harflari, raqamlar va chiziqcha (faqat raqam emas)." · `unique` "Bu manzil (slug) boshqa mutaxassisda bor." (a lost race too) |
| `force` | PATCH only, bool, default `false` | |

- **Deactivation guard** (`is_active: true → false`). Under the specialist's row lock the open appointments ahead are counted; when there are any and `force` is not `true` → **409 `conflict`** "Mutaxassisda {n} ta kelgusi qabul bor (kutilayotgan yoki tasdiqlangan). Qabullar o‘zgarmaydi: avval ularni boshqa vaqtga yoki mutaxassisga ko‘chiring yoki bekor qiling, yoki baribir nofaol qiling." and nothing of the PATCH is written. The UI shows the message and offers "Baribir nofaol qilish" (the same PATCH with `force: true`); `upcoming_appointments` gives the count beforehand. With `force` (or with nothing ahead) the specialist leaves the customer catalogue, the slots (`specialists/{id}/` answers 404 to customers) and the booking dialogs at once; the appointments stay as they were (still confirmable, attendable, reschedulable to another specialist and cancellable) and the day board keeps the specialist's column on the days it still has appointments (§2.9, `is_active: false`). A booking that races the deactivation is refused (`specialist_id` `does_not_exist`), or is counted by it — never both missed. The Django admin (disabled in production) applies the same guard without a `force`: unchecking `is_active` there with appointments ahead is a form error on `is_active` "Mutaxassisda {n} ta kelgusi qabul bor (kutilayotgan yoki tasdiqlangan). Avval ularni boshqa vaqtga yoki mutaxassisga ko‘chiring yoki bekor qiling." under the same row lock.
- Audit: `professional.created` (the new row), `professional.updated` (the changed fields, `{field: old}` / `{field: new}`), `professional.deactivated` (the changed fields plus `upcoming_appointments` and `forced`), `professional.activated`. A PATCH that changes nothing writes nothing (no audit row, `updated_at` kept).

**`PUT specialists/{id}/photo/`** (multipart, one part `file`) → 200 `ClinicSpecialist`. The product-image rules of §2.3: ≤5 MiB (`file` `file_too_large`), JPEG / PNG / WebP decoded by content (`file` `unsupported_image`), missing part → `file` `required`, re-encoded with EXIF stripped and the longest side ≤2400 px, stored as `professionals/{id}/<sha256 prefix>.<ext>`; the previous file is removed after commit; the same bytes again change nothing. A JSON body → 415. Audit `professional.photo_updated` (`{photo}` before / `{photo, sha256}` after). **`DELETE specialists/{id}/photo/`** → 204 (idempotent; audit `professional.photo_deleted` only when there was a photo).

**`PUT specialists/{id}/services/`** `{"services": [{"service_id": int, "price": int, "duration_min"?: int | null, "is_active"?: bool}]}` → 200 `ClinicSpecialist`. The exact set: a service left out stops being offered (its link is removed; existing appointments keep their snapshots), `[]` offers nothing.

| Field | Rule | Field errors |
|---|---|---|
| `services` | list, ≤50 items | `required` · `max_items` "Ko‘pi bilan 50 ta." |
| `services.N.service_id` | an existing service (an inactive one may be offered; it is bookable once active), once per list | `does_not_exist` "Xizmat topilmadi." · `unique` "Bu xizmat ro‘yxatda ikki marta berilgan." |
| `services.N.price` | so'm, 0..100 000 000 (0 = free) | `min_value` · `max_value` "Narx juda katta." |
| `services.N.duration_min` | 5..480, a multiple of 5; omitted or null = the service's `default_duration_min` | `min_value` · `max_value` · `invalid` "Davomiylik 5 daqiqaga karrali bo‘lsin." |
| `services.N.is_active` | default `true` | |

Audit `professional.services_updated` with the whole set before and after (`[{service_id, price, duration_min, is_active}]`), only when it changed.

**`PUT specialists/{id}/working-hours/`** `{"days": [{"weekday", "start", "end", "location_id", "break_start"?, "break_end"?}]}` → 200 `ClinicSpecialist`. The whole week: a weekday left out is not a working day; one interval at one assigned clinic per weekday plus at most one recurring break (rendered as an `off` block with label null on the day board).

| Field | Rule | Field errors |
|---|---|---|
| `days` | ≤7 items, each weekday at most once | `max_items` "Ko‘pi bilan 7 ta." · `days.N.weekday` `unique` "Bu hafta kuni ikki marta berilgan." |
| `days.N.weekday` | 0 (Monday) … 6 (Sunday) | `min_value` · `max_value` |
| `days.N.start`, `days.N.end` | `"HH:MM"` (00:00–23:55), minutes a multiple of 5, `start < end` | `invalid` "Vaqtni SS:DD ko‘rinishida kiriting." · `invalid` "Vaqt 5 daqiqalik qadamda bo‘lsin." · `end` `invalid` "Tugash vaqti boshlanishidan keyin bo‘lsin." |
| `days.N.break_start`, `days.N.break_end` | both or neither (null / omitted), same format, `break_start < break_end`, inside `[start, end]` | the missing one `required` "Tanaffusning boshlanishi va tugashini birga kiriting." · `break_end` `invalid` "Tugash vaqti boshlanishidan keyin bo‘lsin." · `break_start` `invalid` "Tanaffus ish vaqti ichida bo‘lsin." |

Audit `schedule.hours_updated` (entity `professional`) with the week before and after, only when it changed.

**Date overrides** (one date's own hours, or a day off; they win over the weekly hours).
```jsonc
// HoursOverride
{"id": 7, "date": "2026-10-27", "start": null, "end": null, "day_off": true,   // start/end "HH:MM" | null (both null = day off)
 "note": "Ta’til", "created_by": {"id": 1, "display_name": "Administrator"},  // Actor | null
 "created_at": "…"}
```
- **`GET specialists/{id}/overrides/`** (P; 20, 50) `?date_from=&date_to=` (dates; `date_from` default today in Tashkent, `date_to` optional and inclusive; `date_to < date_from` → `date_to` `invalid` "Oxirgi sana boshlang‘ich sanadan oldin bo‘lmasin."; either date more than 3660 days (≈10 years) from today → that field `out_of_range` "Sana bugundan ko‘pi bilan 10 yil uzoqlikda bo‘lsin.") → `Page<HoursOverride>` by date.
- **`POST specialists/{id}/overrides/`** `{"date", "start"?, "end"?, "location_id"?, "note"?}` → 201 `HoursOverride`. `date`: today … today + 365 (`in_past` "O‘tgan sanani tanlab bo‘lmaydi." · `out_of_range` "Ko‘pi bilan bir yil oldinga kiritish mumkin."), one per date (`unique` "Bu sana uchun alohida jadval allaqachon bor." — delete the old one first); `start` / `end` both set (`"HH:MM"`, 5-minute grid, `start < end`) or both null / omitted (the missing one `required` "Boshlanish va tugash vaqtini birga kiriting (ikkalasi bo‘sh bo‘lsa, dam olish kuni)."); working hours require an assigned active `location_id`; a day off has no location. `note` ≤120. Audit `schedule.override_created` (entity `professional`, `{override_id, date, start, end, location_id, note}`).
- **`DELETE specialists/{id}/overrides/{override_id}/`** → 204; the date falls back to the weekly hours; 404 for another specialist's override. Audit `schedule.override_deleted` (the deleted row).

**Blocked periods** (absolute busy / off ranges, e.g. a meeting or a leave; they remove free slots).
```jsonc
// BlockedPeriod
{"id": 12, "start": "2026-10-26T16:00:00+05:00", "end": "2026-10-26T16:30:00+05:00",
 "kind": "busy",                                                                // BlockKind
 "label": "Band qilingan",                                                      // str | null
 "created_by": {"id": 1, "display_name": "Administrator"}, "created_at": "…"}
```
- **`GET specialists/{id}/blocks/`** (P; 20, 50) `?date_from=&date_to=` (as for overrides) → the blocks touching those Tashkent days, by start.
- **`POST specialists/{id}/blocks/`** `{"start", "end", "kind", "label"?}` → 201 `BlockedPeriod`. `start` / `end` aware datetimes (naive → `invalid`) on the 5-minute grid in Tashkent time — zero seconds and microseconds, minutes a multiple of 5 (that field `invalid` "Vaqt 5 daqiqalik qadamda bo‘lsin.", checked `start` first; the slot engine reads blocks as `HH:MM`, so an off-grid edge would leave a bookable slot inside the block) — `end > start` (`end` `invalid` "Tugash vaqti boshlanishidan keyin bo‘lsin."), `end` in the future (`end` `in_past` "Tugagan vaqtni band qilib bo‘lmaydi."), at most 366 days long (`end` `out_of_range` "Ko‘pi bilan 366 kunlik davr."), not overlapping another block of the specialist (touching is fine; `start` `overlap` "Bu vaqt boshqa band vaqt bilan kesishadi."); `label` ≤80, trimmed, `""` / null = none. Appointments inside the block are kept (they appear in `conflicts/`). Audit `schedule.block_created` (entity `professional`, `{block_id, start, end, kind, label}`).
- **`DELETE specialists/{id}/blocks/{block_id}/`** → 204; 404 for another specialist's block. Audit `schedule.block_deleted`.

**`GET specialists/{id}/conflicts/`** (declared by `appointments`) → `{"results": [day-item…]}` (the §2.9 appointment shape): the specialist's `requested` / `confirmed` appointments starting now or later that the current schedule no longer covers — a clinic that differs from the scheduled clinic, a day without hours (a day-off override, or no weekly row), a range not inside that day's hours, or an overlap with a blocked period or the recurring break — by start; `[]` when every booking still fits; 404 for an unknown specialist. Four queries whatever the number of days.

**Services** (bookable offerings; prices live on each specialist's offer).
```jsonc
// ClinicService
{"id": 3, "name": {"uz": "Yuzni chuqur tozalash", "ru": "", "en": ""},          // i18n, uz 2..120
 "short_name": {"uz": "Yuzni chuqur tozalash", "ru": "", "en": ""},             // i18n, uz 1..40 (the day board and the app chips)
 "description": {"uz": "", "ru": "", "en": ""},                                 // i18n, ≤2000 each
 "category": "cosmetology",                                                     // ServiceCategory
 "default_duration_min": 75, "default_price": null,                             // int | null: suggested when offering it
 "position": 2, "is_active": true,
 "specialist_count": 1,                                                         // active specialists offering it through an active offer
 "created_at": "…", "updated_at": "…"}
```
- **`GET services/`** (P E) → `Page<ClinicService>`; page sizes 20, 50; `search` (trimmed, ≤64: name or short name in any language contains, case-insensitive and folded, §1.4), `is_active`, `category` (ServiceCategory), `ordering` (DirectoryOrdering, default `position` = position, Uzbek name, id; `name` / `-name` by the Uzbek name); export `xizmatlar` (§1.8). Two queries for any page.
- **`POST services/`** → 201; **`GET services/{id}/`**; **`PATCH services/{id}/`** → 200 (every field optional, texts merge leaf by leaf). There is **no delete** (405): `is_active: false` retires a service.

| Field | Rule | Field errors |
|---|---|---|
| `name.uz` | trimmed 2..120, required on create | `required` · `blank` · `min_length` · `max_length` "120 belgidan oshmasin." |
| `name.ru`, `name.en` | ≤120, optional | `max_length` |
| `short_name.uz` | 1..40; omitted on create → the first 40 characters of `name.uz` | `blank` · `max_length` "40 belgidan oshmasin." |
| `short_name.ru`, `.en` / `description.*` | ≤40 / ≤2000, optional | `max_length` |
| `category` | ServiceCategory, required on create | `required` · `null` · `invalid_choice` |
| `default_duration_min` | 5..480, a multiple of 5, required on create | `min_value` · `max_value` · `invalid` "Davomiylik 5 daqiqaga karrali bo‘lsin." |
| `default_price` | so'm 0..100 000 000 or null | `min_value` · `max_value` "Narx juda katta." |
| `position` | int 0..100000; default: appended last | |
| `is_active` | bool, default `true` | |

- A deactivated service leaves the customer catalogue (`services/`, the specialists' `services[]`, the slots) and the admin booking dialog (`appointments/meta/`) at once; its appointments are kept and stay manageable. Names are not snapshotted on appointments (a rename shows on past appointments too); prices and durations are.
- Audit `service.created`, `service.updated` (changed fields), `service.deactivated`, `service.activated`; a PATCH that changes nothing writes nothing.

**Clinics**
```jsonc
// ClinicLocation
{"id": 1, "name": {"uz": "Glow Derma klinikasi", "ru": "", "en": ""},           // i18n, uz 2..120
 "address": {"uz": "Toshkent, Chilonzor tumani, …", "ru": "", "en": ""},        // i18n, ≤300 each
 "directions": {"uz": "Ikkinchi qavat", "ru": "", "en": ""},                        // i18n, ≤500 each
 "is_active": true,
 "phone": "+998712000000",                                                      // E.164 | null
 "latitude": 41.311081, "longitude": 69.240562,                                 // number (≤6 dp) | null, a pair
 "updated_at": "…"}
```
- **`GET clinics/`** returns `{"results": [ClinicLocation…]}` including archived clinics. **`GET clinics/{id}/`** retrieves one clinic.
- **`POST clinics/`** creates a clinic (201) and requires `name.uz` and `address.uz`. **`PATCH clinics/{id}/`** updates one clinic (200) and merges translated fields leaf by leaf. `phone` E.164 or null (`invalid` "Telefon raqamini to‘g‘ri kiriting."); `latitude` −90..90 and `longitude` −180..180, ≤6 decimals (`max_decimal_places` "Ko‘pi bilan 6 ta kasr raqam."), set or cleared together (the missing one `latlng_pair` "Kenglik va uzunlikni birga kiriting."). Set `is_active` to `false` to archive and `true` to restore. Archiving returns 409 when the clinic has an active specialist assignment or an upcoming requested or confirmed visit. Historical visits keep their clinic. Audit `location.created` (the new row) / `location.updated` (changed leaves); a PATCH that changes nothing writes nothing.

### 2.18 Yandex Delivery (courier dispatch, returns, exception queue)
Yandex Delivery is the courier carrier of home delivery next to BTS (`docs/integrations/yandex-delivery.md`). An
order ships by Yandex when the customer checked out with a Yandex quote (customer contract §2.7.1); its
`Shipment` then has `provider = "yandex"`, a `method` (`standard` | `express`) and a `stage`. Nothing is sent to
Yandex at payment: staff prepare the parcel and call the courier here. Every BTS rule of §2.2 stays BTS-only
(`retry_shipment`, the BTS cancel texts, `attention.bts_failed`, K4). Amounts are so'm; Yandex's prices are
VAT-inclusive.

**`delivery`** — on every `OrderSummary` (`orders/` rows) and `Order` (`orders/{id}/`); `null` for pickup. A BTS
order carries `provider: "bts"` and its `open_issues`; every other key is `null` for it. Never a courier phone.
```jsonc
// OrderDelivery
{"provider": "yandex",                                   // DeliveryProvider
 "method": "standard",                                   // DeliveryMethod | null
 "stage": "in_transit",                                  // ShipmentStage | null
 "promised_from": "2026-11-15T15:30:00+05:00",           // | null: the promised window (from paid_at)
 "promised_to": "2026-11-15T17:30:00+05:00",             // | null
 "quote_fee": 25000,                                     // | null: the fee the checkout quote charged
 "carrier_price": 21800,                                 // | null: Yandex's estimate behind that fee
 "offer_price": 23000,                                   // | null: the current claim's binding offer
 "approved_price": null,                                 // | null: the price staff approved for the current claim
 "carrier_cost_total": 0,                                // | null (BTS): Yandex's final prices and paid cancel fees of every claim
 "cost_difference": null,                                // | null until a cost is recorded: paid fee − carrier_cost_total
 "parcel_weight_g": 900, "prepared_at": "2026-11-15T14:05:00+05:00",   // | null: set by the dispatch
 "courier_name": "Aziz", "courier_vehicle": "Chevrolet Cobalt oq 01A777AA",   // | null; blanked 7 days after the claim ended
 "tracking_url": "https://…",                            // | null: Yandex's link for the recipient
 "claim_status": "pickuped",                             // | null: Yandex's status of the current claim, verbatim
 "claim_number": 1,                                      // | null: 1 for the first courier claim, then 2, …
 "failure_code": null,                                   // | null: why the current claim ended (its error code)
 "open_issues": [{"id": 77, "kind": "dispatch_failed", "code": "offer_expired", "urgent": false,
                  "opened_at": "2026-11-15T14:20:00+05:00"}]}   // urgent first, then oldest first
```

| `stage` | Meaning |
|---|---|
| `awaiting_dispatch` | paid; staff prepare the parcel and call a courier |
| `courier_requested` | a courier claim exists (estimating, waiting for acceptance, or Yandex is searching) |
| `courier_assigned` · `courier_arrived` | Yandex found a courier · the courier is at the warehouse |
| `in_transit` · `at_destination` | picked up (the order is `handed_to_courier`) · at the customer |
| `delivered` | handed over (the order is `delivered`) |
| `returning` · `returned` | the parcel is on its way back · back at the warehouse (staff confirm it) |
| `cancelled` | the claim was cancelled (our cancel) |
| `failed` | no courier came (the claim failed before pickup): staff dispatch again |

**`POST orders/{id}/dispatch/`** (I) — staff checked the parcel and call a courier («Kuryer chaqirish»). Body:
```jsonc
{"contents_verified": true,       // required, must be true
 "weight_g": 900,                 // required, 1..20000 (grams)
 "approved_price": null,          // optional int 0..100000000 | null: only while a dispatch_review issue is open
 "method": null}                  // optional DeliveryMethod | null: switch the method (the paid fee never changes)
```
→ **202 `Order`** (`delivery.stage = "courier_requested"`, `available_actions.dispatch = false`). Nothing is sent
to Yandex in the request: a new open courier claim (a fresh `request_id`) is stored and the outbox message
`yandex.dispatch` does the rest (create → estimate → pricing policy → accept, `docs/integrations/yandex-delivery.md`
§8–§9). Allowed for a paid Yandex order that is `processing` (or `handed_to_courier` with stage `returned`: a new
attempt after a return), at stage `awaiting_dispatch` / `failed` / `returned`, with no open claim and no cancel in
progress, a pinned address and a warehouse that may call a courier now. Resolves the order's open
`dispatch_failed`, `dispatch_review` and `preparation_overdue` issues. Audit `order.dispatch_requested
{claim_number, request_id, method, taxi_class, weight_g, approved_price}`.

| Error | Code · message |
|---|---|
| body | 400 `validation_error`: `contents_verified` `required` / `invalid` "Jo‘natma tarkibini tekshirganingizni tasdiqlang."; `weight_g` `required` / `min_value` / `max_value`; `method` `invalid_choice`; `approved_price` `min_value` / `max_value` |
| unknown order | 404 |
| not a paid open Yandex delivery waiting for a courier (stage `awaiting_dispatch` / `failed` / `returned`; a `handed_to_courier` order only once its parcel is `returned`) | 409 `invalid_transition` "Bu buyurtma uchun kuryer chaqirib bo‘lmaydi." |
| a courier claim is still open (double click, a dispatch running, a courier may come) | 409 `conflict` "Kuryer allaqachon chaqirilgan." |
| a carrier cancel is in progress and its claim has ended (the cancel stamp still set, e.g. the claim closed as `cancel_requested` with the order open; while the claim is open the row above answers) | 409 `conflict` "Buyurtma bekor qilinmoqda." |
| `approved_price` without an open `dispatch_review` | 409 `conflict` "Bu buyurtmada tasdiqlanadigan narx yo‘q." |
| the order's address has no map pin | 409 `coordinates_required` "Manzilning xaritadagi nuqtasi yo‘q." |
| the switched-to method is off | 409 `method_disabled` "Bu yetkazish usuli o‘chirilgan." |
| `weight_g` above the class limit (courier 10 kg, express 20 kg) | 409 `too_heavy` "Jo‘natma bu yetkazish usuli uchun juda og‘ir." |
| warehouse calendar | 409 `not_configured` "Ombor ma’lumotlari to‘liq kiritilmagan." · `suspended` "Kuryer chaqirish vaqtincha to‘xtatilgan." · `closed` "Ombor hozir yopiq." · `after_cutoff` "Bugun kuryer chaqirish vaqti tugadi." · `returns_unavailable` "Ombor hozir qaytgan jo‘natmalarni qabul qilmaydi." |
| the order is busy > 5 s | 409 `conflict` (§2.2 shared outcomes) |

The outcome arrives later: the stage moves with Yandex's statuses (`courier_assigned`, … `delivered`), or an issue
opens (`dispatch_failed`, `dispatch_review` with the offer, the estimate and the limits in `details`,
`dispatch_unknown`). For a `dispatch_review`, staff dispatch again with `approved_price` (accepted up to that
amount), with another `method`, or cancel the order. A dispatch that ends without a courier — Yandex refused the
claim or the accept, the accept is still not visible 15 minutes after it was sent (or, once the offer expired, at
least 2 minutes after it: `accept_unconfirmed`), the dispatch died with no accept outstanding (`dead_letter`),
`claims/info` refuses the claim after the accept (`dispatch_unknown` `not_found`) — closes its claim and leaves the stage `failed` (`returned` after a return), so `dispatch` is
allowed again; only a claim Yandex may still be serving (a dispatch running, an accept whose outcome is not known
yet, a dead dispatch after the accept: urgent `dispatch_unknown` `dead_letter`) keeps it blocked
(`docs/integrations/yandex-delivery.md` §9).

**Cancel of a Yandex order** (`POST orders/{id}/cancel/`, body and success as §2.2). Before its idempotent
transaction the view makes the order carrier-safe (skipped on a replay of the same key), holding the order's Yandex
advisory lock; then the unchanged cancel runs (full refund when paid).

| Situation | Result |
|---|---|
| unpaid, or paid with no open courier claim | as §2.2 (no Yandex call) |
| a claim whose accept was never sent | the claim is closed here (it summons nobody; best-effort free cancel at Yandex) → 200, cancelled and refunded |
| claim open, before pickup | Yandex's cancel terms, then its cancel (free or paid) and a re-read showing it cancelled → 200, cancelled and refunded; a paid cancel fee is recorded in `carrier_cost_total` as soon as Yandex answered the cancel 2xx and that answer was recorded under the order lock (also when the 409 below follows) — after a lost answer, or the busy order below, only once Yandex shows `cancelled_with_payment`; the `cancelled` event has `bts_cancelled: true` |
| Yandex refuses (terms `unavailable`, a conflict) | 409 `not_cancellable` "Yandex kuryer buyurtmasini bekor qilmadi (kuryer posilkani olgan yoki bekor qilish muddati o‘tgan). Holatni tekshirib, qayta urinib ko‘ring." — order unchanged (a refused cancel whose re-read shows the claim cancelled all the same → 200) |
| nothing can have reached Yandex (Yandex unreachable for the first read or the terms, or the cancel / return answered "not processed") | 503 `provider_unavailable` "Yandex javob bermadi, buyurtma bekor qilinmadi. Qayta urinib ko‘ring." — order unchanged |
| the cancel was answered 2xx, **or its answer was lost**, and Yandex does not show it yet | 409 `conflict` "Yandex kuryer buyurtmasini bekor qilishni hali tasdiqlamadi. Birozdan so‘ng qayta urinib ko‘ring." — order unchanged |
| picked up (in transit, at the customer) | Yandex is asked to bring the parcel back: shown `returning…` → 200, cancelled and refunded, issue `return_pending` until staff confirm the parcel back; not shown yet, **or the answer lost** → **409 `return_requested`** "Yandexdan posilkani omborga qaytarish so‘raldi. Buyurtma hali bekor qilinmadi: Yandex qaytarishni tasdiqlagach, bekor qilishni qayta bosing." (order unchanged, `return_pending` open); refused → 409 `not_cancellable` "Yandex posilkani omborga qaytarishni rad etdi. Yandex qo‘llab-quvvatlash xizmatiga murojaat qiling."; not processed → 503 |
| a retry after 409 `conflict` / `return_requested` | the cancel or return is **never sent again** once it was recorded as sent (Yandex answered 2xx, or the outcome is unknown): Yandex's claim is only read again — shown → 200, cancelled and refunded; not yet, or the read fails → the same 409 (never 503). Without that record (lost in a crash, or ERROR `yandex.carrier_marker_lost` below) a later attempt may send the cancel / return again |
| the order stays busy (> 15 s) right after Yandex answered the cancel / return 2xx (or its outcome turned unknown) | 409 `conflict` with the pending-cancel / requested-return message (a return without the code `return_requested`); the "sent" record is written without the order lock (up to 5 s more), so a retry only reads — unless that write times out too (ERROR `yandex.carrier_marker_lost`): no record, and a later attempt may send the call again. This step writes no `order.carrier_cancel` audit (log WARNING `yandex.carrier_record_busy`); a paid fee is counted only once Yandex shows `cancelled_with_payment` (never when it shows plain `cancelled`) |
| stage `returning` / `returned` | no Yandex call → 200, cancelled and refunded; `return_pending` kept / opened (unless the return of the claim that carried the parcel was confirmed) |
| delivered | 409 `not_cancellable` "Buyurtma mijozga yetkazilgan: uni bekor qilib bo‘lmaydi." |
| picked up, the claim ended without a return | 409 `not_cancellable` "Posilka kuryerda, Yandex esa uni omborga qaytarayotganini bildirmagan: buyurtmani hozir bekor qilib bo‘lmaydi." |
| another Yandex operation of the order is running (a dispatch) | 409 `conflict` "Bu buyurtma bo‘yicha Yandex bilan boshqa amal bajarilmoqda (kuryer chaqirilmoqda). Birozdan so‘ng qayta urinib ko‘ring." |

While a sent cancel or return waits for Yandex (409 `conflict` / `return_requested` above) its claim is still
open, so `orders/{id}/dispatch/` answers 409 `conflict` "Kuryer allaqachon chaqirilgan."; "Buyurtma bekor
qilinmoqda." (409 `conflict` too) comes only once that claim has ended with the cancel stamp still set (e.g. a
claim closed as `cancel_requested`). Each carrier outcome is audited `order.carrier_cancel {outcome, claim_number,
cancel_state?, cancel_fee?, status?, error_code?}` next to `order.cancelled` — except the busy-order step above,
which writes no audit row (only its WARNING `yandex.carrier_record_busy` / ERROR `yandex.carrier_marker_lost` log
line). A cancelled Yandex order also resolves its `dispatch_failed`,
`dispatch_review`, `preparation_overdue`, `delivery_failed` and `cancel_incomplete` issues (`return_pending`,
`parcel_unaccounted`, `dispatch_unknown` and `incident` stay).

**`POST orders/{id}/confirm-return/`** (I) — the returning / returned parcel is physically back at the warehouse.
Body `{"note": str ≤1000, optional, default ""}` (its condition; it becomes the `return_pending` resolution note).
→ **200 `Order`**: stage `returned`, event `shipment_returned` (source `admin`), the open `return_pending`
resolved by the caller (audit `delivery.issue_resolved`), audit `order.return_confirmed {claim_number,
order_status, note}`. A cancelled order stays cancelled (its failed-delivery issues close); an open order stays as
it is (staff dispatch again or cancel). The return confirmed is the one of the claim that **carried the parcel**
(the highest-numbered claim that picked it up), not of the current claim: `claim_number` and the event's
`claim_id` are that claim's, and a re-dispatch after a confirmed return that fails or goes to price review (stage
back at `returned`) offers no second confirmation (`available_actions.confirm_return` false, a further call 409
`conflict`, a cancel opens no new `return_pending`). Errors: 400 `note` `max_length` "1000 belgidan oshmasin."; 404;
409 `invalid_transition` "Qaytayotgan posilka yo‘q: qaytishni tasdiqlab bo‘lmaydi." (also when no claim ever
picked the parcel up); 409 `conflict` "Posilka qaytgani allaqachon tasdiqlangan." (or the order is busy).

**Manual transition.** A Yandex order offers no manual status change, except `delivered` from
`handed_to_courier` while a `parcel_unaccounted` issue is open (staff know the parcel reached the customer although
Yandex says otherwise); that transition resolves `parcel_unaccounted` and sets `delivery.stage` to `delivered`
(a manual shipment event records who confirmed it; the shipment state, the costs and the claim stay as Yandex
left them), so neither the customer nor staff see the stage Yandex left (`cancelled` / `failed`) on a delivered
order.

**Delivery issues** — the exception queue. One open row per order and kind; most are opened and closed by the
system; urgent ones alert staff (alert webhook → Telegram, `docs/ops/monitoring.md` §1) and are reminded every
30 minutes while nobody touches them.
```jsonc
// DeliveryIssue
{"id": 77, "order_id": 10251, "kind": "dispatch_failed",   // DeliveryIssueKind
 "code": "offer_expired",                                  // why (Yandex's or our short code)
 "urgent": false,
 "details": {"order_id": 10251, "claim_number": 1},        // ids and amounts only
 "note": "",                                               // staff text (incidents)
 "state": "open",                                          // DeliveryIssueState: open | resolved
 "opened_at": "2026-11-15T14:20:00+05:00", "updated_at": "2026-11-15T14:20:00+05:00",
 "reminded_at": null, "reminders": 0,
 "resolved_at": null, "resolved_by": null,                 // Actor | null (null while open or when the system closed it)
 "resolution_note": ""}
```

| `kind` | Opened when (codes) |
|---|---|
| `dispatch_failed` | a dispatch summoned no courier: Yandex's refusal code (of the claim, or the 409 code of a refused accept), `not_configured`, `coordinates_required`, `estimating_timeout`, `offer_expired`, `previous_claim_active` (urgent), `not_found`, a calendar refusal at the accept, `accept_unconfirmed` (the accept still not visible 15 minutes after it was sent, or once the offer expired and at least 2 minutes after it was sent), `dead_letter` (the dispatch died with no accept outstanding); a claim that failed before pickup (`performer_not_found`, `cancelled_by_taxi`, `failed`, …); `courier_search_slow` (urgent: no courier 30 minutes after acceptance) |
| `dispatch_unknown` | Yandex may have taken our accept: `not_found` (urgent: Yandex refuses to show the claim 15 minutes after the accept; the claim stays polled), `dead_letter` (urgent: the dispatch died after the accept was sent), `unexpected_acceptance` (urgent: a claim we no longer follow shows accepted), `foreign_claim` |
| `dispatch_review` | the binding offer needs staff: `increase_over_limit`, `loss_over_limit`, `above_approved`, `warnings`, `offer_missing` |
| `delivery_failed` | Yandex started a return we did not ask for (Yandex's return reason, else the status) |
| `return_pending` | a parcel is coming / came back: `returning`, `returned`, `return_requested`, `warehouse_closed` (urgent) |
| `preparation_overdue` | still not dispatched `prep_deadline_minutes` working minutes after payment: `overdue`, `suspended`, `closed`, `not_configured`, `returns_unavailable` |
| `cancel_incomplete` (urgent) | a carrier cancel stopped half-way for 10 minutes (from its first press) with the order still open: `carrier_cancelled` (Yandex shows the claim cancelled, or the claim was withdrawn for the cancel), `returning` |
| `parcel_unaccounted` (urgent) | the claim ended after pickup without a return (code = Yandex's status) or Yandex delivered a cancelled order (`delivered_after_cancel`) |
| `incident` | staff report (`staff_report`) |

- **`GET delivery/issues/`** (P) — query `state` (`open` | `resolved`), `urgent` (bool), `kind`
  (DeliveryIssueKind), `order` (order id); bad values → 400 `invalid_choice` / `invalid`. Open issues first, urgent
  first, then oldest first; `state=resolved` lists the newest resolved first. → `Page<DeliveryIssue>`.
- **`POST orders/{id}/issues/`** (I) — staff report an incident on a delivery order. Body `{"kind": "incident",
  "note": str 3..1000, "urgent": bool = false}` → **201** the new issue, **200** the open incident refreshed (note,
  urgency). Audit `delivery.issue_opened`; an urgent incident alerts. Errors: 400 `kind` `invalid_choice` (any
  other kind), `note` `required` / `min_length` "Kamida 3 ta belgi kiriting." / `max_length` "1000 belgidan
  oshmasin."; 404; 409 `conflict` "Olib ketiladigan buyurtma uchun yetkazish muammosi ochilmaydi." (a pickup order).
- **`POST delivery/issues/{id}/resolve/`** (I) — body `{"note": str 3..1000}` → 200 `DeliveryIssue` (audit
  `delivery.issue_resolved`). 404; 409 `conflict` "Bu muammo allaqachon hal qilingan.".

**Dispatch suspension** — the panel switch over `DeliverySettings.dispatch_suspended` (the warehouse settings
themselves are edited on the ops console, `/django-admin/delivery/deliverysettings/1/change/`).
- **`GET delivery/suspension/`** → `{"suspended": bool, "until": datetime | null, "note": str, "updated_at":
  datetime}`; `suspended` is what is in effect now (an `until` that passed → `false`).
- **`POST delivery/suspension/`** (I) — body `{"suspended": bool, "until": datetime with an offset | null =
  until lifted, "note": str ≤300 = ""}` → 200 the same shape; audit `delivery.suspension_updated` (before / after);
  no change → nothing written. `until` in the past while suspending → 400 `until` `in_past` "Kelajakdagi vaqtni
  tanlang."; `until` is ignored when resuming; `note` `max_length` "300 belgidan oshmasin.". While suspended, no
  courier is called (`dispatch` → 409 `suspended`) and checkout lists the Yandex options as unavailable
  (`dispatch_suspended`).

**Dev only** — `POST /api/v1/admin/dev/yandex/{order_id}/advance/` `{"status": <one of Yandex's 26 claim
statuses>}` → 200 `Order`: moves the order's current claim in the **fake** Yandex adapter and applies it exactly as
the poll would (409 `conflict` "Buyurtmada Yandex kuryer buyurtmasi yo‘q." without a claim; 400 `invalid_choice`).
Mounted only under the local/test settings (`DEV_FEATURES_ALLOWED`) with `YANDEX_DELIVERY_MODE=fake` (404
otherwise), staff auth, not in the OpenAPI document; the admin panel's mock tools and the e2e suites drive the fake
courier with it.

### 2.19 AI assistant (feat/ai-assistant)
The product editor's "✨ AI yordamchi": suggestions for missing or chosen product texts, SEO and image alt texts in
uz / ru / en, generated asynchronously and **never written until a person applies them**. Any active staff member
(the people who may edit products) may use it; the provider, model, API key (Vault KV v2), prompts, budgets and
limits are managed by superusers in the ops console only. Responses never carry tokens, costs, model ids, prompts,
provider errors or the key.

**Keys.** Every target is a product snapshot key (§2.3 audit vocabulary): `texts.<lang>.<field>` (`name`,
`short_description`, `description`, `ingredients`, `usage`), `seo.<lang>.<title|description>` and
`images.<image_id>.alt.<lang>`. Nothing else can be targeted or written (never `slug`, price, status, promo,
category, Billz fields, volume or stock).

**`GET assistant/status/`** → `{"enabled": bool, "max_images": int}` — `enabled` = switched on in the console
and its provider ready; the editor shows the button only then.

**`POST products/{id}/assistant/jobs/`** (optional `Idempotency-Key`; throttle `assistant_generate`, §1.9)
```jsonc
{"mode": "fill_missing",          // AssistantMode: fill_missing (default) | languages | improve | photos ("test" → 400)
 "languages": ["uz", "ru", "en"], // optional, default all three
 "keys": [],                      // optional: narrows the automatic choice; required (non-empty keys) for improve
 "goal": "professional",          // AssistantImproveGoal, improve only (default professional)
 "image_ids": []}                 // photos only: the product's images to read (default: the first max_images)
```
Modes: **fill_missing** — only empty keys; `name`, `ingredients` and `usage` only when the same field is filled in
another language (translated, never invented); the rest written from the product data. **languages** — only
empty keys whose field is filled in another language (localisation). **improve** — only the chosen keys that are
filled; suggestions never replace anything by themselves. **photos** — `name`, `ingredients`, `usage` of the chosen
languages read from the packaging; every suggestion starts unchecked with warning `confirm_packaging`; the brand and
volume read are information only (`result.warnings` item `{"code": "extracted", "brand"?, "volume"?}`).
- 202 `{"nothing_to_fill": false, "job": AssistantJob}` — queued.
- 200 `{"nothing_to_fill": false, "job": AssistantJob}` — the product already has a queued/running job (one per
  product); it is returned instead of a second one.
- 200 `{"nothing_to_fill": true, "job": null}` — the mode found no key to suggest (no job, no cost).
- 400 `validation_error`: `mode` `invalid_choice` ("Bu rejim faqat boshqaruv konsolida ishlatiladi." for `test`),
  `languages.<i>` `invalid_choice`, `keys.<i>` `invalid` "Bu maydonni AI to‘ldira olmaydi.", `keys` `required` /
  `invalid` "Yaxshilash uchun kamida bitta to‘ldirilgan maydonni tanlang." (improve), `image_ids` `invalid`
  "Mahsulotda rasm yo‘q." (photos without an image) or `invalid` (an image of another product), `goal`
  `invalid_choice`.
- 404 unknown product. 409 `assistant_disabled` "AI yordamchi o‘chirilgan."; 409 `ai_not_configured` "AI yordamchi
  hali sozlanmagan. Administratorga murojaat qiling." (provider disabled or no active key); 409
  `assistant_budget_exceeded` "AI uchun ajratilgan byudjet tugadi. Administratorga murojaat qiling." (today's or this
  month's spend reached the console budget, Tashkent days); 429 `assistant_daily_limit` "Bugungi AI so‘rovlari
  chegarasiga yetdingiz. Ertaga qayta urinib ko‘ring." (the person's jobs today reached the console limit); 429
  `throttled`.

**`GET assistant/jobs/{id}/`** → `AssistantJob` (poll like §2.14: `state` `queued|running|succeeded|failed`)
```jsonc
{"id": 41, "product_id": 12, "mode": "fill_missing", "languages": ["uz", "ru", "en"],
 "keys": ["texts.ru.name", "seo.ru.title"],        // the job's target keys
 "goal": "", "image_ids": [],
 "state": "succeeded", "outcome": "pending",        // AssistantJobOutcome: pending | applied | partial | discarded
 "progress": null,                                  // always null
 "result": {                                        // null unless succeeded
   "suggestions": [
     {"key": "texts.ru.name", "current": "", "suggested": "Сыворотка с гиалуроновой кислотой",
      "warnings": [],               // unsupported_claim | inci_mismatch | confirm_packaging | seo_title_long | seo_description_long
      "origin": "ai",               // ai | image (photos)
      "checked": true,              // pre-selected in the review; false for photos and unsupported_claim
      "max_length": 120}],
   "warnings": [{"code": "rejected", "key": "texts.ru.description", "reason": "unsupported_number"},
                {"code": "dropped", "count": 1}]},  // reasons: too_long | unsupported_number | unsupported_spf | empty
 "error": null,                                     // failed: {"code", "message"} (table below), message in the request language
 "applied_keys": [],
 "created_at": "…", "started_at": "…", "finished_at": "…",
 "created_by": {"id": 1, "display_name": "Administrator"}}
```
- Only keys with a suggestion are listed; a suggestion equal to the current value is not. A value with a number,
  percentage or SPF that the product data does not contain is **rejected** (listed in `result.warnings`, never
  suggested; photos excepted, they read new facts from the label); a certification / clinical / medical term the
  data does not contain is **flagged** (`unsupported_claim`, unchecked).
- 404 unknown id (ops-console prompt tests are not served here).

| `error.code` | `error.message` | When |
|---|---|---|
| `provider_unavailable` | "Tashqi xizmat javob bermadi. Qayta urinib ko‘ring." | The provider timed out / 429 / 5xx after its bounded retries, or the run ran out of time (90 s) |
| `ai_bad_output` | "AI javobini tushunib bo‘lmadi. Qayta urinib ko‘ring." | The answer was malformed twice (one retry) |
| `ai_refused` | "AI bu so‘rovni bajarmadi. Ma’lumotlarni tekshirib, qayta urinib ko‘ring." | The provider refused the request (safety block, invalid request) |
| `ai_not_configured` | "AI yordamchi hali sozlanmagan. Administratorga murojaat qiling." | No active key, the key was rejected, the stored version is gone, or the provider was disabled |
| `vault_unavailable` | "AI xizmati vaqtincha ishlamayapti. Birozdan so‘ng qayta urinib ko‘ring." | The key could not be read from Vault |
| `assistant_disabled` | "AI yordamchi o‘chirilgan." | Switched off after the job was queued |
| `not_found` | "Topilmadi." | The product was deleted before the run |
| `worker_lost` | "Jarayon uzilib qoldi. Qayta urinib ko‘ring." | No heartbeat for 5 minutes, or still queued 5 minutes after creation (beat `assistant.reap_stale_jobs`, every 2 minutes) |
| `server_error` | "Kutilmagan xato." | An unexpected error in the worker |

**`POST assistant/jobs/{id}/apply/`** (optional `Idempotency-Key`) Body
`{"items": [{"key": "texts.ru.name", "value": "Сыворотка"}, …]}` (1–120 items; `value` may be the edited
suggestion) → 200
```jsonc
{"job": AssistantJob,                  // outcome applied (no conflict) | partial (some conflicts) | pending (nothing applied)
 "applied": ["texts.ru.name"],
 "conflicts": ["texts.en.name"],       // the product value changed since the generation: not written
 "product": Product}                   // §2.3, after the write (re-base the editor on it)
```
- Each value is trimmed, Uzbek apostrophes normalised (o‘ / g‘ U+2018, tutuq belgisi U+2019) and checked again:
  400 `items.<i>.value` `max_length` (the field's §2.3 limit), `blank`, or `invalid` "Mahsulot ma’lumotlarida yo‘q
  raqam yoki da’vo bor: matnni tekshiring."; `items.<i>.key` `invalid` "Bu maydon uchun taklif yo‘q." (not one of
  the job's suggestions); `items` `max_items` / empty.
- Written through `PATCH products/{id}/`'s service in one transaction with the values seen at generation as the
  expectation (§2.3 "Concurrent change"): changed keys become `conflicts`, the others are written (one
  `product.updated` audit row) — never a silent overwrite.
- 409 `assistant_job_not_ready` "Bu takliflar hali tayyor emas yoki allaqachon ko‘rib chiqilgan." unless the job
  succeeded and its outcome is `pending` (a review is applied or discarded once). 404 unknown id.

**`POST assistant/jobs/{id}/discard/`** (optional `Idempotency-Key`, no body) → 200 `AssistantJob` (outcome
`discarded`); 409 `assistant_job_not_ready` as above.

Audit: `assistant.suggestions_applied` (entity `product`; `{job_id, applied, conflicts}`) and
`assistant.suggestions_discarded` (`{job_id, suggestions}`), plus the usual `product.updated` of the write.

### 2.20 Product scenes and screen fitting

A new background around a saved product photo, or proposed 4:5 and square crops of it. **The product itself is
never redrawn**: the photo's own pixels are pasted back over whatever the image model returns, and results
where the model moved, resized or restyled the product are refused. Ships switched off (ops console
`ai/scenes/`); `docs/ops/product-scenes.md` describes the pipeline.

**`GET scenes/status/`** → `{"enabled": bool, "ai_enabled": bool, "max_occluded_pct": "15.00", "segmenter":
"gemini_matte" | "bria" | "gemini_json"}`.

**`GET scenes/templates/`** → `{"results": [{"id", "key", "kind": "studio" | "ai_scene", "name" (request
language, else uz), "colours": ["#rrggbb", …] (studio swatch), "preview_url": url | null}]}`, enabled ones in
console order. `preview_url` is a 4:5 sample picture of the scene (600 px wide): a bundled sample product in it
(made in the ops console: studio ones at once, AI ones as a billed job of kind `preview`), or, for a template
staff saved, the scene they made. It is cleared when the template's scene changes.

**`POST products/{id}/images/{image_id}/scenes/`** (optional `Idempotency-Key`; throttle `scene_generate`
20/hour per staff) Body `{"kind": "studio" | "ai_scene" | "fit", "template_id"?: int, "custom_prompt"?: str}`
(`template_id` required unless `fit` or `custom_prompt`; its kind must match). `custom_prompt` (`ai_scene` only,
at most 400 characters, any language) is a scene staff typed: the worker turns it into an English scene
description with the analyst model and refuses one that asks for people, hands, text or brands
(`scene_custom_refused`); the job's `template` is then `{key: "custom", name, custom_text}` → 202 `SceneJob`
(new) or 200 (the image's queued/running job). 409 `scenes_disabled`, `scene_ai_disabled`,
`assistant_budget_exceeded`; 429 `scene_daily_limit`; 400 `template_id` / `custom_prompt` / `kind` (`preview` is
the console's); 404 another product's image.

**`GET scenes/jobs/{id}/`** → `SceneJob = {id, product_id, source_image_id, kind, template: {key, name, custom_text,
saved_template_id} | null,
state (JobState), outcome ("pending" | "accepted" | "discarded" | "expired"), progress: null, result, error:
{code, message} | null, warnings: [str], accepted_image_id, created_at, finished_at, expires_at, created_by}`.
`result` (succeeded and not expired) = `{url (the 4:5 candidate; null for fit), overlay_url (the candidate with
the parts drawn inside the product washed pink; null when none), product_box, crops: {portrait, square},
generated_inside_pct, occluded_pct}`. Failure codes: `scene_product_cut_off` (the product is not whole in the
photo), `scene_hand_covers_product` (a hand hides more than `max_occluded_pct`), `scene_occlusion_needs_ai`
(studio templates fill nothing), `scene_occlusion_not_fillable` (the AI scene provider is Bria, which paints only the
background), `scene_no_product`, `scene_mask_unusable`, `scene_product_drift`,
`scene_result_rejected`, `scene_source_changed`, `provider_unavailable`, `ai_not_configured`, `ai_refused`,
`ai_bad_output`, `vault_unavailable`, `worker_lost`, `server_error`. Warnings: `hand_removed`,
`product_partly_generated`, `product_floating`, `low_resolution`, `straightened`.

**`POST scenes/jobs/{id}/accept/`** (optional `Idempotency-Key`) Body `{"make_main"?: bool, "acknowledge_generated"?:
bool}` → 200 `{"job": SceneJob, "image": ProductImage}`. A scene becomes a **new** gallery image (`source`
`studio` / `ai_scene`, `derived_from_id` = the source, both renditions set; the source stays; `make_main` puts
it first; 400 `images` `max_items` at 8); a `fit` writes the proposed renditions (`mode` `auto`) and
`product_box` on the source image. 400 `acknowledge_generated` when `generated_inside_pct` > 0 and not
acknowledged; 409 `scene_job_not_ready` (not succeeded or already reviewed), `scene_source_changed`.

**`POST scenes/jobs/{id}/discard/`** (no body) → 200 `SceneJob` (`discarded`; its files are deleted).

**`POST scenes/jobs/{id}/template/`** (optional `Idempotency-Key`) Body `{"name": str}` (≤ 60, used for every
language) → 201 `SceneTemplate` (the list's shape). Saves a finished scene staff typed (`template.key ==
"custom"`) as an AI template for everyone: its English description, at the end of the list, with the scene as
its preview. Allowed while it waits for review or after it was accepted, once per job. 409 `scene_not_custom`,
`scene_template_exists`; 400 `name`. Audit `assistant.scene_template_created` (entity `assistant_scene_template`,
`{name, from_job, prompt: {length}}`).

Unreviewed candidates expire after `candidate_ttl_hours` (default 72; beat `assistant.scenes_housekeeping`).
Audit: `assistant.scene_accepted` (entity `product`; `{job_id, kind, image_id, source_image_id}`) plus the
catalog's own `product.image_added` / `product.image_renditions`.

#### 2.20.8 Banner images

An AI image for a home-screen banner (§2.5): exactly **2.15:1**, the app's banner, which shows all of it; the
website's 2:1 banner shows its middle (`banner.web_crop`). It is text-free (the website and the app draw the
title over its lower left, which the scene keeps calm) and made at 21:9 by the scene provider, then cropped in
the middle (never stretched). Needs scenes and AI scenes switched on; shares their budgets, daily limit, worker
(one scene at a time) and review window. A banner image is a scene job of kind `banner` (no product image).

**`POST scenes/banner-images/`** (optional `Idempotency-Key`; throttle `scene_generate`) Body
`{"mode": "products"|"scene", "product_ids": [int] (products: 1-3, distinct; the first is the main one, the
tallest), "template_id": int|null (an AI template), "custom_prompt": str (≤ 400, instead of a template, as in
§2.20)}` → 202 `SceneJob` (a new one) or 200 (the staff member's banner image still being made). `products`
places each product's main photo (cut out as for a scene, never redrawn: its own pixels are pasted back) on the
right, inside the website's window, side by side on one baseline. 400 `product_ids` (count, duplicates, a
product without a photo: the message names it), `template_id` (missing or not an AI template),
`custom_prompt`; 409 `scenes_disabled`, `scene_ai_disabled`, `scene_daily_limit`, `scene_budget_exceeded`.

`SceneJob` gains `banner` (null for other kinds): `{mode, products: [{id, name}], failed_product: {id, name} |
null, web_crop: [x, y, w, h], accepted_banner_id}`; `result.url` is the 2.15:1 WebP. Refusals (`error.code`):
`banner_product_photo_unusable` (`failed_product` names the product: not whole in its photo, a hand over it, no
usable cut-out), `scene_source_changed` (a product's main photo was replaced meanwhile),
`scene_custom_refused`, `scene_product_drift`, `scene_result_rejected` (text, people, logos or another product in
the image), plus the provider errors of §2.20.

**`POST scenes/jobs/{id}/banner/`** (optional `Idempotency-Key`) Body `{"banner_id": int|null, "banner": {...}}`
→ 200 `{"job": SceneJob, "banner": Banner}`. Without `banner_id` it creates a banner: `banner` as the fields of
`POST banners/` without `image`; with it, it updates that banner: `banner` as `PATCH banners/{id}/`. The image is
stored through the banner upload path (same checks and variants) in the same transaction; the banner's own
validation applies (400 as §2.5). 404 unknown banner; 409 `scene_job_not_ready` (not a finished banner image, or
already used, discarded or expired). `POST scenes/jobs/{id}/accept/` refuses a banner image (409); discard as
any scene. Audit `assistant.banner_image_used` (entity `banner`; `{job_id, mode, new}`) plus the catalog's own
`banner.created` / `banner.updated`.

---

## 3. Enumerations

### 3.1 Value enums

| Enum | Literals | Used in |
|---|---|---|
| OrderStatus | `awaiting_payment`, `processing`, `handed_to_courier`, `delivered`, `cancelled` | orders, events |
| PaymentStatus | `pending`, `paid`, `failed`, `refunded` | order payment, payments |
| PaymentProvider | `click`, `payme` | |
| FulfilmentMethod | `delivery`, `pickup` | |
| OrderChannel | `ios`, `android`, `web` | `web` (B-W1): an order placed on the customer website, a website session |
| OrderPeriod | `today`, `week`, `month`, `all` | `orders/`, `payments/` |
| EventSource | `system`, `bts`, `admin`, `yandex` (Yandex Delivery) | OrderEvent |
| OrderEventType | 18 values, §4 | OrderEvent |
| CancellationReason | `reservation_expired`, `admin`, `customer_request` | |
| ShipmentState | `not_required`, `not_created`, `failed`, `created` | |
| ShipmentVia | `webhook`, `polling` | |
| DeliveryProvider (Yandex Delivery) | `bts`, `yandex` | `delivery.provider`, `orders/` `provider` |
| DeliveryMethod | `standard`, `express` | `delivery.method`, `orders/{id}/dispatch/` `method` |
| ShipmentStage | `awaiting_dispatch`, `courier_requested`, `courier_assigned`, `courier_arrived`, `in_transit`, `at_destination`, `delivered`, `returning`, `returned`, `cancelled`, `failed` | `delivery.stage`, `orders/` `stage` (§2.18) |
| DeliveryIssueKind | `dispatch_failed`, `dispatch_unknown`, `dispatch_review`, `delivery_failed`, `return_pending`, `preparation_overdue`, `cancel_incomplete`, `parcel_unaccounted`, `incident` | `DeliveryIssue.kind` (§2.18) |
| DeliveryIssueState | `open`, `resolved` | `DeliveryIssue.state` |
| DeliveryUnavailableReason | `coordinates_required`, `provider_unavailable`, `out_of_zone`, `method_disabled`, `too_heavy`, `warehouse_closed`, `dispatch_suspended`, `not_configured` | customer `delivery/calculate/` options (customer contract §2.6) |
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
| SpecialistRole | `dermatologist`, `cosmetologist` | appointments, `specialists/` (the professional category) |
| ServiceCategory | `dermatology`, `cosmetology` | `services/` (FND-7), customer `services/` |
| DirectoryOrdering | `position`, `name`, `-name`, `created_at`, `-created_at` | `specialists/`, `services/` `ordering` |
| AppointmentStatus | `requested`, `confirmed`, `rejected`, `attended` | |
| AppointmentAction | `confirm`, `reject`, `attend` | path segment |
| RequestedVia | `app`, `admin` | |
| BlockKind | `busy`, `off` | |
| NotificationKind | `promo`, `general` | |
| NotificationState | `draft`, `tested`, `scheduled`, `sending`, `stopping`, `sent`, `partially_sent`, `cancelled`, `failed` | approval workflow (§2.8); `queued` was dropped |
| AudienceType | `all`, `segment`, `customer` | |
| LinkKind | `none`, `product`, `url` (gap ADM-10.1) | notifications |
| BannerLinkKind | `none`, `product`, `category`, `url` (gap X-BANNER-URL) | banners |
| TemplateStatus | `processing`, `handed_to_courier`, `delivered`, `cancelled` | notification-templates |
| DashboardRange / Bucket | `today`, `week`, `month`, `custom` / `hour`, `day` | |
| JobKind / JobState | `billz_import` / `queued`, `running`, `succeeded`, `failed` | |
| BillzImportMode | `delta`, `full` | |
| AssistantMode (feat/ai-assistant) | `fill_missing`, `languages`, `improve`, `photos`, `test` (`test` = an ops-console prompt test; refused by the admin API) | §2.19 |
| AssistantJobOutcome | `pending`, `applied`, `partial`, `discarded` | §2.19 |
| AssistantImproveGoal | `professional`, `clear`, `natural`, `seo`, `shorter` | §2.19 `goal` |
| SessionRevokeReason | `logout`, `expired`, `password_changed`, `deactivated`, `replaced`, `admin_revoked`, `refresh_reused` (M2: a customer refresh token came back after its rotation, customer contract §2.1.6) | AuthSession, audit |
| LogoutScope | `current`, `others`, `all` | `auth/logout/` |
| ExportFormat | `csv`, `xlsx` | |
| IntegrationMode | `fake`, `live` | `health/?ready=1`; the ops console settings page (§2.11) |
| SeoSeverity (B-W1) | `error`, `warning`, `info` | `seo_status.issues[].severity` (§2.3, §2.4) |
| SeoIssueCode (B-W1) | `name_missing`, `name_translation_missing`, `description_missing`, `description_short`, `images_missing`, `alt_text_fallback`, `category_missing`, `seo_title_missing`, `seo_title_short`, `seo_title_long`, `seo_description_missing`, `seo_description_short`, `seo_description_long`, `image_missing`, `no_active_products` | `seo_status.issues[].code` (§2.3, §2.4) |

### 3.2 Audit actions (M1; M2 cross-reference row)

Who reads which of them in the admin panel is decided by the prefix tiers of §2.13 (feat/admin-ux): a **new
action prefix must be added to `core/audit_scopes.py`** (rule 1, 2, 3 or 4), otherwise it falls into rule 5
(superusers only) and `core/tests/test_audit_scopes.py::test_every_audited_action_is_classified` fails.
Ops-console actions (`console.*`, `ops.*`, `ops_access.*`), `outbox.*`, `integrations.*`, `report.*`, `pos.*`
and the hand-run fixes listed in rule 1 are superuser-only; `staff.*` is manager+; staff `auth.*` and
`export.downloaded` are own-only for ordinary staff.

| Area | `action` values |
|---|---|
| Auth | `auth.login_succeeded` (feat/admin-ux: `mfa` after an MFA step), `auth.login_failed`, `auth.logout` (`{scope, revoked_count}`), `auth.password_changed` (feat/admin-ux: `{forced}`), `auth.session_revoked` (`{sid, reason}`), `auth.social_linked`, `auth.social_unlinked`; admin panel MFA (feat/admin-ux, §2.1; entity `staff`, actor the person unless noted): `auth.mfa_challenged` (`{email, method}`), `auth.mfa_succeeded` (`{factor, method}`), `auth.mfa_failed` (`{factor, context}`), `auth.mfa_locked` (`{wait}`; `{wait, failures}` when ten wrong codes in a row start the lock), `auth.mfa_enabled` (`{recovery_codes}`), `auth.mfa_disabled` (`{factor, totp_devices, recovery_codes}`), `auth.mfa_recovery_used` (`{remaining}`), `auth.mfa_recovery_codes_regenerated` (`{recovery_codes}`), `auth.mfa_reset` (`{totp_devices, recovery_codes, revoked_count, via}`, actor the manager or `--actor` / system); never a code, a recovery code, the secret or the challenge |
| Staff (§2.16, gap closure) | `staff.created` (`{email, full_name, is_manager, delivery, must_change_password}`), `staff.updated` (before / after of the changed `full_name` / `is_manager`), `staff.deactivated` (before `{is_active: true}`, after `{is_active: false, revoked_count}`), `staff.reactivated` (before `{is_active: false}`, after `{is_active: true}`), `staff.password_reset` (`{revoked_count, generated, delivery, must_change_password}`; never the password), feat/admin-ux `staff.password_link_sent` (`{purpose, via: create|reset|resend|self, expires_at, link_id}`; never the link) and `staff.password_set` (`{purpose, link_id, revoked_count[, console_sessions]}`, actor = the person); every session they revoke adds `auth.session_revoked`. Entity `staff`, id = the account. From the admin API the actor is the manager; from the operator commands it is the `--actor` staff member or `system`, with `reason` "manage.py <command>". They replace M1's `auth.staff_deactivated` / `auth.staff_reactivated`, which were never written (their services had no caller; decision GST-5). |
| System | `outbox.dead` (`{topic, dedupe_key, attempts, last_error}`), `outbox.requeued` (M3, actor `system`, reason "manage.py requeue_outbox"; before `{status, attempts, last_error}`, after `{topic, dedupe_key, status: "pending"}`), `outbox.resolved` (M3, `requeue_outbox --resolved`: a dead message handled by hand is closed as `done` without running it — a `billz.push_sale` only once its sale is recorded with `billz_mark_recorded`; reason "manage.py requeue_outbox --resolved", before as above, after `{topic, dedupe_key, status: "done"}`); `billz.sale_recorded_by_hand` (M3, entity `order`, actor = the staff member named by `manage.py billz_mark_recorded --actor`, reason "manage.py billz_mark_recorded"; before `{purchase_id, billz_sale_id, billz_pushed_at: null, billz_payment_attempted_at, push_status}`, after `{purchase_id, billz_sale_id, billz_sale_ref, billz_pushed_at}`), `billz.payment_attempt_forgotten` (M3, entity `order`, actor = `requeue_outbox --forget-payment-attempt --actor`, reason "manage.py requeue_outbox --forget-payment-attempt"; before `{purchase_id, billz_sale_id, billz_payment_attempted_at}`, after `{purchase_id, billz_payment_attempted_at: null}`) |
| Orders | `order.cancelled`, `order.status_changed`, `order.shipment_retry_requested`, `refund.requested`, `refund.state_changed` (system) |
| Yandex Delivery (§2.18) | `order.dispatch_requested` (`{claim_number, request_id, method, taxi_class, weight_g, approved_price}`), `order.carrier_cancel` (`{outcome, claim_number, cancel_state?, cancel_fee?, status?, error_code?}`; outcomes `withdrawn`, `cancelled`, `already_cancelled`, `already_finished`, `returning`, `already_returning`, `return_requested`, `cancel_pending`, `refused`, `unavailable`, `unavailable_terms`, `return_refused`, `delivered`, `parcel_out`, `no_point`), `order.return_confirmed` (`{claim_number, order_status, note}`) — entity `order`; `delivery.issue_opened` (staff incident), `delivery.issue_resolved` — entity `delivery_issue`; ops console and panel switch: `delivery.settings_updated` (changed columns `{from, to}`; the warehouse address and contacts as `{changed: true}`), `delivery.suspension_updated`, `delivery.hours_saved`, `delivery.hours_deleted`, `delivery.closure_saved`, `delivery.closure_deleted`, `delivery.tariffs_checked` (entity `integration` / `yandex`: mode, classes or error code) |
| Catalog | `product.created`, `product.updated`, `product.image_added`, `product.image_deleted`, `product.images_reordered`, `product.image_updated` (feat/ai-assistant: alt texts), `billz.import_started`, `billz.import_finished`, `category.created`, `category.updated`, `category.deleted`, `category.reordered`, `banner.created`, `banner.updated`, `banner.deleted`, `banner.reordered`. Gap closure (catalog): the `category.*` snapshot carries `image` (file name or `null`), the `banner.*` snapshot `link.url`, `notification.created` `link_url`; no new action |
| AI assistant (feat/ai-assistant, §2.19; prefix `assistant`, business tier) | Admin API: `assistant.suggestions_applied` (entity `product`; `{job_id, applied, conflicts}`), `assistant.suggestions_discarded` (`{job_id, suggestions}`). Ops console (superusers; metadata only, never a key): `assistant.settings_updated`, `assistant.provider_updated`, `assistant.credential_pending`, `assistant.credential_rotated`, `assistant.credential_rejected`, `assistant.credential_tested`, `assistant.credential_rolled_back`, `assistant.prompt_draft_created`, `assistant.prompt_draft_updated`, `assistant.prompt_test_started`, `assistant.prompt_activated`, `assistant.prompt_rolled_back` |
| Loyalty | `cashback.adjusted`, `cashback.policy_version_created`, `cashback.policy_version_superseded`. Gap CB-9.2.4: migration `loyalty.0006` writes one `cashback.policy_version_created` (actor `system`, reason "migration loyalty.0006: first cashback policy with the L1–L3 defaults", the service's data shape) when it creates the install's first policy. That v1's `created_at` (2026-05-20 10:00) and `effective_from` (2026-06-01 00:00, Tashkent) are fixed placeholder instants, not the deploy time (GS-6) |
| Notifications | `notification.created`, `notification.draft_updated`, `notification.draft_deleted`, `notification.test_sent` (`{revision, tests[]}`), `notification.test_confirmed`, `notification.approved` (`{approved_revision, dispatch_after, …}`), `notification.returned_to_draft`, `notification.cancelled` (M3; before `{state: "scheduled", scheduled_at, dispatch_after}`, after `{state: "cancelled", cancelled_at}`), `notification.send_started` (system), `notification.stop_requested`, `notification.stopped` (system), `notification.failed` (system; a dead `campaign.send_batch`), `test_device.registered`, `test_device.removed`, `notification_template.updated`, `segment.created`, `segment.updated`, `segment.deleted`, `segment.members_added`, `segment.member_removed`, `notification.push_dead` (system; a dead `push.order_status` / `push.appointment` / `campaign.send_batch` message: `{topic, dedupe_key, attempts, last_error, customer_id, campaign_id}`) |
| Appointments | `appointment.confirmed`, `appointment.rejected`, `appointment.attended`, `appointment.created`, `appointment.rescheduled` (`{…, from_start, from_end, reschedule_no}`), `appointment.cancelled` (`{…, reason}`) |
| Shell | `settings.updated` (changed leaves only, each `{from, to}`; legal-text leaves as `{changed, old_len, new_len, sha256}`, §2.11), `settings.terms_published` (`{from, to, sha256}`, §2.11) — both written by the ops console settings page since 2026-11-07, `export.downloaded` |
| M2 (customer API) | The customer, payment and POS actions listed in `docs/api/customer-api-v1.md` §3 ("Audit actions added in M2"): `customer.*` (B-W1 adds `customer.session_revoked`, also by staff from §2.6, and `customer.sessions_revoked`), `auth.device_mismatch`, `appointment.requested`, `appointment.cancelled` (`by: "customer"`), `payment.late_capture`, `pos.*`. The `audit-log/` filters accept them unchanged. |
| Specialists and services (admin API, §2.17) | `professional.created` (the new row), `professional.updated` (changed fields `{field: old}` / `{field: new}`), `professional.deactivated` (changed fields + `upcoming_appointments`, `forced`), `professional.activated`, `professional.photo_updated` (`{photo}` / `{photo, sha256}`), `professional.photo_deleted`, `professional.services_updated` (`{services: [{service_id, price, duration_min, is_active}]}` before / after), `schedule.hours_updated` (`{days: [WorkingDay]}` before / after), `schedule.override_created`, `schedule.override_deleted` (`{override_id, date, start, end, location_id, note}`), `schedule.block_created`, `schedule.block_deleted` (`{block_id, start, end, kind, label}`) — all with object type `professional` and the specialist's id, so `audit-log/?object_type=professional&object_id=…` is one specialist's history; `service.created`, `service.updated`, `service.deactivated`, `service.activated` (object type `service`); `location.created`, `location.updated` (object type `location`). One row per operation, none when nothing changed. |
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
| `billz_sale_recorded` | system | null | `billz_order_id` (str) | folded into `payment_confirmed`. M3: written once the sale is **paid** in Billz; never for an order cancelled meanwhile (staff refund such a sale in Billz by hand) |
| `cashback_credited` | system | null | `ledger_entry_id`, `amount`, `base`, `percent`, `policy_version` | folded into `payment_confirmed` |
| `payment_failed` | system | null | `provider`, `transaction_id` (str\|null), `error_code` (str\|null) | row |
| `shipment_created` | system or admin | null | `tracking_number`, `tariff`, `manual` (bool) | row. Yandex Delivery: written once per order when Yandex accepts the first courier claim — `tracking_number: ""` (the claim id is never shown), `tariff` = the accepted offer, `manual` = a claim after the first, actor = the staff member who dispatched |
| `shipment_failed` | system or admin | null | `attempt` (int), `manual` (bool), `error_code` (str\|null) | row |
| `status_changed` | admin | `to` | `from`, `to`, `push_sent` (bool) | row (manual transition) |
| `handed_to_courier` | bts or yandex | `handed_to_courier` | `via`, `bts_status` (str: the carrier's status — for Yandex its claim status, e.g. `pickuped`), `push_sent` | row |
| `delivered` | bts or yandex | `delivered` | `via`, `bts_status`, `push_sent` | row |
| `cancelled` | admin (or system for `customer_request`) | `cancelled` | `reason`, `note` (str\|null), `refund_id` (int\|null), `cashback_reversed` (bool), `stock_released` (bool), `bts_cancelled` (bool: the carrier shipment was cancelled — the BTS parcel, or the Yandex courier claim), `push_sent` | row |
| `reservation_expired` | system | `cancelled` | `released_item_count`, `payment_failed` (bool) | row |
| `refund_requested` | admin or system | null | `refund_id`, `amount`, `provider`, `manual` (bool: the refund was created `manual_required`), `manual_reason` (M2: `"payme"` \| `"no_paydoc"` \| `"fake_mode"` when `manual`, else null; absent on M1 rows, read as `"payme"`) | folded into `cancelled` when `cancelled.data.refund_id` equals it, else a row |
| `refund_succeeded` | system | null | `refund_id`, `amount`, `provider` | row |
| `refund_failed` | system | null | `refund_id`, `amount`, `provider`, `state` (`failed`\|`unknown`), `error_code` | row |
| `delivery_failed` (Yandex Delivery) | yandex | null | `provider` (`yandex`), `carrier_status`, `reason` (Yandex's first `return_reasons` entry or `""`), `claim_id` | row: the courier could not hand the parcel over and is bringing it back (once per courier claim; not for a return staff asked for) |
| `shipment_returned` (Yandex Delivery) | yandex or admin | null | `provider`, `carrier_status`, `claim_id` | row: Yandex reports the parcel back (source `yandex`) or staff confirmed it (`admin`, §2.18), once per courier claim |

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
| note: `refund_requested` | `provider`, `amount`, `manual`, `manual_reason` | `` `${P} · ${SOM(amount)}` `` + (manual ? by `manual_reason ?? "payme"`: `payme` " · Payme kabinetida qo‘lda qaytariladi" · `no_paydoc` " · Click to‘lov raqami yo‘q · qo‘lda to‘lanadi" · `fake_mode` " · Click sinov rejimida · qo‘lda to‘lanadi" : "") |
| note: `refund_succeeded` | `provider`, `amount` | `` `${P} · ${SOM(amount)}` `` |
| note: `refund_failed` | `provider`, `state` | `unknown`: `` `${P} · holat noma’lum, tekshirish kerak` ``; else `` `${P} · xato, administrator tekshiruvi kerak` `` |
| `OrderCashbackEntry` | `cashback[]` (+ `cashback_hold`) | `{kind, amount, label}`. label: debit "Ishlatildi"; credit `` `${PCT(percent)}% × ${N(base)}` `` ("1% × 568 000"); reversal "Qarama-qarshi yozuv". If there is no debit entry and `cashback_hold.state` is `active`, prepend `{kind: "debit", amount: -hold.amount, label: "Ishlatildi"}`. |
| Refund state (new UI line) | `refunds[0].state`, `refunds[0].provider`, `refunds[0].manual_reason` | requested "Qaytarish so‘raldi" · submitting "Qaytarilmoqda" · succeeded "Qaytarildi" · failed "Qaytarib bo‘lmadi" · manual_required by reason and provider: `manual_reason` `payme` (or, without a reason, provider `payme`) "Payme kabinetida qaytarish kerak"; `no_paydoc` / `fake_mode` (or provider `click`) "Mijozga qo‘lda to‘lang, so‘ng «Qaytarildi» deb belgilang" · unknown "Holat noma’lum · tekshiring". Never assume `manual_required` means Payme. The `Qaytarish` column of the payments export (§2.12, `tolovlar`) uses the same line for the payment's newest refund. |
| `cancellation` | `cancellation` | `{reason, note: note ?? undefined}`; labels stay `CANCEL_LABEL` |

### 5.2 Products and catalog

| FE field | API source | Rule |
|---|---|---|
| `Product.category` | `category` | Slug string or null (`CategoryId` becomes `string`, §7) |
| `Product.images[]` | `images[]` | `{id: String(id), src: url, label: `Rasm ${position + 1}`}`; `art` omitted |
| `Product.art` | – | `null` |
| list thumbnail (new) | `main_image_url` | Rendered before `art` |
| `Product.lastChange.by` / `at` | `last_change.actor` / `.at` | `actor?.display_name ?? "Tizim"` / `at` — M3: a null actor is the system (e.g. a product the Billz sync hid or created), never "Administrator"; `A(actor)` keeps its own fallback elsewhere |
| `Product.lastChange.note` | `last_change.kind`, `old_price`, `new_price` | created "mahsulot yaratildi" · price_changed `` `narx ${N(old_price)} → ${N(new_price)}` `` · activated "faollashtirildi" · hidden "yashirildi" · promo_added "aksiyaga qo‘shildi" · promo_removed "aksiyadan olindi" · updated "ma’lumotlar yangilandi" |
| `CatalogStats.lastSyncLabel` | `billz.last_sync_at`, server `now` | null → "hali sinxronlanmagan"; <60 s → "hozirgina"; <60 min → `` `${m} daqiqa oldin` ``; <24 h → `` `${h} soat oldin` ``; else `formatDateTimeShort` |
| Sidebar Billz block | `billz.connected`, label above | "Billz ulangan" / "Billz ulanmagan"; second line `` `Sinxronlangan: ${lastSyncLabel}` `` |
| Category label | `categories/` `name.uz` | Replaces `CATEGORY_LABEL` |

### 5.3 Customers and ledger

| FE field | API source | Rule |
|---|---|---|
| `Customer.totals` | `totals.store`, `totals.online` | As is; lifetime = `totals.lifetime` |
| `Customer.onlineOrders` | `online_orders` | `{count, lastOrderId: last_order_id ?? undefined, lastOrderAt, allCount: all_count}` |
| Card orders link (new) | `online_orders.all_count` | `` `Buyurtmalari (${N(all_count)})` `` → `/admin/orders?customer=${id}`; shown when `all_count > 0` |
| `Customer.groups: string[]` | `groups[].name` | Names only |
| Balance card | `balance` | Server value (no longer the ledger sum) |
| Tier card (`TierInfo`) | `tier` | `{percent: percent && parseFloat(percent), next: next && {percent: parseFloat, remaining, progress}}` |
| `LedgerEntry.source` | `source_type`, `source_ref`, `actor` | order → `` `Buyurtma №${source_ref}` `` ("Buyurtma №10231"); billz_sale → `` `Billz savdo ${source_ref}` `` ("Billz savdo S-88213"); admin → `A(actor)` ("Administrator") |
| `LedgerEntry.note` | `kind`, `source_type`, `base`, `percent`, `promo_excluded`, `reason` | credit + billz_sale: `` `Chek ${N(base)} so‘m · ${PCT(percent)}%` `` ("Chek 231 000 so‘m · 1%"); credit + order: `` `${N(base)} so‘m · ${PCT(percent)}%` `` + (promo_excluded ? " · aksiyasiz qism" : "") ("314 000 so‘m · 1% · aksiyasiz qism"); debit "To‘lovning bir qismi"; reversal "Buyurtma bekor qilindi · cashback qaytarildi"; adjustment `reason` |
| Ledger BALANS column | `balance_after` | Replaces `withRunningBalance` |
| `CustomerPurchase.document` | `source`, `order_id`, `billz_sale_ref` | online → `` `№${order_id}` ``; store_qr → `billz_sale_ref` |
| `CustomerPurchase.promoAmount` | `promo_amount` | As is |
| `Customer.device.appVersion` | `device.app_version` | `device ?? undefined`; `platform` labelled per §5.8 |
| `Customer.devices[]` (new) | `devices[]` | `{id: String(id), platform, model, os, appVersion, pushEnabled, isActive, lastActiveAt, createdAt}`; an inactive row is shown muted ("Faol emas") |
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
| `sent` / `failed` / `attempted` / `delivered` / `state` | same | As is; `sent` / `failed` are live tallies while `sending` (`attempted = sent + failed`); `delivered` stays null in M1 |
| `revision`, `testedRevision`, `approvedRevision`, `dispatchAfter`, `approvedAt/By`, `testConfirmedAt`, `stopRequestedAt`, `stoppedAt`, `failedAt`, `lastError`, `tests[]`, `authorId` | same | As is; the UI derives the allowed actions from the state and `me.is_superuser` / `author.id` (approve needs `testedRevision === revision`); `dispatchAfter` drives the cancel countdown |
| `approvalDelayMinutes`, `testDevices` | meta `approval_delay_minutes`, `notifications/test-devices/` | the latter superuser only |
| `totalDevices`, `segments` | meta `total_devices`, `segments` | `{id: String(id), name, size}` |
| `linkOptions` | meta `link_options` | `[{kind: "none"}, ...map(o => ({kind: "product", productId: String(o.product_id), label: o.label}))]` |
| `StatusTemplate` | meta `templates[]` | `text = texts.uz`; `translations[l] = texts[l].trim() !== ""` |
| `defaultSchedule` | – | Mapper: next Tashkent day 10:00 relative to the server `now` |
| `draft` | – | `null` (no draft persistence in M1) |
| `history` | `notifications/` page 1 (20) | `results` |

### 5.6 Appointments

| FE field | API source | Rule |
|---|---|---|
| `Specialist.role` | `role` | dermatologist → "Dermatolog"; cosmetologist → "Kosmetolog" (§5.8 SpecialistRole) |
| Day-board column of a deactivated specialist (FND-1) | `specialists[].is_active` | `false` → render the column with a "Nofaol" mark; its appointments stay actionable |
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
| `attention.*` | `attention.*` | Camel-case; `refundsNeedingAction` is new; `btsPollingFailing` (M2) is new: when true the attention block shows "BTS kuzatuvi ishlamayapti · buyurtma holatlari yangilanmayapti"; `cashbackPolicyMissing` (gap CB-9.2.4) is new: when true it shows "Cashback siyosati yo‘q · xaridlarga cashback yozilmayapti" linking to the cashback settings; "Eng yaqini: …" stays `formatWeekdayShortDateTime` |
| Recent orders | `orders/?page_size=6` | `results` |
| Sidebar badges | `nav-counts/` | orders = `orders_processing`; appointments = `appointments_pending`; «Muammolar» (Yandex Delivery issue queue) = `delivery_issues_open` |
| Fulfilment tiles (Yandex Delivery, `features/admin/dashboard/fulfilment-tiles.ts`) | `fulfilment.*` | «To‘langan · tayyorlash» → `orders/?status=processing`; «Kuryerga berish kerak» → `status=processing&provider=yandex`; «Yetkazilmoqda» → `status=handed_to_courier`; «Qaytarilmoqda» → `provider=yandex&stage=returning`; «Bugun yakunlangan» → `status=delivered`; «Harakat kerak» → `attention=open`; «Shoshilinch» → `attention=urgent` (all with `period=all`) |
| `attention.yandexFailing` (Yandex Delivery) | `attention.yandex_failing` | when true the attention block links to the delivery issue queue |
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
| NotificationState / Kind | draft "Qoralama" · tested "Test tasdiqlangan" · scheduled "Rejalashtirilgan" · sending "Yuborilmoqda" · stopping "To‘xtatilmoqda" · sent "Yuborildi" · partially_sent "Qisman yuborildi" · cancelled "Bekor qilindi" · failed "Yuborilmadi" / promo "Aksiya" · general "Umumiy" |
| SpecialistRole / ServiceCategory (§2.17) | dermatologist "Dermatolog" · cosmetologist "Kosmetolog" / dermatology "Dermatologiya" · cosmetology "Kosmetologiya" |
| `is_active` of specialists and services (§2.17) | true "Faol" · false "Nofaol" |
| Booleans in exports | "Ha" / "Yo‘q" |
| Staff (§2.16; UI only, no export) | `is_manager` true "Menejer" · false "Xodim" / `is_active` true "Faol" · false "Nofaol" |
| Device platform / OrderChannel (§2.6, orders; UI only) | ios "iOS" · android "Android" · web "Veb-sayt" (B-W1) |

---

## 6. Cross-references: `docs/decisions/open-questions.md`
Provisional, configurable rules that shape payloads:
- **Admin cancel rule.** Allowed from `awaiting_payment` and `processing`. From `handed_to_courier` only if the BTS cancel succeeds (status 100); otherwise 409 `not_cancellable`.
  - This contract applies the same synchronous BTS-cancel check to `processing` orders whose BTS shipment exists.
- **Refund pending display.** Cancel never sets `payment.status=refunded`. The payment stays `paid` with a Refund in `requested`/`submitting`/`manual_required`/`unknown` until the provider confirms.
  - Payme is `manual_required`, done in the Payme cabinet and confirmed by inbound CancelTransaction. M2: Payme returns the money to the payer only through that successful CancelTransaction; while Payme is fake under the production settings every callback is refused, so a cabinet refund cannot complete and the money stays with Payme — switching a payment provider from live to fake is therefore a runbook step taken only without open money, and what a provider switched to fake still holds is reconciled by hand in its cabinet (customer contract §2.13.0, §6.14).
  - A Click refund is `manual_required` too when its payment has no `click_paydoc_id` or the Click port is fake under the production settings (M2, customer contract §2.13.0): staff pay the customer outside Click and close it with `…/resolve/` (§7 item 18).
  - Click `unknown` is never auto-retried.
- **Dashboard revenue.** Σ paid totals by payment confirmation time, net of refunds; orders = created in the bucket. Refunds are attributed to the payment's bucket.
- **Rate basis / cashback-paid earning.** `before_purchase`; the cashback-paid part does not earn. Both are policy fields (`basis`, `earn_on_cashback_paid`).
- **Seed tiers.** 0–20M 1%, 20–30M 2%, 30–50M 3%; open top. The install's first policy (migration `loyalty.0006`, gap CB-9.2.4) uses them as `[0, 20M)` 1%, `[20M, 30M)` 2%, `[30M, ∞)` 3% from 2026-06-01 (author system); open question L3.
- **Spendable timing.** Credits are spendable immediately (`LOYALTY_PENDING_DAYS=0`).
- **Refund larger than the balance.** The reversal is still posted; the balance may go negative and is flagged. `available` can be negative.
- **Reservation length.** 15 min (`settings.reservation_minutes`). Pickup completion reuses `delivered`.
- **Price authority.** Admin `price`; `billz_price` is stored for reference (`PRICE_SOURCE`). M3 (decision D1, provisional): the Billz sale of an online order records each line at the `unit_price` the customer was charged and is paid only when Billz's bill equals Σ `unit_price` × `quantity` exactly; otherwise the push stops for staff (dead-lettered with the reason).
- **Customer delete and re-register.** Phone is unique. Delete scrubs the name, image and address (`name: ""`). Re-registration reactivates the same id, so ledger and purchases persist.
- **Production media storage.** Open; URLs are always absolute from `PUBLIC_MEDIA_BASE_URL`.
- **Staff management (GST-1..GST-6).** One role plus the manager flag: only managers manage staff; at least one active manager remains; a manager never deactivates, demotes or resets the password of their own account through the API; temporary passwords are shown once (sealed for the idempotent replay) with no forced change at the first sign-in; operator commands exist for when no manager can sign in.
- **Customer devices on the card (GST-7).** At most 20 installations, active first; never a token, key or installation id.
- **Specialists and services (FND-1 / FND-7, decisions PS-1 … PS-13).** Catalogue and schedule changes never alter booked appointments (`specialists/{id}/conflicts/` lists the ones left outside the schedule); deactivating a specialist with appointments ahead needs `force`; services and clinics can be archived, and new specialists start inactive.

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
   - `OrderPaymentCard` shows the refund state line (§5.1), chosen by state **and provider** (a `manual_required` refund can be a Click one in M2).
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
    - Approval workflow (§2.8): the composer saves a **draft** («Qoralama saqlash»; the "send now" dialog is gone, the schedule is the requested time) and edits with `expected_revision`; history rows show the title **and body** and one of nine state badges; the campaign panel shows the superuser controls by state («Test yuborish» with the device picker, «Testni tasdiqlash» per delivered test, «Yuborishni tasdiqlash», «Yuborishni bekor qilish» with the countdown to `dispatch_after`, «Qoralamaga qaytarish», «Favqulodda to‘xtatish»); a test-devices card registers devices by phone + label. Non-superusers see none of the privileged controls; 403 / 409 answers refresh the row.
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
    - Refund operations for the Payments screen (M1c, consumed by F3): `POST payments/{id}/refunds/{refund_id}/retry/` (from `failed`) and `…/resolve/` `{state: succeeded|failed}` (from `unknown` / `manual_required`, audited `refund.state_changed` with the actor). M2 (customer contract §2.13.0): `…/resolve/ {state: succeeded, manual_payout?: bool}` also closes a `failed` **Click** refund (Click refuses reversals of a previous month, of offline/Humo cards, or when UZCARD declines). The server first asks Click for the payment's status (a fresh check through the port, never under a row lock): `reversed` → closed `succeeded` (Click returned the money; do not pay out); still `paid` → 409 **`click_not_reversed`** (M3; `invalid_transition` before) "Click to‘lovni qaytarmagan. Avval mijozga qo‘lda to‘lang, so‘ng «qo‘lda to‘landi» belgisi bilan yakunlang yoki qaytarishni qayta urinib ko‘ring." unless the body carries `manual_payout: true` (staff paid the customer outside Click; then closed `succeeded`, audited with `manual_payout: true`); Click unreachable → 503 `provider_unavailable`; Click fake under the production settings → 409 `click_fake_mode` (feat/admin-ux; `invalid_transition` before) "Click sinov rejimida: to‘lov holatini tekshirib bo‘lmaydi. Click ulanganidan keyin yakunlang."; a payment without a Click payment id is **not checked** (Click cannot be asked about it) and needs `manual_payout: true` — without it 409 `click_no_paydoc` (feat/admin-ux; `conflict` before) "Bu to‘lovda Click to‘lov raqami yo‘q, shuning uchun Click’dan holatini tekshirib bo‘lmaydi. Mijozga qo‘lda to‘lang, so‘ng «qo‘lda to‘landi» belgisi bilan yakunlang." (M3: never the "Click to‘lovni qaytarmagan" answer, since Click was not asked). The UI therefore offers "Yakunlash" (no flag) first and, after the 409 `click_not_reversed` (or this `click_no_paydoc`), "Qo‘lda to‘landi" (`manual_payout: true`) once the payout is done. The close is allowed only while it is the payment's newest refund, the payment is still `paid` and no other refund of the payment is open or succeeded (otherwise 409 `invalid_transition` "Bu qaytarishni yakunlab bo‘lmaydi.", so a refund retried with `…/retry/` cannot be paid out twice); M3: this guard runs **before** the Click status check (a refund that can no longer be closed is refused without asking Click) and again under the row locks. `manual_payout` is ignored for `unknown` / `manual_required` refunds, whose resolve is unchanged. While Click is fake under the production settings, `…/retry/` of a `failed` Click refund also answers 409 `click_fake_mode` "Click sinov rejimida: to‘lov holatini tekshirib bo‘lmaydi. Click ulanganidan keyin yakunlang." (nothing written), so a retried refund can never skip the status check.
    - The logout action posts `{refresh: <gd_admin_refresh cookie>, scope: "current"}` and clears both cookies whatever the response (a 401 `SESSION_EXPIRED` counts as logged out). Settings (A1) can offer "others" and "all".
    - The login page, `auth.actions.ts`, cookies and `proxy.ts` follow the spec's "Auth" section.
19. **Settings: contacts and legal texts (gap closure, settings package; moved to the ops console 2026-11-07).**
    - The store settings, contacts and legal-texts cards and «E’lon qilish» are no longer a Next screen: `settings/` and `settings/terms/publish/` are retired and the same three cards live on the ops console page `/django-admin/settings/` (§2.11). The Next settings screen drops those cards with their data-layer calls, mappers and fixtures, keeps the staff account part and points staff to the console.
    - Dashboard attention: `cashbackPolicyMissing` (§5.7).
20. **Staff screen "Xodimlar" (gap closure FND-4, §2.16).**
    - Shown in the sidebar only when `Me.is_manager`; the audit-log actor picker keeps reading `GET staff/` for everyone (it uses `id` and `display_name` only).
    - The table reads `GET staff/` (`is_active` tab "Faol" / "Nofaol", `search`) with the manager's columns; "Yangi xodim" posts `POST staff/` with one key per dialog; the answer's `temporary_password` is shown once in a dialog with a copy button and the hint "Parolni xodimga shaxsan bering; u «Parolni o‘zgartirish» orqali almashtiradi." (never stored client-side).
    - Row actions with confirmation dialogs: edit name / manager role (`PATCH`), "Faolsizlantirish" (`deactivate/`, I), "Qayta faollashtirish" (`reactivate/`), "Parolni tiklash" (`reset-password/`, I; the same once-only password dialog). Hide the role, deactivate and reset actions on the caller's own row (the server answers 409 `conflict` anyway); show the 409 message as the toast.
21. **Customer card devices and orders (gap closure ADM-8.1, §2.6).** A "Qurilmalar" block lists `devices` (platform label §5.8, model, OS, app version, push on/off, last activity; inactive rows muted); the card links "Buyurtmalari (N)" (`online_orders.all_count`) to `/admin/orders?customer={id}`, so unpaid, expired and cancelled orders are reachable from the card.
22. **Gap closure — catalog package (`feat/gap-catalog`; open questions GC-1–GC-8).**
    - Search: nothing to change in the UI; every name `search` now folds the six encodings of the Uzbek o‘/g‘ mark and `ё`/`е` on both sides (§1.4).
    - Categories (gap ADM-5.1): `CategoryDialog` gains an image picker with a preview and the limits of §2.3 ("JPEG, PNG yoki WebP, 5 MB gacha"); a picked file is sent as multipart (`slug`, `name.uz`, `name.ru`, `name.en`, `is_active`, `image`) to `POST categories/` / `PATCH categories/{slug}/`, a "Rasmni olib tashlash" action sends `{"remove_image": true}`; the table shows `image_url` as a thumbnail. Map `image` errors to the picker.
    - Banners (gap X-BANNER-URL): `BANNER_LINK_KINDS` gains `url`; `BannerDialog` offers "Havola (URL)" with a text input sent as `link.url`; the domain `BannerLink` gains `{kind: "url", url}`; map `link.url` errors to that input.
    - Notifications (gap ADM-10.1): `NotificationLink` gains `{kind: "url", url}`; the "Bosilganda ochiladigan sahifa" picker offers a URL input next to the products (`link_options` unchanged) and sends `link.kind = "url"`, `link.url`; history rows render `link.url`.
