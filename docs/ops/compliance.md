# Personal data and compliance (operator runbook)

Status: M3 operations package, 2026-10-27. Scope: the backend (this repository) and the infrastructure it runs
on. The design spec lists this as the gate of M3 ("Compliance (gates M3)": Postgres, Redis, media, backups and
logs hosted in Uzbekistan; database registration; no personal data to foreign SaaS; retention for raw payloads
and audit). Everything here describes what the code does today; the open questions at the end need an answer
from the product owner (and, where marked, from legal counsel) before go-live.

`docs/ops/deploy.md` and `compose.prod.yaml` are the deployment runbook and stack of branch `feat/ops-deploy`.

> This document is not legal advice. It names the obligations the project brief and the design spec record and
> what the system does about each. Confirm the current legal requirements, the competent authority and its
> procedure with counsel before launch.

## 1. Data inventory

### 1.1 What personal data is stored, and where

| Data | Fields | Store | Notes |
|---|---|---|---|
| Customer profile | phone (unique, the loyalty key), name, profile image, the default delivery address (`address_text`), language, password hash (Argon2) and `password_changed_at`, consent (`terms_version`, `consent_at`, `consent_ip`), `registered_at`, `last_login_at`, `deleted_at`, `reactivated_at`, `history_from`, status | Postgres `accounts_customer`; the image file in the media volume | Deletion scrubs name, image, address text, password, consent and last sign-in; the phone and the other dates stay (§3) |
| Saved addresses | label, address text, BTS region/city codes, comment, **map position (`latitude`, `longitude`)** — a precise location, usually the home | Postgres `accounts_address` | Deleted with the account |
| Devices | Firebase installation id (`fid`), FCM token, platform, model, OS, app version, locale, device-key hash, push opt-in, `last_active_at` | Postgres `accounts_device` | Token and key hash cleared on deletion; the row (model, OS, `fid`) is kept, inactive |
| Sessions | refresh-token hashes (SHA-256), device label, sign-in method, timestamps, revoke reason — customers and staff | Postgres `accounts_authsession`; revoke tombstones in Redis | Revoked or expired customer sessions deleted after 90 days; staff sessions are kept |
| SMS codes | phone, purpose, HMAC of the code, verification-token hash, Eskiz message id, **the requesting IP (`request_ip`)** | Postgres `accounts_smscode`; the code itself only AES-GCM-sealed in Redis for 120 s | Rows deleted after 24 h |
| Social sign-in | provider, provider subject, email at link time, last sign-in, Apple refresh token (sealed with `SECRETBOX_KEY`) | Postgres `accounts_customersocialidentity` (customers), `accounts_socialidentity` (staff) | Deleted with the account; Apple tokens revoked |
| Staff | email, full name, role, password hash, last sign-in; staff ids as the actor of catalog, cashback, refund, schedule and campaign changes | Postgres `accounts_staffuser` (+ `*_by_id` / `actor_id` columns across modules) | |
| Specialists (natural persons, published) | name, role, **photo**, **biography** (uz/ru/en), years of experience, the location they work at; their working hours, date overrides (with a staff note) and blocked periods (`busy`/`off` with a label) — an employee's schedule and absences | Postgres `professionals_professional`, `professionals_workinghours`, `professionals_workinghoursoverride`, `professionals_blockedperiod`; photos in the media volume | Shown in the app to everyone; staff edit and deactivate them; no deletion flow (Q9) |
| Business contacts | location address and phone, support / cosmetologist / store phones | Postgres `professionals_location`, `site_config_sitesettings` | Business data; a phone that is a person's own mobile becomes personal data |
| Carts | the customer's cart: products, quantities, the price at adding, timestamps, the last checkout order | Postgres `commerce_cart`, `commerce_cartitem` | Lines deleted with the account and by the dormant-account reset |
| Orders | recipient name and phone, delivery address and comment, customer note, channel, items, amounts, status history (with staff actors and notes), cancellation note, provider `metadata` | Postgres `commerce_order`, `commerce_orderitem`, `commerce_orderevent`, `commerce_stockreservation` | The recipient snapshot is kept after account deletion (open question Q4) |
| Payments and refunds | amounts, provider transaction ids, provider protocol state (Click `click_paydoc_id`, Payme times), provider error text (scrubbed), the staff member who requested a refund | Postgres `payments_payment`, `payments_refund` | Card data never reaches the backend |
| Shipments | tracking number, BTS ids, statuses, error text, and each BTS status body as received (`delivery_shipmentevent.payload`: whatever BTS sends — it may echo the recipient's name, phone or address) | Postgres `delivery_shipment`, `delivery_shipmentevent` | |
| Loyalty | balance, ledger entries (staff reason for adjustments), holds, purchases with their **items** (what the customer bought, in store too, with prices) and in-store Billz sale ids | Postgres `loyalty_*` | Kept after deletion (decision A1) |
| Appointments | customer, specialist, service, time, customer note, staff note, status history | Postgres `appointments_*` | |
| Customer segments (**profiling**) | segment rules (minimum online orders, minimum lifetime amount) evaluated over a customer's purchases, and manual memberships (`customer`, the staff member who added them, when) | Postgres `notifications_customersegment`, `notifications_segmentmember` | Manual memberships are **kept** after account deletion (the customer row stays); used only to target campaigns (Q10) |
| Notifications | inbox rows (title, body, link, read/delivered times, push state), campaign texts and image, a campaign's single-customer / single-phone audience snapshot (`audience_customer`, `audience_phone`) | Postgres `notifications_inboxnotification`, `notifications_campaign` | Retention not implemented (Q5) |
| Campaign deliveries | one row per customer and **device** a campaign reached: delivery state, error, FCM provider message id and state, sent/delivered times, the inbox row | Postgres `notifications_campaignrecipient` | Kept after deletion; retention not implemented (Q5) |
| Background jobs | the staff member who started a Billz import, its parameters, progress and error text (no customer data) | Postgres `integrations_job` | |
| Raw provider callbacks | Click/Payme/BTS/Billz request bodies (`payload`, `raw_body` ≤ 64 KiB), allow-listed headers, remote IP | Postgres `integrations_webhookinbox` (append-only) | Purged after `WEBHOOK_INBOX_RETENTION_DAYS` (180) |
| Outbox messages | ids; `apple.revoke` carries a sealed Apple token; `push.account_security` may carry FCM tokens until sent | Postgres `core_outboxmessage` | Done rows purged after `OUTBOX_RETENTION_DAYS` (30); dead rows kept until requeued or resolved (§4); done/dead `sms.send` after 24 h |
| Idempotency records | stored responses of customer and staff writes (may contain profile or address data) | Postgres `core_idempotencyrecord` | Deleted after 24 h; a deleted customer's are deleted at once |
| Audit log | actor (staff name/email; customers as a masked phone `+998 90 *** ** 67`), action, before/after documents, IP, user agent, request id | Postgres `core_auditentry` (append-only) | Never purged (Q1) |
| Cache | session tombstones, throttle counters keyed by `pii_ref` (HMAC) of phones and device ids, the sealed OTP (120 s), QR token HMACs (60 s), Billz client ids keyed by `pii_ref(phone)` (30 days), provider tokens | Redis DB 0 | No plain phone number is a cache key |
| Task queue | task names and ids | Redis DB 1 (Celery broker) | |
| Logs | key=value lines with `request_id`; phones only as `pii_ref`; provider free text scrubbed (`core.pii.scrub_free_text`) | container stdout (Docker json-file, rotated 5 × 20 MB) | httpx request lines are silenced (Billz URLs carry a phone) |
| Backups | full `pg_dump` and a tarball of the media volume | host `/srv/backups` (14 days) plus the off-host copy | Contain everything above |

Reviewed model by model (every table of `glowderma.*`, 2026-10-27). No personal data: `catalog_*` (products,
categories, banners, images — `uploaded_by` / `last_change_actor` are staff ids), `professionals_service`,
`professionals_professionalservice`, `loyalty_cashbackpolicy`/`loyalty_cashbacktier` (author: staff id),
`loyalty_posdevice` (a till: name and location label), `notifications_statustemplate`,
`integrations_integrationstate` (sync cursors and the last error text, scrubbed) and `core_outboxmessage` beyond
what its row above lists.

### 1.2 What leaves the server, and to whom

Uzbek providers (data stays in Uzbekistan as far as the provider's own hosting does — confirm with each):

| Provider | Why | What we send | What we receive |
|---|---|---|---|
| Click | payments | the pay URL the app opens (service id, merchant id, amount, the HMAC order reference, return URL); Merchant API status/reversal calls keyed by service id and `click_paydoc_id` | SHOP API callbacks (transaction ids, amount, the order reference, signature) |
| Payme | payments | the checkout URL (merchant id, the HMAC order reference, amount, language, return URL) | Merchant API JSON-RPC callbacks (transaction id, amount, the order reference, times); refunds are made in the Payme cabinet |
| BTS | delivery | parcel creation: recipient name, recipient phone, delivery address and comment, region/city codes, our order id, weight; tariff quotes (city codes, weight); tracking by our tracking number | parcel ids, tracking numbers, statuses (webhook and polling) |
| Eskiz | SMS | the phone number (digits) and the OTP text in the customer's language | a message id |
| Billz | POS / stock | catalog reads; for each paid online order: product ids, quantities, the prices paid, the customer's phone (client lookup, and client creation when none exists — no name), the comment `Glow Derma #<order>` | products, stock, prices; sale ids |

Foreign services:

| Service | Why | What it receives | What we receive |
|---|---|---|---|
| Firebase Cloud Messaging (Google) | push notifications | the device's FCM token and the notification: title, body and `data` with ids, the order number and provider names only — never names, phones, addresses or amounts (customer contract §2.14.2, C1); campaign texts as staff wrote them and the campaign image URL; the service-account JWT for its access token | a message id or an error |
| Sign in with Google / Facebook / Apple | customer social sign-in, staff Google/Facebook sign-in | nothing about the customer: only what the provider issued itself, sent back for verification — Facebook's `debug_token`/`me` calls carry the user's Facebook access token, Apple's token endpoint receives the authorization code and (on unlink or deletion) the Apple refresh token to revoke, Google's token endpoint receives the staff authorization code; Google and Apple ID tokens are verified locally against the providers' published keys | the provider's ID/access token: subject, email (when shared), name claims |

Not a data flow: the mobile app stores (Apple, Google) receive nothing from the backend.

**Alert webhook (optional, `ALERT_WEBHOOK_URL`).** Alerts carry ids and counts only (`core.alerts` scrubs the
fields: keys naming personal data or credentials are redacted, digit runs of seven or more are masked). The
destination must be hosted in Uzbekistan **or** be guaranteed to receive no personal data; prefer a receiver in
the same hosting. Do not point it at a foreign chat or incident SaaS without that check.

**Error tracking and log shipping.** None is configured. Before adding one (Sentry, a hosted log service), it
must be hosted in Uzbekistan or receive scrubbed data only (customer contract §6, item 9).

## 2. Hosting requirements

Everything that stores or processes personal data runs on infrastructure physically located in Uzbekistan:

- **PostgreSQL** (all tables above) and its WAL/archives.
- **Redis** (cache and broker; it holds sealed OTPs, session tombstones and `pii_ref` keys).
- **Media volume** (customer profile images, product and campaign images).
- **Backups** — the local copy and every off-host copy (`docs/ops/deploy.md` §8 "copy the directory off the
  VPS": that destination must be in Uzbekistan too).
- **Logs** — container logs on the host and any log shipping or aggregation.
- The api, worker and beat containers, and the edge proxy that terminates TLS (it sees request bodies).

Operator checklist: the VPS provider's data-centre location is in Uzbekistan (ask for it in writing); the backup
target is in Uzbekistan; no log, metrics or error-tracking agent forwards to a foreign service; the alert webhook
destination follows §1.2.

## 3. Personal-data deletion (`DELETE /api/v1/me/`)

The customer deletes the account in the app (customer contract §2.2.3; password, or an SMS code for social-only
accounts). `customers.services.delete_account`, in one transaction:

1. refuses while a Click payment of an unpaid order is in flight (409, retry in ≤ 2 minutes);
2. cancels every `awaiting_payment` order (orders already paid continue: BTS delivers with the recipient
   snapshot);
3. cancels every open future appointment;
4. deletes the cart lines;
5. `accounts.services.delete_customer`: status `deleted`; name, profile image (the file is deleted after the
   commit), default address, password scrubbed; language reset; consent cleared (the audit log keeps the consent
   history); saved addresses and social identities deleted (Apple refresh tokens revoked through `apple.revoke`);
   devices deactivated with their FCM token and device key cleared; every session revoked; QR tokens deleted; the
   customer's idempotency records deleted;
6. audit `customer.deleted {orders_cancelled, appointments_cancelled}`.

Kept on purpose: the customer row with its phone (unique key; re-registration reactivates it, decision A1), the
loyalty account, ledger and purchases, orders (with their recipient snapshot), payments, appointments, inbox rows
and the audit log. A re-registration shows nothing created before it (history horizon, §2.2.3).

Not covered by the app flow (operator steps on request): copies in backups (they age out with the backup
retention), raw provider callbacks (purged after 180 days), Billz's own client record created from the phone
(delete it in Billz), BTS and Eskiz records (provider-side retention).

## 4. Retention

| Data | Retention | Mechanism |
|---|---|---|
| Raw provider callbacks (`WebhookInbox`) | `WEBHOOK_INBOX_RETENTION_DAYS`, default 180 days (customer contract §6 item 11, decision T13) | beat `integrations-purge-webhook-inbox` 03:25 Tashkent: batches inside `core.db.purge_window()` (the append-only trigger lets only that transaction delete), audit `integrations.inbox_purged {provider, rows, before}` |
| Done outbox messages | `OUTBOX_RETENTION_DAYS`, default 30 days | beat `core-purge-outbox` 03:20: ORM batches (shipments drop their link), audit `outbox.purged {rows, before}` |
| Dead outbox messages | kept until resolved | never purged by retention: a dead message is an action item (`requeue_outbox --dead` replays it from the row; the consistency report lists dead `billz.push_sale` rows of any age and K6 the recent ones). They hold ids; a dead `push.*` or `campaign.send_batch` row may hold FCM tokens. Once the cause is fixed, replay them; dead `sms.send` rows are the exception (deleted after 24 h, phone and code) |
| Done/dead `sms.send` messages, SMS codes | 24 h | `accounts.cleanup_auth`, hourly |
| Revoked/expired customer sessions | 90 days | `accounts.cleanup_auth` |
| Idempotency records | 24 h | `core.purge_idempotency_records`, 03:00 |
| Sealed OTP, QR tokens, refresh grace entries (Redis) | 120 s / 60 s / 120 s | key TTL |
| Container logs | 5 × 20 MB per container | Docker json-file rotation (`compose.prod.yaml`) |
| Backups | 14 days on the host | cron in `docs/ops/deploy.md` §8; the off-host copy needs its own rule (Q6) |
| Audit log (`AuditEntry`) | kept indefinitely | **not purged** — legal retention unknown (Q1). The purge window allows `core_auditentry`, but no job uses it |
| Orders, payments, refunds, ledger, purchases, appointments | kept indefinitely | accounting and loyalty history (Q2) |
| Customer row and phone after deletion | kept | decision A1 (Q3) |
| Inbox notifications, campaign recipients | kept | the M1 design proposed 365 / 180 days (N-8, N-9); not implemented (Q5) |

Changing a retention period is an environment change (`.env`, then `up -d worker beat`); the next nightly run
applies it. Shortening it deletes the older rows on that run.

## 5. Registering the personal-data database (operator step)

Uzbek personal-data law requires the operator of a personal-data database to register it with the state body
authorised for personal data before processing starts. This is a step for the operator (the legal entity running
Glow Derma), not something the software does:

1. With counsel, confirm the authorised body, the current procedure and the form.
2. Describe the database using §1 of this document: the categories of data subjects (customers, staff), the data
   listed in §1.1, the purposes (online shop, clinic appointments, loyalty programme, delivery, payments), the
   recipients in §1.2 (Uzbek providers, and the foreign services FCM and the social sign-in providers with what they
   receive), the storage location (§2) and the retention (§4).
3. Name the responsible person and the security measures (access limited to the admin network and the internal
   secret, hashed and sealed credentials, append-only audit log, backups).
4. File the registration, keep the confirmation with this document and re-file when the description changes (a new
   provider, a new purpose, a new hosting location).

## 6. Open questions for the product owner

Tracked in `docs/decisions/open-questions.md`: Q1 under C4, Q2–Q6 and Q8–Q10 as M3-R1–M3-R8; Q7 is settled (T13).

| # | Question | Today |
|---|---|---|
| Q1 | Legal retention of the audit log (it holds staff names, masked phones, IPs and user agents) | kept indefinitely; the purge window could delete it once a period is decided |
| Q2 | Legal retention of orders, payments and loyalty records (accounting/tax) and whether they are anonymised afterwards | kept indefinitely |
| Q3 | Whether a deleted customer's phone may be kept indefinitely (it is the loyalty key and keeps the balance) | kept |
| Q4 | Whether the order recipient snapshot (name, phone, address) is scrubbed after delivery or after deletion | kept |
| Q5 | Retention of inbox notifications and campaign recipients (designed 365 / 180 days) | kept |
| Q6 | Backup retention off the host, and its location | not defined in code |
| Q7 | Raw callback retention: settled at 180 days by customer contract §6 item 11 ("180-day retention, C4") and decision T13 | 180 days implemented (`WEBHOOK_INBOX_RETENTION_DAYS`); a shorter period needs a contract change first |
| Q8 | Which alert receiver is used (§1.2) | none configured |
| Q9 | Specialists' personal data (name, photo, biography, schedule and absences): the legal basis for publishing it (employment contract or consent) and what happens when a specialist leaves | staff deactivate the specialist; the row, photo and bookings are kept |
| Q10 | Customer segments are profiling (rules over purchase history, manual memberships): does the privacy policy cover it, and should a deleted account's memberships and campaign deliveries be removed | kept |
