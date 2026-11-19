# AI product content assistant — operations

The assistant suggests product texts (name, short description, description, ingredients, usage), SEO title and
meta description and image alt text in Uzbek, Russian and English, from the product's own data and optionally its
photos. A person always reviews the suggestions in the admin panel's product editor and applies the ones they want;
nothing is written to a product before that. Code: `glowderma/assistant/`, `glowderma/integrations/llm/`,
`glowderma/integrations/vault/`.

## 1. Moving parts

| Part | Where | Notes |
| --- | --- | --- |
| Settings singleton | ops console «AI yordamchi» (superusers) | on/off (ships **off**), daily and monthly budget (USD), jobs per staff member per day, images per request, active provider |
| Provider | ops console, provider page | model id (default `gemini-2.5-flash-lite`), temperature, output-token limit, timeout per attempt (5–20 s, default 20) and retries (0–2, default 2), which must fit the run's 90 s limit (each call's timeout is cut to the time left), prices per 1M input/output tokens (default 0.10 / 0.40 USD), enabled |
| API key | **Vault KV v2 only** | `<VAULT_KV_MOUNT>/data/<VAULT_AI_PREFIX>/<provider>` (e.g. `secret/data/production/ai/gemini`, field `api_key`); PostgreSQL keeps only the active / previous / pending version numbers, rotation time and actor, and the last test result |
| Prompt templates | ops console, prompt templates | five templates (description, languages, improvement, SEO, image extraction) with numbered versions; only style guidance is editable — the mandatory content rules are code |
| Generation jobs | Celery worker | one job per request; records mode, languages, fields, input snapshot, provider, model, prompt versions, suggestions, warnings, tokens, cost, error code |

## 2. Environment

| Variable | Default | Meaning |
| --- | --- | --- |
| `AI_MODE` | `fake` | `live` = Google Gemini through the official `google-genai` SDK. A fake is refused when used under the production settings (`ai_not_configured`). |
| `VAULT_MODE` | `fake` | `live` = KV v2 through the local Vault Agent `api_proxy` (`use_auto_auth_token`); Django holds no Vault token. The fake (in-memory) is refused under the production settings. |
| `VAULT_PROXY_URL` | – | The agent proxy: the **writer** agent for the api (it stores new key versions), the **reader** agent for the worker (it reads them). Required in production with `VAULT_MODE=live`. |
| `VAULT_KV_MOUNT` | `secret` | KV v2 mount. |
| `VAULT_AI_PREFIX` | `local/ai` | Key prefix under the mount; production must set the environment's (`production/ai`, `staging/ai`). |
| `AI_KEY_CACHE_SECONDS` | `300` | How long a worker keeps the active key version in memory (0..3600). The cache key includes the version number, so a rotation or rollback is used by every worker on its next job, without a restart. |

The defaults are `fake` (not `INTEGRATIONS_MODE`) so a deployment without the Vault agents keeps booting; the
assistant is simply unavailable there. The worker needs outbound HTTPS to `generativelanguage.googleapis.com`; the
api does not call Gemini.

## 3. Going live

1. Infra `feat/ai-vault`: policies `ai-writer` (create/update on `secret/data/<env>/ai/*`) and `ai-reader` (read on
   it), their AppRoles, `max_versions` on the path, and one Vault Agent sidecar per role with `api_proxy` on a private
   network shared only with the api (writer) or the worker (reader).
2. Backend environment: the infra compose sets `VAULT_MODE=live`, `VAULT_PROXY_URL`
   (api → `http://vault-agent-ai-writer:8100`, worker → `http://vault-agent-ai-reader:8100`), `VAULT_KV_MOUNT=secret`
   and `VAULT_AI_PREFIX=<env>/ai` on the api and worker services only. These four are topology keys: the shared
   secrets file (`secret/<env>/django` → `/run/secrets/django.json`) refuses them, because a copy there would give
   the worker the writer's address. `AI_MODE=live` goes in `secret/<env>/django` like every provider mode. Beat and
   migrate get no `VAULT_*` variables (their `VAULT_MODE` stays `fake`) and never call Vault. Run the migrations
   (`catalog.0011`, `assistant.0001`–`0002`).
   Infra sets `max_versions=20` on `secret/<env>/ai/<provider>`, so failed tests never prune the active or the
   previous version. The writer policy has no read and no metadata access: the api only writes a new version (the
   answer carries its number); every read — the test of a pending version and generation — runs in the worker.
3. `health/?ready=1`: `ai` and `vault` are `live` and configured.
4. Ops console, as a superuser: enter the key on the provider page (write-only field). The key is stored as a new,
   **inactive** Vault version; a worker reads that version and checks it with Gemini (`models.get`, no tokens
   generated); only then does it become active (the previous one is kept for rollback). A failed check leaves the
   old key active.
5. Test each prompt template on a sample product if you changed them, then switch the assistant on and set the
   budgets.

## 4. Safety

- The key is never in PostgreSQL, Celery arguments, logs, audit rows, admin history, API responses or exception
  texts: the code carries it as `integrations.secret.Secret` (renders `Secret(***)`, refuses pickling) from the Vault
  read to the SDK call. The SDK's own retry log lines (which quote the provider's error body) are kept out of the
  logs (`google_genai` logger at WARNING).
- Only product texts, SEO fields, names, volume, category name and the selected photos are sent to the provider —
  never prices, stock, customers, orders or staff data.
- Suggestions are validated (allowed keys only, length limits, claims lint) before they are shown and again when
  applied; the apply writes through `catalog.services.update_product` with the values seen at generation time and
  answers `409 product_changed` when someone changed them meanwhile.

## 5. Dependency note

`google-genai==3.5.0` (owner-approved) declares `tenacity<9.2`; the stack pins `tenacity==9.2.1`, so `pyproject.toml`
overrides the SDK's bound (`[tool.uv] override-dependencies`). The SDK's retry wrapper works unchanged with 9.2.1 (it
only emits a `DeprecationWarning` for `wait_exponential_jitter(initial=…)`); revisit when the SDK lifts its bound.
