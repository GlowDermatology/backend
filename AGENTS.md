# AGENTS.md — Glow Derma backend

Instructions for any coding agent (Codex, Claude) working in this repository.
The design spec is `docs/specs/2026-10-24-backend-foundation-design.md`; the admin API contract is
`docs/api/admin-api-v1.md`; open business questions are `docs/decisions/open-questions.md`.
Task-level plans live in `docs/plans/`. If the repository state differs from what a task brief describes,
**stop and report** instead of reconciling it yourself.

## Scope rules (hard)
- Codex writes only inside `backend/`. Never touch `../frontend`, `../infra`, `../mobile` or files in the
  `GlowDerma` parent folder.
- Every git command is `git -C backend ...` when run from the parent folder. Commit on the branch named in the
  task brief. **Never push**, never rebase or force-update `main`, never change git config.
- Claude (lead) owns `docs/`, `AGENTS.md`, `README.md`, `.gitignore`, `.gitattributes`. Do not edit them unless
  the brief says so; propose changes in your reply instead.
- No secrets in the repo. Every credential comes from environment variables (`.env.example` lists them with
  dummy values).

## Stack (pinned — do not change versions without asking)
Python 3.14 · Django 6.1.2 · djangorestframework 3.18.3 · drf-spectacular 0.30.0 (+ sidecar) ·
psycopg[binary,pool] 3.3.6 · gunicorn 26.2.0 (gthread) · whitenoise 6.12.0 · celery[redis] 5.6.3 ·
redis 6.4.0 (kombu bound — do not upgrade) · PyJWT 2.15.1 · argon2-cffi 25.1.0 · django-filter 26.2 ·
Pillow 13.0.0 · openpyxl 3.1.5 · httpx 0.28.1 · tenacity 9.2.1.
Dev: ruff 0.17.2 · mypy 2.3.1 + django-stubs[compatible-mypy] 6.1.2 + djangorestframework-stubs[compatible-mypy]
3.18.1 · drf-spectacular-sidecar (pin the version resolved in uv.lock) · pytest 9.1.1 · pytest-django 4.14.0 · factory-boy 3.3.3 · coverage · import-linter.
Not used: simplejwt, knox, django-celery-beat, django-cors-headers, django-environ (settings read `os.environ`
through a small typed helper in `config/env.py`).

## Local environment facts (Windows host)
- Host Python: use **`py -3.14`** (the default `python` is 3.15, which is NOT supported).
- Host uv is 0.11.32; the Docker image uses uv 0.13.5. Generate `uv.lock` with the host uv and keep
  `required-version` unset in `pyproject.toml` until the host is upgraded.
- Docker is **not reachable from the Codex sandbox**. Claude runs all Docker checks.
- Dev services already run on the host, bound to 127.0.0.1 (started by Claude, same credentials as `compose.yaml`):
  - PostgreSQL 18 on `localhost:5433` — user `glowderma`, password `glowderma`, db `glowderma`
    (the test runner creates `test_glowderma`).
  - Redis 8 on `localhost:6380`.
  - Host port 5432 belongs to an unrelated local PostgreSQL 17 — never use it.
- `core.autocrlf=true` on this machine; `.gitattributes` forces LF. Shell scripts must be LF.

## Dev loop (Codex)
Codex's shell is **Windows PowerShell 5.1**: run each command on its own (no `&&`) and stop at the first non-zero exit.
uv 0.11.32 is at `C:\Users\vbaho\.local\bin\uv.exe`; if `uv` is not found, stop and report (do not pip-install it).
`pyproject.toml` sets `[tool.uv] cache-dir = ".uv-cache"` because the sandbox cannot write the user-level uv cache.
```
uv sync
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run mypy .
uv run lint-imports
uv run python manage.py makemigrations --check --dry-run
uv run python manage.py spectacular --validate --fail-on-warn --file openapi/schema.yaml
```
`config.settings.test` uses the dev Postgres on 5433, `CELERY_TASK_ALWAYS_EAGER=True` and LocMem cache.
A task is done only when all of the above pass; report the commit SHA and the command outputs' summary.

## Docker (Claude runs these)
`docker compose up -d --build` starts api, worker, beat, postgres (published 5433), redis (published 6380).
Stop the standalone `gd-dev-postgres` / `gd-dev-redis` containers first — they use the same ports and credentials.

## Conventions
- Modular monolith: apps under `glowderma/<module>/` with `models.py`, `services.py` (transactional writes),
  `selectors.py` (reads), `api/admin/`, `api/customer/`, `tasks.py`, `tests/`. Cross-module access only via the
  other module's `services`/`selectors`; FKs by string reference. import-linter enforces it.
- Money: integer so'm (`BigIntegerField`). Percent: `DecimalField(5, 2)`; cashback math in integer basis points.
- Time: `USE_TZ=True`, `TIME_ZONE="Asia/Tashkent"`; API datetimes ISO 8601 with `+05:00`.
- API: snake_case keys, enum literals as in the contract, error envelope + pagination envelope from `core`.
- DRF defaults are deny-all; every view inherits an audience base class (`AdminAPIView` etc.).
- Append-only models (ledger, audit, order events, policy versions, webhook inbox) never update or delete.
- Tests: pytest + factory-boy, one `tests/` package per module; prefer service-level tests plus API tests for
  every endpoint (auth matrix, validation, happy path).
- Commits: small, imperative subject lines (`feat(accounts): add staff login endpoint`).
