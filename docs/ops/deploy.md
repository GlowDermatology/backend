# Production deployment (Docker Compose on one VPS)

This runbook deploys the backend (this repository) and the frontend (`GlowDermatology/frontend`) as one Compose
project: `compose.prod.yaml`, `deploy/Caddyfile` and `deploy/env.production.example`.

## 1. Topology

```
                 internet :80 / :443 (tcp+udp)
                              │
                     ┌────────▼────────┐  172.30.0.10  (TLS, HTTP/3, X-Forwarded-For overwrite)
                     │  edge (Caddy)   │
                     └──┬───────────┬──┘
   SITE_DOMAIN,         │           │   API_DOMAIN: /api/v1/* (customer API, webhooks, POS, health),
   ADMIN_DOMAIN         │           │   /media/* served from the media volume (read-only)
             ┌──────────▼──┐     ┌──▼──────────┐
             │  frontend   │────▶│  api        │  gunicorn :8000
             │  Next :3000 │     │  (Django)   │  /api/v1/admin/* accepted ONLY from 172.30.0.20 + internal secret
             └─172.30.0.20─┘     └──┬───────┬──┘
                                    │       │      worker (Celery, N processes)   beat (Celery, exactly one)
                              ┌─────▼──┐ ┌──▼────┐
                              │postgres│ │ redis │  DB 0 = cache, DB 1 = Celery broker (AOF on)
                              └────────┘ └───────┘
                     private network 172.30.0.0/24 (dynamic addresses from .128/25)
```

- **SITE_DOMAIN** (e.g. `glowderma.uz`): the landing page. `www.` redirects to it; `/admin*` redirects to the admin
  host.
- **ADMIN_DOMAIN** (e.g. `admin.glowderma.uz`): the admin panel. The Next server is the BFF: it holds the staff
  session in HttpOnly `__Secure-` cookies and calls Django over the private network (`http://api:8000`).
- **API_DOMAIN** (e.g. `api.glowderma.uz`): the public Django surface for the mobile app, Click/Payme/BTS webhooks
  and POS devices, plus `/media/`. The edge answers `404` for `/api/v1/admin/`, `/api/v1/schema/`,
  `/api/v1/docs/` and `/django-admin/`; Django refuses them from any address but the frontend container as well.

Only the edge publishes ports. PostgreSQL and Redis are never reachable from outside the Docker network.

## 2. Prerequisites

| Item | Requirement |
| --- | --- |
| VPS | 2 vCPU / 4 GB RAM minimum (4 GB lets the frontend build run on the host); 40 GB disk plus media growth |
| OS | Any Linux with Docker Engine 25+ and the Compose plugin 2.24+ (`docker compose version`) |
| DNS | `A`/`AAAA` records for SITE_DOMAIN, `www.`SITE_DOMAIN, ADMIN_DOMAIN and API_DOMAIN pointing at the VPS |
| Firewall | Inbound 22/tcp (SSH), 80/tcp (ACME + redirect), 443/tcp, 443/udp (HTTP/3). Nothing else |
| Outbound | 443 to Let's Encrypt, Docker Hub, npm and PyPI (builds), and the providers in live mode |

Docker publishes ports through its own iptables chains, which bypass `ufw`. That is why no service except the edge
has a `ports:` entry; do not add one to PostgreSQL or Redis for convenience.

## 3. Layout and secrets

```bash
sudo mkdir -p /srv/glowderma && cd /srv/glowderma
git clone git@github.com:GlowDermatology/backend.git
git clone git@github.com:GlowDermatology/frontend.git    # the compose build context ../frontend
cd backend
cp deploy/env.production.example .env && chmod 600 .env
```

Fill `.env`. Every `change-me` must be replaced; production settings refuse to start otherwise.

| Variable | How to generate |
| --- | --- |
| `SECRET_KEY` | `python3 -c "import secrets; print(secrets.token_urlsafe(50))"` |
| `INTERNAL_API_SECRET` | `python3 -c "import secrets; print(secrets.token_urlsafe(48))"` (also becomes the frontend's `GLOWDERMA_INTERNAL_SECRET`) |
| `JWT_SIGNING_KEYS` | `{"k1":"<key>"}` with `python3 -c "import secrets,base64;print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())"`; `JWT_ACTIVE_KID=k1` |
| `POSTGRES_PASSWORD` | `openssl rand -hex 24` (hex only: it is embedded in `DATABASE_URL`) |

`compose.prod.yaml` derives the topology settings itself (`DATABASE_URL`, `REDIS_URL`, `CELERY_BROKER_URL`,
`ALLOWED_HOSTS`, `ADMIN_API_ALLOWED_NETS`, `TRUSTED_PROXIES`, `PUBLIC_MEDIA_BASE_URL`,
`DJANGO_SETTINGS_MODULE`). Do not define them in `.env`; the compose values win anyway.

## 4. First boot

```bash
cd /srv/glowderma/backend
export GIT_SHA=$(git rev-parse --short HEAD) FRONTEND_GIT_SHA=$(git -C ../frontend rev-parse --short HEAD)

docker compose -f compose.prod.yaml build                      # backend image + frontend image (build args from .env)
docker compose -f compose.prod.yaml up -d postgres redis
docker compose -f compose.prod.yaml run --rm api migrate        # creates pg_trgm, btree_gist, the ICU collation, triggers
docker compose -f compose.prod.yaml run --rm api manage createstaff owner@glowderma.uz --superuser --full-name "Owner"
docker compose -f compose.prod.yaml up -d
```

`createstaff` prompts for the password (or reads it with `--password-stdin`). The migration step needs the database
role to create extensions; the official `postgres` image's `POSTGRES_USER` is a superuser, so this works as is.

Then, in the admin panel:

1. **Cashback policy.** Open *Cashback sozlamalari* and save version 1. Until a version is in effect, purchases earn
   no cashback (the engine treats "no policy" as zero).
2. **Site settings.** Fill phones, links, pickup address and the policy texts in *Sozlamalar*.

## 5. Verification

```bash
curl -fsS "https://$API_DOMAIN/api/v1/health/?ready=1"        # "status": "ok", checks database/cache/broker ok, backend sha
curl -fsS https://$ADMIN_DOMAIN/healthz                         # {"status":"ok","version":"<frontend sha>"}
curl -s -o /dev/null -w '%{http_code}\n' https://$API_DOMAIN/api/v1/admin/auth/me/   # 404 (edge)
docker compose -f compose.prod.yaml ps                          # api, frontend, worker healthy; beat running
docker compose -f compose.prod.yaml exec api python manage.py check --deploy
```

`check --deploy` reports `security.W004` (Django's own HSTS is off): expected, the edge sends
`Strict-Transport-Security` on every host. Finally sign in at `https://ADMIN_DOMAIN/admin` and open the dashboard.

## 6. Upgrades

```bash
cd /srv/glowderma/backend && git pull && git -C ../frontend pull
export GIT_SHA=$(git rev-parse --short HEAD) FRONTEND_GIT_SHA=$(git -C ../frontend rev-parse --short HEAD)
docker compose -f compose.prod.yaml build
docker compose -f compose.prod.yaml run --rm api migrate
docker compose -f compose.prod.yaml up -d                       # recreates changed services
docker image prune -f
```

- Read the release's PR notes first: they list `.env` additions and server steps.
- Migrations run while the previous containers still serve traffic, so every migration must be compatible with the
  code before it (add columns nullable or with defaults; drop columns one release after the code stops using them).
- The frontend build bakes `SITE_DOMAIN`, `ADMIN_DOMAIN` and `API_DOMAIN`: changing a domain needs `build` again.
- Open admin tabs from the previous frontend build do a full reload on their next navigation (Next `deploymentId`),
  instead of failing Server Actions.
- **Rollback:** `git checkout <previous tag>` in both repositories, `build`, `up -d`. A rollback across a migration
  that dropped or rewrote data needs the backup from section 8.

## 7. Operations

| Task | Command |
| --- | --- |
| Logs (JSON, rotated 5 × 20 MB per container) | `docker compose -f compose.prod.yaml logs -f --tail=200 api worker` |
| Django shell / SQL | `docker compose -f compose.prod.yaml exec api python manage.py shell` / `... manage.py dbshell` |
| Celery workers alive | `docker compose -f compose.prod.yaml exec worker celery -A config inspect ping` |
| More API capacity | raise `GUNICORN_WORKERS` (≈ 2 × vCPU) and `GUNICORN_THREADS` in `.env`, then `up -d api` |
| More task throughput | raise `CELERY_CONCURRENCY`, then `up -d worker` |
| Renew TLS | automatic (Caddy); certificates live in the `caddy_data` volume |

- **beat is a singleton.** Never `--scale beat=2`: every periodic task would run twice. Current schedule
  (`config/celery.py`, Asia/Tashkent): outbox dispatch every 10 s, stale-job reaper every 5 min, due campaigns every
  minute, idempotency purge 03:00, loyalty balance reconciliation 03:10. Later releases add entries there.
- **Outbox.** Provider calls (BTS, Billz, refunds, pushes) leave through `core.OutboxMessage`. Messages that exhaust
  their retries become `dead` and raise an `outbox.dead` audit entry; review them after any provider outage.
- **Django admin** is off (`DJANGO_ADMIN_ENABLED=false`) and, even when enabled, accepts only the frontend container's
  address. Use the shell commands above for data inspection.
- **Media** lives in the `media` volume, shared by api, worker and (read-only) the edge.

## 8. Backups and restore

Nightly on the host (cron), keeping 14 days locally; copy the directory off the VPS as well:

```bash
# /etc/cron.d/glowderma-backup
15 2 * * * root cd /srv/glowderma/backend && \
  docker compose -f compose.prod.yaml exec -T postgres pg_dump -U glowderma -Fc glowderma > /srv/backups/db-$(date +\%F).dump && \
  docker run --rm -v glowderma_media:/media:ro -v /srv/backups:/backup alpine tar czf /backup/media-$(date +\%F).tgz -C /media . && \
  find /srv/backups -mtime +14 -delete
```

Restore into a fresh stack (stop api, worker and beat first so nothing writes):

```bash
docker compose -f compose.prod.yaml stop api worker beat frontend
docker compose -f compose.prod.yaml exec -T postgres pg_restore -U glowderma -d glowderma --clean --if-exists < db-YYYY-MM-DD.dump
docker run --rm -v glowderma_media:/media -v /srv/backups:/backup alpine sh -c 'cd /media && tar xzf /backup/media-YYYY-MM-DD.tgz'
docker compose -f compose.prod.yaml up -d
```

Append-only tables (ledger, order events, audit log, webhook inbox) have triggers that refuse `UPDATE`/`DELETE`;
`pg_restore --clean` drops and recreates them, which the triggers do not block. Test a restore on a scratch VPS at
least once before go-live.

## 9. Environment reference

**Backend `.env`** (template `deploy/env.production.example`):

| Variable | Required | Notes |
| --- | --- | --- |
| `SITE_DOMAIN`, `ADMIN_DOMAIN`, `API_DOMAIN`, `ACME_EMAIL` | yes | edge, frontend build args, `ALLOWED_HOSTS`, `PUBLIC_MEDIA_BASE_URL` |
| `SECRET_KEY`, `INTERNAL_API_SECRET`, `JWT_SIGNING_KEYS`, `JWT_ACTIVE_KID`, `POSTGRES_PASSWORD` | yes | section 3 |
| `INTEGRATIONS_MODE`, `OAUTH_MODE`, `BILLZ_MODE`, `FCM_MODE` | no (fake) | `live` needs the provider credentials (see `.env.example`) |
| `CAMPAIGN_BATCH_SIZE` | no (500) | recipients per push batch |
| `GUNICORN_WORKERS`, `GUNICORN_THREADS`, `CELERY_CONCURRENCY`, `LOG_LEVEL` | no | tuning |
| `DJANGO_ADMIN_ENABLED`, `SECURE_HSTS_SECONDS`, `THROTTLE_RATES` | no | defaults: off, 0 (edge sends HSTS), contract §1.9 |
| `FRONTEND_CONTEXT` | no (`../frontend`) | frontend checkout used as the build context |

**Frontend** (set by `compose.prod.yaml`, nothing to add): build args `NEXT_PUBLIC_SITE_URL`,
`GLOWDERMA_ALLOWED_ORIGINS`, `GLOWDERMA_MEDIA_HOSTS`, `GLOWDERMA_BUILD_SHA`; runtime `GLOWDERMA_DATA_SOURCE=api`,
`GLOWDERMA_API_URL=http://api:8000`, `GLOWDERMA_INTERNAL_SECRET`, `GLOWDERMA_ALLOWED_ORIGINS`,
`GLOWDERMA_COOKIE_SECURE=true`.

## 10. Edge contract (if you replace Caddy)

Any other proxy (nginx, Traefik, a cloud load balancer) must keep all of these:

1. **One hop, X-Forwarded-For overwritten** with the TCP peer address (never appended to a client-supplied value),
   and the proxy's own address listed in `TRUSTED_PROXIES`. Django's per-IP throttles, the Payme IP allow-list
   and `AuditEntry.ip` depend on it. Keep `Host` unchanged and send `X-Forwarded-Proto`.
2. **No response buffering** towards Next (`proxy_buffering off;` in nginx; the app also sends
   `X-Accel-Buffering: no`). Admin pages stream.
3. **Body limits**: 7 MB on the admin host (Server Actions with 5 MB images), 10 MB on the API host, 1 MB on the
   landing host.
4. **Private paths**: `/api/v1/admin/`, `/api/v1/schema/`, `/api/v1/docs/`, `/django-admin/` never forwarded from
   the internet.
5. **Media**: `/media/` on the API host served from the media volume with `X-Content-Type-Options: nosniff` and a
   sandboxing `Content-Security-Policy`; no directory listing.

## 11. Known limitations of this setup

- The Docker images were not built in the development environment (no Docker daemon there); the backend and frontend
  CI workflows build them on every pull request. Run the first build on a staging VPS before production.
- `INTEGRATIONS_MODE=fake` sends nothing to Billz, BTS, Click, Payme, Eskiz or FCM. It is meant for staging and
  demos. Taking real customer orders needs live adapters with credentials.
- One VPS is a single point of failure. Backups (section 8) are the recovery path; there is no standby.
