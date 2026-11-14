# Image variants: thumbnail and medium sizes (design)

Date: 2026-11-13. Status: approved in conversation. Spans `backend/` and `frontend/`; `mobile/` has no image code
yet and only consumes the new API fields.

## Intent

List screens load several product photos at once. Today every screen receives the URL of the single stored file
(re-encoded, up to 2400 px). The web shop hides this behind the `next/image` optimizer, but the optimizer first
has to download the full file for every cold image, and the mobile app has no optimizer at all. The owner wants
both the web shop and the mobile app to load small files in lists and the large file only where a single image
is shown.

## Goals

- Every uploaded image has two extra, smaller files from the moment the upload returns.
- Every API response that carries an image URL also carries the URLs of the two smaller files.
- The web shop and the admin panel use the smaller files in lists and cards.
- Existing images get their smaller files through one re-runnable command.

## Non-goals and anti-goals

- No CDN and no on-the-fly resizing service (imgproxy, Thumbor).
- No new database columns and no migration.
- No asynchronous generation: there is never a moment when a stored image lacks its variants.
- Existing URL fields keep their name, value and nullability. Nothing a current client reads changes.
- The push notification image sent to FCM stays the original file.
- The category export column stays the original URL.
- No change to `process_image()` limits (5 MiB, 2400 px, JPEG/PNG/WebP).

## Approaches considered

1. **Fixed variants generated in Django at upload (chosen).** Pillow and the storage backend are already there,
   mobile gets small files without any new service, and the web optimizer starts from a small source.
2. On-the-fly resizing (imgproxy or Thumbor in front of the bucket). Rejected: a new service to run and secure,
   and it needs a CDN, which the S3 media spec deliberately left out.
3. Tuning `next/image` only (cache TTL, size list). Rejected: does nothing for the mobile app.

Inside approach 1, generation is **synchronous in the upload request** (owner decision). A Celery task was
rejected because the API would need a fallback while the variant does not exist yet.

## Design

### Variants

| Variant | Longest side | Use |
|---|---|---|
| `thumbnail` | 400 px | cart and order rows, admin tables, pickers, avatars: any slot up to 200 CSS px |
| `medium` | 1000 px | product cards and grids, mobile detail screen |
| original | up to 2400 px (unchanged) | web product page, banners in the hero slot, zoom |

- Both variants are WebP, quality 80, with no metadata. Transparency is kept.
- A source smaller than the target is never upscaled. The variant is still written, at the source's own size,
  so a variant URL is valid for every stored image.
- The same two sizes apply to all six image kinds: product images, category images, banners, specialist
  photos, customer images and notification campaign images.

### Naming

A variant's storage name is derived from the **stored** name of the original (the name in the database field,
which may carry Django's collision suffix), never recomputed from the bytes:

```
products/12/d5872db9aa73d5e9.jpg          original
products/12/d5872db9aa73d5e9_400.webp     thumbnail
products/12/d5872db9aa73d5e9_1000.webp    medium
```

The rule is a pure string operation on the last path segment (replace the extension with `_<side>.webp`). It
therefore also works on a full URL, which matters for the two places that store a URL or path snapshot
(`OrderItem.image_url`, `InboxNotification.image_url`).

### Backend

- **One place for the logic, in `core`.** Pixel work sits next to `process_image()` in `core/images.py`, the
  naming rule next to `media_name()` in `core/media.py`, and the storage steps (write, delete, backfill) in a
  new `core/variants.py`. No module has its own resizing code.
- **Write.** Each upload service calls the core helper right after it saves the original, inside the same
  transaction block. Six upload paths: product image, category image, banner image (`catalog`), specialist
  photo, both the admin API path and the Django-admin path (`professionals`), customer image (`accounts`,
  called from `customers`), campaign image (`notifications`).
- **Existing variant.** Names are content-addressed, so a variant that already exists is left as it is. The
  helper checks `exists()` first because both storages rename on a name collision (`file_overwrite=False` in
  S3 mode); a file that was renamed anyway by a race is removed.
- **Failure.** If a variant cannot be written, the helper removes what it wrote in that call and re-raises.
  The upload fails with the service's existing error handling; no row points at an image without variants.
- **Delete.** Every place that deletes or replaces an image file deletes the two variants with it, with the
  same timing as today (on commit where it is on commit today).
- **API.** For every image URL field, two sibling fields with the same nullability. The name is the existing
  field name with `thumbnail_` or `medium_` inserted before `url`:

  | Existing field | New fields |
  |---|---|
  | `url` (product gallery item) | `thumbnail_url`, `medium_url` |
  | `image_url` | `image_thumbnail_url`, `image_medium_url` |
  | `main_image_url` | `main_image_thumbnail_url`, `main_image_medium_url` |
  | `photo_url` | `photo_thumbnail_url`, `photo_medium_url` |

  This covers the admin API (products, product images, categories, banners, specialists, campaigns, order
  items) and the customer API (profile, `PUT me/image/` response, banners, product list and detail,
  categories, cart, order items, specialists, inbox notifications). URLs are derived from the stored name; the
  serializers do not ask the storage whether the file exists.
- **Existing images.** `manage.py backfill_image_variants` walks every `ImageField` of every installed model,
  so a new image kind is covered without touching the command. It writes only missing variants, reports an
  original that is missing from storage and continues, and has `--dry-run`. `seed_demo` runs the same backfill
  after the seeders, so demo data always has variants.
- **Contract documents.** `docs/api/admin-api-v1.md`, `docs/api/customer-api-v1.md` and `openapi/schema.yaml`
  gain the new fields; `AGENTS.md` names the variants helper next to `process_image()`.

### Frontend (web shop and admin panel)

- The API types are regenerated (`npm run gen:api`).
- The mappers read the new fields and fall back to the original URL when a field is absent, so the frontend
  also works against a backend that predates this change.
- Which file a slot uses follows its rendered size, because `next/image` requests up to twice the CSS width on
  dense screens and must not upscale a 400 px file into a 2x card:
  - slots up to 200 CSS px (cart rows, order rows, booking rows, admin tables, pickers, avatars): thumbnail
  - product cards, grids, category tiles, specialist cards: medium
  - product page gallery, banner hero, Open Graph and JSON-LD: original
- `next/image` stays in place. It now fetches a small source, which removes the slow first load.

### Rollout

1. Backend deploy.
2. `backfill_image_variants` once per environment (dry run first).
3. Frontend deploy.

Step 2 must finish before step 3: the frontend requests variant URLs, and an image uploaded before this change
has none until the backfill has run.

## Accepted trade-offs

- Three files per image in storage instead of one.
- Variants are generated and written under the row lock the upload service already holds (a few hundred
  milliseconds of CPU, and two extra writes in S3 mode). Uploads are staff actions, plus a customer's own
  avatar, so this is accepted.
- Changing a size later means re-running the backfill and removing the old files.
- An image whose original was deleted keeps returning a variant URL that 404s wherever a snapshot still
  points at it (old order items, old inbox notifications). The original URL behaves the same way today.

## Left to the builder

- The Pillow call sequence for resizing (for example the thumbnail derived from the medium image).
- How each serializer declares the two sibling fields, as long as the names follow the table above.
- The exact frontend domain field names, following the existing `image` / `imageUrl` pattern in each mapper.
