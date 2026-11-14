# S3 Media Storage Switch Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Media can be stored on local disk (default) or in any S3-compatible bucket (`USE_S3`). The deployed
stack can optionally pull and run SeaweedFS (`OBJECT_STORAGE_ENABLED`).

**Architecture:** A pure settings helper builds `STORAGES["default"]` from env, either `FileSystemStorage` or
`storages.backends.s3.S3Storage`. Public URLs stay `PUBLIC_MEDIA_BASE_URL` + stored name. In infra, SeaweedFS and a
one-shot `object-storage-init` sit behind Compose profiles that `lib.sh` turns on from `stack.env`. Caddy serves
`/media/*` from disk or proxies to the object storage, and Vault supplies the credentials.

**Tech Stack:** Django 6.1, django-storages[s3] (boto3), moto (tests), Docker Compose profiles, SeaweedFS + aws-cli, Caddy
2.11, Vault Agent templates, bash.

**Spec:** `backend/docs/specs/2026-11-13-s3-media-storage-design.md`

Two git repos: `backend/` (tasks 1-3) and `infra/` (tasks 4-7). Both are on `main`. Each starts with
`git switch -c feat/s3-media-storage`.

## Global Constraints

- Local disk stays the default everywhere: `USE_S3` defaults to `false` and `OBJECT_STORAGE_ENABLED=false` in both
  `stack.env` files.
- No signed URLs: `querystring_auth=False`, `default_acl=None`, `file_overwrite=False`.
- S3 defaults: `S3_REGION_NAME=us-east-1`, `S3_ADDRESSING_STYLE=path`, `S3_LOCATION=media`, `S3_VERIFY=true`.
- Stored names keep `<kind>/<owner>/<sha256-prefix>.<ext>`.
- Provider settings live in Vault so a provider or bucket change needs no PR: `secret/<env>/s3` keys are env var
  names: `S3_ENDPOINT_URL`, `S3_BUCKET_NAME`, `S3_REGION_NAME`, `S3_ADDRESSING_STYLE`, `S3_ACCESS_KEY_ID`,
  `S3_SECRET_ACCESS_KEY`, plus optional `S3_VERIFY` and `PUBLIC_MEDIA_BASE_URL` (backend + datastores). Changing it
  = `vault kv patch` + `scripts/restart.sh <env> backend`. `secret/<env>/object-storage` (admin access key pair:
  `OBJECT_STORAGE_ADMIN_ACCESS_KEY_ID`, `OBJECT_STORAGE_ADMIN_SECRET_ACCESS_KEY`) is datastores only.
- `stack.env` holds only `OBJECT_STORAGE_ENABLED`, `USE_S3`, `S3_LOCATION` (they change what runs and how Caddy serves).
  Topology keys (Vault may not set): `USE_S3`, `S3_LOCATION`. `PUBLIC_MEDIA_BASE_URL` is no longer a topology key.
- The stack's own bucket is fixed: `glowderma-media` (Caddy and `object-storage-init` use the literal; Vault's
  `S3_BUCKET_NAME` is seeded with the same value).
- Error messages name keys, never values.
- Engine: SeaweedFS (Apache-2.0), image `chrislusf/seaweedfs:4.53@sha256:ccad23e18639c8b98cee17e7100b95830a06077b56d8e5b006d6dcf8ec5e94a0`, command
  `server -dir=/data -s3 -s3.config=/etc/seaweedfs/s3.json` (S3 API on 8333; add `-ip.bind=0.0.0.0` where needed).
  Tool image for init and backups: `amazon/aws-cli:2.40.3@sha256:33a16c53ea442e918b58df67301ad76a8fb57082c39f7e78753b898c3f3e87c2` (every step is `aws --endpoint-url ... s3api|s3`).
  Both are pinned by tag and digest. SeaweedFS runs as `999:999` (the datastores agent's UID, so it can read the
  0400 secret files; the image runs as root by default, so the implementer verifies it works as 999:999 with a
  999-owned data dir) with the `x-hardened` anchor, and publishes no ports.
- Fixed IPv4 literal for the object storage on `front`: `172.30.0.40`. It is also on `back`. Backend services and `object-storage-init` reach it by its service name `object-storage` on `back` (`http://object-storage:8333`); only the edge uses the IP.
- Access model: a SeaweedFS `s3.json` identities file rendered by `vault-agent-datastores` from Vault
  (`secret/<env>/object-storage` + `secret/<env>/s3`), mounted read-only into the service. Identities: `admin`
  (`Admin`, `Read`, `Write`, `List`, `Tagging`), `app` (`Read|Write|List|Tagging:glowderma-media`, the
  `S3_ACCESS_KEY_ID` / `S3_SECRET_ACCESS_KEY` pair) and `anonymous` (`Read:glowderma-media`, which is how
  SeaweedFS grants unauthenticated GET). No admin API, no bucket policy. Anonymous read is bucket-wide, not
  limited to `<location>/*` (the bucket holds only media plus short-lived healthcheck probes); anonymous
  LIST/PUT/DELETE are refused.
- New dependencies are pinned exactly, matching `pyproject.toml` style.

## Review Focus

1. **An existing environment deploys before `secret/<env>/s3` exists.** The backend agent's template would
   block and the API would never start. Expected: `generate-app-secrets.sh` creates both paths idempotently, and
   `deploy.sh` refuses early with the exact command to run (Task 4, Task 7).
2. **`USE_S3=true` is turned on before existing files are copied.** Every old image would 404. Expected:
   `media_storage_check` lists stored names missing from the active storage and exits 1, and `deploy.sh` runs it
   (Task 2, Task 7).
3. **`/media/` as a probe into the object storage.** `/media/`, `/media/../x` or `/media/%2e%2e/...` must not list the bucket
   or reach another bucket or prefix. Expected: 404 or the object only (Task 6).
4. **The same image uploaded twice.** S3 must behave like disk: same resulting name and URL behavior as
   `FileSystemStorage`, no orphan objects after replace or delete (Task 2).
5. **`USE_S3=true` with Vault's endpoint host `object-storage` while `OBJECT_STORAGE_ENABLED=false`, or an external endpoint
   without a `PUBLIC_MEDIA_BASE_URL` override.** Expected: `deploy.sh` refuses it with a clear message before
   touching containers (Task 7; `validate.sh` can't see Vault).

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

def test_use_s3_builds_s3_options(s3_env):  # USE_S3=true, bucket, keys, endpoint http://object-storage:8333
    cfg = media_storage()
    assert cfg["BACKEND"] == "storages.backends.s3.S3Storage"
    assert cfg["OPTIONS"] == {
        "bucket_name": "glowderma-media", "endpoint_url": "http://object-storage:8333",
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

@pytest.mark.parametrize("key", ["USE_S3", "S3_LOCATION"])
def test_secrets_file_cannot_set_s3_topology(key):
    with pytest.raises(ImproperlyConfigured, match=key):
        parse_secrets(json.dumps({key: "x"}), source="f")

def test_secrets_file_may_set_provider_settings():
    keys = ["S3_ENDPOINT_URL", "S3_BUCKET_NAME", "S3_REGION_NAME", "S3_ADDRESSING_STYLE", "S3_VERIFY",
            "PUBLIC_MEDIA_BASE_URL"]
    assert set(parse_secrets(json.dumps({k: "x" for k in keys}), source="f")) == set(keys)
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
In `production.py`, use `STORAGES = {**STORAGES, "staticfiles": {...whitenoise...}}` and keep the comment. Add
`USE_S3` and `S3_LOCATION` to `TOPOLOGY_KEYS`, remove `PUBLIC_MEDIA_BASE_URL` from it, and update the module
docstring and any existing test that asserted it was refused.

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

### Task 3: Local object storage and decision log (backend)

**Files:**
- Modify: `backend/compose.yaml`, `backend/.env.example`, `backend/docs/decisions/open-questions.md` (C1, I1, T8), `backend/README.md` (media section, if one exists)
- Create: a committed dev-only `s3.json` (e.g. `backend/docker/object-storage/s3.json`)

- [ ] **Step 1: Add `object-storage` and `object-storage-init` to `compose.yaml` under `profiles: ["object-storage"]`**

`object-storage` uses the same pinned SeaweedFS image and command as Task 5, publishes `127.0.0.1:8333:8333`, and
has a volume for `/data`. Its `s3.json` is committed, mounted read-only at `/etc/seaweedfs/s3.json`, and labelled
as dev-only keys, not for production (identities as in Global Constraints; `app` keys `glowderma` /
`glowderma-dev-secret`). `object-storage-init` is a one-shot (the aws-cli image from Task 5, admin dev keys) that
creates the bucket `glowderma-media`; HTTP 409 counts as success.

- [ ] **Step 2: Document the variables in `.env.example`, commented out by default**

```
# S3 media (optional): docker compose --profile object-storage up
# USE_S3=true
# S3_ENDPOINT_URL=http://localhost:8333
# S3_BUCKET_NAME=glowderma-media
# S3_ACCESS_KEY_ID=glowderma
# S3_SECRET_ACCESS_KEY=glowderma-dev-secret
# PUBLIC_MEDIA_BASE_URL=http://127.0.0.1:8333/glowderma-media/media/
```

- [ ] **Step 3: Verify**

Run: `docker compose --profile object-storage up -d object-storage object-storage-init`, then with the variables above enabled,
`docker compose up -d api` and `docker compose exec api python manage.py media_storage_check`.
Expected: `ok backend=S3Storage`. Then run `docker compose config --services` without the profile.
Expected: no `object-storage`.

- [ ] **Step 4: Update the decision log**

- C1: obsolete (owner, 2026-11-13).
- I1: resolved (SeaweedFS as the self-hosted option), pointing to the spec.
- T8: storage is disk or S3 via `USE_S3`.

- [ ] **Step 5: Commit**

```bash
git add compose.yaml .env.example docs/
git commit -m "chore(storage): optional local SeaweedFS profile; record I1 resolved and C1 obsolete"
```

### Task 4: Vault secrets for object storage and the app key (infra)

**Files:**
- Modify: `infra/scripts/vault/generate-app-secrets.sh`, `infra/vault/policies/{production,staging}/backend.hcl`, `infra/vault/policies/{production,staging}/datastores.hcl`, `infra/vault/agent/templates/django.json.ctmpl`, `infra/vault/agent/datastores.hcl`, `infra/scripts/ci/test-vault.sh`, `infra/docs/secrets-management.md`
- Create: `infra/vault/agent/templates/object_storage_s3.json.ctmpl`, `object_storage_admin_access_key.ctmpl`, `object_storage_admin_secret_key.ctmpl`

**Interfaces:**
- Produces:
  - Vault paths `secret/<env>/object-storage` `{OBJECT_STORAGE_ADMIN_ACCESS_KEY_ID,
    OBJECT_STORAGE_ADMIN_SECRET_ACCESS_KEY}` and `secret/<env>/s3`, whose keys are env var
    names (seeded: `S3_ENDPOINT_URL=http://object-storage:8333`, `S3_BUCKET_NAME=glowderma-media`,
    `S3_REGION_NAME=us-east-1`, `S3_ADDRESSING_STYLE=path`, random `S3_ACCESS_KEY_ID`, random
    `S3_SECRET_ACCESS_KEY`).
  - `django.json` gains every key of `secret/<env>/s3` (ranged like `secret/<env>/django`).
  - Files in the datastores volume (0400, uid 999): `/run/secrets/object_storage_s3.json` (the SeaweedFS
    identities file: `admin` from the object-storage pair, `app` from `S3_ACCESS_KEY_ID` /
    `S3_SECRET_ACCESS_KEY`, plus `anonymous`; see Global Constraints), and `object_storage_admin_access_key`,
    `object_storage_admin_secret_key` (for `object-storage-init` and backups).

- [ ] **Step 1: Write the failing checks in `test-vault.sh`**

- The backend agent's `django.json` contains every key of `secret/<env>/s3` with its value, and a key added later
  with `vault kv patch` (e.g. `PUBLIC_MEDIA_BASE_URL`) appears after re-render.
- The datastores agent renders all 3 files, and `object_storage_s3.json` is valid JSON with the three identities
  and the `app` key pair from `secret/<env>/s3`.
- The backend token reading `secret/data/<env>/object-storage` gets 403.
- The frontend token reading `secret/data/<env>/s3` gets 403.
- A second `generate-app-secrets.sh` run keeps existing values.

- [ ] **Step 2: Run it to verify it fails**

Run: `VAULT_BIN=$(which vault) scripts/ci/test-vault.sh`
Expected: `FAIL` lines for the new checks.

- [ ] **Step 3: Implement**

- `put_new object-storage` uses `OBJECT_STORAGE_ADMIN_ACCESS_KEY_ID` `glowderma-admin` and a random 40-char
  `OBJECT_STORAGE_ADMIN_SECRET_ACCESS_KEY`.
- `put_new s3` with the seeded values above (access key: random 20 alphanumeric chars, secret: random 40).
- Update the layout comment.
- Policies:
  - backend reads `s3`
  - datastores reads `object-storage` and `s3`
- In `django.json.ctmpl`, range over `secret/<env>/s3` the same way as `secret/<env>/django`, placed before the
  `redis` block so the JSON stays valid.
- The 3 templates go in `datastores.hcl` with `error_on_missing_key = true`.
- `secrets-management.md`: changing provider or bucket = `vault kv patch secret/<env>/s3 ...` then
  `scripts/restart.sh <env> backend` (no PR). For an external provider, also set `PUBLIC_MEDIA_BASE_URL` there.
  The frontend's `GLOWDERMA_MEDIA_HOSTS` is build-time, so a new image host still needs a frontend rebuild. For
  existing environments, rerun `generate-app-secrets.sh` before deploying this release.

- [ ] **Step 4: Run it to verify it passes**

Expected: every line `ok`, exit 0.

- [ ] **Step 5: Commit**

```bash
git add scripts/vault vault scripts/ci/test-vault.sh docs/secrets-management.md
git commit -m "feat(vault): object storage admin credentials, S3 app credentials and s3.json template"
```

### Task 5: Compose services, profile switch and validation (infra)

**Files:**
- Modify: `infra/compose/app.yaml`, `infra/scripts/lib.sh` (`app_compose`), `infra/scripts/validate.sh`, `infra/environments/{production,staging}/stack.env`, `infra/scripts/bootstrap-server.sh` (`install -d -o 999 -g 999 "$RUNTIME/object-storage" "$RUNTIME/media-mirror"`)
- Create: `infra/object-storage/init.sh`

**Interfaces:**
- Consumes: the Task 4 secret files.
- Produces:
  - Services `object-storage` (profile `object-storage`) and `object-storage-init` (profile
    `object-storage-tools`; run with `run --rm`, never `up`).
  - `app_compose` exports `MEDIA_BACKEND=object-storage|disk` and adds `--profile object-storage` when
    `OBJECT_STORAGE_ENABLED=true`.
  - Backend env keys from the stack: `USE_S3`, `S3_LOCATION` only (the other `S3_*` come from Vault via
    `django.json`). `PUBLIC_MEDIA_BASE_URL` stays `https://${SITE_DOMAIN}/media/` in compose; Vault may override
    it.
  - Edge env `MEDIA_BACKEND`, `OBJECT_STORAGE_UPSTREAM: 172.30.0.40:8333`,
    `OBJECT_STORAGE_BUCKET: glowderma-media`, `S3_LOCATION`.
    `object-storage-init` gets `OBJECT_STORAGE_BUCKET: glowderma-media` too.

- [ ] **Step 1: Write the failing validations in `validate.sh`**

For each env, render `docker compose config` both without and with `--profile object-storage`, and assert:
- `OBJECT_STORAGE_ENABLED` and `USE_S3` are `true` or `false`; `S3_LOCATION` is non-empty.
- `172.30.0.40` is the same in `object-storage`'s `ipv4_address` and the edge's `OBJECT_STORAGE_UPSTREAM`; the seeded `S3_ENDPOINT_URL` is `http://object-storage:8333` and `object-storage` is on `back`
  (seed in `scripts/vault/generate-app-secrets.sh`).
- `glowderma-media` is the same in the edge env, `object-storage-init` env and the seeded `S3_BUCKET_NAME`.
- Without the profile, the rendered config has no `object-storage` service.
(Vault-dependent consistency rules live in `deploy.sh`, Task 7.)

- [ ] **Step 2: Run it to verify it fails**

Run: `scripts/validate.sh`
Expected: fails because the keys are missing from `stack.env` and there's no `object-storage` service.

- [ ] **Step 3: Implement**

`stack.env` (both envs; public values only):

```
# Media storage. OBJECT_STORAGE_ENABLED pulls/runs SeaweedFS; USE_S3 makes Django store media in S3 (either can be off).
# Provider, bucket, endpoint and keys are in Vault: secret/<env>/s3 (change + scripts/restart.sh <env> backend).
OBJECT_STORAGE_ENABLED=false
USE_S3=false
S3_LOCATION=media
```

- `object-storage`:
  - `command: server -dir=/data -s3 -s3.config=/etc/seaweedfs/s3.json -ip.bind=0.0.0.0`
  - `user: "999:999"`; verify the image works as 999:999 with a 999-owned `/data` (it runs as root by default)
  - `object_storage_s3.json` from `datastores-secrets` mounted read-only at `/etc/seaweedfs/s3.json` (a named-volume
    `subpath` mount or equivalent)
  - volume `${RUNTIME_DIR}/object-storage:/data`
  - healthcheck: the image has wget and curl; the implementer verifies the exact probe against S3 port 8333
    (e.g. `["CMD", "curl", "-f", "http://127.0.0.1:8333/status"]`)
  - `depends_on vault-agent-datastores`
- `object-storage-init`: the aws-cli image, `entrypoint: ["/bin/sh", "/init/init.sh"]`, and mounts
  `../object-storage:/init:ro`, `datastores-secrets`, and `${RUNTIME_DIR}/media-mirror:/mirror`. `init.sh` only
  creates the bucket, idempotent:
  1. export the admin key pair from `/run/secrets/object_storage_admin_*` as `AWS_ACCESS_KEY_ID` /
     `AWS_SECRET_ACCESS_KEY`, plus `AWS_DEFAULT_REGION=us-east-1`
  2. `aws --endpoint-url http://object-storage:8333 s3api create-bucket --bucket "$OBJECT_STORAGE_BUCKET"`; HTTP 409
     (BucketAlreadyOwnedByYou / BucketAlreadyExists) counts as success, any other failure exits non-zero
- Backend services `backend-api`, `backend-worker`, `backend-migrate`, `backend-beat`: the new env keys, plus
  `depends_on: object-storage: {condition: service_healthy, required: false}`.
- `lib.sh`: compute `MEDIA_BACKEND` from `stack_value OBJECT_STORAGE_ENABLED` and `stack_value USE_S3` (`object-storage` only when
  both are `true`) and export it before `docker compose`.

- [ ] **Step 4: Run it to verify it passes**

Run: `scripts/validate.sh`, then `OBJECT_STORAGE_ENABLED=true` in a temp copy of `stack.env` and validate again.
Expected: both pass. Each mismatch rule, tried in a temp copy, fails with its message.

- [ ] **Step 5: Commit**

```bash
git add compose object-storage scripts/lib.sh scripts/validate.sh scripts/bootstrap-server.sh environments
git commit -m "feat(stack): optional SeaweedFS behind OBJECT_STORAGE_ENABLED, S3 settings passed to Django"
```

### Task 6: Caddy serves media from disk or the object storage (infra)

**Files:**
- Modify: `infra/caddy/Caddyfile` (site block around line 79 and ops block around line 153), `infra/caddy/edge_probe.py`

**Interfaces:**
- Consumes: edge env `MEDIA_BACKEND`, `OBJECT_STORAGE_UPSTREAM`, `OBJECT_STORAGE_BUCKET`, `S3_LOCATION` (Task 5).
- Produces: snippets `(media_disk)` and `(media_object_storage)`, used as `import media_{$MEDIA_BACKEND:disk}` inside both
  `handle_path /media/*` blocks. The headers (Cache-Control, nosniff, the site's CSP) stay in the shared block.

- [ ] **Step 1: Write the failing probe checks**

`edge_probe.py` gains a third stub upstream (the "object storage" stub records the request path and answers 200 for
`/glowderma-media/media/products/1/a.webp`, 404 otherwise). It runs the existing media checks with
`MEDIA_BACKEND=disk`, then a second Caddy with `MEDIA_BACKEND=object-storage`, asserting:
- `GET /media/products/1/a.webp` → 200, the stub saw `/glowderma-media/media/products/1/a.webp`, and the response
  keeps the Cache-Control, nosniff and CSP headers with no `Server` or `X-Amz-*` headers
- `GET /media/` → 404 and the stub never saw a bucket-root or `?list-type` request
- `GET /media/../x`, `/media/%2e%2e/x` and `/media/..%2fother-bucket/x` → the stub only ever sees paths under
  `/glowderma-media/media/`
- the ops host `/media/` behaves the same

- [ ] **Step 2: Run to verify it fails**

Run: `python3 caddy/edge_probe.py --caddy $(which caddy)`
Expected: FAIL on the object-storage checks.

- [ ] **Step 3: Implement the snippets**

- `(media_disk)`: today's `root` + `file_server`.
- `(media_object_storage)`: `rewrite * /{$OBJECT_STORAGE_BUCKET}/{$S3_LOCATION:media}{path}`, then
  `reverse_proxy {$OBJECT_STORAGE_UPSTREAM}` with `header_up Host {upstream_hostport}` and
  `header_down -Server -X-Amz-*`.
- If the env-placeholder `import` doesn't parse in Caddy 2.11, switch to a matcher on `{env.MEDIA_BACKEND}` and
  say so in the commit.

- [ ] **Step 4: Run to verify it passes**

Run: `caddy validate --config caddy/Caddyfile --adapter caddyfile` (with each `MEDIA_BACKEND`), then the probe.
Expected: valid in both modes, and every probe line passes.

- [ ] **Step 5: Commit**

```bash
git add caddy
git commit -m "feat(edge): serve /media from disk or proxy to the object storage"
```

### Task 7: Deploy, backup, manage and runbook (infra)

**Files:**
- Modify: `infra/scripts/deploy.sh`, `infra/scripts/backup.sh` (around lines 60-67), `infra/scripts/manage.sh`, `infra/docs/architecture.md`, `infra/docs/deployment.md`, `infra/docs/disaster-recovery.md`

**Interfaces:**
- Consumes: `object-storage`, `object-storage-init` and `MEDIA_BACKEND` (Task 5), the Task 2 commands, and the Task 4 Vault paths.

- [ ] **Step 1: Change `deploy.sh`**

- Preflight, after the Vault status: if `OBJECT_STORAGE_ENABLED` or `USE_S3` is `true`, `vault kv get` `secret/<env>/s3`
  and, with the object storage, `secret/<env>/object-storage`. If either is missing, die with
  `run scripts/vault/generate-app-secrets.sh $ENV first`. With `USE_S3=true` and `OBJECT_STORAGE_ENABLED=false`, read
  `S3_ENDPOINT_URL` and `PUBLIC_MEDIA_BASE_URL` from `secret/<env>/s3` (never print other values) and die if the
  endpoint host is `object-storage` (`USE_S3 points at the stack's object storage but OBJECT_STORAGE_ENABLED=false`) or if
  `PUBLIC_MEDIA_BASE_URL` is unset (`an external provider needs PUBLIC_MEDIA_BASE_URL in secret/$ENV/s3`).
  Put these rules in a function in `lib.sh` and cover each with a case in a small `scripts/ci/test-media-preflight.sh`
  that stubs the Vault read.
- Pull: when the object storage is enabled, also
  `app_compose --profile object-storage-tools pull --quiet object-storage-init`.
- After the datastores start: with the object storage, `app_compose up -d --wait object-storage`, then
  `app_compose --profile object-storage-tools run --rm --no-deps object-storage-init`, and die on failure.
- After the release is healthy: if `USE_S3=true`, run
  `app_compose exec -T backend-api python manage.py media_storage_check` and fail the deploy with its output.

- [ ] **Step 2: Change `backup.sh` (nightly)**

- `MEDIA_BACKEND=object-storage`: `object-storage-init` run with `--entrypoint sh -c` does
  `aws --endpoint-url http://object-storage:8333 s3 sync s3://glowderma-media/<location> /mirror --delete` (admin keys
  exported as in `init.sh`), then today's tar of `$RUNTIME_DIR/media-mirror`.
- `USE_S3=true` without the object storage: print `media: external provider, not backed up here` and skip.
- Otherwise: unchanged.

- [ ] **Step 3: Add `sync-media` to `manage.sh`**

`scripts/manage.sh <env> sync-media [--apply]` is the same as
`app_compose exec -e USE_S3=true backend-api python manage.py sync_media_to_s3 "$@"`. Add it to the usage line.
It runs before the switch (stack.env `USE_S3=false`, the command refuses a disk default storage): `USE_S3` is a
topology key read at settings import, so `-e USE_S3=true` gives only this process the bucket of `secret/<env>/s3`
(its `S3_*` keys are in `django.json` in every mode) while the source stays `runtime/media`. (Final-review fix C1.)

- [ ] **Step 4: Write the runbook and docs**

Switch-on order in `deployment.md`:
1. Set `OBJECT_STORAGE_ENABLED=true` and deploy.
2. Run `scripts/manage.sh <env> sync-media` (dry run), then with `--apply` (it sets `USE_S3=true` for that one
   process, see Step 3).
3. Set `USE_S3=true` and deploy. `media_storage_check` gates this deploy.
4. Right after that deploy, `sync-media --apply` again (uploads made between step 2 and the switch went to the
   disk), then `media_storage_check`.

For an external provider (or a bucket change), `deployment.md` also covers, with no PR:
- `vault kv patch secret/<env>/s3` with `S3_ENDPOINT_URL`, `S3_BUCKET_NAME`, region, addressing style, keys and
  `PUBLIC_MEDIA_BASE_URL`, then `scripts/restart.sh <env> backend`
- if the image host changes, adding it to the frontend's `GLOWDERMA_MEDIA_HOSTS` (a frontend rebuild)

Rollback in `deployment.md`: `aws s3 sync` the bucket back into `runtime/media` (otherwise uploads made while on
S3 404), set `USE_S3=false` and deploy, then copy back once more for the uploads made in between (both copies only
add files). Leaving the stack's own storage for a provider needs `OBJECT_STORAGE_ENABLED=false` (a PR):
`/media`, the backup and the deploy checks follow `stack.env`, and deploy, `restart.sh` and backup refuse the mix.

`disaster-recovery.md`: restore per mode (untar into `media-mirror`, then `aws s3 sync` back into the bucket).
`architecture.md`: the `/media/*` row and the object-storage service.

- [ ] **Step 5: Verify**

Run: `bash -n scripts/*.sh && shellcheck scripts/deploy.sh scripts/backup.sh scripts/manage.sh scripts/lib.sh && scripts/validate.sh`
Expected: clean.

If a staging server is available, deploy there with `OBJECT_STORAGE_ENABLED=true USE_S3=false`. Then run
`sync-media --apply`, deploy with `USE_S3=true`, and expect `media_storage_check` to print `ok` and a product
image URL to return 200. If no server is available, report that this end-to-end check wasn't run.

- [ ] **Step 6: Commit**

```bash
git add scripts docs
git commit -m "feat(ops): deploy, backup and sync for S3 object-storage media"
```
