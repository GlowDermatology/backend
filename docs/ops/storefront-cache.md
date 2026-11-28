# Storefront cache: instant refresh after an edit

The website (Next.js) caches its catalog reads so that one backend answer serves every visitor. Before this
change an edit reached the website only when the cached read expired:

- 5 minutes for products, categories and banners;
- 60 minutes for specialists, services and settings;
- 1 minute for the landing.

Now every write the website shows tells the website which cached reads are stale. The website refetches them
on the next visit.

There is no Redis copy of the API's answers to invalidate. The 60 s on `home/` and the catalog endpoints is
only a `Cache-Control` header for browsers and the mobile app. The cache that made edits slow is the website's
own (`"use cache"` with `cacheLife`, tagged per `src/data/shop/tags.ts`), so that is the one this invalidates.

## Reads and their tags (frontend)

| Website read | Tags |
|---|---|
| Product lists, product pages, related, sitemap (`getProduct`, `listProducts`, …) | `shop:products` (+ `shop:product:{id}`) |
| The landing summary (`home/`) | `shop:home`, `shop:products`, `shop:settings` |
| The discover feed | `shop:products`, `shop:categories` |
| Categories | `shop:categories` |
| Banners | `shop:banners` |
| Specialists, a specialist's page | `shop:specialists` (+ `shop:specialist:{id}`) |
| Services | `shop:services` |
| Next free slots | `shop:availability`, `shop:specialists` |
| Clinics with today's hours | `shop:clinics`, `shop:specialists` |
| Settings | `shop:settings` |

Every product read carries `shop:products`, so one tag refreshes every product list and page. Per-product tags
would refresh nothing more.

## Writes and the tags they mark (backend)

Each app declares what it shows in its `storefront` module (`<app>/storefront.py`, connected from its
`AppConfig.ready`):

- `core.storefront.watch(model, tags)` covers every save and delete of a model: services, the Django admin,
  the ops console, seeders.
- `core.storefront.changed(*tags)` is called explicitly where a write uses `QuerySet.update()` / `bulk_*`
  (which send no model signal) or writes outside the database.

| App | Write | Tags |
|---|---|---|
| catalog | `Product` saves and deletes; a Billz product change (a product's status, price or category decides its category's count and its banner links) | `shop:products`, `shop:categories`, `shop:banners` |
| catalog | `ProductImage`, `ProductStock` saves and deletes; a photo reorder; every stored Billz stock read (a newer read changes availability even with the same quantity: sold units Billz has now seen stop counting) | `shop:products` |
| catalog | `Category` saves and deletes (an inactive category hides its products and drops banner links) | `shop:categories`, `shop:products`, `shop:banners` |
| catalog | category reorder | `shop:categories` |
| catalog | `Banner` saves and deletes, banner reorder, banners unlinked from a deleted category | `shop:banners` |
| catalog | «Bosh sahifa» product and category picks | `shop:home` |
| professionals | `Professional` (the landing lists its picked specialists only while published) | `shop:specialists`, `shop:services`, `shop:home` |
| professionals | `Service` | `shop:services`, `shop:specialists` |
| professionals | `Location` | `shop:clinics`, `shop:specialists`, `shop:home` |
| professionals | the clinics a specialist works at (many-to-many) | `shop:clinics`, `shop:specialists` |
| professionals | `WorkingHours`, `WorkingHoursOverride`, `BlockedPeriod` | `shop:availability`, `shop:clinics` |
| professionals | photo removal | `shop:specialists` |
| professionals | «Bosh sahifa» specialist picks | `shop:home` |
| appointments | `Appointment` (booking, confirmation, cancellation, reschedule) | `shop:availability` |
| commerce | a checkout's reservations (`bulk_create`); a reservation converted or released (`_settle_reservations`); an order cancelled; reservations settled against Billz | `shop:products` |
| commerce | a new hourly bestsellers list (only when it differs, after it is stored) | `shop:home` |
| loyalty | a new cashback policy version (and its tiers) | `shop:home` |
| loyalty | a sale linked to its Billz push (`link_billz_sale`: whether its units still count as sold) | `shop:products` |
| site_config | `SiteSettings` | `shop:settings` |

## How a change travels

1. `changed()` runs inside the writer's transaction and registers the tags on its commit, so a rolled-back
   write sends nothing.
2. After the commit, the tags become one outbox message (`storefront.revalidate`), keyed by the tag set and a
   2-second window. A burst (a Billz import, a reorder, many checkouts) costs one call per tag set per window.
3. The message is due at the end of its window. A write committed inside a window is therefore always covered
   by that window's call, which is sent after it. The process also schedules the dispatcher for just after
   the window, so the call usually leaves 2–3 seconds after the save; the 10-second beat is the fallback.
4. `integrations.storefront` sends `POST {STOREFRONT_REVALIDATE_URL}` with
   `Authorization: Bearer {STOREFRONT_REVALIDATE_SECRET}` and `{"tags": [...]}`. The website expires the tags
   at once (`revalidateTag(tag, { expire: 0 })`), so the next visitor waits for fresh data.
5. Failures:
   - 400 / 401 / 404 (an unknown tag, a wrong secret, a website without the secret) go dead with the reason
     (ops console → outbox).
   - A website that is down or slow is retried with backoff.

The admin panel's own Server Actions also call `updateTag` for what they just saved («Bosh sahifa», clinics,
specialists). That costs nothing extra and makes the editor's own next page view fresh immediately.

## What still refreshes on the cache lifetime

These have no write to signal them. They change with time alone, so the lifetimes stay as the safety net:

- a banner's start or end time passing;
- a cashback policy version saved for a later date taking effect;
- a clinic opening or closing ("open now");
- the automatic carousel rotating with the hour;
- a free slot passing into the past;
- a lost call: a crash between the commit and step 2, or a dead message.

## Configuration

| Where | Setting | Value |
|---|---|---|
| backend (`secret/<env>/django`) | `STOREFRONT_REVALIDATE_URL` | `https://<site domain>/api/revalidate` |
| backend (`secret/<env>/django`) | `STOREFRONT_REVALIDATE_SECRET` | 32+ random characters |
| frontend (`secret/<env>/frontend`) | `GLOWDERMA_REVALIDATE_SECRET` | the same value |

- Set both backend settings or neither; production requires https.
- Without them the backend records the calls in memory and sends nothing; the website then refreshes on its
  lifetimes alone, exactly as before.
- Without the frontend secret the route answers 404, so the calls go dead with that reason.
- The worker reaches the website through its `egress` network, via the edge (Caddy routes `/api/revalidate`
  to Next.js). No compose change is needed.
- One website process: the cache is per Next.js process. With several frontend replicas each would need the
  call, or a shared cache handler.

Generate a secret with `openssl rand -hex 32`.
