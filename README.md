# Glow Derma — backend

Django 6.1 + Django REST Framework modular monolith for the Glow Derma customer app, admin panel and
integrations (Billz, BTS, Click, Payme, Eskiz, Firebase).

- Design spec: [`docs/specs/2026-10-24-backend-foundation-design.md`](docs/specs/2026-10-24-backend-foundation-design.md)
- Admin API contract: [`docs/api/admin-api-v1.md`](docs/api/admin-api-v1.md)
- Open business questions: [`docs/decisions/open-questions.md`](docs/decisions/open-questions.md)
- Implementation plans: [`docs/plans/`](docs/plans/)
- Agent instructions and local commands: [`AGENTS.md`](AGENTS.md)
- Production deployment: the [GlowDermatology/infra](https://github.com/GlowDermatology/infra) repository
  (Docker Compose, Caddy, Vault, staging and production). What this image provides to it:
  [`docs/ops/deploy.md`](docs/ops/deploy.md); the pipeline: [`.github/workflows/ci.yml`](.github/workflows/ci.yml)
- Monitoring, alerts and the nightly consistency report: [`docs/ops/monitoring.md`](docs/ops/monitoring.md)
- Personal data, hosting in Uzbekistan, retention, database registration: [`docs/ops/compliance.md`](docs/ops/compliance.md)
- Provider adapters and their go-live checklists: [`docs/integrations/`](docs/integrations/) (Billz, Click, Payme,
  BTS, Eskiz, FCM)

## Quick start (Docker)
```bash
cp .env.example .env
docker compose up -d --build
docker compose exec api python manage.py createstaff admin@glowderma.uz --manager
```
API: http://localhost:8000/api/v1/ · OpenAPI docs: http://localhost:8000/api/v1/docs/ (local only)

`compose.yaml` is the local development stack (local settings, ports on 127.0.0.1; the api container migrates on
start). Production runs the image built by CI from the infra repository; see [`docs/ops/deploy.md`](docs/ops/deploy.md).

## Container image

| | |
|---|---|
| Build | `Dockerfile`: multi-stage, `uv sync --locked --no-dev`, Python 3.14 slim, no package manager in the runtime |
| Runs as | uid 10001, read-only source, writable `/app/media` and `/tmp` only |
| Entrypoint | `entrypoint.sh api\|worker\|beat\|migrate\|manage\|shell` (waits for secrets and the database) |
| Static files | collected at build (`config.settings.build`), served by WhiteNoise |
| Secrets | `DJANGO_SECRETS_FILE` (JSON from Vault Agent), loaded by `config/secrets.py` before the settings |
| Workers | `GUNICORN_WORKERS`, `GUNICORN_THREADS`, `CELERY_CONCURRENCY` |
| Health | `GET /api/v1/health/` (liveness), `?ready=1` (database, cache, broker) |

Dependencies: `pyproject.toml` (runtime `[project.dependencies]`, development `[dependency-groups] dev`) and
`uv.lock`. Add one with `uv add <pkg>` (or `uv add --dev <pkg>`), commit both files.
