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
factory-boy 3.3.3 · coverage · import-linter · pytest-xdist 3.8.0 · pytest-split 0.11.0.
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
  processes against the same `TEST_DB_NAME`. `-n auto` (pytest-xdist) runs the suite on every CPU: the workers
  use `<TEST_DB_NAME>_gw<n>` databases cloned from one `<TEST_DB_NAME>_template` migrated once per run (root
  `conftest.py`); `-n 0` (the default) for a single test or a debugger. CI runs it in 4 shards (pytest-split,
  `.test_durations`; refresh that file from the `test-durations` artifact of a CI run when shards drift apart).
- Redis DB 0 is the Django cache, DB 1 the Celery broker. Tests use LocMem; never call `cache.clear()` in
  application code. `config/__init__.py` loads the Celery app (`celery_app`) so `shared_task.delay()` from a
  request reaches the configured app (eager in tests, Redis otherwise); beat entries are declared centrally in
  `config/celery.py` (`beat_schedule`) — add yours there and list the file in `shared_files_touched`.

## Dev loop
Run from the repository root and stop at the first non-zero exit:
```
export UV_SYSTEM_CERTS=1
uv sync
TEST_DB_NAME=test_gd_<branch> uv run pytest -q -n auto
uv run ruff check .
uv run ruff format --check .
uv run mypy .
uv run lint-imports
uv run python manage.py makemigrations --check --dry-run
uv run python manage.py spectacular --validate --fail-on-warn --file openapi/schema.yaml
```
`config.settings.test` uses the dev Postgres on 5433, `CELERY_TASK_ALWAYS_EAGER=True` and LocMem cache. Every
task that changes the API regenerates and commits `openapi/schema.yaml`.
**M2 exception (packages P0–P3 of `docs/plans/2026-10-26-m2-customer-api.md`, branches `feat/m2-p*`):** a package
runs the `spectacular --validate --fail-on-warn` step above but does **not** commit `openapi/schema.yaml` (write it
to a scratch path, e.g. `--file /tmp/m2-schema.yaml`, or restore the tracked file before committing); the lead
regenerates, diffs and commits the schema once on `feat/m2-integration`. Every other branch keeps the rule above.
Coverage must stay ≥ 85 %
(`uv run coverage run -m pytest -n auto`, `uv run coverage combine`, then `uv run coverage report
--fail-under=85`: coverage writes one data file per process). A task is done only when all of
the above pass; report the commit SHA and the command outputs' summary.

## Docker (lead / CI only)
`docker compose up -d --build` starts api, worker, beat, postgres (published 5433), redis (published 6380). There
is no daemon in the cloud session; CI builds the image and the user runs the compose smoke test.

## Architecture rules
- Modular monolith: apps under `glowderma/<module>/` with `models`, `services` (transactional writes),
  `selectors` (reads), `api/admin/`, `api/customer/`, `tasks`, `tests/`. Cross-module writes only through the
  other module's `services`, reads through `selectors`; FKs by string reference (`"app.Model"`).
- Split by subject, behind a facade. A module too large for one subject becomes a package with one subject per
  file and a pure facade `__init__.py` (re-exports and an explicit `__all__`, nothing else), so callers keep
  importing the old path (`from glowderma.<module> import services`, `selectors`, `models`, `tasks`,
  `api.admin.views`, …). Precedent: `glowderma/accounts/` — `models/` (staff, customers, devices, otp_codes,
  auth_sessions, defaults), `services/` (common, otp, customer, staff), `selectors/`, `tasks/` (one module per
  outbox handler), `admin/`, `authentication/`, `mfa/`, `second_factor/`, `security/` (lockout primitives),
  `api/throttles/`, `api/admin/{views,serializers}/` with `api/admin/payloads.py` (response bodies), and
  `console/views/` (one module per sign-in step). Shapes are checked by `accounts/tests/test_services_package.py`,
  `models/test_models_package.py` and `test_package_facades.py`.
  - Submodules import siblings as modules (`locks.lock_customer(...)`) with absolute imports, never a name through
    their own facade, and their layering is an exhaustive `.importlinter` contract (the whole accounts app has
    one too: a new accounts module must be placed in it).
  - A test patches a name where it is looked up: on the defining submodule for a call made inside the package,
    on the facade for a call another module makes through `services.<name>`. Never re-export a name only so a
    test can patch it (the patch would silently do nothing).
  - Submodules log under the package's logger name (`glowderma.accounts.services`, `glowderma.accounts.tasks`).
  - `models/__init__.py` imports every model (Django loads them from there); names migrations reference
    (`accounts.models.date_joined_default`, `accounts.media.customer_image_path`) must keep resolving.
  - `tasks/__init__.py` imports every handler module (outbox handlers register on import); Celery task names
    are explicit `name=` strings.
  - Moving a file with marked texts or templates: run `manage.py console_messages` (the `.po` files record source
    paths; the `.mo` files must not change). Moving or adding a class must keep `openapi/schema.yaml`
    byte-identical: never rename a serializer, never add a docstring to a base class or mixin, keep field order.
  - Tests mirror the source layout (`accounts/tests/{api/admin, console, services/staff, …}`); helper modules
    other apps import by path stay at the root of their `tests/` package.
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
  `core.media.media_name()`; URLs come from `core.media.absolute_media_url()` (`PUBLIC_MEDIA_BASE_URL`). Right
  after saving the original, call `core.variants.store_variants(field.storage, field.name, content)` (400 px and
  1000 px WebP copies); delete or replace a file only through `core.variants.delete_with_variants()`. API image
  fields come with `<prefix>_thumbnail_url` / `<prefix>_medium_url` siblings built by `core.media.url_fields()` /
  `optional_variant_url()`; `manage.py backfill_image_variants` writes missing variants
  (`docs/specs/2026-11-13-image-variants-design.md`).
- Throttle scopes and their default rates live in `glowderma/core/throttling.py` (`DEFAULT_THROTTLE_RATES`,
  overridable by the `THROTTLE_RATES` env JSON); a new throttle class must add its scope there or settings
  validation fails. `LOG_LEVEL` sets the `glowderma.*` logger level; every log line carries `request_id`.
- The Django admin is read-only: register models through `glowderma.core.admin.register_read_only` (`ReadOnlyAdmin`);
  never expose `password` or `refresh_hash`. Only the professionals models and the Yandex Delivery warehouse pages
  become editable, through services, with `glowderma.core.admin.ServiceBackedAdmin` (abstract `service_save` /
  `service_delete` hooks; a `ValidationError` from a hook rolls back and re-renders the form; professionals keeps a
  thin subclass): `DeliverySettings` (the singleton page `/django-admin/delivery/deliverysettings/1/change/`, saved
  through `delivery.services.update_settings`, with the detail action «Yandex bilan tekshirish» →
  `check_yandex_tariffs`, which runs **outside** a transaction), `WarehouseHours` and `WarehouseClosure`
  (`delivery.services.save_hours` / `delete_hours` / `save_closure` / `delete_closure`); every write is audited
  (`delivery.*`). `CourierClaim`, `DeliveryIssue`, `DeliveryQuote` stay read-only.
  One exception that is not a ModelAdmin: the settings page `/django-admin/settings/` (`admin:site_config_settings`,
  `glowderma/site_config/admin_views.py` + `forms.py`, mounted by `UNFOLD["SITE_VIEWS"]` in
  `config/settings/ops_admin.py`) edits the store settings, contacts and legal texts and publishes the terms, writing
  only through `site_config.services` (`update_settings`, `publish_terms(expected_fingerprint=…)`); `SiteSettings`
  itself stays a `ReadOnlyAdmin` registration. `site_config` has no `api/admin/`: the admin API's `settings/` and
  `settings/terms/publish/` were retired (contract §2.11), so it is not in `config.urls.ADMIN_API_MODULES`.
- The ops console speaks English, Russian and Uzbek (`glowderma/core/console_i18n.py`, switcher cookie
  `gd_ops_language`, default `OPS_DEFAULT_LANGUAGE`); only `/django-admin/` switches, everything else stays
  `LANGUAGE_CODE = "uz"`. The admin API answers in uz / ru / en by `Accept-Language`
  (`docs/api/admin-api-v1.md` §1.2, §1.6, §1.8).
  **Uzbek is the source language** of the ops console and of the admin API. Console texts: write them in Uzbek
  and mark them (`gettext_lazy` at import time, `gettext` / `ngettext` with `%(name)s` placeholders in
  functions, `{% translate %}` / `{% blocktranslate %}` in templates); model `verbose_name` / `help_text` /
  `choices` need no marking; an enum label or `core.labels` value goes through `gettext(value)` where the
  console renders it. **Admin API texts** a staff member reads — error messages (`raise`, `field_error`,
  `ErrorDetail`, `ContractValidationError`, `default_detail`, `default_error_messages`, `MESSAGE*` constants),
  export headers and `export_name` — are Uzbek literals marked with `glowderma.core.uzsource.N_` (a no-op),
  interpolated as `N_("… %(n)s …") % {...}` (never an f-string). They are translated only at the edge: the
  error envelope renders `message` / `errors[].message` in the request's `Accept-Language`
  (`core.api.language.admin_language`), and `ExportMixin` renders headers, sheet title and file name with
  `uzsource.tr`, values with `labels.text(mapping, key)`. A header whose translation depends on the list (Russian
  gender, «Название» vs «Имя») is marked `NC_("export <export_resource>", "<Uzbek>")` in `core/labels.py`
  `_EXPORT_RENDERINGS` and collected as a `msgctxt` entry. **Never translate what is stored or sent** (audit
  rows, outbox payloads, notifications, events, e-mail bodies — e-mails use `core.i18n.t` with the
  recipient's language) and never activate a language around service code; `tr` / `localize_admin_message`
  are for response rendering only. `core/tests/test_admin_api_languages.py` fails on an unmarked staff-visible
  Uzbek literal outside `NOT_YET_MARKED` (which only shrinks; it is empty now) and on a stored Cyrillic text.
  Customer, POS and public views keep `core.i18n` keys. After changing a console text or a marked text run
  `manage.py console_messages`, translate the new entries of `locale/en` and `locale/ru`, then
  `manage.py compile_locale`; `console_messages --check`, `compile_locale --check` and
  `test_console_languages.py` fail otherwise. Import `StrOrPromise` / `_StrPromise` only under `TYPE_CHECKING`
  (django-stubs is a dev dependency).
- Provider adapters live in `glowderma/integrations/<provider>/` behind a port (Protocol) with a fake and a live
  implementation chosen by `*_MODE` settings (default `INTEGRATIONS_MODE=fake`); views and services obtain them
  through the module's `registry`. `integrations` cannot host admin views (`AdminAPIView` lives in `accounts`,
  above it): provider-facing admin routes (`integrations/billz/import/`, `jobs/<pk>/`) are declared by the
  orchestrating module (`catalog`) while the paths stay as the contract says. Billz: `integrations/billz/{ports,fake,
  registry}.py` (`FakeBillz`: a known product returns its own fields, an id ending in `0000` is not found, anything
  else is synthetic; an import always yields the three `46070199xx` new products once). Yandex Delivery:
  `integrations/yandex_delivery/{ports,protocol,fake,live,registry}.py` (`YANDEX_DELIVERY_MODE`; `FakeYandexDelivery`
  is deterministic, idempotent per `request_id`, scripted through `Order.metadata["fake"]["yandex"]` /
  `X-GD-Fake-Scenario` — see `docs/integrations/yandex-delivery.md` §5 — and process-wide: tests call
  `fake.reset()`; under the production posture it invents nothing) plus `manage.py yandex_delivery_check`
  (`integrations/management/commands/`). Jobs go through
  `integrations.services` (`start_job` single-flight, `claim_job`, `report_progress`, `finish_job`, `fail_job`,
  `reap_stale_jobs`). OAuth: `integrations/oauth/{ports,fake,google,facebook,registry}.py` (fake
  codes `fake:<subject>:<email>[:hd]`, `fake-unverified:…`, `fake-fail`, `fake-down`; FakeGoogle nonce
  `nonce-<subject>` or `*`). Adapters never put a secret or bearer token in a URL (POST bodies / `Authorization`
  headers only), retry only on transport errors, and map failures to `OAuthExchangeError`-style typed errors.
  **Billz is the documented exception** (`integrations/billz/client.py`): its 429 is Billz's documented rate-limit
  signal (a shared limiter pauses every worker), and transport errors / 5xx are retried only for calls that are
  safe to repeat (reads, absolute upserts) or that certainly never reached Billz.
- **Live adapters (M3: Click, Payme, BTS, Eskiz, FCM; Yandex Delivery)** share `glowderma/integrations/httpclient.py`: one
  `Deadline` per port call (10 s; 4 s on a customer request path), `send()` retries only what never left
  (`TransportFailure(sent=False)`), and **every** other failure once the request may have left — a timeout, a body
  that does not decode, any unexpected exception — is `TransportFailure(sent=True)` ("outcome unknown", never
  resent by the adapter); a watchdog shuts the socket down at the deadline (headers included); a Celery soft time
  limit is re-raised as it is. `Answer.ok` is any 2xx, `Answer.not_processed` a redirect (never followed),
  401/403/408/425/429: map it to the provider's "unavailable, nothing happened" error, never to a definitive
  refusal. Parse provider codes with `httpclient.int_code` (strict ASCII digits) and every answer shape
  defensively: an unexpected shape is the provider's Unavailable error, never an untyped exception; provider
  free text goes through `httpclient.safe_text` (Eskiz also masks 4-digit runs: an echoed OTP). Bearer tokens
  (BTS, Eskiz) live only in the cache through `integrations.tokens.SharedToken` (key scoped by
  `account_scope(base_url, login)`, single-flight `cache.add` lock of 45 s with the fetch bounded to half of it,
  owner-checked unlock, a `LoginRefused` remembered 60 s, a cache outage fails closed with a process-local
  single-flight fallback). Click's port types the outcomes the refund state machine relies on:
  `ClickRefundRejected` (definitive), `ClickNotProcessed` (nothing happened: the handler releases its claim),
  `ClickUnavailable` (unknown) and `ClickStatusUnknown` (a status it cannot name: never `paid`/`reversed`); any
  other exception after the refund claim records `unknown`. FCM results add `not_sent` (give the claim back,
  send later) and `unavailable` (unknown: never resent). Yandex Delivery's port types `YandexRejected(code)` (a
  documented 4xx, or our own `invalid_request` before sending), `YandexConflict(code)` (409 of accept / cancel /
  return / tracking links) and `YandexUnavailable(outcome_unknown=…, status=…)`; `claims/create` is idempotent per
  `request_id`, so the **caller** retries it with the same stored UUID (the adapter never resends); every price must
  be in `YANDEX_DELIVERY_CURRENCY`. No BTS call runs under the Order row lock. Tests replay
  documented answers with `integrations/tests/live_fixtures.Script` (`Script().on(method, path, *responders)`
  over `httpx.MockTransport`; an unscripted route answers 599) and `monkeypatch.setattr(httpclient, "new_client",
  …)` for code that builds its own client — never the network.
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
  gives the whole test session a temporary `MEDIA_ROOT`. A migrated database already holds cashback policy v1
  from `loyalty.0006` (GS-6): a plain `seed_demo` keeps it and adds the design v2/v3 (credits, №10248 and
  Malika's ledger still reproduce), but the design's policy-history cards (v1 "20 mln gacha 1%") need
  `seed_demo --reset`; the test settings disable the bootstrap (`LOYALTY_BOOTSTRAP_POLICY=False`). `settings.FAKE_NOW` (env `FAKE_NOW`, local/test only) pins
  `timeutils.now()` for demos and e2e runs.
- **Yandex Delivery calls** (`commerce.yandex_flow`, `commerce.services.cancel_at_carrier`): never inside a database
  transaction and never under the Order row lock — plan under the Order lock and commit, call Yandex holding only the
  session advisory lock `commerce.locks.yandex_lock(order_id)` (`pg_try_advisory_lock(hashtext('yandex:{order_id}'))`;
  busy → `YandexBusy`: the dispatch handler defers, an admin request answers 409), then re-lock and record. Every
  flow that calls Yandex for an order holds that lock; views call Yandex in `post()` **before** their `@idempotent`
  inner method (skipped on a replay). `commerce.services.apply_yandex_claim` is the single writer of claim
  statuses. `accept` is sent **at most once** per claim, after `accept_sent_at` is committed; only an answer proving
  Yandex did not take it ("not processed", or 409 `old_version`) releases the stamp
  (`delivery.services.release_accept_sent`) so a later run re-checks the offer and sends one accept again — an
  accept whose outcome is unknown is never resent, the claim is re-read. A carrier `claims/cancel` /
  `claims/return` is never sent again once Yandex answered it 2xx or its outcome is unknown: right after it, in
  its own short transaction, the marker is committed — `CourierClaim.cancel_sent_at` + `cancel_state`
  (+ `cancel_price`, a paid cancel's `cancel-info` price) or a `return_requested` `ShipmentEvent` — and every retry
  only re-reads the claim (except that a sent cancel whose claim the courier has since picked up takes the return
  path: one `claims/return`, itself marked). A refusal (409 / another 4xx, unless the re-read shows the change all the same) or a
  "not processed" answer writes no marker and clears the stamp, so a later attempt may send the call again; so may
  one after a lost marker (below). The stamp `Shipment.cancel_requested_at` keeps its first time; the cancel clears
  it only when Yandex provably changed nothing (nothing sent: the first read or `cancel-info` failed, the call not
  processed; terms `unavailable`; a definitive refusal the re-read confirms; delivered; no point B id; a parcel
  gone after pickup), and staff confirming the return of an order that stays open (`confirm-return`) clear it
  too. The marker's transaction waits up to `CARRIER_RECORD_WAIT` (15 s) for the Order row (txn A and txn B wait
  the 5 s admin lock wait) and, past it, writes the marker alone without the Order lock (claim row lock, or an
  insert-only event), waiting up to `CARRIER_MARK_WAIT` (5 s) more; the snapshot writer's own Order-row waits in
  the same request (`apply_yandex_claim` for the first read and every re-read) are unbounded, as in every call of
  that writer. A crash between Yandex's answer and that commit, or both waits running out (ERROR
  `yandex.carrier_marker_lost`), loses the marker (a known limit, `docs/integrations/yandex-delivery.md` §12).
- Retention jobs delete from `core.db.PURGEABLE_TABLES` only inside `core.db.purge_window()`; the outbox offers
  `outbox.on_dead(topic)` hooks for the orchestrating module to record what a dead message means.
- Rows that must always exist (the `SiteSettings` and `DeliverySettings` singletons, the seven `IntegrationState`
  rows — billz, bts, click, payme, eskiz, fcm, yandex —, status templates,
  …) are created by a data migration **and** re-ensured by a `post_migrate` receiver in the app's `AppConfig.ready()`
  (pattern: `glowderma/integrations/apps.py` `ensure_provider_rows`, `glowderma/delivery/apps.py` `ensure_settings`,
  `glowderma/site_config/apps.py`
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
