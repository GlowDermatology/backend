# M0 Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A runnable Django 6.1 / DRF backend skeleton with the cross-cutting infrastructure every module needs (error and pagination envelopes, append-only records, audit, idempotency, outbox, request security, exports) and working staff email/password authentication.

**Architecture:** One Django project (`config/`) with apps under `glowderma/`. M0 creates `core` (infrastructure, no business models) and `accounts` (staff identity, sessions, JWT). One Docker image runs api / worker / beat by command. Business modules arrive in M1; staff Google/Facebook sign-in is M0b.

**Tech Stack:** AGENTS.md "Stack" (pinned). Python 3.14, Django 6.1.2, DRF 3.18.3, PostgreSQL 18, Redis 8, Celery 5.6.3, PyJWT 2.15.1, drf-spectacular 0.30.0.

**Spec:** `docs/specs/2026-10-24-backend-foundation-design.md` ("Backend architecture", "Financial integrity", "Auth and request security", "API conventions"). **Contract:** `docs/api/admin-api-v1.md` §1 (Conventions) and §2.1 Auth / Health — authoritative for status codes, error codes, messages, throttle values, idempotency semantics and response shapes. If this plan and the contract disagree, follow the contract and say so in your reply.

**Branch:** `feat/m0-foundation`, created with `git -C backend switch -c feat/m0-foundation main`.
**Precondition (check first; otherwise stop and report):** `git -C backend rev-parse --verify main` succeeds and `git -C backend status --porcelain` is empty.

## Global Constraints
- Python `>=3.14,<3.15`; `.python-version` = `3.14`. Exact AGENTS.md pins. Dev tools live in `[dependency-groups] dev` (so `uv sync` installs them and the image's `--no-dev` excludes them). **No other dependencies** (no freezegun/time-machine, no pytest-cov, no django-environ).
- `[tool.uv] cache-dir = ".uv-cache"` (the sandbox cannot write the user-level uv cache).
- **Shell:** Codex runs Windows PowerShell 5.1 — run every command separately (no `&&`), stop at the first non-zero exit.
- **Settings modules:** `manage.py` defaults to `config.settings.local`; `wsgi.py`, `asgi.py`, `config/celery.py` default to `config.settings.production`; pytest and the django-stubs mypy plugin use `config.settings.test`.
- `config/env.py`: `env_str/env_int/env_bool/env_list/env_json(name, default=MISSING)`; an **empty value counts as missing**; missing required → `ImproperlyConfigured` naming the variable.
- **Production settings refuse to start** unless: `SECRET_KEY` and `INTERNAL_API_SECRET` are ≥32 chars and contain no `change-me`; `JWT_SIGNING_KEYS` is a JSON object `{kid: base64url secret}` whose every value decodes (urlsafe, re-padded) to ≥32 bytes (the decoded bytes are the HMAC key); `JWT_ACTIVE_KID` is one of its keys; `DATABASE_URL`, `REDIS_URL`, `ALLOWED_HOSTS`, `ADMIN_API_ALLOWED_NETS` are set. Production must not open DB/Redis connections at import.
- `TIME_ZONE="Asia/Tashkent"`, `USE_TZ=True`, `LANGUAGE_CODE="uz"`. `REST_FRAMEWORK["DATETIME_FORMAT"] = "%Y-%m-%dT%H:%M:%S%:z"` (seconds precision, `+05:00`). Hand-built datetimes use `timeutils.isoformat(dt)`.
- **Time in code is read only via `glowderma.core.timeutils.now()`**, referenced as `timeutils.now()` (never `from ... import now`), so tests `monkeypatch.setattr("glowderma.core.timeutils.now", ...)`.
- `REST_FRAMEWORK`: `DEFAULT_AUTHENTICATION_CLASSES=[]`, `DEFAULT_PERMISSION_CLASSES=["glowderma.core.permissions.DenyAll"]`, `DEFAULT_THROTTLE_CLASSES=[]`, `EXCEPTION_HANDLER="glowderma.core.api.exceptions.envelope_exception_handler"`, `DEFAULT_PAGINATION_CLASS="glowderma.core.api.pagination.EnvelopePagination"`, `DEFAULT_SCHEMA_CLASS="drf_spectacular.openapi.AutoSchema"`, `DEFAULT_RENDERER_CLASSES=["rest_framework.renderers.JSONRenderer"]`. No `SessionAuthentication`/`BasicAuthentication` on any API view. No CORS package.
- Every task that changes the API regenerates and commits `openapi/schema.yaml`; `spectacular --validate --fail-on-warn` must pass in every task (every APIView method carries `@extend_schema` as the outermost decorator).
- **Tests:** root `conftest.py` autouse fixture clears `django.core.cache.cache` before and after each test. On-commit effects are asserted with `django_capture_on_commit_callbacks(execute=True)` or `@pytest.mark.django_db(transaction=True)`. Thread tests use `transaction=True`, `threading.Event` waits with a 10 s timeout (never barriers), and every worker thread calls `django.db.connection.close()` in `finally`. Celery tests run eager with `CELERY_TASK_EAGER_PROPAGATES=True`.
- **Internal secret check** everywhere through one helper `glowderma.core.clientip.has_internal_secret(request) -> bool` = `bool(settings.INTERNAL_API_SECRET) and hmac.compare_digest(request.META.get("HTTP_X_GD_INTERNAL_SECRET", "").encode("latin-1"), settings.INTERNAL_API_SECRET.encode())`.
- Ports bound to **127.0.0.1 only**: api `8000`, Postgres `5433`, Redis `6380`.
- `core` never imports `accounts`; it uses `django.contrib.auth.get_user_model()` and duck typing.

## Review Focus
1. A revoked or replaced session is rejected on the very next request in every worker, including when a revoke commits while another request is filling the session cache (tombstone + `cache.add`).
2. DRF 3.18 nested errors (`texts.uz.name`, `tiers.1.percent`, `non_field_errors`) flatten to dotted paths, and auth failures stay **401** (never DRF's 403 coercion) on views with and without authenticators.
3. A duplicate `Idempotency-Key` sent while the first request runs gets 409 `request_in_progress` immediately without blocking; after commit it gets the replay; a non-2xx stores nothing.
4. Append-only tables reject `QuerySet.update()`, `.delete()`, instance re-`save()` and raw SQL `UPDATE/DELETE`.
5. A locked email gets 429 even with the correct password, unknown emails behave identically (same code path, same timing), and spoofed `X-Forwarded-For` cannot bypass the per-IP login throttle.

---

### Task 1: Project skeleton, settings, staff user model, health, tooling

**Files:**
- Create: `pyproject.toml`, `uv.lock`, `.python-version`, `manage.py`, `.env.example`, `conftest.py`, `.importlinter`, `openapi/schema.yaml`
- Create: `config/{__init__,env,urls,wsgi,asgi,celery}.py`, `config/settings/{__init__,base,local,test,production}.py`
- Create: `glowderma/__init__.py`, `glowderma/core/{__init__,apps,permissions,clientip}.py`, `glowderma/core/api/{__init__,health}.py`
- Create: `glowderma/accounts/{__init__,apps,models,managers}.py`, `glowderma/accounts/migrations/0001_initial.py`
- Test: `glowderma/core/tests/test_health.py`, `glowderma/core/tests/test_settings.py`, `glowderma/accounts/tests/test_user_model.py`

**Interfaces:**
- `StaffUser(AbstractBaseUser, PermissionsMixin)` — **no username**: `email` (EmailField 254, stored lower-cased, `UniqueConstraint(Lower("email"), name="staffuser_email_ci_unique")`), `full_name` (150, blank), `role` (choices `admin`, default `admin`), `is_active` (default True), `is_staff` (Django-admin access, default False), `date_joined`. `USERNAME_FIELD="email"`, `REQUIRED_FIELDS=[]`. `StaffUserManager.create_user(email, password=None, **extra)` / `create_superuser(...)` normalize with `strip().lower()`. `AUTH_USER_MODEL="accounts.StaffUser"` from this first commit; never run `migrate` before it exists.
- `INSTALLED_APPS = ["django.contrib.admin", "django.contrib.auth", "django.contrib.contenttypes", "django.contrib.sessions", "django.contrib.messages", "django.contrib.staticfiles", "rest_framework", "drf_spectacular", "drf_spectacular_sidecar", "glowderma.core", "glowderma.accounts"]`.
- **Final `MIDDLEWARE` order** (Task 1 lists the Django ones; Tasks 3 and 7 insert theirs at these positions): `SecurityMiddleware`, `whitenoise.middleware.WhiteNoiseMiddleware`, `glowderma.core.middleware.RequestIdMiddleware` (T3), `glowderma.core.clientip.ClientIPMiddleware` (T7), `glowderma.core.network.AdminNetworkMiddleware` (T7), `SessionMiddleware`, `CommonMiddleware`, `CsrfViewMiddleware`, `AuthenticationMiddleware`, `MessageMiddleware`, `XFrameOptionsMiddleware`.
- `glowderma/core/clientip.py` starts with `has_internal_secret(request)` (Global Constraints); Task 7 adds the rest.
- `config/urls.py`: `api/v1/` prefix; `django-admin/` mounted only when `DJANGO_ADMIN_ENABLED` (`env_bool`; local default true, production default false).
- `config/celery.py`: `app = Celery("glowderma")`, `config_from_object("django.conf:settings", namespace="CELERY")`, `autodiscover_tasks()`, `app.conf.beat_schedule = {}`.
- `config.settings.local`: `DEBUG=True`, `DATABASE_URL` default `postgres://glowderma:glowderma@localhost:5433/glowderma`, `REDIS_URL` default `redis://localhost:6380/0`, `CACHES` = Django `RedisCache` with `OPTIONS {"socket_connect_timeout": 1, "socket_timeout": 1}`, fixed dev JWT key / internal secret. `config.settings.test`: same DB, LocMem cache, `CELERY_TASK_ALWAYS_EAGER=True`, `CELERY_TASK_EAGER_PROPAGATES=True`, `CELERY_BROKER_URL` default `redis://localhost:6380/0`, MD5 hasher, fixed test `JWT_SIGNING_KEYS`, `JWT_ACTIVE_KID`, `INTERNAL_API_SECRET` (≥32 chars). DB `OPTIONS["connect_timeout"] = 3` in all settings.
- `.env.example`: `DJANGO_SETTINGS_MODULE=config.settings.local` and `change-me-...` placeholders for every secret.
- Health `GET /api/v1/health/`: `APIView` with `authentication_classes=[]`, `permission_classes=[AllowAny]`, `throttle_classes=[]`; shallow → `200 {"status": "ok"}`; `?ready=1` → database `SELECT 1`, cache set/get, broker `redis.Redis.from_url(settings.CELERY_BROKER_URL, socket_connect_timeout=1, socket_timeout=1).ping()` (never kombu `ensure_connection`) → `200 {"status": "ok", "checks": {"database": "ok", "cache": "ok", "broker": "ok"}, "version": env GIT_SHA or "dev"}` or 503 with `"status": "error"` and the failing check `"error"`. `@extend_schema(operation_id="health_retrieve", auth=[], parameters=[OpenApiParameter("ready", OpenApiTypes.BOOL)], responses={200: HealthSerializer, 503: HealthSerializer})`.
- `glowderma/core/permissions.py`: `DenyAll` (always False).
- pyproject: `[tool.pytest.ini_options] DJANGO_SETTINGS_MODULE = "config.settings.test"`; `[tool.mypy] python_version = "3.14"`, `plugins = ["mypy_django_plugin.main", "mypy_drf_plugin.main"]`, `strict = true`; `[tool.django-stubs] django_settings_module = "config.settings.test"`; `[tool.coverage.run] source = ["glowderma"]`, `branch = true`; ruff line length 110, rules `E,F,I,B,UP,S,DJ` (tests may ignore `S101`).
- `.importlinter`:
  ```
  [importlinter]
  root_package = glowderma

  [importlinter:contract:core-independent]
  name = core is independent
  type = layers
  layers =
      glowderma.accounts
      glowderma.core
  ```
  (M1 adds its modules to the upper layer.)

- [ ] **Step 1:** Failing tests: `test_health_ok`; `test_health_ready_ok`; `test_health_ready_db_failure_503` (patch cursor to raise → 503, `checks.database == "error"`); `test_production_settings_require_secrets` (`monkeypatch.delenv` each required var with `raising=False`, `sys.modules.pop("config.settings.production", None)`, `importlib.import_module` inside `pytest.raises(ImproperlyConfigured, match="SECRET_KEY")` — never a subprocess with an emptied env); `test_production_rejects_placeholder_secrets`; `test_production_rejects_unknown_active_kid`; `test_production_rejects_short_jwt_key`; `test_empty_env_value_counts_as_missing`; `test_empty_secret_never_matches`; `test_non_ascii_secret_header_is_ignored`; `test_api_defaults_deny`; `test_email_is_case_insensitive_unique` (`Admin@X.uz` then `admin@x.uz` → IntegrityError); `test_manager_normalizes_email`.
- [ ] **Step 2:** `uv run pytest -q` → FAIL.
- [ ] **Step 3:** Implement; generate `openapi/schema.yaml` with `uv run python manage.py spectacular --validate --fail-on-warn --file openapi/schema.yaml`.
- [ ] **Step 4:** Run separately: `uv run pytest -q`, `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy .`, `uv run lint-imports`, `uv run python manage.py makemigrations --check --dry-run` — all exit 0.
- [ ] **Step 5:** Commit `chore: bootstrap Django 6.1 project with email staff user`.

### Task 2: Docker image and compose stack

**Files:**
- Create: `Dockerfile`, `.dockerignore`, `compose.yaml`, `docker/entrypoint.sh`, `config/gunicorn.conf.py`
- Modify: `config/settings/production.py`
- Test: `glowderma/core/tests/test_celery.py`

**Interfaces:**
- `docker/entrypoint.sh`: LF, `#!/bin/sh`, `set -eu`; commands `api` (gunicorn `config.wsgi`, gthread, `GUNICORN_WORKERS=3`, `GUNICORN_THREADS=4`, bind `0.0.0.0:8000`), `worker` (`celery -A config worker -l info`), `beat` (`celery -A config beat -l info --schedule /tmp/celerybeat-schedule`), `migrate`, `manage <args>`; every branch ends in `exec`. Commit with `git -C backend update-index --chmod=+x docker/entrypoint.sh`.
- Dockerfile: builder `python:3.14-slim-trixie` + `COPY --from=ghcr.io/astral-sh/uv:0.13.5 /uv /uvx /bin/`, `UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=0 UV_CACHE_DIR=/root/.cache/uv`, `uv sync --locked --no-dev --no-editable --no-install-project` (cache + bind mounts) then copy source and `uv sync --locked --no-dev --no-editable`. Runtime `python:3.14-slim-trixie`: `ENV DJANGO_SETTINGS_MODULE=config.settings.production PATH=/app/.venv/bin:$PATH PYTHONUNBUFFERED=1`; copy `/app/.venv` then source; `COPY --chmod=0755 docker/entrypoint.sh` + `RUN sed -i 's/\r$//' /app/docker/entrypoint.sh`; collectstatic with throwaway values on that RUN only: `RUN SECRET_KEY=build-only-build-only-build-only-0000 JWT_SIGNING_KEYS='{"build":"YnVpbGQtb25seS1rZXktbm90LXVzZWQtYXQtcnVudGltZSEh"}' JWT_ACTIVE_KID=build INTERNAL_API_SECRET=build-only-build-only-build-only-0000 DATABASE_URL=postgres://b:b@localhost:5432/b REDIS_URL=redis://localhost:6379/0 ALLOWED_HOSTS=localhost ADMIN_API_ALLOWED_NETS=127.0.0.0/8 python manage.py collectstatic --noinput` (no secret in any `ENV`/`ARG`); `RUN useradd -u 10001 app && mkdir -p /app/media && chown -R app:app /app/media`; `USER app`; `ENTRYPOINT ["/app/docker/entrypoint.sh"]`; `CMD ["api"]`. **No `HEALTHCHECK` in the image.**
- `.dockerignore` exactly: `.git`, `.github`, `.venv`, `.uv-cache`, `**/__pycache__`, `.mypy_cache`, `.pytest_cache`, `.ruff_cache`, `.coverage`, `htmlcov`, `.env`, `.env.*`, `!.env.example`, `media`, `staticfiles`, `docs`.
- `compose.yaml` (local development only — fixed `glowderma/glowderma` credentials, Redis without password; never publish on all interfaces): `api` (`127.0.0.1:8000:8000`, healthcheck `python -c` urllib request to `http://127.0.0.1:8000/api/v1/health/` with `Host: localhost`, interval 10s, retries 6; depends_on healthy postgres+redis; `DJANGO_SETTINGS_MODULE=config.settings.local` via `.env`), `worker` (healthcheck `celery -A config inspect ping -d celery@$$HOSTNAME --timeout 5`, interval 30s), `beat` (`healthcheck: {disable: true}`), `postgres` (`postgres:18-trixie`, volume `pgdata:/var/lib/postgresql`, `127.0.0.1:5433:5432`, `pg_isready`), `redis` (`redis:8-alpine`, `127.0.0.1:6380:6379`, `redis-cli ping`); named volume `media` at `/app/media` on api and worker.
- Production settings: `ALLOWED_HOSTS = env_list("ALLOWED_HOSTS") + ["localhost"]`; `SECURE_SSL_REDIRECT = env_bool("SECURE_SSL_REDIRECT", False)` with `SILENCED_SYSTEM_CHECKS = ["security.W008"]` and a comment that TLS terminates at the edge proxy and Next calls the API over HTTP on the private network; `SECURE_HSTS_SECONDS` from env (default 0); `SESSION_COOKIE_SECURE = CSRF_COOKIE_SECURE = True`; do not set `SECURE_PROXY_SSL_HEADER`.

- [ ] **Step 1:** Failing test `test_celery_app_configured` (`from config.celery import app`; `app.main == "glowderma"`; `app.conf.task_always_eager is True` under test settings).
- [ ] **Step 2–4:** fail → implement → pass; dev loop green.
- [ ] **Step 5:** Commit `build: add Docker image and compose stack`. (Codex cannot run Docker; Claude verifies `docker compose up -d --build` → api and worker healthy, `curl 127.0.0.1:8000/api/v1/health/?ready=1` → 200.)

### Task 3: API envelopes — exceptions, errors, pagination, response headers

**Files:**
- Create: `glowderma/core/api/exceptions.py`, `glowderma/core/api/pagination.py`, `glowderma/core/api/schema.py`, `glowderma/core/middleware.py`
- Modify: `config/settings/base.py` (MIDDLEWARE), `config/urls.py` (`handler404`, `handler500`)
- Test: `glowderma/core/tests/test_exceptions.py`, `test_pagination.py`, `test_middleware.py`

**Interfaces:**
- Exception classes in `glowderma.core.api.exceptions`, each subclassing `rest_framework.exceptions.APIException` **directly** (never `NotAuthenticated`/`AuthenticationFailed`, which DRF rewrites to 403 on views without an authenticate header): `Conflict(409,"conflict")`, `InvalidTransition(409,"invalid_transition")`, `NotCancellable(409,"not_cancellable")`, `RequestInProgress(409,"request_in_progress")` (adds `Retry-After: 1`), `IdempotencyKeyRequired(400,"idempotency_key_required")`, `IdempotencyKeyReused(422,"idempotency_key_reused")`, `AuthFailed(401, code)` with code ∈ `not_authenticated|SESSION_EXPIRED|SESSION_REPLACED|invalid_credentials` and the contract §1.6 message for that code.
- `envelope_exception_handler(exc, context)`: first calls `rest_framework.views.exception_handler(exc, context)` (keeps `WWW-Authenticate`, `Retry-After`), then replaces the body with exactly `{"code", "message", "errors": [{"field", "code", "message"}], "request_id"}`. `code` = the exception's own code for the classes above; DRF built-ins map per contract §1.6 (`ValidationError` → `validation_error` "Maydonlarni tekshiring." with flattened errors; `NotAuthenticated`/`AuthenticationFailed` → `not_authenticated`; `PermissionDenied` → `permission_denied`; `NotFound`/`Http404` → `not_found`; `MethodNotAllowed`; `UnsupportedMediaType` → 415 `unsupported_media_type`; `Throttled` → 429 `throttled` with the wait in the message only, no extra body key). Field paths are dotted snake_case, list indexes numeric (`tiers.1.percent`), `non_field_errors` → `field: null`. Non-APIException → `logger.exception`, 500 `server_error` with no internals.
- `not_found_view(request, exception)` / `server_error_view(request)` registered as `handler404`/`handler500` return the same envelope for non-DRF routes.
- `RequestIdMiddleware`: reuses inbound `X-Request-ID` only when `has_internal_secret(request)` and the id matches `^[A-Za-z0-9-]{8,64}$`, else UUID4. Sets `request.request_id`; on every response sets `X-Request-ID`, and for `path_info` starting with `/api/` sets `Cache-Control: no-store` (unless set) and `Content-Language: uz`. Rejects a body over 6 MiB (`CONTENT_LENGTH`) with a 413 `payload_too_large` envelope.
- `EnvelopePagination(PageNumberPagination)`: response `{"count", "page", "page_size", "page_count", "results"}`; non-integer `page` → 1; `page` clamped to `[1, page_count]` (`page_count >= 1`); views declare `page_size_options: tuple[int, ...]` and `default_page_size`; a `page_size` outside the options → 400 `validation_error` with `errors=[{"field": "page_size", "code": "invalid_choice", ...}]`. `get_paginated_response_schema` returns the envelope object; `get_schema_operation_parameters` returns `page` and `page_size` (enum = options).
- `glowderma/core/api/schema.py`: `ErrorItemSerializer`, `ErrorEnvelopeSerializer` for `@extend_schema(responses=...)`.

- [ ] **Step 1:** Failing tests: `test_nested_list_errors_flatten` (`tiers = TierSerializer(many=True)`, bad percent in item 1 → `errors[0]["field"] == "tiers.1.percent"`); `test_nested_dict_errors_flatten` (`texts.uz.name`); `test_non_field_error_has_null_field`; `test_invalid_transition_is_409`; `test_auth_failed_is_401_on_view_without_authenticators` (view with `authentication_classes=[]` raising `AuthFailed("invalid_credentials")` → 401, code `invalid_credentials`); `test_throttled_has_retry_after_header_only`; `test_500_hides_details`; `test_unknown_route_returns_404_envelope`; `test_request_id_trusted_only_with_secret`; `test_api_responses_are_no_store`; `test_payload_too_large_413`; `test_datetime_has_seconds_precision`; `test_page_clamped_to_last` (25 items, size 10, page 99 → page 3, 5 results); `test_non_integer_page_is_one`; `test_empty_list_is_page_one`; `test_page_size_not_allowed_is_invalid_choice`.
- [ ] **Step 2–4:** fail → implement → pass; dev loop green; regenerate schema.
- [ ] **Step 5:** Commit `feat(core): error and pagination envelopes`.

### Task 4: Append-only records, audit log, time and money helpers

**Files:**
- Create: `glowderma/core/models.py`, `glowderma/core/db.py`, `glowderma/core/audit.py`, `glowderma/core/timeutils.py`, `glowderma/core/money.py`, migrations `core/0001_initial.py`, `core/0002_auditentry_append_only.py`
- Test: `glowderma/core/tests/test_append_only.py`, `test_audit.py`, `test_timeutils.py`, `test_money.py`

**Interfaces:**
- `TimeStampedModel` (abstract: `created_at`, `updated_at`). `AppendOnlyModel` (abstract): `save()` raises `AppendOnlyError` unless `self._state.adding`; instance `delete()` raises; default manager QuerySet raises on `update`, `delete`, `bulk_update`.
- `glowderma.core.db.append_only_trigger_sql(table: str) -> tuple[str, str]`: forward = `CREATE OR REPLACE FUNCTION glowderma_forbid_mutation() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'append-only table %', TG_TABLE_NAME; END $$;` + `CREATE TRIGGER {table}_append_only BEFORE UPDATE OR DELETE ON {table} FOR EACH ROW EXECUTE FUNCTION glowderma_forbid_mutation();`; reverse = `DROP TRIGGER IF EXISTS {table}_append_only ON {table};` only (never drop the shared function). Every append-only model's migration runs it via `RunSQL`.
- `AuditEntry(AppendOnlyModel)`: `created_at`, `actor_type` (`staff|customer|system|integration`), `actor_id` (BigInteger null), `actor_label`, `action`, `entity_type`, `entity_id` (str), `before`/`after` (JSON null), `reason` (text blank), `ip` (GenericIPAddressField null), `user_agent` (text blank), `request_id`. Indexes `(entity_type, entity_id)`, `created_at`, `action`.
- `audit.record(*, action, entity_type, entity_id, actor=None, actor_type=None, request=None, before=None, after=None, reason="") -> AuditEntry`: actor by duck typing (`actor.pk`, `getattr(actor, "full_name", "") or actor.get_username()`), `actor_type` defaults to `staff` when an actor is given else `system`; ip = `getattr(request, "client_ip", None) or request.META.get("REMOTE_ADDR")`; request id from `request.request_id`.
- `timeutils`: `TASHKENT = ZoneInfo("Asia/Tashkent")`, `now()`, `local_date(dt)`, `day_bounds(d) -> (start, end)`, `month_start(d)`, `isoformat(dt)` = `dt.astimezone(TASHKENT).replace(microsecond=0).isoformat()`.
- `money`: `percent_to_bp(Decimal) -> int` (rejects >2 decimal places and values outside (0, 100]), `apply_bp(amount: int, bp: int) -> int` = `amount * bp // 10_000`.

- [ ] **Step 1:** Failing tests: `test_append_only_rejects_resave`, `test_append_only_rejects_queryset_update`, `test_append_only_rejects_queryset_delete`, `test_append_only_rejects_raw_sql_update` (raw `UPDATE core_auditentry SET reason='x'` → `DatabaseError` containing `append-only`), `test_append_only_rejects_raw_sql_delete`, `test_audit_record_captures_request_meta`, `test_audit_record_without_request_is_system`, `test_day_bounds_tashkent` (2026-10-24 → `2026-10-23T19:00:00Z` .. `2026-10-24T19:00:00Z`), `test_isoformat_has_offset_and_seconds` (`…T12:00:00+05:00`), `test_apply_bp_integer_math` (`apply_bp(10_000, percent_to_bp(Decimal("0.57"))) == 57`; `Decimal("2.3")` → 230; `Decimal("1.5")` of 999 → 14), `test_percent_to_bp_rejects_three_decimals`.
- [ ] **Step 2–4:** fail → implement → pass; dev loop green.
- [ ] **Step 5:** Commit `feat(core): append-only records, audit log, time and money helpers`.

### Task 5: Idempotency-Key support

**Files:**
- Create: `glowderma/core/idempotency.py`; model `IdempotencyRecord` in `glowderma/core/models.py`; migration
- Modify: `config/celery.py` (beat entry)
- Test: `glowderma/core/tests/test_idempotency.py`

**Interfaces:**
- `IdempotencyRecord`: `audience`, `actor_id` (BigInteger, **NOT NULL**), `method`, `path` (`request.path_info`, ids included), `key` (UUID), `request_hash` (sha256 hex), `response_status`, `response_body` (JSON, `DjangoJSONEncoder`), `created_at`, `expires_at` (+24 h). `UniqueConstraint(fields=["audience", "actor_id", "method", "path", "key"])`. A row exists only for a committed 2xx (no status column). Not append-only.
- `@idempotent(required: bool = True)` decorator for APIView methods (`functools.wraps`). Requires an authenticated actor (anonymous → `ImproperlyConfigured`); never applied to `auth/*` views or through a base class. Audience = `request.auth["aud"]`; actor = `request.user.pk`. Fingerprint: JSON body → `json.dumps(parsed, sort_keys=True, separators=(",", ":"), ensure_ascii=False)`; multipart → sorted non-file fields + SHA-256 of each file's bytes; SHA-256 of that. One `transaction.atomic()` wraps everything:
  1. Header missing or not a UUID: required → `IdempotencyKeyRequired`; optional → run the view without idempotency.
  2. `SELECT pg_try_advisory_xact_lock(hashtextextended(%s, 0))` with `f"{audience}|{actor_id}|{method}|{path}|{key}"`; false → `RequestInProgress` immediately. **Never detect in-flight duplicates with a unique-index INSERT** (PostgreSQL makes the second INSERT wait).
  3. Read the scope's row; `expires_at <= timeutils.now()` → delete it and continue. Same hash → return the stored status/body with header `Idempotent-Replayed: true`. Different hash → `IdempotencyKeyReused`.
  4. Run the view. 2xx → insert the row (same transaction as the effect). Non-2xx response → `transaction.set_rollback(True)`, store nothing. Exception → rollback.
- Beat entry `core.purge_idempotency_records` daily at 03:00 Tashkent.

- [ ] **Step 1:** Failing tests with a throwaway test view (authenticated via a stub authenticator) that writes an AuditEntry per call: `test_same_key_same_body_replays_once`; `test_same_key_different_body_422`; `test_concurrent_duplicate_409` (`transaction=True`: thread A's view sets Event `entered` then waits on `release`; main thread sends B with the same key → 409 `request_in_progress` with `Retry-After: 1` within 1 s; set `release`; A → 2xx; C replays; exactly one AuditEntry); `test_non_2xx_not_stored`; `test_same_key_different_path_is_separate`; `test_expired_key_counts_as_new`; `test_multipart_same_size_different_file_422`; `test_missing_key_400_when_required`; `test_optional_mode_without_key_runs_normally`.
- [ ] **Step 2–4:** fail → implement → pass; dev loop green.
- [ ] **Step 5:** Commit `feat(core): Idempotency-Key support`.

### Task 6: Transactional outbox

**Files:**
- Create: `glowderma/core/outbox.py`, `glowderma/core/tasks.py`; model `OutboxMessage`; migration
- Modify: `config/celery.py` (beat entry)
- Test: `glowderma/core/tests/test_outbox.py`

**Interfaces:**
- `OutboxMessage`: `topic`, `payload` (JSON), `dedupe_key` (unique), `status` (`pending|in_flight|done|failed|dead`), `attempts` (int, default 0), `available_at`, `lease_expires_at` (null), `last_error` (text blank), timestamps.
- `outbox.enqueue(topic, payload, *, dedupe_key, delay=None) -> OutboxMessage` — called inside the caller's transaction; duplicate `dedupe_key` returns the existing row; schedules `transaction.on_commit(dispatch_outbox.delay)`.
- `@outbox.register(topic)` registers `handler(message: OutboxMessage) -> None`. Docstring states: delivery is **at-least-once**; handlers must be idempotent at the provider boundary.
- Task `core.dispatch_outbox(batch: int = 50)`: inside `transaction.atomic()` claim up to `batch` rows where `available_at <= now` AND (`status IN ("pending", "failed")` OR (`status = "in_flight"` AND `lease_expires_at <= now`)), ordered by `(available_at, id)`, `select_for_update(skip_locked=True)`; set `status="in_flight"`, `lease_expires_at = now + 5 min`, and `attempts += 1` when reclaiming an expired lease; commit. Run each handler outside that transaction; success → `done`. Exception → `attempts += 1`, `last_error`, `available_at = now + min(2**attempts, 3600) s`, `status="failed"`, or `"dead"` when `attempts >= 10` (AuditEntry `outbox.dead`, actor_type `system`). Beat: every 10 s.

- [ ] **Step 1:** Failing tests: `test_enqueue_dedupes`; `test_dispatch_runs_handler_and_marks_done`; `test_failure_backs_off_then_retries` (failed row with past `available_at` is claimed again); `test_dead_after_max_attempts`; `test_expired_lease_is_reclaimed_and_counted` (simulates "provider succeeded, worker crashed before marking done": the row stays `in_flight` with a past lease → next dispatch runs the handler again; the test handler's own guard keeps the external effect count at 1); `test_skip_locked_claims_disjoint_rows` (`transaction=True`, two concurrent dispatchers never run the same message).
- [ ] **Step 2–4:** fail → implement → pass; dev loop green.
- [ ] **Step 5:** Commit `feat(core): transactional outbox with leased dispatch`.

### Task 7: Client IP, admin network gate, safe exports

**Files:**
- Modify/Create: `glowderma/core/clientip.py`, `glowderma/core/network.py`, `glowderma/core/exports.py`
- Modify: `config/settings/{base,local,test,production}.py` (MIDDLEWARE, `TRUSTED_PROXIES`, `ADMIN_API_ALLOWED_NETS`, `ADMIN_API_REQUIRE_INTERNAL_SECRET`)
- Test: `glowderma/core/tests/test_clientip.py`, `test_network.py`, `test_exports.py`

**Interfaces:**
- `peer_ip(request) -> str`: walk `[*X-Forwarded-For hops (split on ",", stripped), REMOTE_ADDR]` from the right; while the current address parses and lies in `TRUSTED_PROXIES` (CIDR list, default `[]`), step one hop left; the first untrusted address is the result; an unparsable hop ends the walk with the last trusted address. (XFF is read only when REMOTE_ADDR itself is a trusted proxy.)
- `client_ip(request) -> str`: `has_internal_secret(request)` and `X-GD-Client-IP` parses with `ipaddress.ip_address` → that IP; else `peer_ip(request)`. `ClientIPMiddleware` sets `request.client_ip`. All throttles' `get_ident` return `request.client_ip`.
- `AdminNetworkMiddleware`: applies when `path_info` starts with `/api/v1/admin/`, `/api/v1/schema/`, `/api/v1/docs/` or `/django-admin/` (never `/api/v1/health/`). Peer = `peer_ip(request)` (never `X-GD-Client-IP`). Peer outside `ADMIN_API_ALLOWED_NETS`, or (`ADMIN_API_REQUIRE_INTERNAL_SECRET` and not `has_internal_secret`) → 403 envelope `permission_denied` before authentication. Local/test: nets `["0.0.0.0/0", "::/0"]`, secret not required. Production: nets required env, secret required (`True`).
- `exports.safe_cell(value)`: string starting with `=`, `+`, `-`, `@`, TAB, CR → prefixed with `'`; ints untouched. `render_csv(rows, headers) -> bytes` (UTF-8 BOM, `;`, CRLF, `csv.QUOTE_ALL`); `render_xlsx(rows, headers, sheet) -> bytes` (openpyxl; str cells `data_type="s"`, ints numeric).
- `ExportMixin` for list views: class attrs `export_name` (contract §1.8 `<name>`) and `export_resource`. `?export=csv|xlsx` (other value → 400 `validation_error`, field `export`, code `invalid_choice`) returns the whole filtered, ordered queryset (ignores `page`/`page_size`); >50 000 rows → 400 `validation_error` with `errors=[{"field": "export", "code": "export_too_large", "message": "Juda ko‘p qator. Filtrni toraytiring."}]`; 200 with `Content-Disposition: attachment; filename="<export_name>-YYYYMMDD-HHMM.<ext>"` (Tashkent); writes `audit.record(action="export.downloaded", entity_type=export_resource, entity_id="", request=request, after={"resource": export_resource, "filters": <query params minus page/page_size/export>, "row_count": n, "format": fmt})`; throttle scope `export` 10/min keyed on `request.user.pk`.

- [ ] **Step 1:** Failing tests: `test_client_ip_trusts_header_only_with_secret`; `test_client_ip_ignores_xff_from_untrusted_peer` (REMOTE_ADDR `8.8.8.8`, XFF `1.2.3.4` → `8.8.8.8`); `test_client_ip_via_trusted_proxy` (REMOTE_ADDR `10.0.0.9`, XFF `1.2.3.4, 10.0.0.5`, proxies `10.0.0.0/8` → `1.2.3.4`); `test_admin_gate_blocks_outside_peer` (nets `10.0.0.0/8`, REMOTE_ADDR `8.8.8.8` → 403); `test_admin_gate_uses_proxy_resolved_peer`; `test_admin_gate_requires_secret_when_enabled`; `test_docs_gated`; `test_health_not_gated`; `test_csv_formula_injection_neutralized` (name `=HYPERLINK("http://x","y")` → cell text starts with `'=`); `test_xlsx_cells_are_text`; `test_csv_has_bom_semicolons_and_quotes`; `test_export_writes_downloaded_audit`; `test_export_too_large_is_field_error`.
- [ ] **Step 2–4:** fail → implement → pass; dev loop green.
- [ ] **Step 5:** Commit `feat(core): client IP resolution, admin network gate, safe exports`.

### Task 8: Sessions, JWT service and staff authentication

**Files:**
- Create: `glowderma/accounts/{tokens,services,authentication,permissions,schema}.py`, `glowderma/accounts/management/commands/createstaff.py`, migration `accounts/0002_sessions_socialidentity.py`
- Modify: `config/settings/base.py` (`PASSWORD_HASHERS` Argon2 first, `AUTH_PASSWORD_VALIDATORS`, JWT settings), `glowderma/accounts/apps.py` (`ready()` imports `schema`)
- Test: `glowderma/accounts/tests/test_tokens.py`, `test_sessions.py`, `test_authentication.py`, `test_createstaff.py`

**Interfaces:**
- `AuthSession`: `id` (UUID = `sid`), `audience` (`staff|customer`), `staff` (FK null, PROTECT), `customer_id` (BigInteger null; FK in M1), `device_label` (100), `refresh_hash` (char 64, unique), `created_at`, `last_used_at`, `absolute_expires_at`, `revoked_at` (null), `revoke_reason` (`logout|expired|password_changed|deactivated|replaced|admin_revoked`, blank).
- `SocialIdentity` (used in M0b): `staff` FK (CASCADE), `provider` (`google|facebook`), `subject`, `email_at_link`, `created_at`, `last_login_at` (null); unique `(provider, subject)` and `(staff, provider)`.
- Settings: `JWT_SIGNING_KEYS` (dict kid → decoded key bytes), `JWT_ACTIVE_KID`, `JWT_ISSUER="glowderma"`, `STAFF_ACCESS_TTL=900`, `STAFF_REFRESH_TTL=43200`. `AUTH_PASSWORD_VALIDATORS`: `UserAttributeSimilarityValidator` (`user_attributes: ["email", "full_name"]`), `MinimumLengthValidator` (`min_length: 12`), `CommonPasswordValidator`, `NumericPasswordValidator`.
- `tokens.issue_access(session) -> tuple[str, datetime]`: HS256, header `kid=JWT_ACTIVE_KID`, claims `iss`, `aud=session.audience`, `sub=str(subject id)`, `sid=str(session.id)`, `jti=uuid4().hex`, `iat`, `exp = min(iat + STAFF_ACCESS_TTL, int(session.absolute_expires_at.timestamp()))`.
- `tokens.decode_access(token, *, audience) -> dict`: one `try` around `jwt.get_unverified_header(token)`, the kid lookup, and `jwt.decode(token, key, algorithms=["HS256"], audience=audience, issuer=settings.JWT_ISSUER, options={"require": ["iss", "aud", "sub", "sid", "iat", "exp", "jti"], "strict_aud": True})`; any `jwt.PyJWTError`, unknown kid, or `ValueError` from `uuid.UUID(claims["sid"])` / `int(claims["sub"])` → `AuthFailed("not_authenticated")`.
- `services.start_staff_session(user, *, device_label) -> tuple[AuthSession, str]` (raw refresh = `secrets.token_urlsafe(32)`, stored as sha256 hex; absolute lifetime `STAFF_REFRESH_TTL`; non-rotating).
- `services.refresh_staff_session(raw_refresh) -> AuthSession`: unknown → `AuthFailed("not_authenticated")`; revoked → `SESSION_REPLACED` if reason `replaced` else `SESSION_EXPIRED`; past `absolute_expires_at` or inactive user → `SESSION_EXPIRED`; updates `last_used_at`.
- `services.revoke_session(session, reason)`, `revoke_user_sessions(user, reason, *, except_sid=None) -> int` (returns count): update rows, then `transaction.on_commit(lambda: cache.set_many({f"auth:sid:{sid}": f"revoked:{reason}" ...}, timeout=STAFF_ACCESS_TTL + 60))` — a **tombstone**, never `cache.delete`, never before commit.
- `StaffJWTAuthentication`: Bearer only; `decode_access(audience="staff")`; session check via cache `auth:sid:{sid}`: `revoked:<reason>` → `AuthFailed` (`SESSION_REPLACED` / `SESSION_EXPIRED`); `active:<absolute epoch>` → accepted while `now < epoch`; miss → read row (must be `audience == "staff"` and `staff_id == int(claims["sub"])`), fill with `cache.add(key, value, timeout=max(1, min(60, seconds_until_absolute_expiry)))` (`add`, so a concurrent tombstone wins). StaffUser loaded from the DB every request (`is_active` False → `SESSION_EXPIRED`). `authenticate_header()` returns `'Bearer realm="api"'`.
- `IsActiveStaff`: `request.user` is a `StaffUser`, `is_active`, `request.auth["aud"] == "staff"`.
- `schema.StaffJWTScheme(OpenApiAuthenticationExtension)`: `target_class = "glowderma.accounts.authentication.StaffJWTAuthentication"`, `name = "StaffJWT"`, bearer JWT security scheme.
- `createstaff <email> [--superuser] [--full-name ...]`: password from `getpass` twice (must match) or `--password-stdin` (strip only a trailing `\n`/`\r\n`); no `--password` flag; runs `validate_password(password, user)` and exits non-zero on failure; non-superuser, `is_staff=False` by default (`--superuser` sets both).

- [ ] **Step 1:** Failing tests: `test_access_token_roundtrip`; `test_access_exp_capped_at_session_expiry`; `test_decode_rejects_wrong_audience`; `test_decode_rejects_unknown_kid`; `test_decode_rejects_alg_none_and_hs512`; `test_malformed_bearer_is_401` (`Bearer abc`, `Bearer a.b.c`, a validly signed token without `jti`); `test_key_rotation_old_kid_still_valid`; `test_refresh_rejects_revoked_with_reason_code`; `test_refresh_rejects_after_absolute_expiry` (monkeypatch `timeutils.now` to `absolute_expires_at + 1 s`); `test_revoke_writes_tombstone_on_commit` (`django_capture_on_commit_callbacks(execute=True)`; warm cache → revoke → next authentication fails immediately); `test_stale_fill_cannot_resurrect_revoked_session` (revoke commits between the DB read and the fill → next request 401 `SESSION_EXPIRED`); `test_inactive_staff_rejected_immediately`; `test_unauthenticated_admin_request_is_401_not_403`; `test_createstaff_creates_non_superuser`; `test_createstaff_rejects_weak_password`; `test_createstaff_preserves_password_whitespace`.
- [ ] **Step 2–4:** fail → implement → pass; dev loop green; regenerate schema.
- [ ] **Step 5:** Commit `feat(accounts): sessions, JWT service and staff authentication`.

### Task 9: Staff auth API, login guard, admin auth matrix

**Files:**
- Create: `glowderma/accounts/api/{__init__,base,throttles,guard}.py`, `glowderma/accounts/api/admin/{__init__,serializers,views,urls}.py`
- Modify: `config/urls.py` (mount `api/v1/admin/auth/`; wrap the Django-admin login with the guard)
- Test: `glowderma/accounts/tests/test_auth_api.py`, `glowderma/accounts/tests/test_admin_auth_matrix.py`, `test_django_admin.py`

**Interfaces:**
- `accounts.api.base`: `AdminAPIView(APIView)` and `AdminGenericViewSet` with `authentication_classes=[StaffJWTAuthentication]`, `permission_classes=[IsActiveStaff]`, `throttle_classes=[StaffRateThrottle]` (scope `staff`, 1200/min, ident = staff id). Every M1 admin view inherits these.
- `LoginGuard` (a service, not a DRF throttle) in `accounts/api/guard.py`: key `h = sha256(email.strip().lower())` from the validated serializer value. `check(scope, h)` runs after validation and **before** `authenticate()`: raises `Throttled(wait)` while `auth:lock:{scope}:{h}` exists (a locked email gets 429 even with the right password; unknown emails identical). `record_failure(scope, h)` increments a 60 s counter; the 5th failure sets the lock for `min(60 * 2**n, 900)` s where n = locks in the last 24 h (`auth:lockn:{scope}:{h}`, TTL 24 h). `reset(scope, h)` on success clears the counter only. Scopes `login`, `change_password`, `django_admin_login`.
- Throttles (`accounts/api/throttles.py`, `SimpleRateThrottle` subclasses keyed on `request.client_ip` / token hash): login per IP 30/min; refresh per session 120/min (key `sha256(refresh)`).
- Serializers: `email = EmailField(max_length=254)`; `password`, `current_password`, `new_password` = `CharField(max_length=128, trim_whitespace=False)`.
- Endpoints (contract §2.1; login/refresh/logout have no authenticators; `@extend_schema(operation_id="admin_auth_<name>", ...)` with 400/401/429 envelope responses):
  - `POST login {email, password}` → `200 {access, access_expires_at, refresh, refresh_expires_at, user: Me}`; wrong credentials, unknown email or inactive staff → 401 `invalid_credentials` (same code path: `authenticate()` hashes for unknown users). `device_label` = `User-Agent[:100]`.
  - `POST refresh {refresh}` → `200 {access, access_expires_at, refresh_expires_at}` (refresh unchanged).
  - `POST logout {refresh, scope: "current"|"others"|"all" = "current"}` → 204 (`current` revokes the token's session; `others` revokes the staff member's other sessions; `all` revokes all; reason `logout`); unknown/revoked refresh → 401 `SESSION_EXPIRED`; bad scope → 400 `invalid_choice`.
  - `GET me` → `Me = {id, email, display_name (full_name or email), is_superuser, linked_providers: [providers of SocialIdentity], session_expires_at (absolute_expires_at of request.auth["sid"])}`.
  - `POST change-password {current_password, new_password}` → 204; guard scope `change_password`; validators + `password_unchanged`; revokes all other sessions (`password_changed`).
  - Auth views ignore `Idempotency-Key` (never decorated).
  - Audit: `auth.login_succeeded`, `auth.login_failed` (`{email, method: "password"}`), `auth.logout` (`{scope, revoked_count}`), `auth.password_changed`.
- Django admin (`DJANGO_ADMIN_ENABLED`): its login view goes through `LoginGuard` scope `django_admin_login`.
- `test_admin_auth_matrix`: iterate `get_resolver()` for every pattern under `api/v1/admin/`; exempt only (`auth/login/`, POST), (`auth/refresh/`, POST), (`auth/logout/`, POST) (M0b adds `auth/oauth/<provider>/` POST). For each remaining (route, method) assert 401 for: no auth; `Authorization: Basic <valid staff creds>`; a logged-in Django session cookie; a JWT with `aud=customer`; a JWT for a revoked session. Must keep passing as M1 adds routes.

- [ ] **Step 1:** Failing tests: `test_login_success_returns_tokens_and_audits` (asserts the `user` keys exactly); `test_login_email_is_case_insensitive`; `test_login_bad_password_is_401_not_403`; `test_login_unknown_email_same_response_as_bad_password`; `test_locked_account_rejects_correct_password`; `test_lockout_after_5_failures_also_for_unknown_email` (6th → 429 `throttled` with `Retry-After`); `test_lock_counter_resets_after_success`; `test_login_ip_throttle_ignores_spoofed_xff`; `test_password_whitespace_preserved`; `test_login_ignores_idempotency_key` (two logins with the same key → two sessions, `IdempotencyRecord.objects.count() == 0`); `test_refresh_returns_new_access_same_session` (`refresh_expires_at` unchanged); `test_logout_revokes_refresh`; `test_logout_others_keeps_current_session`; `test_logout_all_revokes_every_session`; `test_me_requires_staff_token`; `test_change_password_revokes_other_sessions`; `test_django_admin_not_mounted_when_disabled`; `test_django_admin_login_locked_after_5_failures`; `test_admin_auth_matrix`.
- [ ] **Step 2–4:** fail → implement → pass; dev loop green; regenerate schema.
- [ ] **Step 5:** Commit `feat(accounts): staff auth API, login guard and admin auth matrix`.

### Task 10: OpenAPI serving and CI

**Files:**
- Create: `.github/workflows/ci.yml`
- Modify: `config/urls.py`, `config/settings/{base,local}.py` (`SPECTACULAR_SETTINGS`), `openapi/schema.yaml`
- Test: `glowderma/core/tests/test_schema.py`

**Interfaces:**
- `GET /api/v1/schema/` (`SpectacularAPIView`) and `/api/v1/docs/` (`SpectacularSwaggerView`, sidecar) mounted in `config/urls.py`. `SPECTACULAR_SETTINGS`: title "Glow Derma API", version "1.0.0", `COMPONENT_SPLIT_REQUEST=True`, `SERVE_INCLUDE_SCHEMA=False`, `SWAGGER_UI_DIST="SIDECAR"`, `SWAGGER_UI_FAVICON_HREF="SIDECAR"`, `SERVE_AUTHENTICATION=["glowderma.accounts.authentication.StaffJWTAuthentication"]`, `SERVE_PERMISSIONS=["glowderma.accounts.permissions.IsActiveStaff"]`; `config/settings/local.py` overrides them to `[]` and `["rest_framework.permissions.AllowAny"]`. Both routes are behind `AdminNetworkMiddleware`.
- CI (`push`, `pull_request`, `ubuntu-latest`): services `postgres` (`postgres:18-trixie`, env `POSTGRES_USER/PASSWORD/DB=glowderma`, `ports: ["5433:5432"]`, health `pg_isready -U glowderma -d glowderma`) and `redis` (`redis:8-alpine`, `ports: ["6380:6379"]`, health `redis-cli ping`). Steps: checkout; `astral-sh/setup-uv` with `version: "0.13.5"`, `python-version: "3.14"`, `enable-cache: true`; `uv sync --locked`; `uv run ruff check .`; `uv run ruff format --check .`; `uv run mypy .`; `uv run lint-imports`; `uv run python manage.py makemigrations --check --dry-run`; `uv run coverage run -m pytest`; `uv run coverage report --fail-under=85`; `uv run python manage.py spectacular --validate --fail-on-warn --file /tmp/schema.yaml`; `diff -u openapi/schema.yaml /tmp/schema.yaml`.

- [ ] **Step 1:** Failing tests: `test_committed_schema_is_current` (generate in-process with drf-spectacular's `SchemaGenerator`, compare to `openapi/schema.yaml`); `test_docs_require_staff_outside_debug`; `test_schema_declares_error_envelope_and_staff_jwt`.
- [ ] **Step 2–4:** fail → implement → pass; full dev loop green.
- [ ] **Step 5:** Commit `chore: OpenAPI serving and CI workflow`.

---

## Done when
- All tasks committed on `feat/m0-foundation`; the AGENTS.md dev loop is green on the host; reply with the final SHA, the per-task commit list and any contract deviations.
- Claude's Docker gate: `docker compose up -d --build` (after stopping `gd-dev-postgres`/`gd-dev-redis`) → api and worker healthy; `curl 127.0.0.1:8000/api/v1/health/?ready=1` → 200; `docker compose run --rm api manage check --deploy` with a production env file → clean; `createstaff` + `curl` login with `X-GD-Internal-Secret` returns tokens; the auth matrix passes in the container.
