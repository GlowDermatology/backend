# S3 media storage switch (design)

Date: 2026-11-13. Status: approved in conversation. Spans `backend/` and `infra/`.

## Intent

Media (product, category, banner and avatar images) can live either on the local disk, as today, or in an
S3-compatible bucket. The bucket can be a self-hosted MinIO in the stack or an external provider. Two independent
switches control this, following the pattern in bsite (`bsite/backend/config/settings/base.py`, `USE_S3`):

- `MINIO_ENABLED` (stack.env): whether the stack pulls and runs MinIO at all.
- `USE_S3` (app config): whether Django stores media in S3. `MINIO_ENABLED=true` with `USE_S3=false` keeps local
  disk.

Decision log: this resolves I1 ("S3-compatible storage later"). C1 (data residency in Uzbekistan) is obsolete
(owner decision, 2026-11-13). `open-questions.md` gets updated to match.

## Goals

- Local disk stays the default everywhere (dev, CI, tests, staging, production) until someone flips the switch.
- Any S3-compatible provider (MinIO, AWS S3, Cloudflare R2, Hetzner, B2) works by changing configuration only.
- With MinIO, public image URLs (`https://SITE_DOMAIN/media/...`), the frontend image optimizer
  (`GLOWDERMA_MEDIA_HOSTS`) and mobile clients stay unchanged.
- Existing files can be copied into the bucket with one command, and the backups cover whichever store is
  active.

## Non-goals and anti-goals

- No signed or expiring URLs. Images are public and URLs are built from `PUBLIC_MEDIA_BASE_URL` + stored name.
  bsite's `querystring_auth` is deliberately not copied.
- No secrets in `stack.env` (the infra repo is public). bsite reads MinIO root credentials. Here the app gets its
  own access key from Vault.
- Static files stay on WhiteNoise. Only `STORAGES["default"]` changes.
- No CDN in this change.

## Approaches considered

1. **bsite-style switch, MinIO optional in the stack (chosen).** Two switches, provider-agnostic S3 settings, and
   Caddy proxies `/media/*` to MinIO so URLs don't change.
2. Managed provider only, no MinIO. Rejected: the owner wants a self-hosted option that can be pulled on demand.
3. MinIO always on. Rejected: it would pull and run an image even when unused.

## Design

### Backend (`backend/config/settings/base.py`)

- `USE_S3 = env_bool("USE_S3", False)`. With `False`, `STORAGES["default"]` stays `FileSystemStorage`
  (production.py keeps its own `STORAGES` block, so the switch is applied after it, or the block is moved into
  base).
- With `True`: `storages.backends.s3.S3Storage` with these options:
  - `bucket_name` = `S3_BUCKET_NAME`
  - `endpoint_url` = `S3_ENDPOINT_URL`
  - `access_key` / `secret_key` = `S3_ACCESS_KEY_ID` / `S3_SECRET_ACCESS_KEY`
  - `region_name` = `S3_REGION_NAME` (default `us-east-1`)
  - `addressing_style` = `S3_ADDRESSING_STYLE` (default `path`, which MinIO needs)
  - `location` = `S3_LOCATION` (default `media`)
  - `querystring_auth=False`, `default_acl=None`, `file_overwrite=False`
  - `verify` = `S3_VERIFY` (default true)
- With `USE_S3` on, bucket, access key and secret key are required (endpoint is optional so plain AWS works); any missing one raises `ImproperlyConfigured` naming the keys (never the values).
- Stored names keep today's layout (`<kind>/<owner>/<sha256-prefix>.<ext>`), so a name in the database means the
  same file in either store.
- Dependency: `django-storages[s3]` (brings `boto3`), pinned like the rest of `pyproject.toml`.
- Tests: the switch off gives FileSystemStorage, on gives S3Storage with the options above, a missing key raises.
  Upload and delete flows run against an in-memory or stubbed S3 (no network in CI).

### Where each value lives (deployed stack)

| Value | Place | Why |
|---|---|---|
| `MINIO_ENABLED` | `environments/<env>/stack.env` | public, deploy-time topology |
| `USE_S3`, `S3_ENDPOINT_URL`, `S3_BUCKET_NAME`, `S3_REGION_NAME`, `S3_ADDRESSING_STYLE` | `stack.env`, passed to backend services by `compose/app.yaml` | public, must agree with Caddy and MinIO |
| `S3_ACCESS_KEY_ID`, `S3_SECRET_ACCESS_KEY` | Vault `secret/<env>/s3-app` → `django.json` (MinIO's app user is created from the same pair; for an external provider the operator overwrites it) | secret |
| MinIO root user/password | Vault `secret/<env>/minio` → rendered by `vault-agent-datastores` | secret, only MinIO and its init job see it |
| `PUBLIC_MEDIA_BASE_URL` | `compose/app.yaml`, now `${PUBLIC_MEDIA_BASE_URL:-https://${SITE_DOMAIN}/media/}` | override only for an external provider |

`USE_S3` and the `S3_*` non-secret keys become topology keys in `config/secrets.py`, so Vault can't silently
override them.

### Infra (`infra/`)

- **MinIO service** in `compose/app.yaml` under `profiles: ["minio"]`. The image is pinned by tag and digest. Data
  goes in `${RUNTIME_DIR}/minio`. It runs hardened like the other services. It sits on `back` (for api and worker)
  and on `front` at a fixed `172.30.0.40` (for the edge). No published ports.
- **`minio-init`** (same profile, one-shot, `mc`): it creates the bucket, sets anonymous read on `<location>/*`,
  creates the app user and a policy limited to that bucket (from Vault `secret/<env>/s3`), and is idempotent.
- **`lib.sh`**: `app_compose` adds `--profile minio` when `stack_value MINIO_ENABLED` is `true`. So with `false`
  the image is never pulled or started. `deploy.sh` runs `minio-init` after MinIO is healthy.
- **Caddy**: `/media/*` on the site and the ops host is chosen by `MEDIA_BACKEND` (`disk` | `minio`) passed to the
  edge. `minio` rewrites to `/<bucket>/<location>/...` and reverse-proxies to `172.30.0.40:9000`, keeping today's
  headers (Cache-Control, nosniff, CSP). `disk` is today's `file_server`. `edge_probe.py` covers both.
- **`validate.sh`**: it refuses `USE_S3=true` with `S3_ENDPOINT_URL` pointing at MinIO while `MINIO_ENABLED=false`.
  It also checks the new IP literal stays consistent.
- **Backups (`backup.sh`)**: with local MinIO in use, it runs `mc mirror` of the bucket into a staging dir and
  then makes the usual tarball. With an external provider, it skips media (provider durability) and says so. With
  disk, nothing changes. `disaster-recovery.md` covers the restore for each mode.
- **Copying existing files**: a `manage.sh` / Django management command (`sync_media_to_s3`) uploads every file
  under `MEDIA_ROOT` that the bucket doesn't have yet. It's idempotent and dry-run first. Rollback is flipping
  `USE_S3=false`, because disk files are kept.

### Local development (`backend/compose.yaml`)

An optional `minio` service under a `minio` profile, with dev-only credentials, so S3 mode can be tried locally
with `docker compose --profile minio up`. The default stays local disk.

## Left to the builder

- The exact MinIO image tag and digest to pin. The last published community image is fine. Note that it gets no
  further security updates.
- Whether `STORAGES` moves fully into `base.py` or production keeps its block and re-applies the switch.
- The Caddy branching mechanism (snippet chosen via env placeholder, or a matcher), as long as `edge_probe.py`
  proves both modes.
- The S3 test double (`moto` as a dev dependency or a stubbed storage).
