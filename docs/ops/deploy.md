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
| `reap-stale-jobs` | `integrations.reap_stale_jobs` | every 5 minutes |
| `commerce-poll-bts` | `commerce.poll_bts` | every 10 minutes (no-op while BTS is fake) |
| `accounts-cleanup-auth` | `accounts.cleanup_auth` | hourly |
| `delivery-refresh-bts-regions` | `delivery.refresh_bts_regions` | every 6 hours |
| `billz-sync-delta` | `catalog.billz_sync_delta` | hourly at :07 (no-op while Billz is fake) |
| `purge-idempotency-records` | `core.purge_idempotency_records` | 03:00 |
| `core-purge-outbox` | `core.purge_outbox` | 03:20 (done messages older than `OUTBOX_RETENTION_DAYS`) |
| `integrations-purge-webhook-inbox` | `integrations.purge_webhook_inbox` | 03:25 (`WEBHOOK_INBOX_RETENTION_DAYS`) |
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
| Eskiz | **No SMS delivered**: nobody can register, reset a password or sign in on a new device (the code is never sent) |
| FCM | **No push reaches a device**: the fake logs ids only and reports the pushes sent; inbox rows are written as usual |
| OAuth | Social sign-in off for staff and customers (password sign-in works) |

So a fake deployment is a demo/staging posture: real customers need live Billz (selling), live Eskiz (sign-up),
live Click and/or Payme (payment), live BTS (delivery) and live FCM (pushes). The local/test development switches
(`FAKE_NOW`, `FAKE_PAYMENTS_ENABLED`, `FAKE_SCENARIO_HEADER_ENABLED`, `ESKIZ_FAKE_CODE`, `POS_API_KEYS`) are refused by
the production settings whatever the modes.

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

