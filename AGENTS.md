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
redis 6.4.0 (kombu bound — do not upgrade) · PyJWT[crypto] 2.15.1 (cryptography is required for RS256 ID-token
verification) · argon2-cffi 25.1.0 · django-filter 26.2 ·
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
  application code. `config/__init__.py` loads the Celery app (`celery_app`) so `shared_task.delay()` from a
  request reaches the configured app (eager in tests, Redis otherwise); beat entries are declared centrally in
  `config/celery.py` (`beat_schedule`) — add yours there and list the file in `shared_files_touched`.

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
- Contract enums live in `glowderma/core/enums.py` (literals exactly as in the contract §3). Serializers declare
  `ChoiceField(choices=<Enum>.values)` (or `.choices`) so drf-spectacular's `ENUM_NAME_OVERRIDES` names the
  component `<Enum>Enum`; adding an enum means adding it to `enums.py` (the override map is built from it).
- Shared serializer field types live in `glowderma/core/api/fields.py` (`AwareDateTimeField`, `PercentField`,
  `PhoneField`, `SlugField`, `BoundedListField`, `QueryBooleanField` (feed query serializers `request.query_params.dict()`), `i18n_text()` for **field-first**
  shapes = `{uz, ru, en}`; create shapes where only `uz` is required use a module serializer like catalog's
  `CategoryNameSerializer`; `RESERVED_SLUGS` = `order`, `none`). Product `texts` is **language-first**
  (`{uz: {name, …}, ru: …}`) and needs its own serializer in `catalog`.
- Field errors: raise contract codes through `core.api.exceptions.field_error(code, message=None, limit=None)`;
  generic codes carry their default Uzbek message, endpoint-specific codes (`ENDPOINT_FIELD_CODES`) require the
  explicit message from the endpoint table. Unknown codes raise `ValueError` at development time.
- Shared contract shapes live in `glowderma/core/api/serializers.py` (`ActorSerializer` = `{id, display_name}`; declare
  it with `allow_null=True` where the actor may be the system) — never redeclare them in a module.
- JSON bodies are parsed by `glowderma.core.api.parsers.JSONParser` (the DRF default and every explicit
  `parser_classes`/`get_parsers`): it answers `400 parse_error` for an unpaired surrogate escape, which no UTF-8
  column or `jsonb` document can store. Never import `rest_framework.parsers.JSONParser` in a module.
- OpenAPI helpers in `glowderma/core/api/schema.py`: `ADMIN_ERRORS` / `PUBLIC_ERRORS` response maps,
  `IDEMPOTENCY_KEY_HEADER`, `EXPORT_PARAM`. Uzbek export labels live in `glowderma/core/labels.py`; datetimes in
  exports go through `core.exports.format_export_datetime`.
- Uploads go through `core.images.process_image()` (re-encode, EXIF strip, ≤2400 px, 5 MiB) and are named by
  `core.media.media_name()`; URLs come from `core.media.absolute_media_url()` (`PUBLIC_MEDIA_BASE_URL`).
- Throttle scopes and their default rates live in `glowderma/core/throttling.py` (`DEFAULT_THROTTLE_RATES`,
  overridable by the `THROTTLE_RATES` env JSON); a new throttle class must add its scope there or settings
  validation fails. `LOG_LEVEL` sets the `glowderma.*` logger level; every log line carries `request_id`.
- The Django admin is read-only: register models through `glowderma.core.admin.register_read_only` (`ReadOnlyAdmin`);
  never expose `password` or `refresh_hash`. Only the professionals models become editable, through services.
- Provider adapters live in `glowderma/integrations/<provider>/` behind a port (Protocol) with a fake and a live
  implementation chosen by `*_MODE` settings (default `INTEGRATIONS_MODE=fake`); views and services obtain them
  through the module's `registry`. `integrations` cannot host admin views (`AdminAPIView` lives in `accounts`,
  above it): provider-facing admin routes (`integrations/billz/import/`, `jobs/<pk>/`) are declared by the
  orchestrating module (`catalog`) while the paths stay as the contract says. Billz: `integrations/billz/{ports,fake,
  registry}.py` (`FakeBillz`: a known product returns its own fields, an id ending in `0000` is not found, anything
  else is synthetic; an import always yields the three `46070199xx` new products once). Jobs go through
  `integrations.services` (`start_job` single-flight, `claim_job`, `report_progress`, `finish_job`, `fail_job`,
  `reap_stale_jobs`). OAuth: `integrations/oauth/{ports,fake,google,facebook,registry}.py` (fake
  codes `fake:<subject>:<email>[:hd]`, `fake-unverified:…`, `fake-fail`, `fake-down`; FakeGoogle nonce
  `nonce-<subject>` or `*`). Adapters never put a secret or bearer token in a URL (POST bodies / `Authorization`
  headers only), retry only on transport errors, and map failures to `OAuthExchangeError`-style typed errors.
- Demo data: each module registers one idempotent seeder in `glowderma/<module>/seeds.py`
  (`core.seeding.register_seeder(name, order=…)`, orders accounts 10 · integrations 20 · site_config 30 · catalog 40 ·
  professionals 50 · loyalty 60 · commerce 70 · appointments 80 · notifications 90); `manage.py seed_demo` runs them in
  one transaction and re-syncs the id sequences (`--reset` flushes first for repeatable demo/e2e runs; refused
  under the production settings), so design rows are inserted with their explicit ids. Shared helpers
  live in `core.seeding` only — never re-port them: `MockRandom` (the frontend's `createRandom`, vectors pinned),
  `pinned_clock(instant)` / `ctx.at(instant)` for running services at a design instant, `placeholder_art(kind, tone)`
  / `ctx.media_placeholder` for image bytes. Seeders look rows up by natural keys, read the demo staff through
  `get_user_model()` (`admin@glowderma.uz`, id 1; password from `SEED_ADMIN_PASSWORD`), and tolerate a missing
  lower-layer row (actor `NULL`) so a module seeder runs standalone. The root `conftest.py` `media_root` fixture
  gives the whole test session a temporary `MEDIA_ROOT`. `settings.FAKE_NOW` (env `FAKE_NOW`, local/test only) pins
  `timeutils.now()` for demos and e2e runs.
- Retention jobs delete from `core.db.PURGEABLE_TABLES` only inside `core.db.purge_window()`; the outbox offers
  `outbox.on_dead(topic)` hooks for the orchestrating module to record what a dead message means.
- Rows that must always exist (the `SiteSettings` singleton, the six `IntegrationState` rows, status templates,
  …) are created by a data migration **and** re-ensured by a `post_migrate` receiver in the app's `AppConfig.ready()`
  (pattern: `glowderma/integrations/apps.py` `ensure_provider_rows`, `glowderma/site_config/apps.py`
  `ensure_singleton`), because pytest-django flushes the database after `transaction=True` tests and `--reuse-db`
  reruns would otherwise start without them. Reads never create such rows.
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
  Root `conftest.py` fixtures: `staff`, `staff_session`, `staff_token`, `staff_client` (DRF `APIClient` with the
  bearer token and internal secret; `admin_client` remains pytest-django's Django-admin client) and
  `anon_client`; factories in `glowderma/accounts/tests/factories.py` (`StaffUserFactory.create(...)`,
  `AuthSessionFactory.create(...)`, `TEST_PASSWORD`, `raw_refresh(session)`). `core` tests must not import
  `accounts` (import-linter): use `get_user_model()`. Each module adds `tests/factories.py` for its own models.
  A migration round-trip test (`migrate <app> zero` → forward) must end with a plain `call_command("migrate",
  verbosity=0)`: the app-scoped forward step leaves every dependent app unapplied for the rest of the
  transactional-test phase. The layering contract exempts `glowderma.*.tests.**`, so a lower module's tests and
  factories import upper-layer factories statically (`from glowderma.commerce.tests.factories import
  OrderFactory`) instead of dotted-path or `apps.get_model` workarounds; production code never does.
- Django-admin inlines use `glowderma.core.admin.ReadOnlyTabularInline` (same view grant as `ReadOnlyAdmin`,
  `core.admin.is_active_staff`); every admin rendering test logs in a plain active `is_staff` user, never a
  superuser, so a missing grant is visible.
- Commits: small, imperative subject lines (`feat(accounts): add staff login endpoint`).
