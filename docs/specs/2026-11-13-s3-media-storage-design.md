# S3 media storage switch (design)

Date: 2026-11-13. Status: approved in conversation. Spans `backend/` and `infra/`.

## Intent

Media (product, category, banner and avatar images) can live either on the local disk, as today, or in an
S3-compatible bucket. The bucket can be a self-hosted SeaweedFS in the stack or an external provider. Two independent
switches control this, following the pattern in bsite (`bsite/backend/config/settings/base.py`, `USE_S3`):

- `OBJECT_STORAGE_ENABLED` (stack.env): whether the stack pulls and runs SeaweedFS at all.
- `USE_S3` (app config): whether Django stores media in S3. `OBJECT_STORAGE_ENABLED=true` with `USE_S3=false` keeps local
  disk.

Decision log: this resolves I1 ("S3-compatible storage later"). C1 (data residency in Uzbekistan) is obsolete
(owner decision, 2026-11-13). `open-questions.md` gets updated to match.

## Goals

- Local disk stays the default everywhere (dev, CI, tests, staging, production) until someone flips the switch.
- Any S3-compatible provider (SeaweedFS, AWS S3, Cloudflare R2, Hetzner, B2) works by changing configuration only.
- With SeaweedFS, public image URLs (`https://SITE_DOMAIN/media/...`), the frontend image optimizer
  (`GLOWDERMA_MEDIA_HOSTS`) and mobile clients stay unchanged.
- Existing files can be copied into the bucket with one command, and the backups cover whichever store is
  active.

## Non-goals and anti-goals

- No signed or expiring URLs. Images are public and URLs are built from `PUBLIC_MEDIA_BASE_URL` + stored name.
  bsite's `querystring_auth` is deliberately not copied.
- No secrets in `stack.env` (the infra repo is public). bsite reads the object-storage root credentials. Here the app gets its
  own access key from Vault.
- Static files stay on WhiteNoise. Only `STORAGES["default"]` changes.
- No CDN in this change.

## Approaches considered

1. **bsite-style switch, self-hosted S3 (SeaweedFS) optional in the stack (chosen).** Two switches,
   provider-agnostic S3 settings, and Caddy proxies `/media/*` to the object storage so URLs don't change. MinIO
   was the original pick, but its free images are no longer published (2025), hence SeaweedFS (Apache-2.0,
   actively released).
2. Managed provider only, no self-hosted storage. Rejected: the owner wants a self-hosted option that can be pulled on demand.
3. Self-hosted storage always on. Rejected: it would pull and run an image even when unused.

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
  - `addressing_style` = `S3_ADDRESSING_STYLE` (default `path`, which SeaweedFS needs)
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
| `OBJECT_STORAGE_ENABLED` | `environments/<env>/stack.env` | public, deploy-time topology |
| `USE_S3`, `S3_LOCATION` | `stack.env`, passed to backend services by `compose/app.yaml` | they change what runs and how Caddy serves; topology keys |
| `S3_ENDPOINT_URL`, `S3_BUCKET_NAME`, `S3_REGION_NAME`, `S3_ADDRESSING_STYLE`, `S3_VERIFY`, `S3_ACCESS_KEY_ID`, `S3_SECRET_ACCESS_KEY` | Vault `secret/<env>/s3` (keys = env var names) → `django.json` | owner decision (2026-11-13): switching provider or bucket must need no PR, only `vault kv patch` + `scripts/restart.sh <env> backend`. Seeded for the stack's object storage; its `app` identity in `s3.json` uses the same key pair |
| Object-storage admin access key pair | Vault `secret/<env>/object-storage` (`OBJECT_STORAGE_ADMIN_ACCESS_KEY_ID`, `OBJECT_STORAGE_ADMIN_SECRET_ACCESS_KEY`) → rendered by `vault-agent-datastores` | secret, only the object storage and its init job see it |
| `PUBLIC_MEDIA_BASE_URL` | compose default `https://${SITE_DOMAIN}/media/`; Vault `secret/<env>/s3` may override (external provider) | no longer a topology key |

The stack's own bucket is the fixed literal `glowderma-media` (Caddy and `object-storage-init` can't read Vault).
Consistency rules that need Vault values (object-storage endpoint while it is off, external provider without
`PUBLIC_MEDIA_BASE_URL`) are checked by `deploy.sh`. The frontend's `GLOWDERMA_MEDIA_HOSTS` is build-time, so a new
image host still needs a frontend rebuild.

### Infra (`infra/`)

- **Object-storage service** (SeaweedFS, `chrislusf/seaweedfs:4.53@sha256:ccad23e18639c8b98cee17e7100b95830a06077b56d8e5b006d6dcf8ec5e94a0`)
  in `compose/app.yaml` under `profiles: ["object-storage"]`, command
  `server -dir=/data -s3 -s3.config=/etc/seaweedfs/s3.json` (S3 API on 8333). The image is pinned by tag and digest.
  Data goes in `${RUNTIME_DIR}/object-storage`. It runs hardened like the other services, as `999:999` (the image
  runs as root by default, so the builder verifies a 999-owned data dir works). It sits on `back` (for api and
  worker) and on `front` at a fixed `172.30.0.40` (for the edge). No published ports.
- **Access model**: a SeaweedFS `s3.json` identities file, rendered by `vault-agent-datastores` from Vault
  (`secret/<env>/object-storage` + `secret/<env>/s3`) and mounted read-only. Three identities: `admin`
  (`Admin`, `Read`, `Write`, `List`, `Tagging`), `app` (`Read`/`Write`/`List`/`Tagging` on `glowderma-media`, using the
  `S3_ACCESS_KEY_ID` / `S3_SECRET_ACCESS_KEY` pair) and `anonymous` (`Read:glowderma-media`; this identity name is how
  SeaweedFS grants unauthenticated GET). There is no admin API and no bucket policy. Anonymous read is bucket-wide,
  not limited to `<location>/*` (the bucket holds only media plus short-lived healthcheck probes); anonymous
  LIST/PUT/DELETE are refused.
- **`object-storage-init`** (profile `object-storage-tools`, one-shot, `amazon/aws-cli:2.40.3@sha256:33a16c53ea442e918b58df67301ad76a8fb57082c39f7e78753b898c3f3e87c2`
  with the admin keys): it only creates the bucket. HTTP 409 (BucketAlreadyOwnedByYou / BucketAlreadyExists)
  counts as success, so it is idempotent.
- **`lib.sh`**: `app_compose` adds `--profile object-storage` when `stack_value OBJECT_STORAGE_ENABLED` is `true`. So with `false`
  the image is never pulled or started. `deploy.sh` runs `object-storage-init` after the object storage is healthy.
- **Caddy**: `/media/*` on the site and the ops host is chosen by `MEDIA_BACKEND` (`disk` | `object-storage`) passed to the
  edge. `object-storage` rewrites to `/<bucket>/<location>/...` and reverse-proxies to `172.30.0.40:8333`, keeping today's
  headers (Cache-Control, nosniff, CSP). `disk` is today's `file_server`. `edge_probe.py` covers both.
- **`validate.sh`**: it refuses `USE_S3=true` with `S3_ENDPOINT_URL` pointing at `172.30.0.40` while `OBJECT_STORAGE_ENABLED=false`.
  It also checks the new IP literal stays consistent.
- **Backups (`backup.sh`)**: with the local object storage in use, it runs `aws s3 sync` of the bucket into a staging dir and
  then makes the usual tarball. With an external provider, it skips media (provider durability) and says so. With
  disk, nothing changes. `disaster-recovery.md` covers the restore for each mode.
- **Copying existing files**: a `manage.sh` / Django management command (`sync_media_to_s3`) uploads every file
  under `MEDIA_ROOT` that the bucket doesn't have yet. It's idempotent and dry-run first. Rollback is flipping
  `USE_S3=false`, because disk files are kept.

### Local development (`backend/compose.yaml`)

An optional `object-storage` service (same SeaweedFS image) under an `object-storage` profile, with a committed
dev-only `s3.json` (dev-only keys, labelled not for production) and an `object-storage-init` one-shot (aws-cli)
that creates `glowderma-media`, so S3 mode can be tried locally with
`docker compose --profile object-storage up`. The default stays local disk.

## Left to the builder

- Whether `STORAGES` moves fully into `base.py` or production keeps its block and re-applies the switch.
- The Caddy branching mechanism (snippet chosen via env placeholder, or a matcher), as long as `edge_probe.py`
  proves both modes.
- The S3 test double (`moto` as a dev dependency or a stubbed storage).
