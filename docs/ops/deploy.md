# Deployment

**The deployment lives in the infra repository: [GlowDermatology/infra](https://github.com/GlowDermatology/infra).**
This repository only builds, scans and publishes the image; `.github/workflows/ci.yml` then writes the new
image digest into infra's `environments/staging/backend/image.env`, which deploys staging. Production is
promoted from staging in the infra repository (with an approval).

| Topic | Where (infra repository) |
|---|---|
| topology, networks, processes | `docs/architecture.md` |
| pipelines, GitHub settings, promotion, rollback | `docs/deployment.md` |
| secrets (Vault), rotation | `docs/secrets-management.md` |
| new server, first deploy | `docs/first-server-runbook.md` |
| backups, restore, rebuilding a server | `docs/disaster-recovery.md` |
| edge (Caddy) routing contract | `caddy/Caddyfile`, `caddy/edge_probe.py` |

What this repository provides to it:

- **One image, one process per container**: `entrypoint.sh` takes `api`, `worker`, `beat`, `migrate`, `manage`
  or `shell`. Worker counts come from `GUNICORN_WORKERS`, `GUNICORN_THREADS`, `CELERY_CONCURRENCY`.
- **Secrets from a file**: when `DJANGO_SECRETS_FILE` names a JSON file (rendered by Vault Agent), `config/secrets.py`
  loads it into the environment before any setting is read. Development and tests do not use it.
- **Migrations run once per release**, in the `migrate` container; `api` never migrates in production
  (`MIGRATE_ON_START` is for local development).
- **Static files** (Django admin, Unfold, DRF) are collected into the image at build time and served by
  WhiteNoise. Media is a directory the edge serves at `/media/`.

Commands below use infra's `scripts/manage.sh <env> …` (run on the server, in `/opt/glowderma/infra`).
The sections that follow are application knowledge that does not depend on how the stack is deployed.

## 7. Beat schedule (`config/celery.py`, Asia/Tashkent)

| Entry | Task | When |
| --- | --- | --- |
| `dispatch-outbox` | `core.dispatch_outbox` | every 10 s (and after every commit that enqueues) |
| `commerce-expire-reservations` | `commerce.expire_reservations` | every minute |
| `start-due-campaigns` | `notifications.start_due_campaigns` | every minute |
| `notifications-finalise-stopping` | `notifications.finalise_stopping_campaigns` | every minute (a `stopping` campaign with no open batch) |
| `commerce-poll-yandex` | `commerce.poll_yandex` | every 60 s (Yandex Delivery `claims/bulk_info`; single flight; no-op while Yandex is fake) |
| `reap-stale-jobs` | `integrations.reap_stale_jobs` | every 5 minutes |
| `commerce-review-delivery-issues` | `commerce.review_delivery_issues` | every 5 minutes (Yandex Delivery exception queue, urgent-issue reminders) |
| `billz-sync-delta` | `catalog.billz_sync_delta` | every `BILLZ_DELTA_SYNC_SECONDS` (300 s; no-op while Billz is fake) |
| `commerce-poll-bts` | `commerce.poll_bts` | every 10 minutes (no-op while BTS is fake) |
| `accounts-cleanup-auth` | `accounts.cleanup_auth` | hourly |
| `delivery-refresh-bts-regions` | `delivery.refresh_bts_regions` | every 6 hours |
| `purge-idempotency-records` | `core.purge_idempotency_records` | 03:00 |
| `core-purge-outbox` | `core.purge_outbox` | 03:20 (done messages older than `OUTBOX_RETENTION_DAYS`) |
| `integrations-purge-webhook-inbox` | `integrations.purge_webhook_inbox` | 03:25 (`WEBHOOK_INBOX_RETENTION_DAYS`) |
| `delivery-purge-quotes` | `delivery.purge_quotes` | 03:35 (unused Yandex checkout quotes a day after they expired) |
| `delivery-blank-courier-details` | `delivery.blank_courier_details` | 03:37 (courier name, vehicle, tracking link 7 days after a claim ended) |
| `reporting-consistency-report` | `reporting.consistency_report` (K1–K6 + loyalty reconciliation) | 03:40 |
| `billz-sync-full` | `catalog.billz_sync_full` | 04:23 (no-op while Billz is fake) |

**beat is a singleton.** Never `--scale beat=2`: every periodic task would run twice. The schedule file lives in the
container (`/tmp/celerybeat-schedule`), so a recreated beat simply resumes the crontab.

## 8. Fake-mode posture

Every provider has a `*_MODE` (`fake` | `live`, default `INTEGRATIONS_MODE`). Fake adapters are allowed under the
production settings (product-owner decision 2026-10-26, customer contract §6.14): the processes log one WARNING at start
(`integrations running in fake mode in production: …`), and `health/?ready=1` lists the modes to the admin network. A
fake never pretends an effect happened:

| Provider fake | What production does |
| --- | --- |
| Billz | **No online selling**: `settings/public` says `online_sales_enabled: false`, cart and checkout answer `409 online_sales_unavailable`, the admin import is refused, the beat syncs do nothing, no sale is recorded |
| Click / Payme | **No money moves**: not offered at checkout (`payment_providers` without them), callbacks refused (`401`, inbox row `fake_mode`); a Click Complete of a transaction prepared while Click was live is accepted and reversed by hand. Never switch live → fake while the provider holds open money (the worker logs ERROR `providers.fake_with_open_money` at start) |
| BTS | **Pickup only** (no home delivery), callbacks refused, no parcel invented |
| Yandex Delivery | **No courier**: no Yandex option at checkout (`settings/public` `delivery.providers` without `yandex`), the callback refused (`403`, inbox row `fake_mode`), the poll does nothing, the fake invents no price or claim. Do not switch live → fake while claims are open (§13) |
| Eskiz | **No SMS delivered**: nobody can register, reset a password or sign in on a new device (the code is never sent) |
| FCM | **No push reaches a device**: the fake logs ids only and reports the pushes sent; inbox rows are written as usual |
| OAuth | Social sign-in off for staff and customers (password sign-in works) |

So a fake deployment is a demo/staging posture: real customers need live Billz (selling), live Eskiz (sign-up),
live Click and/or Payme (payment), live BTS and/or live Yandex Delivery (delivery) and live FCM (pushes). The local/test development switches
(`FAKE_NOW`, `FAKE_PAYMENTS_ENABLED`, `FAKE_SCENARIO_HEADER_ENABLED`, `ESKIZ_FAKE_CODE`, `POS_API_KEYS`) are refused by
the production settings whatever the modes.

**Staging: `config.settings.staging`.** Staging runs its own settings module, which imports
`config.settings.production` (every production check applies) and adds only the staging differences: the
`STAGING` badge, `ALERT_ENV=staging` by default, and `STAGING_ACTIVE_FAKES = ["billz"]` — the fake Billz behaves as
locally, so products can be created (synthetic Billz name, price and stock; an id ending in `0000` is "not found"),
the import yields the three demo products, and online selling is on (payments, delivery, SMS and OAuth fakes keep
the rows above). It has no effect once staging sets `BILLZ_MODE=live`. The infra repository selects the module per
environment (`DJANGO_SETTINGS_MODULE` in `environments/<env>/stack.env`). `seed_demo --reset` and the demo-admin
password rule treat staging exactly like production.

A live provider that is not configured makes `health/?ready=1` answer `503` (§10). The production settings already
refuse to start when a live provider misses a required value, so in practice that means a key file that became
unreadable after boot or a setting the adapter itself refuses when it is built.

A staging stack that loads the demo data (`manage.py seed_demo --force` under the production settings) needs
`SEED_ADMIN_PASSWORD` in its `.env`: seed_demo refuses to create `admin@glowderma.uz` without it, with fewer than 12
characters or with the committed demo password, because the stack is internet-facing.

## 9. Provider go-live checklists

Do each on a **staging** stack first (same compose file, its own `.env`, `ALERT_ENV=staging`, a
`BTS_CLIENT_ID_PREFIX` such as `stg-` when it shares the BTS account), then switch production by setting the block in
`.env` and `up -d api worker beat`. Every adapter is marked "not verified against a live sandbox": its document's
"Verify M3" points are part of the checklist.

**Billz** ([billz.md](../integrations/billz.md) §10–§11): integration key with the sale and client routes; read the
shop, cash box and payment-type ids (`GET /v1/shop`, `/v1/cash-box`, `/v1/company-payment-type`); set
`BILLZ_MODE=live`, `BILLZ_SECRET_KEY`, `BILLZ_SHOP_ID`, `BILLZ_CASHBOX_ID`, `BILLZ_PAYMENT_TYPE_ID`; admin → Products →
Billz import `full` succeeds; one paid test order appears in Billz → Sales at the paid price (D1). Runbooks to have at
hand (billz.md §7 "Dead-letter runbook", monitoring.md §2–§3):
- a sale entered in Billz by hand → record it, then close its dead message; each command first with `--dry-run`
  (it lists the order and the message), then again without it:
  `scripts/manage.sh <env> django billz_mark_recorded --order <id> --actor <staff email> [--billz-sale-id <uuid>] --dry-run`,
  then `… manage.py requeue_outbox --topic billz.push_sale --dead --resolved --order <id> --dry-run` — check that the
  listed message is that order's sale: `--resolved` closes it **without** sending it to Billz;
- a configuration error fixed → replay the dead sales:
  `… manage.py requeue_outbox --topic billz.push_sale --dead [--since <ISO>] [--order <id>] --dry-run`, then without
  `--dry-run` (never replay a sale someone entered by hand; `--forget-payment-attempt --actor <email>` only after
  confirming Billz never took the payment).

**Click** ([click.md](../integrations/click.md) sandbox checklist): in the Click cabinet set the SHOP API
**Prepare URL** `https://API_DOMAIN/api/v1/webhooks/click/prepare/` and **Complete URL**
`https://API_DOMAIN/api/v1/webhooks/click/complete/`; set `CLICK_MODE=live`, `CLICK_SERVICE_ID`, `CLICK_MERCHANT_ID`,
`CLICK_MERCHANT_USER_ID`, `CLICK_SECRET_KEY`; optionally `CLICK_ALLOWED_IPS` with Click's callback ranges; register the
VPS egress IP for the Merchant API if Click requires it. Pay a test order, cancel it, check the reversal.

**Payme** ([payme.md](../integrations/payme.md) sandbox checklist): in the Payme cabinet set the **Merchant API
endpoint** `https://API_DOMAIN/api/v1/webhooks/payme/` and the account field `order_id` (`PAYME_ACCOUNT_FIELD`); set
`PAYME_MODE=live`, `PAYME_MERCHANT_ID`, `PAYME_KEY` (the cabinet's `TEST_KEY` with `PAYME_TEST_MODE=true` on staging,
the production `key` without it); set `PAYME_ALLOWED_IPS` to Payme's published callback addresses (the edge passes the
real client address: §16). Run the test.paycom.uz scenarios, then one real low-value payment. Refunds are made in the
Payme cabinet (`manual_required`).

**BTS** ([bts.md](../integrations/bts.md) "Webhook setup" and sandbox checklist): set `BTS_MODE=live`, `BTS_BASE_URL`
(the production URL from BTS), `BTS_LOGIN`, `BTS_PASSWORD`, the sender block (`BTS_SENDER_NAME`, `BTS_SENDER_PHONE`,
`BTS_SENDER_ADDRESS`, `BTS_SENDER_CITY_CODE`, `BTS_PICKUP_TYPE`/`BTS_SENDER_BRANCH_CODE`); configure BTS's webhook
(`/v1/webhook/webhook-config` or BTS support) with
`webhook_url = https://API_DOMAIN/api/v1/webhooks/bts/?token=<BTS_WEBHOOK_SECRET>` and **`has_token` off**. The edge
writes no query string to any log — the access logs and the error line of a request the api could not answer (a 502
while it restarts) alike — and gunicorn logs no query string either, so the secret stays out of the logs; the inbox
stores no query string either. Pay a delivery order: the parcel appears in the BTS cabinet with our `clientId`.

**Eskiz** ([eskiz.md](../integrations/eskiz.md) sandbox checklist): approved **sender nickname** (production refuses
`4546`, the shared test sender) and approved **templates** of the OTP text in uz/ru/en exactly as rendered (with the
Android hash variant when `ANDROID_SMS_APP_HASH` is set); set `ESKIZ_MODE=live`, `ESKIZ_EMAIL`, `ESKIZ_PASSWORD`,
`ESKIZ_SENDER`; leave `ESKIZ_CALLBACK_URL` empty. Register a test phone through the app: the SMS arrives with the
catalog text. Watch the Eskiz balance.

**FCM** ([fcm.md](../integrations/fcm.md) sandbox checklist): service account with the "Firebase Cloud Messaging API
Admin" role, FCM HTTP v1 API enabled; **mount the key** as `/srv/glowderma/secrets/fcm-service-account.json` (§3.3),
set `FCM_MODE=live`, `FCM_PROJECT_ID`, `FCM_CREDENTIALS_FILE=/run/secrets/glowderma/fcm-service-account.json`; the boot
check passes in api, worker and beat; a test device receives an order-status push.

**Yandex Delivery** ([yandex-delivery.md](../integrations/yandex-delivery.md) §16; §13 below): contract, token,
prepaid balance, `auto_accept` off; set `YANDEX_DELIVERY_MODE=live`, `YANDEX_DELIVERY_TOKEN`, the pilot ids; run
`manage.py yandex_delivery_check`; configure the warehouse and switch one method on in the ops console; the alert
webhook points at the Telegram bot; one supervised pilot order (§13). Outbound HTTPS to `b2b.taxi.yandex.net` from
api and worker.

**Social sign-in** (`OAUTH_MODE=live`): Google and Facebook credentials, the two callback URLs of §3.5, and — for Sign
in with Apple — `APPLE_BUNDLE_IDS`, `APPLE_TEAM_ID`, `APPLE_KEY_ID` and the `.p8` mounted as
`APPLE_PRIVATE_KEY_FILE=/run/secrets/glowderma/apple-signin.p8`. Customer Google sign-in also needs
`GOOGLE_APP_CLIENT_IDS` (the app's client ids). While social sign-in is not ready but other providers go live with
`INTEGRATIONS_MODE=live`, keep `OAUTH_MODE=fake` in `.env` explicitly.

After every switch: `health/?ready=1` is `200` and the readiness one-liner of §10 shows the provider `live`, configured,
and not in `sandbox`.

## 12. Compliance gate (before real customers)

[`docs/ops/compliance.md`](compliance.md) is the gate of M3. Before the first real customer:

1. **Hosting in Uzbekistan** (§2): VPS, backups (local and off-host), logs and any log/metrics shipping, the alert
   receiver — confirmed in writing.
2. **Database registration** (§5): the operator registers the personal-data database with the authorised state body
   (with counsel), using §1 of compliance.md as the description; keep the confirmation with that document and re-file
   when a provider, purpose or hosting location changes.
3. **Open questions** Q1–Q10 (§6) answered by the product owner (retention of the audit log, orders, backups …).
4. **Retention** settings left at their contractual defaults unless decided otherwise (`WEBHOOK_INBOX_RETENTION_DAYS=180`).

## 13. Yandex Delivery release (`feat/yandex-delivery`)

Courier delivery through Yandex Delivery's Express API ([yandex-delivery.md](../integrations/yandex-delivery.md)).
It ships **off**: with the defaults nothing changes for customers (every Yandex method is switched off in the
warehouse settings, and fake Yandex under the production settings offers nothing). Infra changes below are reported
here; they are made in the infra repository.

**Migrations** (additive; the usual release order — api, worker and beat stopped, then `migrate`, then started):

| Migration | What |
|---|---|
| `integrations.0006_yandex_provider` | `yandex` in the provider choices of `WebhookInbox` / `IntegrationState`; inserts the `IntegrationState(yandex)` row (reverse deletes it) |
| `commerce.0013_yandex_delivery` | `Order.address_latitude` / `address_longitude` (nullable, both or neither); the `OrderEvent` checks regenerated (`delivery_failed`, `shipment_returned`; source `yandex` for `handed_to_courier` / `delivered`) — checks only widen |
| `delivery.0006_yandex_delivery` | new tables `delivery_deliverysettings`, `delivery_warehousehours`, `delivery_warehouseclosure`, `delivery_deliveryquote`, `delivery_courierclaim`, `delivery_deliveryissue`; `Shipment` gains `provider` (database default `bts`), `method`, `stage`, `quote`, `claim`, the parcel and cost columns (nullable or with database defaults); `ShipmentEvent.carrier_status` (database default `""`); constraints and partial indexes (one open claim per shipment, one open issue per order and kind). Depends on `commerce.0013` |
| `delivery.0007_seed_delivery_settings` | the `DeliverySettings` singleton (also re-ensured on every `migrate`) |
| `delivery.0008_courierclaim_cancel_sent` | `CourierClaim` gains `cancel_sent_at` (nullable: our `claims/cancel` was sent, never sent again), `cancel_state` (`varchar(16)`, existing rows `''`: the terms it was sent with) and `cancel_price` (nullable: a paid cancel's quoted price, counted as the fee once Yandex shows `cancelled_with_payment`); columns only, no index or constraint. Depends on `delivery.0007` |

**Environment** (Vault `secret/<env>/django`; full list in `.env.example`, rules in the integration doc §7):
`YANDEX_DELIVERY_MODE` (`live` to go live), `YANDEX_DELIVERY_TOKEN` (**secret**, required when live),
optionally `YANDEX_DELIVERY_CALLBACK_URL` (`https://API_DOMAIN/api/v1/webhooks/yandex-delivery/`, no query),
`YANDEX_DELIVERY_PILOT_CUSTOMER_IDS` for the pilot; `YANDEX_DELIVERY_BASE_URL`, `_CURRENCY` (`UZS`), `_LANGUAGE`
(`ru`), `_SKIP_CONFIRMATION_SOURCE` / `_DESTINATION` (`false`) keep their defaults. Alerts: `ALERT_WEBHOOK_URL`
(https, the bot's `/hooks/alerts`) and `ALERT_WEBHOOK_SECRET` (≥ 32 characters, the same value as the bot's);
with Yandex live and no alert URL every process logs a startup WARNING. Frontend (its own repository):
`NEXT_PUBLIC_MAP_TILE_URL` (the address-pin map; default OpenStreetMap tiles) and its CSP origins.

**Beat** — four new entries (§7): `commerce-poll-yandex` (60 s), `commerce-review-delivery-issues` (5 min),
`delivery-purge-quotes` (03:35), `delivery-blank-courier-details` (03:37). No new queue, port or system package.

**Network.**
- Outbound HTTPS (TLS 1.2+) to **`b2b.taxi.yandex.net:443`** from the **api** (checkout price checks, the admin
  cancel, the ops console «Yandex bilan tekshirish», `manage` runs of `yandex_delivery_check`) and the **worker**
  (dispatch, callback re-reads, the poll). Beat makes no call.
- **Callback route at the edge** (only when `YANDEX_DELIVERY_CALLBACK_URL` is set): `POST
  /api/v1/webhooks/yandex-delivery/` to the api, like the other webhooks. Its query string carries our HMAC order
  reference (`?ref=…&updated_ts=…&claim_id=…`): **the edge must not log query strings** for it (it logs none for any
  route today, as for the BTS `token=`); gunicorn logs none and the inbox stores none. Yandex documents no source
  IPs, so no allow-list is possible.

**Telegram bot (new service, repository `bot`).** Infra item, not built here: a compose service from the bot's
Dockerfile (non-root, port `BOT_PORT` 8080, health `GET /healthz`), its secrets (`ALERT_WEBHOOK_SECRET` = the
backend's, `TELEGRAM_BOT_TOKEN` from BotFather, `TELEGRAM_CHAT_ID`; optionally `ADMIN_BASE_URL`,
`BOT_ENV_ALLOWLIST=production`), outbound HTTPS to `api.telegram.org`, and an https route the backend worker
reaches for `ALERT_WEBHOOK_URL` (the production settings refuse a non-https alert URL). Find the chat id with
`python -m glowderma_bot chat-id`; test with any alert (monitoring §1).

**Yandex account (manual, before `live`).** A contract for the Uzbek legal entity; the OAuth token from the business
cabinet's *Integration* tab (a cabinet password change invalidates it → `yandex.auth_failed`, then
`delivery.provider_failing`); a **prepaid balance** topped up and watched; **`auto_accept` stays off** — the backend
never sends it, and every claim must wait for our `claims/accept` after the price check (a WARNING
`yandex.accepted_without_us` means the cabinet accepted a claim by itself). Staff who edit the warehouse need the
ops console (`DJANGO_ADMIN_ENABLED=true`, `OPS_DOMAIN`, `OPS_ALLOWED_IPS`, TOTP; `docs/ops/staff-access.md`).

**Pilot order procedure** (staging first, then production; one supervised order before opening to everyone):
1. Set `YANDEX_DELIVERY_MODE=live`, `YANDEX_DELIVERY_TOKEN`, `YANDEX_DELIVERY_PILOT_CUSTOMER_IDS=<pilot customer
   ids>` (and the alert webhook); `up -d api worker beat`; `health/?ready=1` → 200 with `yandex` configured.
2. `scripts/manage.sh <env> django yandex_delivery_check --lat <warehouse lat> --lon <lon> --address "<address>"`:
   `courier` and `express` listed, prices in `UZS`; exit status 0.
3. Ops console «Yetkazish sozlamalari»: warehouse name, address, pin, contact (E.164 phone, e-mail), courier
   comment; «Ombor ish vaqti» (every working weekday) and special days; «Yandex bilan tekshirish»; switch on **one**
   method (Standard); keep the policy defaults unless decided otherwise.
4. As a pilot customer: pin an address, `delivery/calculate/` shows the Yandex option with a fee and ETA; check out
   and pay (a small real amount).
5. Admin panel: the order is «To‘langan · tayyorlash»; pack it, «Kuryer chaqirish» with the weighed parcel; watch
   `delivery.stage` and the claim status (`claims/info` via the poll every 60 s) through `performer_found` →
   `pickuped` → `delivered`; the customer sees the stage, the courier and the tracking link. Note every status,
   price, VAT and currency Yandex sent (`CourierClaim`, the shipment events) and the final price on the cabinet's
   bill (`cost_difference`).
6. A second pilot order cancelled before pickup (free cancel), and — if Yandex agrees — one after pickup (return,
   confirm return, refund). Record the fees.
7. Clear `YANDEX_DELIVERY_PILOT_CUSTOMER_IDS` to open Yandex to everyone; switch on Express when decided.

**Restarts during an admin cancel.** An api restart that kills a Yandex order's cancel between Yandex's answer and
the commit of its "sent" marker loses the marker (a known limit: integration doc §12, residual window; gunicorn
waits `graceful_timeout`, 30 s, for running requests). Staff who got no answer for a cancel during a deploy should
let the poll catch up (a claim Yandex cancelled then raises `cancel_incomplete` 10–15 minutes after the first press,
and repeating the cancel then makes no carrier call) rather than press cancel again at once.

**Switching off.** To stop offering Yandex, switch the methods off in the ops console (or suspend dispatch): open
claims keep being polled and cancellable. Do **not** set `YANDEX_DELIVERY_MODE=fake` while claims are open: under the
production settings the fake refuses everything, so the poll stops and an admin cancel of such an order answers
503.

## 14. Admin UX release (`feat/admin-ux`)

Staff invites and forced password changes, optional admin panel MFA (TOTP), tiered audit log, the appointments
week/month view and the admin API in uz / ru / en (`docs/api/admin-api-v1.md` §1.2, §1.6, §1.8, §2.1, §2.9,
§2.13, §2.16; runbook `docs/ops/staff-access.md`). Everything is additive and off by default for existing staff
(nobody is forced to change a password, MFA is each person's own choice). Infra changes below are made in the
infra repository.

**Migrations** (the usual release order — api, worker and beat stopped, then `migrate`, then started):

| Migration | What |
|---|---|
| `accounts.0015_staff_invite_password_change` | `StaffUser.must_change_password` (database default `false`), `password_changed_at` (nullable), `language` (`uz` / `ru` / `en`, database default `uz`); `AuthSession.mfa_verified_at` (nullable); new table `accounts_staffpasswordtoken` (SHA-256 token hashes only, unique, index `accounts_pwtoken_staff_created`). Columns with database defaults, so workers of the previous release keep inserting during the rolling deploy; no data rewritten. Ordinary (atomic) migration. |
| `core.0014_audit_request_id_actor_idx` | two indexes on `core_auditentry`, built **`CREATE INDEX CONCURRENTLY`** (`atomic = False`, `AddIndexConcurrently`): `audit_request_id_like_idx` (`request_id varchar_pattern_ops WHERE request_id <> ''`) and `audit_actor_created_idx` (`actor_type, actor_id, created_at`). Writes to the audit table are not blocked while they build. Needs a direct session to PostgreSQL (no transaction-mode pooler in between; `migrate` runs it outside a transaction by itself); it waits for transactions already open on the table to finish, so do not run it next to a long-running report or `psql` session. If the build is interrupted, PostgreSQL leaves an `INVALID` index of that name and a second `migrate` fails with "already exists": `DROP INDEX CONCURRENTLY IF EXISTS <name>;` then `migrate` again. Valid on PostgreSQL 16 and 18. |

Rollback: `migrate core 0013` drops the two indexes; `migrate accounts 0014` drops the token table and the new
columns (open invite links stop working; staff keep their passwords).

**Environment** (Vault `secret/<env>/django`; documented in `.env.example`):
- `ADMIN_PANEL_URL` — the admin panel's base URL, e.g. `https://glowderma.uz/admin` (the production settings
  require `https`; absolute, no query; a trailing `/` is dropped). The invite link is
  `{ADMIN_PANEL_URL}/set-password?token=…`. Empty (the default) = e-mail invites are not offered: the panel shows
  the option disabled and only temporary passwords work.
- `STAFF_INVITE_TTL_HOURS` — optional, default `72` (1–168): how long an invite / password link stays valid.
- E-mail invites also need `EMAIL_MODE=smtp` with the `EMAIL_*` relay values (already required for customer
  e-mail codes, `docs/ops/release-hybrid-identity.md`); with `EMAIL_MODE=fake` under the production settings the
  option is shown disabled. Messages go through the existing outbox topic `email.staff_password_link` on the
  worker (no new queue).
- Nothing else is new: no new secret, throttle rates have defaults (`staff_password_token` 30/min,
  `staff_password_link` 5/hour, `mfa_verify` 30/min; overridable through `THROTTLE_RATES` as before).

**Services, ports, beat.** None new: no new container, queue, port, system package or beat entry
(`config/celery.py` unchanged; the existing hourly `accounts-cleanup-auth` also drops the finished
`email.staff_password_link` outbox rows; token rows stay as the invite history). Outbound traffic is unchanged
(SMTP to the existing relay only when e-mail invites are used).

**Frontend.** Ship the matching `feat/admin-ux` frontend (it sends `Accept-Language` and handles the
`password_change_required` / `mfa_required` answers); no new frontend variable. The Next server must be able to
serve `ADMIN_PANEL_URL/set-password`.

**After the upgrade.** Operator commands: `manage.py resetstaffmfa <email> [--actor EMAIL]` (lost phone, when no
manager can use `staff/{id}/reset-mfa/`), `createstaff … --must-change`, `resetstaffpassword … --must-change`.
New alerts: `auth.mfa_locked`, `auth.mfa_reset` (`docs/ops/monitoring.md`).
