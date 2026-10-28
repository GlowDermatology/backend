# Production deployment (Docker Compose on one VPS) — M3 backend

This runbook deploys the backend (this repository) and the frontend (`GlowDermatology/frontend`) as one Compose
project. Files:

| File | Role |
| --- | --- |
| `compose.prod.yaml` | the stack: Caddy edge, Next.js frontend, Django api, Celery worker, a single Celery beat, PostgreSQL 18, Redis 8 (AOF) |
| `deploy/Caddyfile` | the edge: TLS, hosts, body limits and buffering, timeouts, private paths, `/media/`, log redaction |
| `deploy/env.production.example` | **every** variable the production settings and the stack read, grouped, with required / default / secret / generator per entry |
| `deploy/pin-release.sh` | writes the release's image tags (`GIT_SHA`, `FRONTEND_GIT_SHA`) into `.env` |
| `deploy/backup.sh`, `deploy/glowderma-backup.cron` | verified database and media backups (nightly, before an upgrade, before a restore) |
| `deploy/postgres/` | creates `glowderma_app`, the non-superuser role the application connects as |
| `deploy/edge_probe.py` | runs the Caddyfile in a local Caddy and checks its behaviour (CI runs it) |
| `docs/ops/monitoring.md` | alerts, the nightly consistency report, health and readiness, beat schedule, INVALID index recovery |
| `docs/ops/compliance.md` | personal data inventory, hosting in Uzbekistan, retention, database registration |
| `docs/integrations/<provider>.md` | Billz, Click, Payme, BTS, Eskiz, FCM: protocol, environment, sandbox checklists |

Sections 3–5 are the ordered procedures (first deploy, every upgrade, the one-time M1 → M3 upgrade); section 13 is the
smoke test to run after each of them. **Every command runs as root** (`sudo -i`; access to the Docker socket is
root-equivalent anyway) **in `/srv/glowderma/backend`** unless stated.

## 1. Topology

```
                 internet :80 / :443 (tcp+udp), IPv4
                              │
                     ┌────────▼────────┐  172.30.0.10  (TLS, HTTP/3, X-Forwarded-For overwrite, body buffering,
                     │  edge (Caddy)   │                timeouts, log redaction)
                     └──┬───────────┬──┘
   SITE_DOMAIN,         │           │   API_DOMAIN: /api/v1/* (customer API, Click/Payme/BTS webhooks, POS, health),
   ADMIN_DOMAIN         │           │   /media/* served from the media volume (read-only)
             ┌──────────▼──┐     ┌──▼──────────┐
             │  frontend   │────▶│  api        │  gunicorn :8000
             │  Next :3000 │     │  (Django)   │  /api/v1/admin/* accepted ONLY from 172.30.0.20 + internal secret
             └─172.30.0.20─┘     └──┬───────┬──┘
   ── network "front" 172.30.0.0/24 ┼───────┼──────────────────────────────────────────────────────────────
                                    │       │      worker (Celery, CELERY_CONCURRENCY processes)   beat (exactly one)
                              ┌─────▼──┐ ┌──▼────┐
                              │postgres│ │ redis │  DB 0 = cache, DB 1 = Celery broker (AOF on, password)
                              └────────┘ └───────┘
   ── network "back" (internal: no route out; api, worker, beat, postgres, redis) ─────────────────────────
       outbound HTTPS to the live providers: api through "front", worker through "egress"; beat needs none
```

- **SITE_DOMAIN** (e.g. `glowderma.uz`): the landing page. `www.` redirects to it; `/admin*` redirects to the admin host.
- **ADMIN_DOMAIN** (e.g. `admin.glowderma.uz`): the admin panel. The Next server is the BFF: it holds the staff session in
  HttpOnly `__Secure-` cookies and calls Django over the private network (`http://api:8000`).
- **API_DOMAIN** (e.g. `api.glowderma.uz`): the public Django surface — the mobile customer API, the provider callbacks
  `POST /api/v1/webhooks/click/prepare/`, `/click/complete/`, `/payme/`, `/bts/?token=…`, the cashier terminals
  `POST /api/v1/pos/cashback/resolve-qr/`, `GET /api/v1/health/` — plus `/media/`. The edge answers `404` for
  `/api/v1/admin/`, `/api/v1/schema/`, `/api/v1/docs/`, `/api/v1/dev/` and `/django-admin/`; Django refuses them from
  any address but the frontend container as well (`ADMIN_API_ALLOWED_NETS` + `X-GD-Internal-Secret`).

Only the edge publishes ports, on IPv4 only. PostgreSQL and Redis are on the `back` network, which neither the edge
nor the frontend joins (an RCE or SSRF in an internet-facing process cannot open a connection to them), and Redis
requires a password as well. The application connects to PostgreSQL as `glowderma_app`, which owns the database but
is not a superuser.

## 2. Prerequisites

| Item | Requirement |
| --- | --- |
| VPS | **In a data centre in Uzbekistan** (`docs/ops/compliance.md` §2; ask the provider for the location in writing). 2 vCPU / 4 GB RAM minimum (the memory limits of `compose.prod.yaml` are sized for it); 40 GB disk plus media and backup growth |
| OS | Any Linux with Docker Engine 25+ and the Compose plugin 2.24+ (`docker compose version`), `git`, `gzip`, `tar` |
| DNS | **`A` records only** (no `AAAA`) for **SITE_DOMAIN**, **www.SITE_DOMAIN**, **ADMIN_DOMAIN** and **API_DOMAIN**, pointing at the VPS before the first `up` (Caddy requests the certificates at once). The Compose networks are IPv4: Docker would relay an IPv6 client through its userland proxy with the bridge gateway as the source address, so every IPv6 client would share one per-IP throttle bucket (one abuser locks all of them out of OTP and POS) and the Click/Payme allow-lists could not match. The edge publishes its ports on `0.0.0.0` only |
| Firewall (inbound) | **22/tcp** (SSH, ideally restricted to the operators' addresses), **80/tcp** (ACME HTTP challenge + redirect), **443/tcp** (HTTPS), **443/udp** (HTTP/3). Nothing else |
| Outbound | 443 (and the ports below) to the hosts in the next table |

Docker publishes ports through its own iptables chains, which bypass `ufw`. That is why no service except the edge has a
`ports:` entry; do not add one to PostgreSQL or Redis for convenience.

**Outbound HTTPS allow-list** (if the hosting firewall filters egress). Provider hosts come from
`docs/integrations/*.md` and the adapters; the live ones are needed only once that provider's mode is `live`.

| Host | Port | Needed by | When |
| --- | --- | --- | --- |
| `acme-v02.api.letsencrypt.org` (and `acme.zerossl.com`, Caddy's fallback issuer) | 443 | edge | always (certificates) |
| `registry-1.docker.io`, `auth.docker.io`, `production.cloudflare.docker.com`, `ghcr.io` | 443 | `docker compose build` / `pull` | builds (the Dockerfile copies `uv` from ghcr.io) |
| `pypi.org`, `files.pythonhosted.org`, `registry.npmjs.org` | 443 | backend and frontend image builds | builds |
| `api-admin.billz.ai` | 443 | worker, api (`BILLZ_BASE_URL`) | `BILLZ_MODE=live` (docs/integrations/billz.md §2) |
| `api.click.uz` | 443 | worker, api (Merchant API: status, reversal) | `CLICK_MODE=live` (click.md) |
| `api.bts.uz` (production URL: confirm with BTS); `apitest.bts.uz` **port 28345** on staging | 443 / 28345 | worker, api | `BTS_MODE=live` (bts.md) |
| `notify.eskiz.uz` | 443 | worker | `ESKIZ_MODE=live` (eskiz.md) |
| `oauth2.googleapis.com`, `fcm.googleapis.com` | 443 | worker | `FCM_MODE=live` (fcm.md) |
| `oauth2.googleapis.com`, `www.googleapis.com` | 443 | api | `OAUTH_MODE=live` (Google sign-in) |
| `graph.facebook.com`, `limited.facebook.com` | 443 | api | `OAUTH_MODE=live` (Facebook sign-in) |
| `appleid.apple.com` | 443 | api, worker | `OAUTH_MODE=live` with `APPLE_BUNDLE_IDS` |
| the `ALERT_WEBHOOK_URL` host | 443 | worker | when set (monitoring.md §1) |
| the off-host backup target (in Uzbekistan) | its port | host (restic) | §11 |

Payme makes no outbound call (its Merchant API calls us); `my.click.uz`, `checkout.paycom.uz` and `test.paycom.uz`
are opened by the customer's phone, not by the server.

## 3. First deploy

Run the steps in order, as root.

**3.1 Layout.**

```bash
install -d -m 0755 /srv/glowderma && cd /srv/glowderma
git clone git@github.com:GlowDermatology/backend.git
git clone git@github.com:GlowDermatology/frontend.git    # the compose build context ../frontend
install -d -m 0700 -o root -g root /srv/backups           # backups hold every personal record (§11)
cd backend
```

**3.2 `.env`.** Copy the template and fill it; it documents every variable (what it is, whether it is required and
under which `*_MODE`, the default, whether it is secret, the generator):

```bash
cp deploy/env.production.example .env && chmod 600 .env
```

[`deploy/env.production.example`](../../deploy/env.production.example) is built so that an unedited copy never boots:
the three infrastructure passwords are empty and `compose.prod.yaml` refuses to start without them (before
PostgreSQL or Redis is initialised with anything), and each other required secret is a `change-me` placeholder that
the production settings refuse (as they refuse the values ever committed to this repository, short keys and malformed
ones; the error names the variable, never its value). Rules: one `NAME=value` per line, LF line endings; Compose
interpolates `$` in values, so a provider-issued value containing `$` is written with `$$` for each `$` or wrapped in
single quotes (`'…'` is literal). Generate the secrets:

| Variable | Generator | Notes |
| --- | --- | --- |
| `SECRET_KEY` | `python3 -c "import secrets; print(secrets.token_urlsafe(50))"` | |
| `INTERNAL_API_SECRET` | `python3 -c "import secrets; print(secrets.token_urlsafe(48))"` | compose also hands it to the frontend as `GLOWDERMA_INTERNAL_SECRET` |
| `JWT_SIGNING_KEYS` | `{"k1":"<python3 -c "import secrets; print(secrets.token_urlsafe(32))">"}`, `JWT_ACTIVE_KID=k1` | rotate by adding a kid (§14) |
| `POSTGRES_PASSWORD`, `POSTGRES_APP_PASSWORD`, `REDIS_PASSWORD` | `openssl rand -hex 24` each | hex only: embedded in URLs. `POSTGRES_PASSWORD` is the superuser's (administration), `POSTGRES_APP_PASSWORD` the application role's |
| `SMS_CODE_HMAC_KEY`, `LOG_PII_KEY`, `PAYMENT_REF_KEY`, `QR_HMAC_KEY`, `BTS_WEBHOOK_SECRET` | `openssl rand -hex 32` each (≥ 32 characters) | never rotate `PAYMENT_REF_KEY` with open payments; `BTS_WEBHOOK_SECRET` goes into the BTS webhook URL |
| `SECRETBOX_KEY` | `python3 -c "import secrets; print(secrets.token_urlsafe(32))"` | base64url of exactly 32 bytes; never rotate (sealed Apple tokens) |
| `CLICK_SECRET_KEY`, `PAYME_KEY` | issued by Click / Payme; until then `openssl rand -hex 16` | required in every mode |

Compose derives the topology settings itself — `DJANGO_SETTINGS_MODULE`, `DATABASE_URL`, `REDIS_URL`,
`CELERY_BROKER_URL`, `ALLOWED_HOSTS`, `ADMIN_API_ALLOWED_NETS`, `TRUSTED_PROXIES`, `PUBLIC_MEDIA_BASE_URL`,
`GOOGLE_OAUTH_REDIRECT_URI`, `FACEBOOK_OAUTH_REDIRECT_URI`, `GIT_SHA` — and its values win over `.env`; the template
lists them as "set by compose — do not set". `GIT_SHA` and `FRONTEND_GIT_SHA` are appended to `.env` by
`deploy/pin-release.sh` (§3.4). Decide the provider posture now (§8): the template starts with every provider `fake`.

If a `docker compose up` ever ran with a wrong `POSTGRES_PASSWORD`, the volume keeps it: on a fresh install remove the
volume (`docker compose -f compose.prod.yaml down && docker volume rm glowderma_pgdata`) and start again; on a volume
with data use `ALTER ROLE glowderma PASSWORD '…'` in `psql` (§14).

**3.3 Secrets directory** (FCM service account, Sign in with Apple key). Compose mounts
`GLOWDERMA_SECRETS_DIR` (default `/srv/glowderma/secrets`) read-only at `/run/secrets/glowderma` in api, worker and beat;
the production settings read the key files at boot in every one of them. Create it even when nothing is live yet:

```bash
install -d -m 0750 -o root -g 10001 /srv/glowderma/secrets          # 10001 = the image's "app" user
# when FCM_MODE=live:  FCM_CREDENTIALS_FILE=/run/secrets/glowderma/fcm-service-account.json
install -m 0440 -o root -g 10001 fcm-service-account.json /srv/glowderma/secrets/
# when OAUTH_MODE=live with APPLE_BUNDLE_IDS:  APPLE_PRIVATE_KEY_FILE=/run/secrets/glowderma/apple-signin.p8
install -m 0440 -o root -g 10001 AuthKey_XXXXXXXXXX.p8 /srv/glowderma/secrets/apple-signin.p8
```

Never commit these files or copy them into the repository checkout; delete the downloaded originals after copying.

**3.4 Build, migrate, create the owner, start.**

```bash
cd /srv/glowderma/backend
deploy/pin-release.sh                                            # GIT_SHA / FRONTEND_GIT_SHA of both checkouts -> .env
docker compose -f compose.prod.yaml config --quiet               # .env complete for compose (domains, passwords, tags)
docker compose -f compose.prod.yaml build api                    # one image at a time: a build is the memory peak
docker compose -f compose.prod.yaml build frontend               # (build args from .env)
docker compose -f compose.prod.yaml up -d postgres redis          # an empty volume: deploy/postgres creates glowderma_app
docker compose -f compose.prod.yaml run --rm api migrate          # as glowderma_app: extensions, collation, triggers, every migration
docker compose -f compose.prod.yaml run --rm api manage createstaff owner@glowderma.uz --superuser --full-name "Owner"
docker compose -f compose.prod.yaml up -d --wait
```

- The image tags live in `.env`, so every later command — also in a new shell, from cron or in a rollback — runs the
  release that was built; without them compose refuses to start instead of building an untagged `:latest`.
- `migrate` runs under the production settings, so it is also the first boot check: a missing or placeholder secret
  stops it with `ImproperlyConfigured: <NAME> …` before anything is written. It runs as `glowderma_app`, the database
  owner: `pg_trgm` and `btree_gist` are trusted extensions, so no superuser is needed (CI runs every migration as
  `glowderma_app` on PostgreSQL 18; the fresh install and the M1 hand-over of §5 were also run on 16).
- A build on the VPS itself competes with the running stack for memory; the containers' memory limits keep a build
  from pushing PostgreSQL out. On a host below 4 GB build elsewhere and load the images
  (`docker save glowderma-backend:<sha> | ssh root@vps docker load`, same for the frontend).
- `createstaff` prompts for the password twice (≥ 12 characters, validated) or reads it with `--password-stdin`. Create
  the other staff accounts the same way, without `--superuser`.

**3.5 Admin setup** (in `https://ADMIN_DOMAIN/admin`, signed in as the owner):

1. **Cashback policy v1.** *Cashback sozlamalari*: save version 1. Until a version is in effect, purchases earn no
   cashback (the engine treats "no policy" as zero).
2. **Site settings.** *Sozlamalar*: phones, links, pickup address, policy texts.
3. **POS keys** (one per cashier terminal; the key is printed **once** — run it in a private terminal and enter the key
   in the till directly, never paste it into chat or tickets):

   ```bash
   docker compose -f compose.prod.yaml exec api python manage.py createposkey --name "Kassa 1" --location "Chilonzor" --actor owner@glowderma.uz
   docker compose -f compose.prod.yaml exec api python manage.py rotateposkey <prefix> --actor owner@glowderma.uz   # new key, old one stops working
   docker compose -f compose.prod.yaml exec api python manage.py revokeposkey <prefix> --actor owner@glowderma.uz   # device lost or retired
   ```

   The prefix is printed by `createposkey` (`gdpos_<prefix>_…`). Terminals call
   `POST https://API_DOMAIN/api/v1/pos/cashback/resolve-qr/` with `Authorization: Api-Key <key>`.
4. **Staff social sign-in** (optional, `OAUTH_MODE=live`): register exactly
   `https://ADMIN_DOMAIN/admin/auth/oauth/google/callback` and `…/facebook/callback` with the providers, set
   `STAFF_OAUTH_PROVIDERS`, then each staff member links an identity from their account card.

**3.6 Then:** the smoke test (§13), the backup cron and one test restore (§11), the provider go-live checklists for
everything that must be live (§9), and the compliance gate before real customers (§12).

## 4. Upgrades (every release)

Read the release notes first (`.env` additions, server steps, whether its migrations are marked online-safe):

```bash
cd /srv/glowderma/backend
git fetch && git log --oneline HEAD..origin/main
```

Prepare while the previous release serves. Each block runs under `bash -euo pipefail`, so the first failing command
stops it (above all a backup that did not complete):

```bash
bash -euo pipefail <<'PREPARE'
deploy/backup.sh pre-upgrade                  # verified dump: the rollback path across migrations (§6)
git pull --ff-only
git -C ../frontend pull --ff-only
deploy/pin-release.sh                         # the new tags -> .env
# "<" lines: variables new in the template (add them to .env before the next block)
diff <(grep -oE '^#?[A-Z][A-Z0-9_]*=' deploy/env.production.example | tr -d '#=' | sort -u) \
     <(grep -oE '^#?[A-Z][A-Z0-9_]*=' .env | tr -d '#=' | sort -u) || true
docker compose -f compose.prod.yaml config --quiet
docker compose -f compose.prod.yaml build api
docker compose -f compose.prod.yaml build frontend
PREPARE
```

If `config --quiet` stops the block (a variable the new release requires is not in `.env` yet), add what the diff
listed and run the block again. Then the release, a short maintenance window for the API (the landing and admin pages show errors meanwhile):

```bash
bash -euo pipefail <<'RELEASE'
docker compose -f compose.prod.yaml stop api worker beat      # no writer of the previous release meets the new schema
docker compose -f compose.prod.yaml run --rm api migrate      # on EVERY release, before up
docker compose -f compose.prod.yaml up -d --wait              # recreates the changed services, waits until healthy
docker compose -f compose.prod.yaml exec edge caddy reload --config /etc/caddy/Caddyfile --adapter caddyfile
RELEASE
docker image prune -f
```

Then run the smoke test (§13). Notes:

- **Why the writers stop.** Django adds a column declared with `default=` as `NOT NULL` and then drops the database
  default (`accounts.0006`, `accounts.0007`, `payments.0004` do exactly that), so an INSERT of the previous code fails
  between `migrate` and `up -d`. Only when the release notes mark every migration of the release **online-safe**
  (new tables, nullable columns or `db_default`, `CONCURRENTLY` indexes) may the `stop` line be left out, so the
  previous release keeps serving during `migrate`.
- **If `migrate` fails** the block stops with api, worker and beat stopped: fix the cause and run the block again
  (an INVALID index: §5 "INVALID index recovery"), or roll back (§6).
- **The edge.** `up -d` recreates the edge only when its compose definition changes; the `caddy reload` line applies a
  changed `deploy/Caddyfile` (compose mounts the `deploy/` directory, so the pulled file is visible in the container)
  without dropping connections, and does nothing when the file is unchanged. A Caddyfile that does not load leaves the
  running configuration in place and the command fails.
- The pre-upgrade dump is the rollback path across migrations (§6); keep it until the release is confirmed, then
  delete it (`/srv/backups/db-pre-upgrade-*.dump`; the nightly pruning does not touch it).
- The frontend build bakes `SITE_DOMAIN`, `ADMIN_DOMAIN` and `API_DOMAIN`: changing a domain needs `build` again.
- Open admin tabs from the previous frontend build do a full reload on their next navigation (Next `deploymentId`).
- Changing only `.env` (a retention period, a provider credential): `docker compose -f compose.prod.yaml up -d api
  worker beat` recreates the backend containers with it (the image tags come from `.env`, so a new shell is fine). The
  frontend reads `INTERNAL_API_SECRET` and the staff sign-in variables (`up -d frontend` after changing them); a
  domain change needs `build` (above).

## 5. Upgrading an M1 deployment to M3 (once)

An M1 stack (`feat/ops-deploy` on the M1 backend) lacks the M2 customer-API keys, and the M3 settings refuse to start
without them; its network layout and database role also change. In this order (the M1 stack keeps serving until
step 4):

1. **Pull first** (the M1 checkout has no `deploy/backup.sh`; the running containers are not affected):
   `git pull --ff-only && git -C ../frontend pull --ff-only`.
2. **`.env` additions** (generators in §3.2): `SMS_CODE_HMAC_KEY`, `LOG_PII_KEY`, `PAYMENT_REF_KEY`, `QR_HMAC_KEY`,
   `SECRETBOX_KEY`, `BTS_WEBHOOK_SECRET`, `CLICK_SECRET_KEY`, `PAYME_KEY`, and `POSTGRES_APP_PASSWORD`,
   `REDIS_PASSWORD`. Keep `POSTGRES_PASSWORD` as it is (it stays the superuser's password). Remove
   `OAUTH_REDIRECT_URI` (deprecated: compose now sets `GOOGLE_OAUTH_REDIRECT_URI` / `FACEBOOK_OAUTH_REDIRECT_URI` from
   `ADMIN_DOMAIN`). Raise `CELERY_CONCURRENCY` to at least 3 (the template's default is 4). Review the new optional
   groups (memory limits, customer API, mobile apps, providers, alerts, retention, consistency report): their defaults
   are production values.
3. **Secrets directory**: create `/srv/glowderma/secrets` (§3.3) — compose now mounts it. Create `/srv/backups`
   (§3.1) if the M1 host has none. Then run the **PREPARE block of §4** (verified dump, tags, config, build).
4. **The switch**, instead of the RELEASE block of §4 (the whole stack stops for a few minutes: announce a
   maintenance window):

   ```bash
   bash -euo pipefail <<'M3'
   docker compose -f compose.prod.yaml down                 # every M1 container; the volumes stay
   docker network rm glowderma_internal                     # the M1 network: its subnet is "front" now
   docker compose -f compose.prod.yaml up -d --wait postgres redis
   docker compose -f compose.prod.yaml exec postgres /docker-entrypoint-initdb.d/10-app-role.sh   # glowderma_app takes over the M1 objects
   docker compose -f compose.prod.yaml run --rm api migrate
   docker compose -f compose.prod.yaml up -d --wait          # also recreates the edge with the M3 Caddyfile
   M3
   ```

   `10-app-role.sh` (idempotent) creates `glowderma_app`, hands it the database and every table, sequence, function and
   collation the superuser created in M1; from then on the application never connects as the superuser. The
   migrations below then run as `glowderma_app`:

   | Migration | What it does | Operator note |
   | --- | --- | --- |
   | `accounts.0006_customer_auth_hardening` | M2 customer auth fields, backfills `history_from` | reverse refused (lossy); `NOT NULL` columns without a database default |
   | `accounts.0007_session_auth_method_identity_client` | how a session's sign-in was proven; OAuth client of a sealed token | `NOT NULL` columns without a database default |
   | `commerce.0004_cartitem_price_at_add` | cart price at add | |
   | `commerce.0005_orderitem_image_path` | order item photos stored as media paths (data step) | |
   | `commerce.0006_stockreservation_billz_settled_at` | settled marker + partial index | |
   | `commerce.0007_settle_billz_reservations` | settles reservations the availability rule no longer counts (SQL) | |
   | `commerce.0008_backfill_refund_manual_reason` | fills `Refund.manual_reason` (SQL) | after `payments.0004` |
   | `core.0009_outboxmessage_deferred_since` | outbox deferral timestamp (Billz closed shift) | |
   | `core.0010_outbox_unfinished_idx` | **`CREATE INDEX CONCURRENTLY outbox_unfinished_idx`** (non-atomic) | INVALID recovery below |
   | `loyalty.0003_posdevice_key_prefix_unique` | POS key prefix unique | stops with the device ids if two devices share a prefix: see below |
   | `loyalty.0004_purchase_billz_payment_attempted_at` | Billz payment-attempt stamp | |
   | `loyalty.0005_purchase_unpushed_online_idx` | **`CREATE INDEX CONCURRENTLY purchase_unpushed_online_idx`** (non-atomic) | INVALID recovery below |
   | `notifications.0003_inbox_account_source` | inbox rows of account-security notices | reverse refused once such rows exist |
   | `notifications.0004_campaign_cancelled` | campaign state `cancelled` | |
   | `payments.0002_payment_active_excludes_late_capture` | one active payment per order ignores late captures | |
   | `payments.0003_payment_late_capture_open_idx` | **`CREATE INDEX CONCURRENTLY payment_late_capture_open_idx`** (non-atomic) | INVALID recovery below |
   | `payments.0004_refund_manual_reason` | `Refund.manual_reason` | `NOT NULL` column without a database default |

   **Shared POS prefixes** (`loyalty.0003` stops and names the device ids; practically unreachable, M1 had no command
   that creates POS devices). The migration counts every device row, active or not, so revoking one is not enough: give
   every duplicate but one its own prefix and deactivate it, then create new keys for those terminals after the upgrade:

   ```bash
   docker compose -f compose.prod.yaml exec postgres psql -U glowderma -d glowderma -c \
     "UPDATE loyalty_posdevice SET key_prefix = 'dup' || id, is_active = false WHERE id IN (<ids from the error but one>);"
   docker compose -f compose.prod.yaml run --rm api migrate
   ```

   **INVALID index recovery.** If a concurrent build fails or is interrupted (deadlock, cancelled `migrate`, container
   killed, lock timeout), PostgreSQL keeps an INVALID index, Django does not record the migration, and the next
   `migrate` fails with `relation "…" already exists`. Then, for the three indexes above only:

   ```bash
   # 1. which of the three is INVALID, and that no CREATE INDEX is still running (a second migrate, a killed client)
   docker compose -f compose.prod.yaml exec postgres psql -U glowderma -d glowderma -c \
     "SELECT indexrelid::regclass AS index FROM pg_index WHERE NOT indisvalid AND indexrelid::regclass::text IN ('outbox_unfinished_idx', 'purchase_unpushed_online_idx', 'payment_late_capture_open_idx');" \
     -c "SELECT pid, state, query FROM pg_stat_activity WHERE query ILIKE 'CREATE INDEX%' AND pid <> pg_backend_pid();"
   # 2. only when step 1 lists the index and no running build: drop it (one statement per index, outside a transaction)
   docker compose -f compose.prod.yaml exec postgres psql -U glowderma -d glowderma -c \
     "DROP INDEX CONCURRENTLY IF EXISTS <index from step 1>;"
   # 3. migrate again (with the image tags of .env: the new release)
   docker compose -f compose.prod.yaml run --rm api migrate
   ```

   and check that the first query of step 1 returns no row (docs/ops/monitoring.md §6). Never drop another INVALID
   index this way: find out who builds it first.
5. **Beat changes**: the loyalty reconciliation (03:10 in M1) now runs inside the nightly consistency report (03:40);
   new entries are listed in §7. Nothing to do: beat reads `config/celery.py`.
6. **Backups**: replace the M1 cron entry with `deploy/glowderma-backup.cron` (§11), then the smoke test (§13).

## 6. Rollback

- **Code only** (the release added no migration): check out the previous tag in both repositories, write its tags
  and start it; the previous images are still local unless pruned (otherwise `build` first):

  ```bash
  git checkout <previous tag> && git -C ../frontend checkout <previous frontend tag>
  deploy/pin-release.sh                     # or: deploy/pin-release.sh <backend sha> <frontend sha>
  docker compose -f compose.prod.yaml up -d --wait
  docker compose -f compose.prod.yaml exec edge caddy reload --config /etc/caddy/Caddyfile --adapter caddyfile
  ```
- **Across migrations**: older code does not know the new columns (an M3 column without a database default makes old
  inserts fail) and several M3 migrations refuse their reverse. Restore the pre-upgrade dump instead of reversing
  (§11 "Restore": it first saves the current database as `db-pre-restore-*.dump`), then check out the previous tags as
  above. Orders, payments and callbacks received since the upgrade are not in the restored database: reconcile them
  from that pre-restore dump, the provider cabinets (Click, Payme, BTS, Billz) and the webhook senders' retries.
- Never roll back across a release whose notes say "data rewritten" without the dump of that release.

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
  `docker compose -f compose.prod.yaml exec api python manage.py billz_mark_recorded --order <id> --actor <staff email> [--billz-sale-id <uuid>] --dry-run`,
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

## 10. Monitoring and alerts

[`docs/ops/monitoring.md`](monitoring.md) is the operator reference: the ERROR events to alert on (§2), the optional
signed alert webhook (`ALERT_WEBHOOK_URL`, `ALERT_WEBHOOK_SECRET`, §1), the nightly consistency report K1–K6 and what
to do per check (§3), health and readiness (§4).

- **Liveness** `GET https://API_DOMAIN/api/v1/health/` → `200 {"status": "ok"}` while gunicorn serves. The api
  container's healthcheck uses it (not `?ready=1`): a readiness 503 — a Redis or broker blip, an unconfigured live
  provider — would mark the container unhealthy and keep `docker compose up` from starting the frontend and the edge,
  taking the admin panel down when it is needed to fix things; Docker does not restart unhealthy containers anyway.
- **Readiness** `GET https://API_DOMAIN/api/v1/health/?ready=1` → `200`, or `503` when the database, the cache or the
  broker fails or a live provider is not configured. Point the external uptime monitor at it. Details (modes, sandbox,
  per-provider `configured` and `last_sync_age_seconds`) are shown only inside the admin network gate; from the host:

  ```bash
  docker compose -f compose.prod.yaml exec api python manage.py shell -c "from glowderma.core import integration_modes as m, readiness as r; print(m.provider_modes(), m.sandbox_providers(), r.live_configured(), r.last_syncs())"
  ```
- **Logs**: `docker compose -f compose.prod.yaml logs -f --tail=200 api worker beat` (JSON on the edge, key=value with
  `request_id` in Django; rotated 5 × 20 MB per container). Alert on the ERROR events of monitoring.md §2 from whatever
  reads the container logs; it must run in Uzbekistan or receive scrubbed data only (compliance.md §1.2).
- **Backups**: alert when no fresh nightly dump exists, e.g. a check run by the monitor's agent or cron:
  `find /srv/backups -name 'db-nightly-*.dump' -mmin -1560 | grep -q .` (exit 1 = none in the last 26 hours), and read
  `/var/log/glowderma-backup.log` after a failure.
- **Consistency report on demand**: `docker compose -f compose.prod.yaml exec api python manage.py consistency_report`
  (`--json` for the whole report).
- **Dead outbox messages**: `… manage.py requeue_outbox --topic <topic> --dead --dry-run` lists them; replay without
  `--dry-run` once the cause is fixed. To close one handled by hand, first
  `… requeue_outbox --topic <topic> --dead --resolved --id <message id> --dry-run` (check the listed message), then the
  same without `--dry-run`: `--resolved` marks it done without running its handler.

## 11. Backups and restore

`deploy/backup.sh` writes a custom-format `pg_dump` (and, nightly, a tarball of the media volume) to `/srv/backups`
(root, 0700; files 0600). Each file is written as `*.tmp`, read back in full (`pg_restore`, `gzip -t`, `tar -t`) and
only then renamed, so a failed or partial backup never sits under a final name; any failure exits non-zero with a
message. Nightly files older than 14 days are removed after a successful run (`GLOWDERMA_BACKUP_KEEP_DAYS`);
pre-upgrade and pre-restore dumps are removed by hand. Backups contain all personal data: the host and every copy stay
in Uzbekistan (compliance.md §2).

**Install the nightly run** (cron uses the host clock; on a UTC host — check `timedatectl` — 21:15 is 02:15 Tashkent,
before the nightly jobs at 03:00–04:23 Tashkent):

```bash
install -d -m 0700 -o root -g root /srv/backups
install -m 0644 -o root -g root deploy/glowderma-backup.cron /etc/cron.d/glowderma-backup   # one line per entry
deploy/backup.sh nightly                                  # once by hand: prints the two files it wrote
```

The next morning check that `/srv/backups` has a new `db-nightly-*.dump` and `media-*.tgz` and that
`/var/log/glowderma-backup.log` shows no `FAILED` line (§10 has a freshness check for the monitor).

**Off-host copy** (encrypted, to a target in Uzbekistan). [restic](https://restic.net) encrypts every snapshot with
the repository password before it leaves the host:

```bash
apt-get install -y restic
install -m 0600 /dev/null /root/.restic-password && openssl rand -hex 32 > /root/.restic-password   # keep a copy with .env
export RESTIC_REPOSITORY=sftp:backup@<uzbek-backup-host>:/glowderma RESTIC_PASSWORD_FILE=/root/.restic-password
restic init
# add ONE line to /etc/cron.d/glowderma-backup (after the 21:15 run):
# 45 21 * * * root RESTIC_REPOSITORY=sftp:backup@<uzbek-backup-host>:/glowderma RESTIC_PASSWORD_FILE=/root/.restic-password restic backup --quiet /srv/backups >>/var/log/glowderma-backup.log 2>&1 && RESTIC_REPOSITORY=sftp:backup@<uzbek-backup-host>:/glowderma RESTIC_PASSWORD_FILE=/root/.restic-password restic forget --quiet --prune --keep-daily 14 --keep-monthly 6 >>/var/log/glowderma-backup.log 2>&1
```

The off-host retention (`--keep-daily 14 --keep-monthly 6` above) is a placeholder until open question Q6 is
decided. Keep `.env`, `/srv/glowderma/secrets` and the restic password in a separate, access-controlled place (a
password manager or an encrypted archive in Uzbekistan): a database dump without `SECRETBOX_KEY`/`PAYMENT_REF_KEY`
cannot serve its sealed tokens and open payments.

**Restore** (a rollback across migrations, or a damaged database). It replaces the database: first save the current
one (the source for reconciling what the restore removes), stop every writer, restore in one transaction, and start the
stack only if the restore succeeded:

```bash
cd /srv/glowderma/backend
bash -euo pipefail <<'RESTORE'
dump=/srv/backups/db-nightly-YYYY-MM-DD-HHMMSS.dump           # or db-pre-upgrade-….dump
media=""                                                       # media-YYYY-MM-DD-HHMMSS.tgz only when the media volume is lost
test -s "$dump"
deploy/backup.sh pre-restore                                   # the current database, for reconciliation
docker compose -f compose.prod.yaml stop frontend api worker beat
docker compose -f compose.prod.yaml exec -T postgres pg_restore -U glowderma -d glowderma \
  --clean --if-exists --exit-on-error --single-transaction < "$dump"
if [ -n "$media" ]; then
  docker compose -f compose.prod.yaml run --rm --no-deps -T -v /srv/backups:/backup:ro --entrypoint tar api \
    -xzf "/backup/$media" -C /app/media
fi
docker compose -f compose.prod.yaml up -d --wait
RESTORE
```

`--single-transaction` makes a failed restore change nothing (`--exit-on-error` stops at the first error); the block
then stops before `up -d`, and the stack stays stopped until the cause is fixed. Ownership (`glowderma_app`) is restored with the objects. Append-only tables (ledger, order events,
audit log, webhook inbox) have triggers that refuse `UPDATE`/`DELETE`; `pg_restore --clean` drops and recreates them,
which the triggers do not block. Test a restore on a scratch VPS at least once before go-live.

## 12. Compliance gate (before real customers)

[`docs/ops/compliance.md`](compliance.md) is the gate of M3. Before the first real customer:

1. **Hosting in Uzbekistan** (§2): VPS, backups (local and off-host), logs and any log/metrics shipping, the alert
   receiver — confirmed in writing.
2. **Database registration** (§5): the operator registers the personal-data database with the authorised state body
   (with counsel), using §1 of compliance.md as the description; keep the confirmation with that document and re-file
   when a provider, purpose or hosting location changes.
3. **Open questions** Q1–Q10 (§6) answered by the product owner (retention of the audit log, orders, backups …).
4. **Retention** settings left at their contractual defaults unless decided otherwise (`WEBHOOK_INBOX_RETENTION_DAYS=180`).

## 13. Smoke test (after every deploy and upgrade)

```bash
cd /srv/glowderma/backend
API=https://$(sed -n 's/^API_DOMAIN=//p' .env) ADMIN=https://$(sed -n 's/^ADMIN_DOMAIN=//p' .env)
curl -fsS "$API/api/v1/health/?ready=1"                    # 200, "status": "ok", checks ok, "version" = GIT_SHA
curl -fsS "$ADMIN/healthz"                                   # {"status":"ok","version":"<frontend sha>"}
curl -s -o /dev/null -w '%{http_code}\n' "$API/api/v1/admin/auth/me/"      # 404 (edge)
curl -s -o /dev/null -w '%{http_code}\n' "$API/api/v1/docs/"               # 404 (edge)
curl -s -w '\n%{http_code}\n' -X POST "$API/api/v1/webhooks/bts/"          # {"ok": false} 401: the callback reaches Django
curl -fsS "$API/api/v1/settings/public/"                    # payment_providers, fulfilment_methods, online_sales_enabled
docker compose -f compose.prod.yaml ps                      # api, frontend, worker, postgres, redis healthy; beat, edge running
docker compose -f compose.prod.yaml exec api python manage.py check --deploy
docker compose -f compose.prod.yaml exec worker celery -A config inspect ping
docker compose -f compose.prod.yaml exec api python manage.py consistency_report
```

- `check --deploy` reports `security.W004` (Django's own HSTS is off): expected, the edge sends
  `Strict-Transport-Security` on every host.
- The webhook probe writes one `rejected` inbox row (bounded per IP); the other callbacks answer their own protocol
  errors without a valid signature.
- `consistency_report` prints one line per check; on a new stack every count is 0 (K1 is `skipped: billz_not_live` while
  Billz is fake).
- **Admin login**: sign in at `https://ADMIN_DOMAIN/admin` with a staff account; the dashboard loads; *Sozlamalar*
  shows the integrations mode ("Integratsiyalar").
- **An order end to end**:
  - *Fake posture*: the production settings cannot take an order (§8: no online selling, no payment provider, no SMS),
    by design. Check the posture instead: `settings/public` shows `online_sales_enabled: false`, `payment_providers: []`,
    `fulfilment_methods: ["pickup"]`. The fake order journey (checkout → `POST /api/v1/dev/payments/{order_id}/simulate/`
    → paid → BTS fake → delivered) runs on the local stack (`compose.yaml`, local settings, `FAKE_PAYMENTS_ENABLED=true`)
    before the release is tagged.
  - *Live*: one real low-value order paid with Click and one with Payme, delivered or picked up; the sale appears in
    Billz, the push arrives, then cancel/refund one of them and follow the refund (Payments screen).

## 14. Operations

| Task | Command (`docker compose -f compose.prod.yaml …`) |
| --- | --- |
| Logs | `logs -f --tail=200 api worker beat edge` |
| Django shell | `exec api python manage.py shell` |
| SQL | `exec postgres psql -U glowderma -d glowderma` (the superuser, over the container's socket; the backend image has no `psql`) |
| Redis | `exec redis redis-cli` (authenticated through `REDISCLI_AUTH`) |
| Celery workers alive | `exec worker celery -A config inspect ping` |
| More API capacity | raise `GUNICORN_WORKERS` (≈ 2 × vCPU) / `GUNICORN_THREADS` and `API_MEM_LIMIT` in `.env`, then `up -d api` |
| More task throughput | raise `CELERY_CONCURRENCY` (≥ 3) and `WORKER_MEM_LIMIT` (about 150 MB per process), then `up -d worker` |
| A staff account | `run --rm api manage createstaff <email> [--full-name "…"]` |
| POS keys | `exec api python manage.py createposkey / rotateposkey / revokeposkey` (§3.5) |
| Billz sale entered by hand / replay | `billz_mark_recorded`, `requeue_outbox --topic billz.push_sale --dead …` (§9; `--dry-run` first) |
| Dead outbox messages | `exec api python manage.py requeue_outbox --topic <topic> --dead --dry-run` |
| Consistency report | `exec api python manage.py consistency_report [--json]` |
| Rotate the JWT key | add `"k2":"<new>"` to `JWT_SIGNING_KEYS`, `JWT_ACTIVE_KID=k2`, `up -d api worker beat`; remove `k1` after 15 minutes |
| Change `POSTGRES_APP_PASSWORD` | edit `.env`, `up -d postgres`, `exec postgres /docker-entrypoint-initdb.d/10-app-role.sh`, `up -d --wait` |
| Change `POSTGRES_PASSWORD` | `exec postgres psql -U glowderma -d glowderma -c "ALTER ROLE glowderma PASSWORD '<new>'"`, then edit `.env` and `up -d postgres` |
| Change `REDIS_PASSWORD` | edit `.env`, `up -d --wait` (recreates redis and every backend container) |
| Renew TLS | automatic (Caddy); certificates live in the `caddy_data` volume |
| Apply a changed Caddyfile | `exec edge caddy reload --config /etc/caddy/Caddyfile --adapter caddyfile` |

- **Outbox.** Provider calls (BTS, Billz, refunds, pushes, SMS, alerts) leave through `core.OutboxMessage`. Messages
  that exhaust their retries become `dead`, raise `outbox.dead` (audit + alert) and stay until replayed or resolved.
- **Django admin** is off (`DJANGO_ADMIN_ENABLED=false`) and, even when enabled, accepts only the frontend container's
  address with the internal secret. Use the shell commands above for data inspection.
- **Media** lives in the `media` volume, shared by api, worker and (read-only) the edge.

## 15. Environment reference

[`deploy/env.production.example`](../../deploy/env.production.example) is the reference: every variable the production
settings (`config/settings/base.py`, `production.py`, `config/gunicorn.conf.py`) and `compose.prod.yaml` read, in these
groups (`glowderma/core/tests/test_deploy_env_completeness.py` fails when a variable is missing from it):

| Group | Required in every mode | Required when | Optional (defaults are production values) |
| --- | --- | --- | --- |
| 0. Domains and stack | `SITE_DOMAIN`, `ADMIN_DOMAIN`, `API_DOMAIN`, `ACME_EMAIL`; `GIT_SHA`, `FRONTEND_GIT_SHA` (written by `deploy/pin-release.sh`) | | `GLOWDERMA_SECRETS_DIR`, `FRONTEND_CONTEXT`, `API_MEM_LIMIT`, `WORKER_MEM_LIMIT`, `FRONTEND_MEM_LIMIT` |
| 1. Core and security | `SECRET_KEY` (S), `INTERNAL_API_SECRET` (S), `JWT_SIGNING_KEYS` (S), `JWT_ACTIVE_KID` | `seed_demo` on staging: `SEED_ADMIN_PASSWORD` (S) | `DJANGO_ADMIN_ENABLED`, `SECURE_*`, `LOG_LEVEL`, `THROTTLE_RATES`, `GUNICORN_*` |
| 2. Database, Redis, Celery | `POSTGRES_PASSWORD` (S), `POSTGRES_APP_PASSWORD` (S), `REDIS_PASSWORD` (S) | | `DB_POOL`, `CELERY_CONCURRENCY`, `REDIS_MAXMEMORY` |
| 4. Staff sign-in | | `OAUTH_MODE=live`: `GOOGLE_OAUTH_CLIENT_ID`, `GOOGLE_OAUTH_CLIENT_SECRET` (S), `FACEBOOK_APP_ID`, `FACEBOOK_APP_SECRET` (S) | `OAUTH_MODE`, `STAFF_OAUTH_PROVIDERS`, `GOOGLE_ALLOWED_HD`, `STAFF_GOOGLE_AUTOLINK`, `FACEBOOK_GRAPH_VERSION` |
| 5. Customer API | `SMS_CODE_HMAC_KEY` (S), `LOG_PII_KEY` (S), `PAYMENT_REF_KEY` (S), `QR_HMAC_KEY` (S), `SECRETBOX_KEY` (S) | | sessions, OTP, cart/checkout, return URLs, QR/POS, booking, webhook limits |
| 6. Mobile apps | | `OAUTH_MODE=live` + `APPLE_BUNDLE_IDS`: `APPLE_TEAM_ID`, `APPLE_KEY_ID`, `APPLE_PRIVATE_KEY_FILE` (S) | `GOOGLE_APP_CLIENT_IDS`, `APPLE_BUNDLE_IDS`, `MIN_APP_VERSION_*`, store URLs |
| 6b. Customer website (B-W1) | | `OAUTH_MODE=live` + `APPLE_SERVICES_IDS`: `APPLE_WEB_REDIRECT_URI` (https) | `GOOGLE_WEB_CLIENT_IDS`, `APPLE_SERVICES_IDS`, `PAYMENT_WEB_RETURN_URL`, `GEOIP_CITY_DB` (a GeoLite2/DB-IP City `.mmdb` placed in `GLOWDERMA_SECRETS_DIR`, i.e. `/run/secrets/glowderma/GeoLite2-City.mmdb` in the containers; replaced monthly by an atomic `mv`, reopened within a minute) |
| 6c. E-mail codes (B-W2, hybrid identity) | | `EMAIL_MODE=smtp`: `EMAIL_HOST`, `EMAIL_FROM` (a real sender), `EMAIL_USE_TLS` or `EMAIL_USE_SSL`; `EMAIL_HOST_USER` and `EMAIL_HOST_PASSWORD` (S) together or neither | `EMAIL_MODE` (default `fake`: nothing delivered, startup WARNING and `email: fake` in `health/?ready=1`), `EMAIL_PORT`, `EMAIL_TIMEOUT`, `EMAIL_GLOBAL_HOURLY_LIMIT`, `EMAIL_OTP_IP_HOURLY_LIMIT`, `CUSTOMER_REAUTH_FRESH_SECONDS` |
| 7. Providers | `CLICK_SECRET_KEY` (S), `PAYME_KEY` (S), `BTS_WEBHOOK_SECRET` (S) | `BILLZ_MODE=live`: `BILLZ_SECRET_KEY` (S), `BILLZ_SHOP_ID`, `BILLZ_CASHBOX_ID`, `BILLZ_PAYMENT_TYPE_ID` · `CLICK_MODE=live`: `CLICK_SERVICE_ID`, `CLICK_MERCHANT_ID`, `CLICK_MERCHANT_USER_ID` · `PAYME_MODE=live`: `PAYME_MERCHANT_ID` · `BTS_MODE=live`: `BTS_LOGIN`, `BTS_PASSWORD` (S), `BTS_SENDER_NAME`, `BTS_SENDER_PHONE`, `BTS_SENDER_ADDRESS`, `BTS_SENDER_CITY_CODE` (+ `BTS_SENDER_BRANCH_CODE` for `branch`) · `ESKIZ_MODE=live`: `ESKIZ_EMAIL`, `ESKIZ_PASSWORD` (S), `ESKIZ_SENDER` · `FCM_MODE=live`: `FCM_PROJECT_ID`, `FCM_CREDENTIALS_FILE` (S) | `INTEGRATIONS_MODE`, each `*_MODE`, base URLs, rate limits, timeouts, allow-lists, polling, `CAMPAIGN_BATCH_SIZE` |
| 8. Alerts | | `ALERT_WEBHOOK_URL` set: `ALERT_WEBHOOK_SECRET` (S) | `ALERT_WEBHOOK_URL` (S), `ALERT_DEDUPE_MINUTES`, `ALERT_ENV` |
| 9. Retention | | | `WEBHOOK_INBOX_RETENTION_DAYS`, `OUTBOX_RETENTION_DAYS`, `RETENTION_BATCH_SIZE`, `OUTBOX_DEFERRAL_WINDOW_HOURS` |
| 10. Consistency report | | | `CONSISTENCY_*` (10 thresholds) |

(S) = secret. **Set by compose, never in `.env`**: `DJANGO_SETTINGS_MODULE`, `DATABASE_URL`, `REDIS_URL`,
`CELERY_BROKER_URL`, `ALLOWED_HOSTS`, `ADMIN_API_ALLOWED_NETS`, `TRUSTED_PROXIES`, `PUBLIC_MEDIA_BASE_URL`,
`GOOGLE_OAUTH_REDIRECT_URI`, `FACEBOOK_OAUTH_REDIRECT_URI`, `GIT_SHA` (from the `.env` line `pin-release.sh` writes).
**Refused by production**: `FAKE_NOW`, `FAKE_PAYMENTS_ENABLED`, `FAKE_SCENARIO_HEADER_ENABLED`, `ESKIZ_FAKE_CODE`,
`POS_API_KEYS`. `.env.example` is the local development template (local settings ship development keys).

**Frontend** (set by `compose.prod.yaml`, nothing to add): build args `NEXT_PUBLIC_SITE_URL`,
`GLOWDERMA_ALLOWED_ORIGINS`, `GLOWDERMA_MEDIA_HOSTS`, `GLOWDERMA_BUILD_SHA`; runtime `GLOWDERMA_DATA_SOURCE=api`,
`GLOWDERMA_API_URL=http://api:8000`, `GLOWDERMA_INTERNAL_SECRET`, `GLOWDERMA_ALLOWED_ORIGINS`,
`GLOWDERMA_COOKIE_SECURE=true`, and the staff sign-in buttons `GLOWDERMA_OAUTH_PROVIDERS` (`STAFF_OAUTH_PROVIDERS`),
`GLOWDERMA_OAUTH_MODE` (`OAUTH_MODE`), `GLOWDERMA_GOOGLE_CLIENT_ID`, `GLOWDERMA_FACEBOOK_APP_ID`.

## 16. Edge contract (if you replace Caddy)

Any other proxy (nginx, Traefik, a cloud load balancer) must keep all of these (`deploy/edge_probe.py` checks them on
the Caddyfile):

1. **One hop, X-Forwarded-For overwritten** with the TCP peer address (never appended to a client-supplied value),
   and the proxy's own address listed in `TRUSTED_PROXIES`. Django's per-IP throttles, the Click/Payme IP allow-lists
   and `AuditEntry.ip` depend on it. Keep `Host` unchanged and send `X-Forwarded-Proto`. The peer address must be the
   client's: IPv4 only on this stack (§2), or IPv6 end to end without a relaying proxy.
2. **No response buffering** towards Next (`proxy_buffering off;` in nginx; the app also sends
   `X-Accel-Buffering: no`). Admin pages stream.
3. **Body limits**: 1 MB on the landing host; 7 MB on the admin host (Server Actions with 5 MiB images; Next refuses
   above 6 MB); on the API host 1 MiB for `/api/v1/webhooks/` and `/api/v1/pos/` and 6 MiB elsewhere (Django's
   `DATA_UPLOAD_MAX_MEMORY_SIZE`; the profile image upload is ≤ 5 MiB).
4. **Request buffering and timeouts**: the request body is buffered at the edge before Django sees it — entirely for
   callbacks and POS, the first MiB elsewhere (`proxy_request_buffering on` in nginx) — so a client trickling its body
   never holds one of gunicorn's threads (3 × 4 by default); headers must arrive within 10 s and the whole request
   within 60 s.
5. **Private paths**: `/api/v1/admin/`, `/api/v1/schema/`, `/api/v1/docs/`, `/api/v1/dev/`, `/django-admin/` never
   forwarded from the internet; `/api/v1/webhooks/*` and `/api/v1/pos/*` always forwarded with the query string intact.
6. **Log redaction**, in the access logs **and** in error logs (a request the upstream could not answer): no query
   string (the BTS webhook's `token`, admin searches by phone, the OAuth callback's `code`/`state`) and no query string
   of the `Referer`; the `X-GD-Webhook-Token` and `X-GD-Internal-Secret` headers and `Authorization` never logged.
7. **Media**: `/media/` on the API host served from the media volume with `X-Content-Type-Options: nosniff` and a
   sandboxing `Content-Security-Policy`; no directory listing.
8. **Headers**: `Strict-Transport-Security` on every host (the `www` redirect included), no `Server` or `Via`.

## 17. Known limitations

- The Docker images were not built in the development environment (no Docker daemon there); the backend and frontend
  CI workflows build them, resolve `compose.prod.yaml` and validate and probe the Caddyfile. The compose details that
  need a daemon (the networks, the memory limits, the Redis start command, the init script on an empty volume) are
  exercised for the first time on staging: run §3 there first.
- The M1 hand-over of `deploy/postgres/app-role.psql` (objects the superuser created, then the M3 migrations as
  `glowderma_app`) was run on PostgreSQL 16 only; CI runs the fresh-install path on 18 at every push. The same
  privileges apply on both (trusted extensions since 13, `pg_database_owner` owning `public` since 15).
- The live adapters are built from the providers' public documentation and tested against recorded answers only; each
  `docs/integrations/<provider>.md` sandbox checklist must pass on staging before that provider goes live (§9).
- One VPS is a single point of failure. Backups (§11) are the recovery path; there is no standby.
