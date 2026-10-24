# Glow Derma backend foundation — design spec

> Approved by the user on 2026-10-24 (plan mode). Source of truth for M0–M3. Changes go through Claude (lead) and are recorded in `docs/decisions/`.


## Context

**Repos.** `C:\Users\vbaho\Documents\GlowDerma` holds four sibling git repos (`GlowDermatology/*`). The parent folder itself is not a git repo.

| Repo | State |
|---|---|
| `frontend` | Next.js 16.4.3 landing + admin panel on deterministic mock data (PR #1 merged) |
| `backend`, `infra`, `mobile` | Empty, no commits |

**How the admin data layer is shaped.** It was built to mirror a DRF API:
- `src/data/*.ts` holds server-only queries; each one's JSDoc names its endpoint.
- `*.actions.ts` holds mutations. Today they are plain modules running in the browser, not Server Actions.
- `Page<T>` and `ActionResult<T>` are defined in `src/data/types.ts`.
- `src/domain/*.ts` holds unit-tested rules.

**Sources of truth.**
- Foundation PDF: 9 modules, data ownership, entities, and the "settle now" foundations.
- Backend PDF: auth/devices, tables, Billz, cart/reservations, Click/Payme, BTS, statuses, cashback/QR, FCM, admin API, jobs, security, acceptance criteria.
- Admin-panel PDF.
- Design canvas: the Backend-Architecture, Backend-OrderLifecycle and Backend-Cashback boards, 14 mobile screens and 9 admin screens.

**Supporting research** (scratchpad `reports/`):
- Frontend contract map, Next 16 wiring guide, mobile-screen API derivation, appointments domain.
- Pinned stack versions and provider protocols for Billz (public API), BTS (public docs), Click, Payme, Eskiz and FCM.
- A three-lens adversarial critique. Its fixes are folded into this plan.

**Goal.** Prove a safe two-agent workflow:
- **Claude** is lead: spec, API contract, reviews and the Next.js wiring.
- **Codex** implements the backend through the codex-bridge MCP.
- The result is a backend foundation where unconfirmed business rules are configurable, provisional and recorded, never hard-coded.

### Decisions taken with the user
| Topic | Decision |
|---|---|
| Codex access | Bridge policy grants `GlowDerma`; verified `authorized=true` and a read-only round trip on 2026-10-24. Codex sandbox: workspace-write on the whole GlowDerma folder, network on, **no Docker access**, host uv 0.11.32, default `python` 3.15 (use `py -3.14`). |
| First milestone | **Admin-first vertical:** scaffold, all cross-module models, staff auth, and every admin API the Next.js panel renders, wired end to end. Customer/mobile API in M2; live providers in M3. |
| Git | Branch per slice, **stacked**: each PR targets the previous slice branch and is retargeted to `main` when that one merges. PRs go to the user for review. The backend gets one bootstrap commit on `main`. |
| Deployment | **`infra` is untouched** (future GitOps). Backend is self-contained: one `Dockerfile` (a single image; roles api/worker/beat) plus `compose.yaml`. No frontend Dockerfile yet. |

## Step 0 — Prerequisites (gate before any Codex work)

**User actions**
1. Quit Claude Desktop from the tray and restart it, then reopen this task.
2. In Codex Desktop, open a regular project task and send it one turn, so the native relay listens.
3. Start Docker Desktop.

**Claude verifies**
1. **Bridge:** `codex_bridge_status` shows a new pid, `native relay: available`, and `authorized=true` for `GlowDerma`.
   - If it is still not authorized, show the user the one-line `CODEX_BRIDGE_ALLOWED_ROOTS` edit in `~/.claude.json`. Claude never edits that file itself.
2. **Docker:** `docker info` succeeds.
3. **Host constraints** go into AGENTS.md:
   - Host PostgreSQL 17 already listens on `:5432`, so compose publishes Postgres as `5433:5432` and Redis as `6380:6379`.
   - Host uv 0.11.32 generates `uv.lock`; the image (uv 0.13.5) runs `uv sync --locked` (lock format unchanged between them).
   - `core.autocrlf=true` is set, so the repo gets a `.gitattributes`.

**Bootstrap commit on backend `main`** (Claude; pushed)
- `README.md`, `AGENTS.md` (commands, conventions, the dev loop, the scope rules below).
- `.gitignore`, `.gitattributes` (`* text=auto eol=lf`, `*.sh eol=lf`, binaries).
- `docs/specs/2026-10-24-backend-foundation-design.md`: this plan, formalized per the brainstorming skill.
- `docs/api/admin-api-v1.md`: the full M1 contract, including the display-field mapping table.
- `docs/decisions/open-questions.md`.
- `docs/plans/`: task-level M0 and M1-models plans produced with `superpowers:writing-plans`.

**Codex round trip** (no file changes)
- Codex reads AGENTS.md and the M0 plan, then replies with:
  - its execution plan;
  - its sandbox mode and writable roots;
  - network access;
  - whether `git -C backend commit` works;
  - whether `docker info` works inside its sandbox.
- **Dev loop (Docker is unreachable from Codex):** `uv run` on Python 3.14 against the dev PostgreSQL 18 on `127.0.0.1:5433` (test DB `test_glowderma`) and Redis on `127.0.0.1:6380`, eager Celery and LocMem cache in `test.py`. Codex's shell is PowerShell 5.1.

## Team protocol (Claude ↔ Codex)

**Ownership**

| Owner | Writes | Never touches |
|---|---|---|
| Codex | Code in `backend/` | `frontend/`, `infra/`, `mobile/` |
| Claude | `frontend/`; in `backend/`, only `docs/`, `AGENTS.md`, `README.md`, `.gitignore`, `.gitattributes` | `infra/` |

- Claude writes into `backend/` only while no Codex task is running.
- Claude's frontend work happens in a worktree outside the Codex root: `C:\Users\vbaho\Documents\GlowDerma-wt\frontend-api-wiring`, branch `feat/api-wiring-*`.

**Codex tasks**
- **Threads and calls**
  - One Codex conversation per slice, via `delegate_to_codex` / `start_codex_thread` with a fresh `requestId`.
  - `cwd` is `GlowDerma`. Every git command is `git -C backend …`. Codex never pushes.
  - Fixes for slice n go to thread n with `send_to_codex_thread`; observe with `wait_codex_reply`. Never resend.
- **Brief format:**
  - Header `[From Claude · GlowDerma · <purpose>]`.
  - Sections: Goal, Context (paths + line refs), Task, Scope, Constraints, Done when (pytest/ruff/mypy/lint-imports green in the dev loop; commit SHA reported), Reply format.
  - The brief says: "if the current state differs, stop and report".
- **Pacing:** a slice has landed when Codex replies with a SHA and Claude's gate on that SHA is green. Codex starts slice n+1 (stacked on n) only after that. Claude wires slice n in parallel.

**Review gate per slice**
- **0. Containment check**
  - Snapshot `git status --porcelain` and HEAD of frontend, infra and mobile, plus a listing of the GlowDerma root, before and after each Codex turn.
  - `git -C backend diff --name-only <base>..<sha>` must stay inside `backend/`.
  - Any violation stops the gate and is reported to the user. No automatic revert.
- **1. Verify** in a separate review worktree (`GlowDerma-wt\backend-review-<slice>` at `<sha>`) with `docker compose -p gd-<slice>`:
  - pytest, ruff, mypy, lint-imports;
  - `spectacular --validate --fail-on-warn`, diffed against `openapi/schema.yaml`;
  - `makemigrations --check`;
  - `check --deploy`.
- **2.–5. Reviews:**
  - spec-compliance review subagent;
  - code-quality review subagent;
  - `anthropic-skills:django-production-review`;
  - `/security-review`.
- **6. Fix loop:** findings go back to the thread until green. Then Claude pushes the branch and opens the PR.
- Frontend tasks run through `superpowers:subagent-driven-development`: a fresh subagent per task, with spec and quality review.

## Backend architecture

### Stack (pinned 2026-10-24)
| Area | Pins |
|---|---|
| Runtime | Python 3.14 (`python:3.14-slim-trixie`); Django **6.1.2**; DRF **3.18.3**; drf-spectacular 0.30.0 + sidecar |
| Database | psycopg[binary,pool] 3.3.6 using Django's pool |
| Server | gunicorn 26.2 gthread; whitenoise |
| Jobs | **Celery 5.6.3** + Redis broker with a static `beat_schedule`. Not django-celery-beat (it requires Django<6.1). Not `django.tasks` (no worker). redis-py pinned **6.4.0** (kombu bound). |
| Libraries | PyJWT 2.15, argon2-cffi, django-filter 26.2, Pillow 13, openpyxl, httpx 0.28.1, tenacity, firebase-admin 7.7 (M2) |
| Tooling | uv 0.13.5, ruff, mypy 2.3.1 + django-stubs/drf-stubs, pytest 9 + pytest-django + factory-boy, import-linter |
| Images | `postgres:18-trixie` (volume at `/var/lib/postgresql`); `redis:8-alpine` (note the AGPL licensing; Valkey is the alternative) |

- **Not used:** simplejwt and knox (unverified on Django 6.1 / DRF 3.18). We write a small PyJWT layer instead.
- **Not used:** CORS. Admin calls are server-to-server; mobile is not a browser.
- **Caches:** `CACHES` uses Redis via Django's `RedisCache`, also for throttles. LocMem is used only in tests.

### Layout (modular monolith: one DB, one release)
```
backend/  pyproject.toml uv.lock .python-version Dockerfile .dockerignore compose.yaml .env.example manage.py
          AGENTS.md README.md .gitattributes .github/workflows/ci.yml openapi/schema.yaml
          docs/{specs,api,decisions,plans}/
  config/settings/{base,local,test,production}.py  config/{urls,wsgi,asgi,celery}.py
  glowderma/
    core/          timestamps base, Money (int UZS), i18n text fields, error envelope + exception handler, pagination,
                   IdempotencyRecord, AuditEntry, OutboxMessage + dispatcher, append-only model mixin, export mixin
                   (csv/xlsx, formula-safe), client-IP + internal-network middleware, Tashkent time utils, permissions
    accounts/      StaffUser (AUTH_USER_MODEL), Customer (phone unique, soft delete + reactivation), AuthSession,
                   Device, Address, SmsCode (M2); token service; staff auth API
    catalog/       Category (slug), Product (uz/ru/en texts, billz refs), ProductImage, ProductStock (Billz-owned), Banner
    commerce/      Cart/CartItem, Order (ids from 10001), OrderItem (snapshots incl. image_url), OrderEvent (typed,
                   append-only), StockReservation, state machine
    payments/      Payment, Refund (state machine), provider registry
    loyalty/       CashbackPolicy (append-only versions) + CashbackTier, LoyaltyAccount (1:1 Customer), LedgerEntry
                   (append-only, balance_after), CashbackHold, Purchase/PurchaseItem, engine, QR tokens (M2)
    delivery/      Shipment (+events), BTS status mapping
    professionals/ Location, Professional, Service, ProfessionalService (price, duration), WorkingHours, BlockedPeriod
    appointments/  Appointment (price snapshot, state machine), AppointmentEvent
    notifications/ Campaign, Recipient, InboxNotification, StatusTemplate (uz/ru/en), CustomerSegment (typed rules)
    integrations/  ports + FAKE adapters (billz, bts, click, payme, eskiz, fcm); WebhookInbox; INTEGRATIONS_MODE
    site_config/   typed singleton SiteSettings
    reporting/     dashboard / nav-count / attention selectors
```

**Module rules**
- Each module has `models.py`, `services.py` (transactional writes), `selectors.py` (reads), `api/admin/`, `api/customer/`, `tasks.py` and `tests/`.
- Cross-module access goes only through the other module's services or selectors. Example: Commerce asks Loyalty to hold or capture cashback; it never writes a ledger row.
- FKs use string references. import-linter enforces these rules in CI.
- **Django admin** at `/django-admin/`:
  - Mounted only when `DJANGO_ADMIN_ENABLED=1`, which is off in production.
  - Same internal-network gate as the admin API; login throttled.
  - Every ModelAdmin is **read-only by default**.
  - Editable: Location, Professional, Service, ProfessionalService, WorkingHours, BlockedPeriod. Their `save_model` and `delete_model` call services, which write AuditEntry.
  - Money, auth and integration models are never editable there.

### Financial integrity and the "settle now" foundations
- **IDs, money, time, snapshots**
  - BigAutoField primary keys. The order id is the order number; sequences are reset after any seed that inserts explicit ids.
  - Money is `BigIntegerField` so'm. Payme tiyin (×100) exists only inside its adapter.
  - `USE_TZ` with UTC storage; the API emits ISO 8601 with `+05:00`; period filters use Tashkent days.
  - Snapshots: order items keep name, price, promo flag and image; appointments keep price; ledger entries keep the policy version.
- **Percent math**
  - `percent` is `DecimalField(5,2)`; more than 2 decimal places is a field error.
  - Credit = `(eligible × basis_points) // 10_000` in integer arithmetic. No floats.
  - The frontend preview uses the same formula.
  - Extra test vectors 0.57%, 2.3% and 1.5% are added to the backend and to `cashback.test.ts`.
- **Append-only records:** LedgerEntry, AuditEntry, OrderEvent, CashbackPolicy versions and WebhookInbox.
  - `save()` refuses updates; the QuerySet's `update`/`delete`/`bulk_update` raise.
  - FKs to them use `on_delete=PROTECT`.
  - A Postgres `BEFORE UPDATE OR DELETE` trigger raises. Tests assert that raw SQL is rejected.
- **Balance**
  - `LoyaltyAccount.balance` changes only in `loyalty.services.post_entry()`, using `F()` under `select_for_update`, in the same transaction as the entry. Each entry stores `balance_after`.
  - available = balance − Σ active holds. It is checked for subtract adjustments and hold creation.
  - A nightly reconciliation asserts balance == Σ entries.
- **Locks and uniqueness**
  - Lock order is always Order → Payment → LoyaltyAccount.
  - Unique constraints: (provider, provider_txn_id); `Purchase.billz_sale_id`; `Purchase.order` (one-to-one); ledger `reverses_id`; WebhookInbox (provider, event_key).
  - In M2, a partial unique index allows **one active Payment per order across providers**. Captures on orders that are no longer `awaiting_payment` are refused. A capture that still lands auto-creates a Refund and alerts staff.
- **Idempotency**
  - `IdempotencyRecord` is keyed on (audience, actor, method, resolved path, key) and stores a SHA-256 of the body and the response.
  - It is written in the same transaction as the effect, and kept for at least 24 h.
  - Same key and same body replays the stored response. Same key with a different body returns 422 `idempotency_key_reused`. A concurrent duplicate returns 409 `request_in_progress`.
  - Required on cashback adjustments, cancel, transition, retry-shipment, policy versions, notification send, and later checkout and booking.
- **Outbox (at-least-once, so handlers must be idempotent)**
  - Rows are written in the same transaction as the effect.
  - The dispatcher claims rows with `select_for_update(skip_locked=True)`, sets `in_flight` with a lease, and retries with backoff.
  - **Refund** state machine: `requested → submitting → succeeded | failed | manual_required | unknown`.
    - Click: status check before any reversal retry. `unknown` is never retried automatically; it alerts staff.
    - Payme: `manual_required`, done by staff in the Payme cabinet and confirmed by Payme's inbound CancelTransaction.
  - **Billz sale:** the Billz order UUID is persisted onto the online Purchase right after it is created; pushed sales use `without_cashback`.
  - **BTS:** look up `clientId` before re-creating a shipment. `retry-shipment` returns 409 while a BTS message is pending or in flight.
  - Tests simulate "the provider succeeded, then the worker crashed before ack".
- **Data ownership:** Billz owns stock and the Billz name (read-only everywhere). Glow Derma owns texts, images, visibility, promo flag and, provisionally, price.

### Auth and request security
- **DRF defaults:** `DEFAULT_AUTHENTICATION_CLASSES=[]`, `DEFAULT_PERMISSION_CLASSES=[DenyAll]`.
  - Base views per audience: `AdminAPIView` uses `StaffJWTAuthentication` + `IsActiveStaff` (aud=`staff`); customer views are the mirror image; webhook and POS views have their own auth.
  - A test walks the URL resolver: every admin route must reject anonymous, Basic, Django-session and customer-token requests.
- **Sessions and tokens:** an `AuthSession` row holds audience, subject, device, a SHA-256 of the 256-bit refresh token (unique index), `absolute_expires_at`, `revoked_at` and the revoke reason.
  - Access JWT: HS256 signed with dedicated `JWT_SIGNING_KEYS` (never `SECRET_KEY`), `kid` header, `iss`.
  - Decoding pins the algorithm, audience and issuer and requires `sub, sid, aud, iat, exp, jti`.
  - Active-`sid` cache entry `auth:sid:{sid}`: TTL ≤ 60 s, filled with `cache.add`; every revoke writes a `revoked` tombstone on commit (never a plain delete).
  - A revoked session returns 401 with `SESSION_REPLACED` or `SESSION_EXPIRED`.
  - Inactive staff and deleted customers are rejected.
  - Password change or deactivation revokes the user's other sessions.
- **Staff (M0)**
  - **Email + password** (no usernames; email case-insensitive unique). Argon2, checked through `authenticate()`, which hashes even for unknown emails.
  - **M0b — staff social sign-in (Google, Facebook):** allow-list only — never creates staff. Next runs the redirect with state + PKCE (+ nonce for Google) in an HttpOnly `Path=/admin/auth` cookie; Django exchanges the code with its client secret, verifies the ID token (Google: JWKS, iss, aud, exp, nonce, `email_verified`; optional Workspace domain) or the Facebook token (`debug_token` app id, `appsecret_proof`), then matches an existing `SocialIdentity`. Identities are linked only by a staff member already signed in with email + password (`POST auth/oauth/{provider}/link/`); optional Google-only auto-link (`STAFF_GOOGLE_AUTOLINK`, default off) only when Google is authoritative for the address (`@gmail.com`, or `hd` equals the email domain). Facebook never auto-links. Unknown identities get one uniform 401 `social_account_not_allowed`.
  - Access 15 min; refresh absolute 12 h, **non-rotating** (avoids races between Next prefetches).
  - Login is throttled per normalized email (with backoff) and per IP; success, failure, logout and password change are audited.
  - `createstaff` creates non-superuser staff; superuser requires an explicit flag.
- **Customer (M2):** the PDF flow.
  - OTP: stored as an HMAC, compared with `compare_digest`; 120 s TTL; 60 s resend; 5 attempts.
  - Phone must match `^\+998\d{9}$` server-side; throttled per phone and per IP.
  - `verification_token`: single-use, bound to (phone, purpose), 10 min.
  - Single active device: login revokes other sessions with reason `replaced`.
  - Refresh: 60-day sliding, rotating; reuse after a short grace window revokes the session.
  - Consent stored on Customer: terms version, time, IP.
  - **Customer social login (Google, Facebook):** the app's native SDK returns an ID token / access token → `POST /api/v1/auth/social {provider, token}`. A linked customer gets tokens (single-device rule applies). An unknown identity gets `next_step: verify_phone` plus a short-lived signed `social_ticket`; after send-code/verify-code on a phone, the identity is linked to that phone's customer (created or reactivated). Phone stays the loyalty key; one identity per provider per customer.
- **Client IP and network**
  - The Next API client sends `X-GD-Client-IP` plus an internal shared-secret header. Django honours the IP only if the secret matches (`compare_digest`).
  - Other traffic uses the rightmost untrusted `X-Forwarded-For` hop against `TRUSTED_PROXIES`.
  - `/api/v1/admin/` and `/django-admin/` are limited to `ADMIN_API_ALLOWED_NETS`: open in local, the compose/private network in production.
- **Exports:** formula-safe (`'` prefix on `= + - @ TAB CR`; xlsx text cells typed `s`; phones as text). Every export writes an AuditEntry.

### API conventions (`docs/api/admin-api-v1.md`; enforced by spectacular + tests)
- **Paths**

  | Prefix | Use |
  |---|---|
  | `/api/v1/admin/…` | Staff |
  | `/api/v1/…` | Customer |
  | `/api/v1/webhooks/{click,payme,bts}/` | Provider callbacks |
  | `/api/v1/pos/…` | Cashier QR resolve: per-device hashed API keys, throttled, audited |
  | `/api/v1/health/` | Health check |
  | `/api/v1/schema/`, `/api/v1/docs/` | OpenAPI; staff-only in production |

- **JSON:** snake_case keys. Enum values exactly equal the frontend literals (`awaiting_payment`, …). Integer IDs, except Category, which is addressed by `slug`. Money is integer so'm. Datetimes are ISO +05:00. Percents are decimal strings.
- **Structured data, not display strings.**
  - The API returns typed events, actors, amounts, references and timestamps.
  - Frontend mappers rebuild today's Uzbek strings.
  - The contract doc carries a **display-field mapping table**: every FE display field → the API fields that feed it. It covers OrderEvent `type` + `data` keys, the cashback entry label (percent, base), ledger source and note (`source_type`, `source_ref`, base, percent, promo-excluded), PolicyVersion (tiers + `effective_to`), audience label, `lastChange`, and block kinds.
  - Exports are the exception: they are documents with Uzbek headers and labels.
- **Pagination:** `{count, page, page_size, page_count, results}`.
  - The page is clamped, never a 404.
  - `page_size` allow-lists per endpoint: orders {6,10,25,50}, products {8}, others {20,50}.
- **Errors:** `{code, message, errors:[{field:"texts.uz.name", code, message}], request_id}`.
  - DRF 3.18 nested errors are flattened server-side.
  - M1 messages are Uzbek; ru/en catalogs and gettext arrive with M2.
  - Bad transitions return 409 `invalid_transition` / `not_cancellable`.
- **Media:** `MEDIA_ROOT` sits on a named compose volume shared by api and worker. `/media/` is served by Django in local only. Absolute URLs are built from `PUBLIC_MEDIA_BASE_URL`, never from the request Host. Uploads are re-encoded with Pillow, which strips EXIF and rejects spoofed types. Production storage is an open question.

### M1 admin endpoint inventory (every designed admin screen; `?export=csv|xlsx` mixin on every list)
| Area | Endpoints |
|---|---|
| Auth | `POST admin/auth/{login,refresh,logout,change-password}` · `GET admin/auth/me` |
| Orders | `GET orders/` (status, search, fulfilment, payment, period, date_from/date_to, customer, ordering=±created_at, page_size) · `GET orders/status-counts/` · `GET orders/{id}/` (items with `image_url`, typed history newest-first, payment + refunds, shipment union, cashback entries) · `POST orders/{id}/cancel/ {reason}` · `POST orders/{id}/transition/ {status}` · `POST orders/{id}/retry-shipment/` |
| Products | `GET products/` (search via pg_trgm over uz/ru/en/billz name/billz_id; category=slug\|none; status; promo; out_of_stock; is_new; ordering=default\|name\|-name; items carry `main_image_url`) · `GET products/stats/` (totals, `billz:{connected, last_sync_at}`) · `POST products/` · `GET/PATCH products/{id}/` · `POST products/{id}/images/` (one multipart file ≤5 MiB, jpeg/png/webp, ≤8) · `DELETE products/{id}/images/{img}/` · `PUT products/{id}/images/order/` · `POST integrations/billz/import/` → job, `GET jobs/{id}/` (fake adapter in M1) |
| Catalog refs | `categories/` CRUD + reorder (delete with products → 409) · `banners/` CRUD + reorder. API only in M1. |
| Customers | `GET customers/` (name, phone, registered_at, lifetime, percent, balance, status) · `GET customers/{id}/` (profile, totals by channel, online-order stats, tier + next tier, balance, groups, device, upcoming appointments, **full ledger with `balance_after` and purchases embedded**) · `POST customers/{id}/cashback-adjustments/` |
| Cashback | `GET cashback/policy/` (active, structured version history with tiers, author, created_at, `effective_from/to`, next allowed date) · `POST cashback/policy-versions/`. It rejects: no tiers, overlaps, empty ranges, percent ∉ (0,100] or >2 decimals, `effective_from` ≤ now. Gaps are warnings. At most one future version; a new one supersedes it (audited). A purchase uses the version active at its confirmation time. |
| Notifications | `GET notifications/` (history; `delivered` is null until M2 acknowledgments) · `GET notifications/meta/` (segments + sizes, push-device total, templates, `link_options` = active products' id + uz name) · `POST notifications/` (multipart: kind, title ≤60, body ≤180, image?, link none\|product, audience all\|segment\|`customer{phone}`, scheduled_at?) · `GET/PUT notification-templates/{status}/` · `segments/` (typed rules `min_online_orders` / `min_lifetime_amount` + manual members) |
| Appointments | `GET appointments/?date=` (specialists with the day's hours and typed blocks; appointments with `service_short_name`, `requested_via=app\|admin`) · `GET appointments/pending-summary/` · `POST appointments/{id}/{confirm\|reject\|attend}/` |
| Shell | `GET dashboard/?range=today\|week\|month\|custom&from=&to=` (buckets with ISO start, revenue, orders; previous-period orders; stats; attention) · `GET nav-counts/` · `GET/PATCH settings/` · `GET payments/` · `GET audit-log/` |

- Fields owned by slices that have not landed yet return `0` or `[]`.

### Provisional business rules (configurable; all logged in `docs/decisions/open-questions.md`)
| Question | Provisional default |
|---|---|
| Rate basis for the threshold-crossing purchase | `before_purchase` (policy field) |
| Does the cashback-paid part earn? | no (policy field) |
| Tier bands | seed 0–20M 1%, 20–30M 2%, 30–50M 3%, open top; admin edits |
| When earned cashback becomes spendable | immediately (`LOYALTY_PENDING_DAYS=0`) |
| Refund larger than the balance | reversal posted; negative balance allowed and flagged |
| Admin cancel | allowed from `awaiting_payment` and `processing`. From `handed_to_courier` only if BTS cancel succeeds (status 100); otherwise 409 `not_cancellable`. The payment stays `paid` with a Refund shown until the provider confirms. |
| Reservation length / pickup "delivered" | 15 min (`SiteSettings`) / reuse `delivered` |
| Price authority | admin price; Billz price stored (`PRICE_SOURCE`) |
| Dashboard revenue | Σ paid totals by payment confirmation time, net of refunds; orders = created in the bucket |
| Customer delete / re-register | `phone` unique; delete sets `deleted`, scrubs name/image/address, revokes sessions, removes devices; re-registration **reactivates the same row**; LoyaltyAccount and Purchase FK the Customer |
| Billz-dependent (block M3) | in-store sale discovery after `resolve-qr`; POS redemption; the online-sale echo is linked to the existing Purchase, never re-credited |
| Compliance (gates M3) | Postgres, Redis, media, backups and logs hosted in Uzbekistan; database registration; no PII to foreign SaaS; retention for raw payloads and audit |

## Frontend wiring (Claude, in the `frontend` worktree)
- **Source seam.** Splits `config.ts` into a server-only `src/data/source.ts` and a client-safe module.
  - Each public dispatcher in `src/data/*.ts` (queries and actions) calls `await connection()` before reading `GLOWDERMA_DATA_SOURCE=mock|api`.
  - Mock bodies move to explicit `src/data/mock/*.queries.ts`. They keep `DEMO_NOW` / `DESIGN_DAY`, and `mock-data.test.ts` points at them. A new dispatcher test stubs the env.
  - API mode uses `requestNow()`. `now` is passed from the server to NotificationsView and order detail. `parseDay`/`dayHref` take `today`.
  - The Demo pill becomes an async `<DemoBadge/>` inside Suspense.
  - `proxy.ts` and `verifyStaff` are no-ops in mock mode.
- **API client** (`src/data/api/client.ts`, server-only).
  - Reads `GLOWDERMA_API_URL`; sends bearer from cookie, `X-GD-Client-IP` + internal secret, `Accept-Language: uz`, `no-store` with a timeout. Wrapped in React `cache()`.
  - Typed per-resource mappers produce the existing domain types and display strings. Mapper unit tests reproduce the design strings (order №10248, Malika's ledger).
  - The error mapper turns the envelope into `ActionResult`, camelizing field paths per segment with per-action remaps (e.g. `audience.phone` → `phone`). 404 → `null`.
  - `npm run gen:api` runs `openapi-typescript` over `git -C backend show <sha>:openapi/schema.yaml` into `src/data/api/schema.d.ts`. The file records the SHA and is ignored by Prettier and ESLint.
- **Server Actions** (`"use server"`; IDs + change + client-generated `idempotencyKey`, created once per dialog open and reset on success)

  | Action | Call sites |
  |---|---|
  | `cancelOrder(orderId, reason, key)` | `cancel-order-dialog` |
  | `changeOrderStatus(orderId, status, key)` | `order-detail-view` |
  | `retryShipment(orderId, key)` | `order-detail-view` |
  | `saveProduct(id\|null, values)`, `uploadProductImage(productId, FormData)`, `deleteProductImage`, `reorderProductImages` | `product-editor`, `image-manager` (keeps `Map<uploadId, File>`) |
  | `adjustCashback(customerId, input, key)` | `adjust-cashback-dialog`, `customer-view` |
  | `saveCashbackPolicy(draft, key)` (summary diff by `(from, to, percent)`) | `cashback-editor` |
  | `sendNotification(FormData incl. audience + image, key)` | `notifications-view` |
  | `updateAppointment(id, action, key)` | `appointments-view` |

  - **Product save orchestration:**
    1. Create the product (forced `hidden`) or patch it.
    2. Upload the new files.
    3. Delete removed images.
    4. Save the image order.
    5. Patch `status` to active if requested.
    6. On create, redirect.
  - Each action re-verifies the session and maps the result, then calls `refresh()` or `redirect` as needed.
- **Auth**
  - `src/app/admin/login/page.tsx`: no AdminShell, `useActionState`, `next` read inside a Suspense child, validated as a relative `/admin…` path.
  - `auth.actions.ts`: login (Server Action). Logout is the `/admin/auth/logout` route handler; the sidebar logout button (replacing `NotConnectedButton`) posts to it.
  - **Token model: BFF (user decision 2026-10-24).** Browser JavaScript never sees a token; only the Next server uses them to call Django.
  - Access cookie `gd_admin_access`: HttpOnly, Secure (prod; `__Secure-` prefix in prod), SameSite=Strict, `Path=/admin`, max-age = access TTL.
  - Refresh cookie `gd_admin_refresh`: HttpOnly, Secure, SameSite=Strict, **`Path=/admin/auth`** — sent by the browser only to the auth route handlers (`/admin/auth/refresh`, `/admin/auth/logout`), never to pages, Server Actions or `/admin/login`.
  - Logout is a POST route handler `/admin/auth/logout` (CSRF-checked) with `scope=current|others|all`: `current` revokes this session, `others` revokes every other session of the staff member, `all` revokes every session. It forwards the refresh token to Django and clears the cookies when the current session ends.
  - Refresh flow: `proxy.ts` sees a missing/near-expiry access cookie on a GET → 307 to `/admin/auth/refresh?next=…` (route handler: verifies Origin/`Sec-Fetch-Site`, calls Django refresh, sets a new access cookie, 303 back to a validated `next`). On 401 it clears both cookies and redirects to login. Server Actions that hit an expired access token return `session_expired`; the client calls the refresh route and retries once.
  - **CSRF on everything state-changing:** SameSite=Strict on both cookies; Next's built-in Server Action Origin/Host check with explicit `serverActions.allowedOrigins`; plus a double-submit token — a per-session random `gd_csrf` value (readable cookie, SameSite=Strict, Path=/admin) that every Server Action receives as an argument and every POST route handler as an `X-CSRF-Token` header, compared in constant time against the cookie in `verifyStaff({ csrf })`. Login, logout and refresh route handlers are included. Django's admin API accepts calls only with a Bearer token plus the internal secret from the Next server, so it is not reachable by a browser at all.
  - DAL `src/data/auth.ts`, never read in layouts. Sidebar `<SidebarUser/>` and `<BillzStatus/>` are async components inside Suspense.
  - `src/proxy.ts`, matcher `/admin/:path*`:
    - GET only; `/admin/login` is exempt; non-GET passes through.
    - When the access cookie is missing or expires within 60 s on a GET, it redirects (307) to `/admin/auth/refresh?next=…`; it cannot refresh itself because the refresh cookie (Path=/admin/auth) never reaches it. It also issues the `gd_csrf` cookie when missing.
    - Clears cookies and redirects only on a 401 from refresh. On 429, 5xx or network errors it passes through.
  - The CSV/XLSX export route verifies the session, maps UI params to API params with the shared query mapper, and streams Django's file.
- **UI deltas (small, listed)**
  - The products table and order item thumbnails render the photo URL before `art`.
  - `CategoryId` becomes a slug string: `getCategories()`; schema `z.string()`; categories passed to ProductEditor, ProductsToolbar and ProductsTable; Suspense on `/admin/products/new`.
  - Ledger table uses `balance_after`; customer view uses the server balance.
  - Remove the always-on bell dot, the hard-coded sidebar Billz/user block, and the Billz-import `NotConnected` (now wired).
  - Fix `?stock=out` → `out=1`.
  - Point the customers "Namuna" link at seeded Malika.
- **Config:** `serverActions.bodySizeLimit: '6mb'`. `.env.example` gets `GLOWDERMA_API_URL`, `GLOWDERMA_DATA_SOURCE`, `GLOWDERMA_INTERNAL_SECRET`, `NEXT_SERVER_ACTIONS_ENCRYPTION_KEY`.

## Milestones (stacked slice branches)

**M0b — Staff social sign-in (Codex + Claude, after M0):** `SocialIdentity` flows for Google and Facebook (contract §2.1), fake providers for tests; Next start/callback route handlers and login-page buttons. Needs Google OAuth web client + Facebook app credentials (user provides; fake mode until then).

**M0 — Foundation**
- Codex, branch `feat/m0-foundation`:
  - uv project, settings, a multi-stage non-root Dockerfile (gunicorn / celery worker / beat by command).
  - Compose with healthchecks, ports 5433/6380 and a media volume.
  - `core` (envelope, pagination, idempotency, audit, outbox, append-only mixin, exports, IP/network middleware, time, money).
  - `accounts` staff auth with `createstaff`; health; spectacular + sidecar; Celery.
  - Tooling: ruff, mypy, pytest, import-linter; GitHub Actions CI (backend repo only).
- Claude, in parallel: source seam, API client, error mapper, auth (login/logout/proxy/DAL), Server Action conversion (still mock-backed). Verified against M0 auth.

**M1 — Admin vertical.** Codex threads, in order; Claude wires each slice once it is green.
- **M1-models:** every cross-module model, migration and FK, so later slices add only services, APIs and tests.
  - Customer, Device, Address, LoyaltyAccount, LedgerEntry, Hold, Purchase (order FK nullable).
  - Category, Product, Image, Stock, Banner.
  - Order, Item, Event, Payment, Refund, Shipment, Outbox.
  - Professionals, Service, Appointment.
  - Segment, StatusTemplate, Campaign, SiteSettings.
  - DB triggers; a module-seeder registry (`seed_demo` runs whichever seeders are present and resets sequences).
- **M1a Catalog:** product, category and banner admin API; images and media; Billz fake port and import job; stats; seeder.
- **M1b Customers + loyalty:**
  - Engine ported with integer math and the shared plus new test vectors.
  - Ledger service and holds; policy versions; customer list, card and adjustments.
  - Seeder with Malika Karimova and tiers 1/2/3%.
- **M1c Orders, payments, delivery (admin side):**
  - State machine identical to `domain/order.ts` plus the cancel rule above.
  - Refund state machine with fake Click/Payme ports; BTS fake port; outbox handlers.
  - List, counts, detail, actions and export; seeder with order №10248.
- **M1d Appointments, notifications, shell:**
  - Day schedule and actions.
  - Campaigns, templates and segments, with a Celery batched fake-FCM send.
  - Dashboard, nav-counts, settings, payments list, audit log; seeder with Nodira, Jasur and Dilnoza plus services.

**A1 — Undesigned admin sections (Claude, after M1).** Design them on the Glow Derma canvas with the design system, get user approval, then build on the M1 APIs: Categories, Banners, Customers list + groups, Payments, Settings (with change password), Audit log.

**M2 — Customer (mobile) API (Codex)**
- **Auth:** SMS auth via the Eskiz port, consent, single-device sessions, device registration (FCM `fid`), ru/en message catalogs.
- **Catalog and cart:** public catalog; server cart with `max_reached` / `OUT_OF_STOCK`.
- **Checkout and payment:**
  - `POST delivery/calculate` (BTS tariff port).
  - Checkout with reservation, cashback hold and an expiry job (which first refuses or cancels active provider transactions).
  - `POST orders/{id}/pay`.
  - Click prepare/complete: MD5 over the raw strings, `compare_digest`, service_id binding, amount == payable (else -2), `merchant_prepare_id` binding; error<0 → cancel and -9.
  - Payme JSON-RPC, all methods plus the sandbox scenarios: Basic auth per mode (-32504), amount == payable×100 (-31001), -31008 for a second active transaction or a non-payable order. The IP allow-list is defence in depth only.
- **After payment:** purchase + credit, Billz sale via the outbox, BTS create with automatic retry plus an attention item.
- **Delivery tracking:** BTS webhook (secret token + re-track) and polling.
- **QR:** 128-bit token, Redis stores its HMAC, 60 s, single-use via GETDEL; `POST pos/cashback/resolve-qr`.
- **Account and appointments:** `/me` (+ delete/reactivate), inbox and read state, public settings, specialists/slots/booking.
- **Jobs and limits:** cleanup jobs for SMS codes and sessions; throttles on checkout and pay; FCM pruning.

**M3 — Live integrations**
- Live Billz, BTS, Click, Payme, Eskiz and FCM adapters against sandboxes (Payme `test.paycom.uz`, BTS apitest, Billz API, Eskiz test texts).
- Billz delta/full sync with a 2 rps limiter and single-flight refresh; Billz sale ingestion; POS redemption.
- Reconciliation, monitoring, backups; compliance gate. Infra/GitOps is the user's separate track.

**Phase 2:** AI.

## Verification
- **Backend gate** (per slice SHA, in the review worktree; see the Team protocol): pytest, ruff, mypy, lint-imports, spectacular validate + schema diff, `makemigrations --check`, `check --deploy`.
- **Required backend tests**
  - **Auth:** URL-walk auth matrix; token audience separation; revoked-session codes across gunicorn workers via Redis.
  - **Ledger and money:** append-only (raw SQL refused); balance == Σ entries; concurrent adjustments; idempotency (replay / 422 / 409); percent vectors incl. 0.57/2.3/1.5; policy rules incl. a past `effective_from`.
  - **Orders:** order transition matrix + cancel rule; outbox "crash after provider success" gives one effect.
  - **Other:** export formula injection; pagination clamp; error-envelope paths; customer delete → reactivate keeps balance and purchases.
- **Frontend:** `npm run check` (lint, typecheck, vitest incl. mapper/display-string tests, error mapping, safe `next`, dispatcher env stub; mock tests green); `npm run build` with the env unset, then `next start` with `api` must serve API data on `/admin/cashback`.
- **End to end:** compose stack + `seed_demo`; `next dev` with `GLOWDERMA_DATA_SOURCE=api`; driven via the Playwright MCP:
  - **Access:** unauthenticated `/admin` → login → dashboard (all ranges).
  - **Orders:** filters, tabs and CSV/XLSX export; detail: cancel (refund pending + reversals), transition, retry-shipment (409 while in flight).
  - **Products:** create (hidden → images → active), edit, image upload/reorder/delete, Billz import.
  - **Customers and cashback:** adjustment (balance, `balance_after`, audit entry; double-click gives one entry); policy save (overlap / past date rejected).
  - **Notifications and appointments:** scheduled notification to one phone; appointments confirm/reject/attend.
  - **Session:** refresh after 15 min; logout.
  - Plus `/security-review` on the frontend PR.
