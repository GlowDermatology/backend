# Release note — customer website (B-W1 web client + B-W2 hybrid identity, `feat/customer-web`)

One release carries both packages: the website as a customer client through its BFF (`X-GD-Platform: web`, several
sessions per customer and the «Faol seanslar» list, GeoIP, slugs and SEO texts, website social sign-in, the website
pay return page, the BFF throttle) and hybrid identity (accounts by e-mail or social without a phone, the phone gate,
`me/phone/`, `me/email/`). The hybrid identity details are in [`release-hybrid-identity.md`](release-hybrid-identity.md);
the variables are listed in `deploy/env.production.example` and §15 of [`deploy.md`](deploy.md) (groups 6b and 6c).

## Before the upgrade
- **Image.** The backend image adds one Python dependency, `maxminddb` 2.8.2 (pure Python reader of the GeoIP file);
  it comes with the rebuilt image (`uv.lock`), nothing to install on the host.
- **`.env` additions** — all optional with production defaults; `docker compose -f compose.prod.yaml config --quiet`
  and the production settings refuse an incomplete combination:
  - website: `PAYMENT_WEB_RETURN_URL` (https, `{order_id}` required), `GOOGLE_WEB_CLIENT_IDS`, `APPLE_SERVICES_IDS`
    (+ `APPLE_WEB_REDIRECT_URI` https, and `APPLE_TEAM_ID` / `APPLE_KEY_ID` / `APPLE_PRIVATE_KEY_FILE`, when
    `OAUTH_MODE=live`), `GEOIP_CITY_DB`;
  - e-mail codes: `EMAIL_MODE` (`fake` delivers nothing), with `smtp`: `EMAIL_HOST`, `EMAIL_FROM`, `EMAIL_USE_TLS` or
    `EMAIL_USE_SSL`, `EMAIL_HOST_USER` + `EMAIL_HOST_PASSWORD` together; `EMAIL_PORT`, `EMAIL_TIMEOUT`,
    `EMAIL_GLOBAL_HOURLY_LIMIT`, `EMAIL_OTP_IP_HOURLY_LIMIT`, `CUSTOMER_REAUTH_FRESH_SECONDS`;
  - `THROTTLE_RATES` may override the new scope `public_bff` (the BFF's own server-side reads).
- **GeoIP file (optional).** Download GeoLite2-City (MaxMind account) or DB-IP City Lite, put it in
  `GLOWDERMA_SECRETS_DIR` (mounted read-only at `/run/secrets/glowderma` in every backend container; group 10001,
  mode 0440) and set `GEOIP_CITY_DB=/run/secrets/glowderma/GeoLite2-City.mmdb`. Without it the session list shows "—".
  Refresh it monthly by copying the new file next to it and `mv`-ing it over the old name (the api reopens it within
  a minute). No compose change is needed.
- **Edge (Caddy).** No change: the website's BFF runs in the existing frontend container and calls the API on the
  internal network with `X-GD-Internal-Secret`, `X-GD-Client-IP` and `X-GD-Client-UA`; the edge keeps overwriting
  `X-Forwarded-For` (§16 of `deploy.md`).
- **Celery beat.** No new entry; `accounts-cleanup-auth` (hourly) now also purges e-mail codes and `email.send` rows
  and retires devices left without an open session.
- **Old app builds.** `Me.phone` and the admin customer phone are nullable now (hybrid identity) — see the hybrid
  release note before the storefront goes live.
- **Take the pre-upgrade dump** (`deploy/backup.sh`).

## Migrations (seven, in this order; one maintenance window as usual, `migrate` before `up -d`)
| Migration | What | Online-safe |
| --- | --- | --- |
| `accounts.0010_web_client_sessions` | `AuthSession.user_agent`, `last_ip`, `location`; `Device.platform` gains `web` | yes (columns with defaults) |
| `accounts.0011_hybrid_identity` | nullable `Customer.phone`, `Customer.email` + `email_verified_at`, `SmsCode.email`, constraints, index | see the hybrid release note (reverse refused once data would be lost) |
| `catalog.0005_seo_slugs` | `Product.slug` (backfilled from the uz name, else the Billz name) and the SEO texts | backfill rewrites every product row once |
| `catalog.0006_product_search_key` | `Product.search_text` rebuilt without apostrophes (`kopik` finds `ko‘pik`); drops the trigram index | short lock (column re-added, every product row recomputed) |
| `catalog.0007_product_search_key_trgm_concurrently` | the trigram index again, `CONCURRENTLY` (non-atomic) | yes; on failure `DROP INDEX CONCURRENTLY catalog_product_search_trgm` and migrate again |
| `commerce.0009_order_channel_web` | `Order.channel` gains `web` | yes (choices only) |
| `professionals.0003_seo_slugs` | `Professional.slug` (backfilled from the name) | backfill rewrites every specialist row once |

Rollback across this release: restore the pre-upgrade dump (`migrate accounts 0009` is refused once an e-mail or a
phoneless account exists).

## API additions (additive)
- Customer: `content_language` on products, categories and specialists (contract §1.4); the shipment timeline entry
  now reads "Yetkazib berish rasmiylashtirildi" (the BTS order is still created right after payment).
- Admin: `CustomerRef.email` (nullable) on orders, payments and appointments.
- Demo seed: 1200×1500 product photos and specialist portraits (`seed_demo` on staging only).

## After the upgrade
- `GET /api/v1/me/sessions/` from the website lists "Chrome (Windows)", the city and the IP of each session; the admin
  customer card shows the same list (`customers/{id}/sessions/`) for phoneless customers too.
- Watch `otp.email_budget_tripped`, dead `email.send` rows and `geoip.unavailable` warnings (a missing or unreadable
  `GEOIP_CITY_DB`).
