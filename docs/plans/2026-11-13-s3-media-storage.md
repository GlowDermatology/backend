# S3 Media Storage Switch Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Media can be stored on local disk (default) or in any S3-compatible bucket (`USE_S3`). The deployed
stack can optionally pull and run MinIO (`MINIO_ENABLED`).

**Architecture:** A pure settings helper builds `STORAGES["default"]` from env, either `FileSystemStorage` or
`storages.backends.s3.S3Storage`. Public URLs stay `PUBLIC_MEDIA_BASE_URL` + stored name. In infra, MinIO and a
one-shot `minio-init` sit behind Compose profiles that `lib.sh` turns on from `stack.env`. Caddy serves
`/media/*` from disk or proxies to MinIO, and Vault supplies the credentials.

**Tech Stack:** Django 6.1, django-storages[s3] (boto3), moto (tests), Docker Compose profiles, MinIO + mc, Caddy
2.11, Vault Agent templates, bash.

**Spec:** `backend/docs/specs/2026-11-13-s3-media-storage-design.md`

Two git repos: `backend/` (tasks 1-3) and `infra/` (tasks 4-7). Both are on `main`. Each starts with
`git switch -c feat/s3-media-storage`.

## Global Constraints

- Local disk stays the default everywhere: `USE_S3` defaults to `false` and `MINIO_ENABLED=false` in both
  `stack.env` files.
- No signed URLs: `querystring_auth=False`, `default_acl=None`, `file_overwrite=False`.
- S3 defaults: `S3_REGION_NAME=us-east-1`, `S3_ADDRESSING_STYLE=path`, `S3_LOCATION=media`, `S3_VERIFY=true`.
- Stored names keep `<kind>/<owner>/<sha256-prefix>.<ext>`.
- No secret in `stack.env` (public repo). Credentials come only from Vault: `secret/<env>/minio` (`root_user`,
  `root_password`, datastores only) and `secret/<env>/s3-app` (`access_key_id`, `secret_access_key`, backend +
  datastores).
- Error messages name keys, never values.
- MinIO and mc images are pinned by tag and digest. MinIO runs as `999:999` (the datastores agent's UID, so it
  can read the 0400 secret files) with the `x-hardened` anchor, and publishes no ports.
- Fixed IPv4 literal for MinIO on `front`: `172.30.0.40`. MinIO is also on `back`.
- New dependencies are pinned exactly, matching `pyproject.toml` style.

## Review Focus

1. **An existing environment deploys before `secret/<env>/s3-app` exists.** The backend agent's template would
   block and the API would never start. Expected: `generate-app-secrets.sh` creates both paths idempotently, and
   `deploy.sh` refuses early with the exact command to run (Task 4, Task 7).
2. **`USE_S3=true` is turned on before existing files are copied.** Every old image would 404. Expected:
   `media_storage_check` lists stored names missing from the active storage and exits 1, and `deploy.sh` runs it
   (Task 2, Task 7).
3. **`/media/` as a probe into MinIO.** `/media/`, `/media/../x` or `/media/%2e%2e/...` must not list the bucket
   or reach another bucket or prefix. Expected: 404 or the object only (Task 6).
4. **The same image uploaded twice.** S3 must behave like disk: same resulting name and URL behavior as
   `FileSystemStorage`, no orphan objects after replace or delete (Task 2).
5. **`USE_S3=true` with the endpoint at `172.30.0.40` while `MINIO_ENABLED=false`.** Expected: `validate.sh`
   refuses it with a clear message (Task 5).

---

### Task 1: Storage switch in settings (backend)

**Files:**
- Create: `backend/config/storage.py`
- Modify: `backend/config/settings/base.py` (after `MEDIA_ROOT`, around line 146), `backend/config/settings/production.py:483-486`, `backend/config/secrets.py` (`TOPOLOGY_KEYS`), `backend/pyproject.toml`, `backend/uv.lock`
- Test: `backend/glowderma/core/tests/test_storage_settings.py`

**Interfaces:**
- Produces: `config.storage.media_storage() -> dict[str, Any]`, the value of `STORAGES["default"]`, read from
  `os.environ` through `config.env`. `config.storage.S3_BACKEND = "storages.backends.s3.S3Storage"`.
  `base.STORAGES` holds `"default": media_storage()` and the plain staticfiles backend. `production.py` replaces
  only `"staticfiles"`.

- [ ] **Step 1: Add the dependencies**

Run: `cd backend && uv add "django-storages[s3]==<latest>" && uv add --dev "moto[s3]==<latest>"` (exact current
versions).
Expected: `pyproject.toml` and `uv.lock` updated, and `uv run python -c "import storages, boto3, moto"` exits 0.

- [ ] **Step 2: Write the failing tests** (use `monkeypatch.setenv`/`delenv`, and clear every `USE_S3`/`S3_*`
  key in a fixture)

```python
def test_default_is_filesystem():
    assert media_storage() == {"BACKEND": "django.core.files.storage.FileSystemStorage"}

def test_use_s3_builds_s3_options(s3_env):  # USE_S3=true, bucket, keys, endpoint http://minio:9000
    cfg = media_storage()
    assert cfg["BACKEND"] == "storages.backends.s3.S3Storage"
    assert cfg["OPTIONS"] == {
        "bucket_name": "glowderma-media", "endpoint_url": "http://minio:9000",
        "access_key": "AK", "secret_key": "SK", "region_name": "us-east-1",
        "addressing_style": "path", "location": "media", "verify": True,
        "querystring_auth": False, "default_acl": None, "file_overwrite": False,
    }

def test_endpoint_optional_for_aws(s3_env, monkeypatch):
    monkeypatch.delenv("S3_ENDPOINT_URL")
    assert media_storage()["OPTIONS"]["endpoint_url"] is None

@pytest.mark.parametrize("key", ["S3_BUCKET_NAME", "S3_ACCESS_KEY_ID", "S3_SECRET_ACCESS_KEY"])
def test_missing_required_names_key_not_value(s3_env, monkeypatch, key):
    monkeypatch.delenv(key)
    with pytest.raises(ImproperlyConfigured, match=key) as exc:
        media_storage()
    assert "SK" not in str(exc.value)

def test_bad_addressing_style_refused(s3_env, monkeypatch):  # allowed: path, virtual, auto
    monkeypatch.setenv("S3_ADDRESSING_STYLE", "sideways")
    with pytest.raises(ImproperlyConfigured, match="S3_ADDRESSING_STYLE"):
        media_storage()

def test_production_keeps_switch(load_fresh_production, s3_env, required_production_env):
    prod = load_fresh_production()
    assert prod.STORAGES["default"]["BACKEND"] == "storages.backends.s3.S3Storage"
    assert prod.STORAGES["staticfiles"]["BACKEND"] == "whitenoise.storage.CompressedManifestStaticFilesStorage"

@pytest.mark.parametrize("key", ["USE_S3", "S3_ENDPOINT_URL", "S3_BUCKET_NAME", "S3_REGION_NAME",
                                 "S3_ADDRESSING_STYLE", "S3_LOCATION", "S3_VERIFY"])
def test_secrets_file_cannot_set_s3_topology(key):
    with pytest.raises(ImproperlyConfigured, match=key):
        parse_secrets(json.dumps({key: "x"}), source="f")
```

Reuse `load_fresh_production` and the required-env setup from `glowderma/core/tests/test_settings.py` (move
shared fixtures to a `conftest.py` there if needed).

- [ ] **Step 3: Run to verify they fail**

Run: `uv run pytest glowderma/core/tests/test_storage_settings.py -v`
Expected: FAIL, `ModuleNotFoundError: config.storage`.

- [ ] **Step 4: Implement `media_storage()` and wire it**

In `config/storage.py`, `USE_S3` is read with `env_bool`, and the required keys are checked all at once (one
error listing every missing key). In `base.py`, set
`STORAGES = {"default": media_storage(), "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"}}`.
In `production.py`, use `STORAGES = {**STORAGES, "staticfiles": {...whitenoise...}}` and keep the comment. Add the
7 keys to `TOPOLOGY_KEYS` and mention them in the module docstring.

- [ ] **Step 5: Run the new tests and the full suite**

Run: `uv run pytest -q && uv run ruff check . && uv run mypy . && uv run lint-imports`
Expected: all pass, and existing tests are unchanged (the default is still FileSystemStorage).

- [ ] **Step 6: Commit**

```bash
git add config/storage.py config/settings/base.py config/settings/production.py config/secrets.py pyproject.toml uv.lock glowderma/core/tests/
git commit -m "feat(storage): USE_S3 switch between local disk and S3-compatible media storage"
```

### Task 2: Media commands and S3 behavior parity (backend)

**Files:**
- Create: `backend/glowderma/core/management/commands/sync_media_to_s3.py`, `backend/glowderma/core/management/commands/media_storage_check.py`, `backend/glowderma/core/media_storage.py`
- Test: `backend/glowderma/core/tests/test_media_storage.py`, `backend/glowderma/catalog/tests/test_s3_parity.py`

**Interfaces:**
- Consumes: `config.storage.S3_BACKEND` (Task 1).
- Produces:
  - `core.media_storage.stored_names() -> Iterator[str]`: every non-empty value of every `FileField`/`ImageField`
    across installed models, plus the image-path columns that store names as text (e.g. commerce
    `OrderItem.image_path`).
  - `core.media_storage.missing_names(storage) -> list[str]`
  - Management commands `sync_media_to_s3 [--apply]` and `media_storage_check`, exit code 0 = ok, 1 = problem.
- Test fixture `s3_storage` (conftest in `glowderma/core/tests/`): `moto.mock_aws` with a created bucket and
  `override_settings(STORAGES=...)` from `media_storage()` with test env.

- [ ] **Step 1: Write the failing tests**

```python
def test_sync_dry_run_uploads_nothing(s3_storage, media_file):     # media_file: categories/1/abc.webp on disk
    call_command("sync_media_to_s3")
    assert not s3_storage.exists("categories/1/abc.webp")

def test_sync_apply_uploads_missing_and_is_idempotent(s3_storage, media_file):
    call_command("sync_media_to_s3", "--apply")
    call_command("sync_media_to_s3", "--apply")
    assert s3_storage.exists("categories/1/abc.webp")
    assert s3_storage.listdir("categories/1")[1] == ["abc.webp"]   # no abc_xyz.webp duplicate

def test_sync_refuses_when_default_storage_is_disk(media_file):
    with pytest.raises(CommandError, match="USE_S3"):
        call_command("sync_media_to_s3", "--apply")

def test_check_ok_writes_reads_deletes_probe(s3_storage):
    call_command("media_storage_check")
    assert s3_storage.listdir("healthcheck") == ([], [])

def test_check_fails_listing_missing_names(s3_storage, category_with_image_on_disk_only):
    with pytest.raises(SystemExit) as exc:
        call_command("media_storage_check")
    assert exc.value.code == 1  # stderr names the count and the first 20 missing names
```

In `test_s3_parity.py`, parametrize over `["disk", "s3"]`, using the existing catalog services and factories:
- the same image uploaded twice to one product gives the same `name` pattern on both backends
- replacing a category image deletes the previous object after commit (`django_capture_on_commit_callbacks(execute=True)`)
- deleting a product image removes the object
- the API's image URL equals `PUBLIC_MEDIA_BASE_URL + name`

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest glowderma/core/tests/test_media_storage.py glowderma/catalog/tests/test_s3_parity.py -v`
Expected: FAIL, unknown command or import errors. The parity "disk" cases pass already.

- [ ] **Step 3: Implement**

`sync_media_to_s3`:
- Walk `FileSystemStorage(location=settings.MEDIA_ROOT)` recursively.
- For each name missing in `default_storage`, without `--apply` print `would upload <name>`. With `--apply`, call
  `default_storage.save(name, File(fh))` and raise `CommandError` if the returned name differs.
- End with a summary `scanned=N uploaded=M already=K`.

`media_storage_check`:
- Save, read back and delete `healthcheck/<uuid4>.txt`.
- Then run `missing_names(default_storage)`.
- Print `ok backend=<class>` or the problem, then `sys.exit(1)`.

Fix any service code that assumed a filesystem (`.path`, `os.*`) if the parity tests expose it.

- [ ] **Step 4: Run tests to verify they pass, plus the full suite and linters (as in Task 1 Step 5)**

Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add glowderma/core glowderma/catalog/tests/test_s3_parity.py
git commit -m "feat(storage): sync_media_to_s3 and media_storage_check commands, S3 parity tests"
```

### Task 3: Local MinIO and decision log (backend)

**Files:**
- Modify: `backend/compose.yaml`, `backend/.env.example`, `backend/docs/decisions/open-questions.md` (C1, I1, T8), `backend/README.md` (media section, if one exists)

- [ ] **Step 1: Add `minio` and `minio-init` to `compose.yaml` under `profiles: ["minio"]`**

Use the same pinned images as Task 5. Ports are `127.0.0.1:9000:9000` and `127.0.0.1:9001:9001`, with a `minio`
volume and dev-only root `glowderma` / `glowderma-dev-secret`. `minio-init` uses mc to create the bucket
`glowderma-media` and gives anonymous download on `media/*`.

- [ ] **Step 2: Document the variables in `.env.example`, commented out by default**

```
# S3 media (optional): docker compose --profile minio up
# USE_S3=true
# S3_ENDPOINT_URL=http://minio:9000
# S3_BUCKET_NAME=glowderma-media
# S3_ACCESS_KEY_ID=glowderma
# S3_SECRET_ACCESS_KEY=glowderma-dev-secret
# PUBLIC_MEDIA_BASE_URL=http://127.0.0.1:9000/glowderma-media/media/
```

- [ ] **Step 3: Verify**

Run: `docker compose --profile minio up -d minio minio-init`, then with the variables above enabled,
`docker compose up -d api` and `docker compose exec api python manage.py media_storage_check`.
Expected: `ok backend=S3Storage`. Then run `docker compose config --services` without the profile.
Expected: no `minio`.

- [ ] **Step 4: Update the decision log**

- C1: obsolete (owner, 2026-11-13).
- I1: resolved, pointing to the spec.
- T8: storage is disk or S3 via `USE_S3`.

- [ ] **Step 5: Commit**

```bash
git add compose.yaml .env.example docs/
git commit -m "chore(storage): optional local MinIO profile; record I1 resolved and C1 obsolete"
```

### Task 4: Vault secrets for MinIO and the app key (infra)

**Files:**
- Modify: `infra/scripts/vault/generate-app-secrets.sh`, `infra/vault/policies/{production,staging}/backend.hcl`, `infra/vault/policies/{production,staging}/datastores.hcl`, `infra/vault/agent/templates/django.json.ctmpl`, `infra/vault/agent/datastores.hcl`, `infra/scripts/ci/test-vault.sh`, `infra/docs/secrets-management.md`
- Create: `infra/vault/agent/templates/minio_root_user.ctmpl`, `minio_root_password.ctmpl`, `s3_app_access_key.ctmpl`, `s3_app_secret_key.ctmpl`

**Interfaces:**
- Produces:
  - Vault paths `secret/<env>/minio` `{root_user, root_password}` and `secret/<env>/s3-app`
    `{access_key_id, secret_access_key}`.
  - `django.json` gains `S3_ACCESS_KEY_ID` and `S3_SECRET_ACCESS_KEY`.
  - Files in the datastores volume: `/run/secrets/minio_root_user`, `minio_root_password`, `s3_app_access_key`,
    `s3_app_secret_key` (0400, uid 999).

- [ ] **Step 1: Write the failing checks in `test-vault.sh`**

- The backend agent's `django.json` has both `S3_*` keys equal to `secret/<env>/s3-app`.
- The datastores agent renders all 4 files.
- The backend token reading `secret/data/<env>/minio` gets 403.
- The frontend token reading `s3-app` gets 403.
- A second `generate-app-secrets.sh` run keeps existing values.

- [ ] **Step 2: Run it to verify it fails**

Run: `VAULT_BIN=$(which vault) scripts/ci/test-vault.sh`
Expected: `FAIL` lines for the new checks.

- [ ] **Step 3: Implement**

- `put_new minio` uses `root_user` `glowderma-root` and a random 32-hex `root_password`.
- `put_new s3-app` uses a random 20-char `access_key_id` and a random 40-char `secret_access_key`.
- Update the layout comment.
- Policies:
  - backend reads `s3-app`
  - datastores reads `minio` and `s3-app`
- In `django.json.ctmpl`, insert the `s3-app` block before the `redis` block, with a trailing comma.
- The 4 templates go in `datastores.hcl` with `error_on_missing_key = true`.
- `secrets-management.md`: for an external provider, overwrite `s3-app` with `put-secret.sh`. For existing
  environments, rerun `generate-app-secrets.sh` before deploying this release.

- [ ] **Step 4: Run it to verify it passes**

Expected: every line `ok`, exit 0.

- [ ] **Step 5: Commit**

```bash
git add scripts/vault vault scripts/ci/test-vault.sh docs/secrets-management.md
git commit -m "feat(vault): MinIO root and S3 app credentials"
```

### Task 5: Compose services, profile switch and validation (infra)

**Files:**
- Modify: `infra/compose/app.yaml`, `infra/scripts/lib.sh` (`app_compose`), `infra/scripts/validate.sh`, `infra/environments/{production,staging}/stack.env`, `infra/scripts/bootstrap-server.sh` (`install -d -o 999 -g 999 "$RUNTIME/minio" "$RUNTIME/media-mirror"`)
- Create: `infra/minio/init.sh`, `infra/minio/app-policy.json`, `infra/minio/public-read.json`

**Interfaces:**
- Consumes: the Task 4 secret files.
- Produces:
  - Services `minio` (profile `minio`) and `minio-init` (profile `minio-tools`; run with `run --rm`, never `up`).
  - `app_compose` exports `MEDIA_BACKEND=minio|disk` and adds `--profile minio` when `MINIO_ENABLED=true`.
  - Backend env keys `USE_S3`, `S3_ENDPOINT_URL`, `S3_BUCKET_NAME`, `S3_REGION_NAME`, `S3_ADDRESSING_STYLE`,
    `S3_LOCATION`.
  - `PUBLIC_MEDIA_BASE_URL: ${PUBLIC_MEDIA_BASE_URL:-https://${SITE_DOMAIN}/media/}`.
  - Edge env `MEDIA_BACKEND`, `MINIO_UPSTREAM: 172.30.0.40:9000`, `S3_BUCKET_NAME`, `S3_LOCATION`.

- [ ] **Step 1: Write the failing validations in `validate.sh`**

For each env, render `docker compose config` both without and with `--profile minio`, and assert:
- `MINIO_ENABLED` and `USE_S3` are `true` or `false`.
- `USE_S3=true` requires `S3_BUCKET_NAME`.
- `USE_S3=true` with `MINIO_ENABLED=false` and an endpoint containing `172.30.0.40` is refused with
  `USE_S3 points at the stack's MinIO but MINIO_ENABLED=false`.
- `USE_S3=true` with `MINIO_ENABLED=false` requires `PUBLIC_MEDIA_BASE_URL` to be set.
- `172.30.0.40` is the same in `minio`'s `ipv4_address`, the default endpoint and the edge's `MINIO_UPSTREAM`.
- Without the profile, the rendered config has no `minio` service.

- [ ] **Step 2: Run it to verify it fails**

Run: `scripts/validate.sh`
Expected: fails because the keys are missing from `stack.env` and there's no `minio` service.

- [ ] **Step 3: Implement**

`stack.env` (both envs; public values only):

```
# Media storage. MINIO_ENABLED pulls/runs MinIO; USE_S3 makes Django store media in S3 (either can be off).
MINIO_ENABLED=false
USE_S3=false
S3_ENDPOINT_URL=http://172.30.0.40:9000
S3_BUCKET_NAME=glowderma-media
S3_REGION_NAME=us-east-1
S3_ADDRESSING_STYLE=path
S3_LOCATION=media
# External provider only: PUBLIC_MEDIA_BASE_URL=https://<bucket or CDN>/media/
```

- `minio`:
  - `command: server /data`
  - `MINIO_ROOT_USER_FILE` / `MINIO_ROOT_PASSWORD_FILE` → `/run/secrets/minio_root_*`
  - `MINIO_BROWSER: "off"`
  - volume `${RUNTIME_DIR}/minio:/data`
  - `datastores-secrets:/run/secrets:ro`
  - healthcheck `["CMD", "mc", "ready", "local"]` (confirm the pinned image ships `mc`; otherwise use
    `curl -f http://127.0.0.1:9000/minio/health/live`)
  - `depends_on vault-agent-datastores`
- `minio-init`: the mc image, `entrypoint: ["/bin/sh", "/init/init.sh"]`, and mounts `../minio:/init:ro`,
  `datastores-secrets`, and `${RUNTIME_DIR}/media-mirror:/mirror`. `init.sh`, idempotent:
  1. `mc alias set`
  2. `mc mb --ignore-existing`
  3. `mc anonymous set-json public-read.json` (`s3:GetObject` on `arn:aws:s3:::<bucket>/<location>/*` only)
  4. `mc admin user add` the app key
  5. `mc admin policy create glowderma-app app-policy.json` (Get/Put/Delete on objects, ListBucket on the bucket)
  6. `mc admin policy attach` (treat "already attached" as success)
- Backend services `backend-api`, `backend-worker`, `backend-migrate`, `backend-beat`: the new env keys, plus
  `depends_on: minio: {condition: service_healthy, required: false}`.
- `lib.sh`: compute `MEDIA_BACKEND` from `stack_value MINIO_ENABLED` and `stack_value USE_S3` (`minio` only when
  both are `true`) and export it before `docker compose`.

- [ ] **Step 4: Run it to verify it passes**

Run: `scripts/validate.sh`, then `MINIO_ENABLED=true` in a temp copy of `stack.env` and validate again.
Expected: both pass. Each mismatch rule, tried in a temp copy, fails with its message.

- [ ] **Step 5: Commit**

```bash
git add compose minio scripts/lib.sh scripts/validate.sh scripts/bootstrap-server.sh environments
git commit -m "feat(stack): optional MinIO behind MINIO_ENABLED, S3 settings passed to Django"
```

### Task 6: Caddy serves media from disk or MinIO (infra)

**Files:**
- Modify: `infra/caddy/Caddyfile` (site block around line 79 and ops block around line 153), `infra/caddy/edge_probe.py`

**Interfaces:**
- Consumes: edge env `MEDIA_BACKEND`, `MINIO_UPSTREAM`, `S3_BUCKET_NAME`, `S3_LOCATION` (Task 5).
- Produces: snippets `(media_disk)` and `(media_minio)`, used as `import media_{$MEDIA_BACKEND:disk}` inside both
  `handle_path /media/*` blocks. The headers (Cache-Control, nosniff, the site's CSP) stay in the shared block.

- [ ] **Step 1: Write the failing probe checks**

`edge_probe.py` gains a third stub upstream (the "MinIO" stub records the request path and answers 200 for
`/glowderma-media/media/products/1/a.webp`, 404 otherwise). It runs the existing media checks with
`MEDIA_BACKEND=disk`, then a second Caddy with `MEDIA_BACKEND=minio`, asserting:
- `GET /media/products/1/a.webp` → 200, the stub saw `/glowderma-media/media/products/1/a.webp`, and the response
  keeps the Cache-Control, nosniff and CSP headers with no `Server` or `X-Amz-*` headers
- `GET /media/` → 404 and the stub never saw a bucket-root or `?list-type` request
- `GET /media/../x`, `/media/%2e%2e/x` and `/media/..%2fother-bucket/x` → the stub only ever sees paths under
  `/glowderma-media/media/`
- the ops host `/media/` behaves the same

- [ ] **Step 2: Run to verify it fails**

Run: `python3 caddy/edge_probe.py --caddy $(which caddy)`
Expected: FAIL on the minio checks.

- [ ] **Step 3: Implement the snippets**

- `(media_disk)`: today's `root` + `file_server`.
- `(media_minio)`: `rewrite * /{$S3_BUCKET_NAME}/{$S3_LOCATION:media}{path}`, then `reverse_proxy {$MINIO_UPSTREAM}`
  with `header_up Host {upstream_hostport}` and `header_down -Server -X-Amz-* -X-Minio-*`.
- If the env-placeholder `import` doesn't parse in Caddy 2.11, switch to a matcher on `{env.MEDIA_BACKEND}` and
  say so in the commit.

- [ ] **Step 4: Run to verify it passes**

Run: `caddy validate --config caddy/Caddyfile --adapter caddyfile` (with each `MEDIA_BACKEND`), then the probe.
Expected: valid in both modes, and every probe line passes.

- [ ] **Step 5: Commit**

```bash
git add caddy
git commit -m "feat(edge): serve /media from disk or proxy to MinIO"
```

### Task 7: Deploy, backup, manage and runbook (infra)

**Files:**
- Modify: `infra/scripts/deploy.sh`, `infra/scripts/backup.sh` (around lines 60-67), `infra/scripts/manage.sh`, `infra/docs/architecture.md`, `infra/docs/deployment.md`, `infra/docs/disaster-recovery.md`

**Interfaces:**
- Consumes: `minio`, `minio-init` and `MEDIA_BACKEND` (Task 5), the Task 2 commands, and the Task 4 Vault paths.

- [ ] **Step 1: Change `deploy.sh`**

- Preflight, after the Vault status: if `MINIO_ENABLED` or `USE_S3` is `true`, `vault kv get` `secret/<env>/s3-app`
  and, with MinIO, `secret/<env>/minio`. If either is missing, die with
  `run scripts/vault/generate-app-secrets.sh $ENV first`.
- Pull: when MinIO is enabled, also `app_compose --profile minio-tools pull --quiet minio-init`.
- After the datastores start: with MinIO, `app_compose up -d --wait minio`, then
  `app_compose --profile minio-tools run --rm --no-deps minio-init`, and die on failure.
- After the release is healthy: if `USE_S3=true`, run
  `app_compose exec -T backend-api python manage.py media_storage_check` and fail the deploy with its output.

- [ ] **Step 2: Change `backup.sh` (nightly)**

- `MEDIA_BACKEND=minio`: `minio-init` run with `--entrypoint sh -c` does `mc mirror --overwrite --remove` of
  `<bucket>/<location>` into `/mirror`, then today's tar of `$RUNTIME_DIR/media-mirror`.
- `USE_S3=true` without MinIO: print `media: external provider, not backed up here` and skip.
- Otherwise: unchanged.

- [ ] **Step 3: Add `sync-media` to `manage.sh`**

`scripts/manage.sh <env> sync-media [--apply]` is the same as
`app_compose exec backend-api python manage.py sync_media_to_s3 "$@"`. Add it to the usage line.

- [ ] **Step 4: Write the runbook and docs**

Switch-on order in `deployment.md`:
1. Set `MINIO_ENABLED=true` and deploy.
2. Run `scripts/manage.sh <env> sync-media` (dry run), then with `--apply`.
3. Set `USE_S3=true` and deploy. `media_storage_check` gates this deploy.

For an external provider, `deployment.md` also covers:
- setting `S3_ENDPOINT_URL` and `PUBLIC_MEDIA_BASE_URL`
- putting the keys in `s3-app`
- adding the bucket host to the frontend's `GLOWDERMA_MEDIA_HOSTS` (a frontend rebuild)

Rollback in `deployment.md`: set `USE_S3=false`, but first `mc mirror` the bucket back into `runtime/media`.
Otherwise uploads made while on S3 404.

`disaster-recovery.md`: restore per mode (untar into `media-mirror`, then `mc mirror` back into the bucket).
`architecture.md`: the `/media/*` row and the MinIO service.

- [ ] **Step 5: Verify**

Run: `bash -n scripts/*.sh && shellcheck scripts/deploy.sh scripts/backup.sh scripts/manage.sh scripts/lib.sh && scripts/validate.sh`
Expected: clean.

If a staging server is available, deploy there with `MINIO_ENABLED=true USE_S3=false`. Then run
`sync-media --apply`, deploy with `USE_S3=true`, and expect `media_storage_check` to print `ok` and a product
image URL to return 200. If no server is available, report that this end-to-end check wasn't run.

- [ ] **Step 6: Commit**

```bash
git add scripts docs
git commit -m "feat(ops): deploy, backup and sync for S3/MinIO media"
```
