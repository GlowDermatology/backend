# Glow Derma — backend

Django 6.1 + Django REST Framework modular monolith for the Glow Derma customer app, admin panel and
integrations (Billz, BTS, Click, Payme, Eskiz, Firebase).

- Design spec: [`docs/specs/2026-10-24-backend-foundation-design.md`](docs/specs/2026-10-24-backend-foundation-design.md)
- Admin API contract: [`docs/api/admin-api-v1.md`](docs/api/admin-api-v1.md)
- Open business questions: [`docs/decisions/open-questions.md`](docs/decisions/open-questions.md)
- Implementation plans: [`docs/plans/`](docs/plans/)
- Agent instructions and local commands: [`AGENTS.md`](AGENTS.md)
- Production deployment (Compose on a VPS, Caddy edge, backups): [`docs/ops/deploy.md`](docs/ops/deploy.md)

## Quick start (Docker)
```bash
cp .env.example .env
docker compose up -d --build
docker compose exec api python manage.py migrate
docker compose exec api python manage.py createstaff admin@glowderma.uz --manager
```
API: http://localhost:8000/api/v1/ · OpenAPI docs: http://localhost:8000/api/v1/docs/ (local only)

`compose.yaml` is the local development stack (local settings, ports on 127.0.0.1). Production uses
`compose.prod.yaml`; see [`docs/ops/deploy.md`](docs/ops/deploy.md).
