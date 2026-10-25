# AGENTS.md — Glow Derma backend

Instructions for any coding agent working in this repository (the lead agent and the subagents it spawns).
The design spec is `docs/specs/2026-10-24-backend-foundation-design.md`; the admin API contract is
`docs/api/admin-api-v1.md`; open business questions are `docs/decisions/open-questions.md`.
Task-level plans live in `docs/plans/`. If the repository state differs from what a task brief describes,
**stop and report** instead of reconciling it yourself.

## Scope rules (hard)
- Write only inside this repository. Never touch the sibling `frontend` checkout unless the brief says so.
- Commit on the branch named in the task brief. **Never push**, never rebase or force-update `main`, never
  change git config.
- The lead owns `docs/`, `AGENTS.md`, `README.md`, `.gitignore`, `.gitattributes`. Do not edit them unless
  the brief says so; propose changes in your reply instead.
- No secrets in the repo. Every credential comes from environment variables (`.env.example` lists them with
  dummy values).
- Do not add dependencies beyond the pinned stack without asking (no freezegun/time-machine, pytest-cov,
  django-environ, simplejwt, knox, CORS packages, django-celery-beat).

## Stack (pinned — do not change versions without asking)
Python 3.14 · Django 6.1.2 · djangorestframework 3.18.3 · drf-spectacular 0.30.0 (+ sidecar) ·
psycopg[binary,pool] 3.3.6 · gunicorn 26.2.0 (gthread) · whitenoise 6.12.0 · celery[redis] 5.6.3 ·
redis 6.4.0 (kombu bound — do not upgrade) · PyJWT 2.15.1 · argon2-cffi 25.1.0 · django-filter 26.2 ·
Pillow 13.0.0 · openpyxl 3.1.5 · httpx 0.28.1 · tenacity 9.2.1.
Dev: ruff 0.17.2 · mypy 2.3.1 + django-stubs[compatible-mypy] 6.1.2 + djangorestframework-stubs[compatible-mypy]
3.18.1 · drf-spectacular-sidecar (pin the version resolved in uv.lock) · pytest 9.1.1 · pytest-django 4.14.0 ·
factory-boy 3.3.3 · coverage · import-linter.
Not used: simplejwt, knox, django-celery-beat, django-cors-headers, django-environ (settings read `os.environ`
through a small typed helper in `config/env.py`).

## Environment (Linux cloud session)
- Python 3.14 comes from `uv` (`uv sync` downloads it). Always `export UV_SYSTEM_CERTS=1` (proxy CA). In a git
  worktree also `export UV_CACHE_DIR=/home/user/.uv-cache` so worktrees share one package cache.
- No Docker daemon. PostgreSQL 16.15 listens on `localhost:5433` (user, password and db `glowderma`) and Redis 8
  on `localhost:6380`, started natively with the same credentials as `compose.yaml`. CI and production use
  `postgres:18-trixie`: every DB feature (extensions, collations, triggers, indexes) is created by migrations and
  must work on both versions; never rely on PG-18-only syntax.
- Parallel work: each subagent works in its own git worktree and branch and runs the suite against its own test
  database: `TEST_DB_NAME=test_gd_<branch> uv run pytest` (default `test_glowderma`). Never run two pytest
  processes against the same `TEST_DB_NAME`.
- Redis DB 0 is the Django cache, DB 1 the Celery broker. Tests use LocMem; never call `cache.clear()` in
  application code.

## Dev loop
Run from the repository root and stop at the first non-zero exit:
```
export UV_SYSTEM_CERTS=1
uv sync
TEST_DB_NAME=test_gd_<branch> uv run pytest -q
uv run ruff check .
uv run ruff format --check .
uv run mypy .
uv run lint-imports
uv run python manage.py makemigrations --check --dry-run
uv run python manage.py spectacular --validate --fail-on-warn --file openapi/schema.yaml
```
`config.settings.test` uses the dev Postgres on 5433, `CELERY_TASK_ALWAYS_EAGER=True` and LocMem cache. Every
task that changes the API regenerates and commits `openapi/schema.yaml`. Coverage must stay ≥ 85 %
(`uv run coverage run -m pytest` then `uv run coverage report --fail-under=85`). A task is done only when all of
the above pass; report the commit SHA and the command outputs' summary.

## Docker (lead / CI only)
`docker compose up -d --build` starts api, worker, beat, postgres (published 5433), redis (published 6380). There
is no daemon in the cloud session; CI builds the image and the user runs the compose smoke test.

## Architecture rules
- Modular monolith: apps under `glowderma/<module>/` with `models.py`, `services.py` (transactional writes),
  `selectors.py` (reads), `api/admin/`, `api/customer/`, `tasks.py`, `tests/`. Cross-module writes only through
  the other module's `services`, reads through `selectors`; FKs by string reference (`"app.Model"`).
- Layering, enforced by import-linter (a module imports only strictly lower layers; siblings never import each
  other):
  ```
  reporting | customers
  notifications
  commerce | appointments
  payments | delivery | loyalty | catalog | professionals
  site_config
  accounts
  integrations
  core
  ```
- Upward effects go through the outbox as string topics (`outbox.enqueue("push.order_status", …)`); the handler
  (`@outbox.register`) lives in the orchestrating module's `tasks.py` and must be idempotent at the provider
  boundary (delivery is at-least-once).
- The upper module owns the transaction. Lock order is always Order → Payment → LoyaltyAccount.
  `LoyaltyAccount.balance` changes only in `loyalty.services.post_entry()`.
- Contract enums live in `glowderma/core/enums.py` (literals exactly as in the contract §3); shared serializer
  field types in `glowderma/core/api/fields.py`; error and OpenAPI helpers in `glowderma/core/api/{exceptions,schema}.py`.
- Time only via `timeutils.now()` (module attribute, never an imported name). Money: integer so'm
  (`BigIntegerField`). Percent: `DecimalField(5, 2)`; cashback math in integer basis points (`core.money`).
- Append-only models (ledger, audit, order events, policy versions, webhook inbox, appointment events) subclass
  `AppendOnlyModel` **and** get a trigger migration (`core.db.append_only_trigger_sql`); FKs to and from
  append-only rows use `on_delete=PROTECT` (a `SET_NULL` would be an UPDATE the trigger rejects).
- API: snake_case keys, enum literals as in the contract, error envelope + pagination envelope from `core`.
  Every admin view inherits `AdminAPIView`/`AdminGenericViewSet`; every handler method carries `@extend_schema`
  (outermost) with an explicit `operation_id`; `@idempotent()` where the contract marks **I**.
- Tests: pytest + factory-boy, one `tests/` package per module; service-level tests plus API tests for every
  endpoint (the auth matrix is automatic — add validation and happy-path tests); `format="json"` on APIClient
  calls; on-commit effects via `django_capture_on_commit_callbacks(execute=True)`; concurrency tests with
  `transaction=True`, threads and `threading.Event` (10 s timeouts), closing the DB connection in `finally`.
- Commits: small, imperative subject lines (`feat(accounts): add staff login endpoint`).
