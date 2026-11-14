# Image Variants Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Every stored image gets a 400 px and a 1000 px WebP variant at upload, every API image URL field gets
two sibling variant URL fields, and the web frontend uses the variants in lists and cards.

**Architecture:** Naming is a pure string rule in `core/media.py`, resizing sits next to `process_image()` in
`core/images.py`, and writing, deleting and backfilling live in a new `core/variants.py`. Each module's upload
service calls the core helper after saving the original; serializers derive variant URLs from the stored name
without touching storage. The frontend mappers read the new fields with a fallback to the original URL.

**Tech Stack:** Django 6.1, DRF, Pillow 13 (already pinned), Django storage API (local disk or S3); Next.js
(`next/image`), vitest.

**Spec:** `backend/docs/specs/2026-11-13-image-variants-design.md`

## Global Constraints

- No new dependency, no new database column, no migration (`makemigrations --check --dry-run` stays clean).
- Variants: `thumbnail` = longest side 400 px, `medium` = longest side 1000 px; both WebP, quality 80, no
  metadata, transparency kept, never upscaled, always written.
- Variant name = stored name of the original with the extension replaced by `_400.webp` / `_1000.webp`. Always
  derive from the name stored in the field (`field.name`), never from `media_name()`.
- Existing URL fields keep their name, value and nullability. New fields insert `thumbnail_` / `medium_` before
  `url` and have the same nullability as the field they sit next to.
- Storage access only through the field's own storage object (`field.storage`), so local disk and S3 both work.
- `core` imports no other module (import-linter). Module code reaches variants only through `glowderma.core`.
- FCM push `image_url` and the category export column stay the original URL.
- Backend: follow `backend/AGENTS.md` (dev loop, `@extend_schema`, regenerate and commit `openapi/schema.yaml`,
  coverage ≥ 85 %). Branch `feat/image-variants`, cut from the branch that contains the S3 media storage work
  (`feat/s3-media-storage` has uncommitted changes today: wait until it is committed). Never push.
- Frontend: branch `feat/image-variants` in `frontend/`; `npm run check` must pass.

## Review Focus

1. A source smaller than 400 px: both variants are written at the source size, not upscaled, and the URLs work.
2. A variant file that already exists (same bytes uploaded again; `file_overwrite=False` in S3 mode): no
   renamed stray file is left, and the existing file is kept.
3. A stored name with Django's collision suffix (`98967745a01b7d3b_mkY6nkg.webp`): the variants are
   `98967745a01b7d3b_mkY6nkg_400.webp` / `_1000.webp`, and deleting that image leaves the sibling image's files.
4. An original missing from storage during the backfill: reported and skipped, the remaining rows still processed.
5. A storage error while writing the second variant: the first variant is removed and the upload fails.

Each line has its test in the task that owns the code (Tasks 1, 2, 3 and 5).

## File Structure

Backend (`backend/`):

| File | Responsibility |
|---|---|
| `glowderma/core/media.py` (modify) | `VARIANT_SIDES`, `variant_name()`, `variant_url()` |
| `glowderma/core/images.py` (modify) | `make_variants()` |
| `glowderma/core/variants.py` (create) | `store_variants()`, `delete_with_variants()`, `backfill()` |
| `glowderma/core/management/commands/backfill_image_variants.py` (create) | the command |
| `glowderma/core/management/commands/seed_demo.py` (modify) | run the backfill after the seeders |
| `glowderma/{catalog,professionals,accounts,notifications}/services.py` (modify) | call store / delete |
| serializers and selectors listed in Tasks 6 and 7 (modify) | sibling URL fields |
| `docs/api/admin-api-v1.md`, `docs/api/customer-api-v1.md`, `openapi/schema.yaml`, `AGENTS.md` (modify) | contract |

Frontend (`frontend/`): `src/data/api/schema.d.ts` (regenerated), the mappers under `src/data/api/mappers/` and
`src/data/api/customer/`, the domain types they fill, and the components listed in Tasks 9 and 10.

---

## Part A: backend

### Task 1: Variant naming and URLs

**Files:**
- Modify: `glowderma/core/media.py`
- Test: `glowderma/core/tests/test_media.py`

**Interfaces:**
- Produces:
  - `type Variant = Literal["thumbnail", "medium"]`
  - `VARIANT_SIDES: Final[dict[Variant, int]] = {"thumbnail": 400, "medium": 1000}`
  - `variant_name(name: str, variant: Variant) -> str` (accepts a storage name or a full URL)
  - `variant_url(name: str, variant: Variant) -> str` = `absolute_media_url(variant_name(name, variant))`

- [ ] **Step 1: Write the failing tests** in `test_media.py`

```python
def test_variant_name_replaces_extension():
    assert variant_name("products/12/d5872db9aa73d5e9.jpg", "thumbnail") == "products/12/d5872db9aa73d5e9_400.webp"
    assert variant_name("products/12/d5872db9aa73d5e9.jpg", "medium") == "products/12/d5872db9aa73d5e9_1000.webp"


def test_variant_name_keeps_collision_suffix():
    assert variant_name("products/4/98967745a01b7d3b_mkY6nkg.webp", "thumbnail") == (
        "products/4/98967745a01b7d3b_mkY6nkg_400.webp"
    )


def test_variant_name_works_on_a_url_and_touches_only_the_last_segment():
    assert variant_name("https://glow.example/media/v1.2/banners/1/8349b8f7b347589a.png", "medium") == (
        "https://glow.example/media/v1.2/banners/1/8349b8f7b347589a_1000.webp"
    )


def test_variant_name_without_extension():
    assert variant_name("customers/7/abc", "thumbnail") == "customers/7/abc_400.webp"


def test_variant_url(settings):
    settings.PUBLIC_MEDIA_BASE_URL = "https://glow.example/media/"
    assert variant_url("categories/3/aa.png", "thumbnail") == "https://glow.example/media/categories/3/aa_400.webp"
```

- [ ] **Step 2: Run** `uv run pytest glowderma/core/tests/test_media.py -q`. Expected: FAIL (ImportError).
- [ ] **Step 3: Implement** the four names above in `core/media.py`. Split on the last `/`, then on the last
  `.` of that segment only.
- [ ] **Step 4: Run** the same command. Expected: PASS.
- [ ] **Step 5: Commit** `feat(core): add image variant naming`

### Task 2: Resizing

**Files:**
- Modify: `glowderma/core/images.py`
- Test: `glowderma/core/tests/test_images.py`

**Interfaces:**
- Consumes: `VARIANT_SIDES`, `Variant` (Task 1)
- Produces: `make_variants(content: bytes) -> dict[Variant, bytes]`, where `content` is the bytes
  `process_image()` returned (already validated)

- [ ] **Step 1: Write the failing tests** (build inputs with Pillow in the test, as the existing tests do)

```python
def _open(data: bytes) -> Image.Image:
    return Image.open(BytesIO(data))


def test_make_variants_sizes_and_format():
    variants = make_variants(_jpeg(2400, 1200))
    thumb, medium = _open(variants["thumbnail"]), _open(variants["medium"])
    assert (thumb.format, thumb.size) == ("WEBP", (400, 200))
    assert (medium.format, medium.size) == ("WEBP", (1000, 500))


def test_make_variants_never_upscales():
    variants = make_variants(_png(300, 150))
    assert _open(variants["thumbnail"]).size == (300, 150)
    assert _open(variants["medium"]).size == (300, 150)


def test_make_variants_keeps_transparency():
    variants = make_variants(_png_rgba(800, 800, alpha=0))
    thumb = _open(variants["thumbnail"]).convert("RGBA")
    assert thumb.getpixel((10, 10))[3] == 0


def test_make_variants_drops_metadata():
    assert "exif" not in _open(make_variants(_jpeg(1200, 1200))["medium"]).info
```

- [ ] **Step 2: Run** `uv run pytest glowderma/core/tests/test_images.py -q`. Expected: FAIL.
- [ ] **Step 3: Implement** `make_variants`. WebP `quality=80, method=4`; mode `RGBA` when the source has alpha,
  else `RGB`; `Image.Resampling.LANCZOS`; resize only when `max(size) > side`.
- [ ] **Step 4: Run** the same command. Expected: PASS.
- [ ] **Step 5: Commit** `feat(core): generate thumbnail and medium image variants`

### Task 3: Store and delete

**Files:**
- Create: `glowderma/core/variants.py`
- Test: `glowderma/core/tests/test_variants.py`

**Interfaces:**
- Consumes: `variant_name`, `VARIANT_SIDES` (Task 1), `make_variants` (Task 2)
- Produces:
  - `store_variants(storage: Storage, name: str, content: bytes) -> list[str]`: returns the names it wrote
  - `delete_with_variants(storage: Storage, name: str) -> None`: the original and both variants; a missing file
    is not an error; an empty `name` does nothing

- [ ] **Step 1: Write the failing tests** (use `default_storage`; the session `media_root` fixture isolates it)

```python
NAME = "products/1/aaaaaaaaaaaaaaaa.jpg"


def test_store_variants_writes_both(jpeg_bytes):
    written = store_variants(default_storage, NAME, jpeg_bytes)
    assert written == ["products/1/aaaaaaaaaaaaaaaa_400.webp", "products/1/aaaaaaaaaaaaaaaa_1000.webp"]
    assert all(default_storage.exists(name) for name in written)


def test_store_variants_keeps_an_existing_file_and_leaves_no_stray(jpeg_bytes):
    store_variants(default_storage, NAME, jpeg_bytes)
    assert store_variants(default_storage, NAME, jpeg_bytes) == []
    _, files = default_storage.listdir("products/1")
    assert sorted(files) == ["aaaaaaaaaaaaaaaa_1000.webp", "aaaaaaaaaaaaaaaa_400.webp"]


def test_store_variants_removes_a_renamed_file(jpeg_bytes, monkeypatch):
    # exists() said "no" but save() still stored under another name (a race): the stray must go
    real_save = default_storage.save
    monkeypatch.setattr(
        default_storage,
        "save",
        lambda name, content, max_length=None: real_save(name.replace(".webp", "_stray.webp"), content),
    )
    assert store_variants(default_storage, NAME, jpeg_bytes) == []
    monkeypatch.undo()
    assert default_storage.listdir("products/1")[1] == []


def test_store_variants_cleans_up_when_the_second_write_fails(jpeg_bytes, monkeypatch):
    real_save, calls = default_storage.save, []

    def save(name, content, max_length=None):
        calls.append(name)
        if len(calls) == 2:
            raise OSError("disk full")
        return real_save(name, content, max_length=max_length)

    monkeypatch.setattr(default_storage, "save", save)
    with pytest.raises(OSError):
        store_variants(default_storage, NAME, jpeg_bytes)
    assert not default_storage.exists("products/1/aaaaaaaaaaaaaaaa_400.webp")


def test_delete_with_variants(jpeg_bytes):
    default_storage.save(NAME, ContentFile(jpeg_bytes))
    store_variants(default_storage, NAME, jpeg_bytes)
    delete_with_variants(default_storage, NAME)
    assert default_storage.listdir("products/1")[1] == []
    delete_with_variants(default_storage, NAME)  # missing files: no error
    delete_with_variants(default_storage, "")    # empty name: nothing
```

`jpeg_bytes` is a fixture in this file: a 1200×800 JPEG built with Pillow. Each test uses its own owner
directory if the session-wide `MEDIA_ROOT` makes them collide.

- [ ] **Step 2: Run** `uv run pytest glowderma/core/tests/test_variants.py -q`. Expected: FAIL.
- [ ] **Step 3: Implement** both functions. `store_variants`: for each variant, skip when `storage.exists(target)`;
  otherwise `saved = storage.save(target, ContentFile(data))`; when `saved != target`, delete `saved` and do not
  report it. Wrap the loop so any exception deletes the names written in this call, then re-raises.
- [ ] **Step 4: Run** the same command. Expected: PASS.
- [ ] **Step 5: Commit** `feat(core): store and delete image variants`

### Task 4: Wire the upload and delete paths

**Files:**
- Modify: `glowderma/catalog/services.py` (`add_image`, `_store_category_image`, `_discard_on_error`,
  `_store_banner_image`, `_delete_file_on_commit`)
- Modify: `glowderma/professionals/services.py` (`_store_photo`, `set_specialist_photo`, `_remove_file_on_commit`)
- Modify: `glowderma/accounts/services.py` (`set_customer_image`, `_delete_image_on_commit`)
- Modify: `glowderma/notifications/services.py` (`_store_image`, `_remove_image`, `delete_draft`)
- Test: `glowderma/<module>/tests/test_image_variants.py` in each of the four modules (create)

**Interfaces:**
- Consumes: `store_variants(storage, name, content)`, `delete_with_variants(storage, name)` (Task 3)

**Rules for every site:**
- Store: right after the original is saved, call `store_variants(field.storage, field.name, processed.content)`
  with the name the field now holds. In `accounts.set_customer_image` the content is the `content` parameter.
- Delete: replace each `storage.delete(name)` and each `field.delete(save=False)` on an image with
  `delete_with_variants(storage, name)`, keeping today's timing (`transaction.on_commit`, `robust=True` where
  present, immediate where immediate). For `field.delete(save=False)`, read `storage` and `name` first, call
  `delete_with_variants`, then clear the field (`campaign.image = ""`) so the following `save` behaves as before.
- Error cleanup: `_discard_on_error` (catalog) and the `except BaseException` block in `set_specialist_photo`
  use `delete_with_variants`, so a rollback after the variants were written removes them too.
- Early returns for identical bytes (`previous == name`) stay; the variants already exist.

- [ ] **Step 1: Write the failing tests**, one file per module, each through the module's public service or
  API, asserting on `field.storage`:

```python
# shape shared by all four files; `names(obj)` = the original plus variant_name(.., "thumbnail"/"medium")
def test_upload_writes_variants():            # after the upload: all three names exist
def test_replace_deletes_previous_variants(): # upload A then B, callbacks executed: A's three names are gone
def test_delete_removes_variants():           # delete/remove endpoint or service: all three names are gone
```

  Cases per module: catalog = product image (also two identical uploads to one product, then delete one: the
  other's three files stay), category image (create, replace, `remove_image`), banner image (create, replace,
  delete banner); professionals = `set_specialist_photo`, `delete_specialist_photo`, `store_photo`;
  accounts/customers = `PUT me/image/`, `DELETE me/image/`, account deletion; notifications = campaign create
  with image, image replace, image removal on update, `delete_draft`. Use
  `django_capture_on_commit_callbacks(execute=True)` for on-commit deletes.
  Add one catalog test where `store_variants` raises (monkeypatch `glowderma.catalog.services.store_variants`):
  the category update fails and neither the new original nor a variant stays in storage.

- [ ] **Step 2: Run** `uv run pytest -q -k image_variants`. Expected: FAIL.
- [ ] **Step 3: Implement** the changes site by site, following the rules above.
- [ ] **Step 4: Run** `uv run pytest -q glowderma/catalog glowderma/professionals glowderma/accounts glowderma/customers glowderma/notifications`. Expected: PASS, including the existing upload tests.
- [ ] **Step 5: Commit** one commit per module: `feat(<module>): write and delete image variants`

### Task 5: Backfill command and seed

**Files:**
- Modify: `glowderma/core/variants.py`
- Create: `glowderma/core/management/commands/backfill_image_variants.py`
- Modify: `glowderma/core/management/commands/seed_demo.py`
- Test: `glowderma/core/tests/test_backfill_image_variants.py`

**Interfaces:**
- Consumes: Task 3
- Produces:
  - `@dataclass(frozen=True) class BackfillReport: checked: int; created: int; missing_originals: list[str]`
  - `backfill(*, dry_run: bool = False) -> BackfillReport` in `core/variants.py`
  - `manage.py backfill_image_variants [--dry-run]`

- [ ] **Step 1: Write the failing tests.** `core` tests may not import other modules' models: build rows through
  `apps.get_model("catalog", "Category")` (or the model with the fewest required fields) and write the original
  with `storage.save`.

```python
def test_backfill_creates_missing_variants():      # report.checked == 1, report.created == 2, files exist
def test_backfill_is_idempotent():                 # second run: created == 0
def test_backfill_dry_run_writes_nothing():        # created == 2 reported, no variant file in storage
def test_backfill_reports_a_missing_original():    # row points at a name not in storage: listed in
                                                   # missing_originals, the next row is still processed
def test_backfill_skips_empty_fields():            # a row with image="" is not counted
def test_command_prints_the_report():              # call_command output contains checked/created/missing counts
```

- [ ] **Step 2: Run** `uv run pytest glowderma/core/tests/test_backfill_image_variants.py -q`. Expected: FAIL.
- [ ] **Step 3: Implement** `backfill`: for every model in `apps.get_models()` and every field that is an
  `ImageField`, iterate rows with a non-empty value (`.iterator()`), skip when both variants exist, otherwise
  read the original through `field.storage` and call `store_variants`. `created` counts variant files written
  (or that would be written in a dry run). The command prints one summary line and each missing original.
- [ ] **Step 4:** In `seed_demo.py`, call `backfill()` after `run_seeders(context)` when seeders ran, and print
  its counts. Add a test in `glowderma/catalog/tests/test_seeds.py`: after the catalog seeder plus `backfill()`,
  the first seeded product image has both variants in storage.
- [ ] **Step 5: Run** `uv run pytest glowderma/core glowderma/catalog/tests/test_seeds.py -q`. Expected: PASS.
- [ ] **Step 6: Commit** `feat(core): backfill image variants for existing files`

### Task 6: Admin API fields

**Files:**
- Modify: `glowderma/catalog/api/admin/serializers.py` (`ProductImageSerializer`, product `main_image_url`,
  category `image_url`, banner `image_url`)
- Modify: `glowderma/professionals/api/admin/serializers.py` (`photo_url`)
- Modify: `glowderma/notifications/api/admin/serializers.py` (campaign `image_url`)
- Modify: `glowderma/commerce/selectors.py`, `glowderma/commerce/api/admin/serializers.py` (order item `image_url`)
- Modify: `docs/api/admin-api-v1.md`, `openapi/schema.yaml`
- Test: the existing API test module of each endpoint

**Interfaces:**
- Consumes: `variant_url(name, variant)` (Task 1)
- Produces (response fields):
  - product image: `thumbnail_url`, `medium_url`
  - product: `main_image_thumbnail_url`, `main_image_medium_url`
  - category, banner, campaign, order item: `image_thumbnail_url`, `image_medium_url`
  - specialist: `photo_thumbnail_url`, `photo_medium_url`

- [ ] **Step 1: Write the failing tests.** For each endpoint, one assertion with an image and one without:

```python
assert body["image_thumbnail_url"] == variant_url(category.image.name, "thumbnail")
assert body["image_medium_url"] == variant_url(category.image.name, "medium")
# without an image: the new fields equal what image_url returns for that case (None, or "" where image_url is "")
```

- [ ] **Step 2: Run** the touched test modules. Expected: FAIL (KeyError).
- [ ] **Step 3: Implement.** Each new field mirrors the empty-value behaviour of the field it sits next to.
- [ ] **Step 4:** Add the fields to the shapes in `docs/api/admin-api-v1.md` (sections for products §2.3,
  categories, banners, specialists, notifications, orders), with one sentence on the naming rule and sizes.
- [ ] **Step 5: Run** `uv run python manage.py spectacular --validate --fail-on-warn --file openapi/schema.yaml`
  and the touched tests. Expected: PASS, schema regenerated.
- [ ] **Step 6: Commit** `feat(api): variant URLs in the admin API`

### Task 7: Customer API fields

**Files:**
- Modify: `glowderma/customers/selectors.py` (profile `image_url`, banner `image_url`, product list
  `main_image_url`, product detail `images[].url`, category `image_url`)
- Modify: `glowderma/customers/api/customer/serializers.py`, `glowderma/customers/api/customer/me_views.py`
  and `glowderma/customers/services.py` (`set_image` response)
- Modify: `glowderma/commerce/selectors.py`, `glowderma/commerce/api/customer/serializers.py` (cart
  `main_image_url`, order item `image_url`)
- Modify: `glowderma/appointments/api/customer/serializers.py` (`photo_url`, two serializers)
- Modify: `glowderma/notifications/api/customer/serializers.py` (inbox `image_url`)
- Modify: `docs/api/customer-api-v1.md`, `openapi/schema.yaml`
- Test: the existing API test module of each endpoint

**Interfaces:**
- Consumes: `variant_url`, `variant_name` (Task 1)
- Produces: the same field names as Task 6, by the same rule. `PUT me/image/` returns
  `{image_url, image_thumbnail_url, image_medium_url}`. `services.set_image` returns the stored name's three
  URLs as a dict with those keys.

- [ ] **Step 1: Write the failing tests**, same shape as Task 6. Inbox notifications hold a full URL in
  `InboxNotification.image_url`: assert `image_thumbnail_url == variant_name(row.image_url, "thumbnail")`, and
  `None` when the stored value is empty. Order items hold a storage path: assert through `variant_url`.
- [ ] **Step 2: Run** the touched test modules. Expected: FAIL.
- [ ] **Step 3: Implement.** Extend `customers.selectors.media_url` with a sibling helper for the variants
  rather than repeating the `None` handling at each call site.
- [ ] **Step 4:** Update `docs/api/customer-api-v1.md` the same way as Task 6 Step 4.
- [ ] **Step 5: Run** the spectacular command and the touched tests. Expected: PASS.
- [ ] **Step 6: Commit** `feat(api): variant URLs in the customer API`

### Task 8: Backend close-out

**Files:**
- Modify: `AGENTS.md` (the "Uploads go through `core.images.process_image()`" line)

- [ ] **Step 1:** Extend that line: after saving the original, call `core.variants.store_variants()`; delete
  with `core.variants.delete_with_variants()`; variant URLs come from `core.media.variant_url()`.
- [ ] **Step 2: Run the full dev loop** from `AGENTS.md` (pytest, ruff check, ruff format --check, mypy,
  lint-imports, `makemigrations --check --dry-run`, spectacular, coverage ≥ 85 %). Expected: all pass, no
  migration generated.
- [ ] **Step 3: Commit** `docs: name the image variants helpers in AGENTS.md`

---

## Part B: frontend

Run after Part A is merged, so `gen:api` sees the new schema.

### Task 9: API types and mappers

**Files:**
- Modify: `src/data/api/schema.d.ts` (regenerated by `npm run gen:api`)
- Modify: `src/data/api/customer/mappers.ts`, `src/data/api/customer/account-mappers.ts`
- Modify: `src/data/api/mappers/products.ts`, `orders.ts`, `categories.ts`, `banners.ts`, `clinic.ts`,
  `notifications.ts`
- Modify: the domain types those mappers fill (`src/domain/shop.ts`, `src/domain/product.ts` and the types the
  other mappers return)
- Test: `src/data/api/customer/customer-api.test.ts`, `account-mappers.test.ts`,
  `src/data/api/mappers/catalog.test.ts`, `commerce.test.ts`

**Interfaces:**
- Produces: next to every existing optimizable image field `X` (`image`, `photo`, `src`, `mainImageUrl`,
  `imageUrl`), two fields `XThumb` and `XMedium` of the same type. Each is the variant URL when the API sends
  it, else the value of `X` (older backend, older fixtures). Customer mappers pass the variant through
  `optimizable(...)` exactly as they do for `X`.

- [ ] **Step 1: Write the failing tests**

```ts
it("maps variant URLs", () => {
  const card = mapProductCard({ ...api, main_image_thumbnail_url: T, main_image_medium_url: M }, hosts);
  expect(card.imageThumb).toBe(T);
  expect(card.imageMedium).toBe(M);
});
it("falls back to the original when the API sends no variants", () => {
  const card = mapProductCard(api, hosts);
  expect(card.imageThumb).toBe(card.image);
  expect(card.imageMedium).toBe(card.image);
});
it("drops a variant from a host next/image may not load", () => {
  expect(mapProductCard({ ...api, main_image_thumbnail_url: "https://other.uz/a_400.webp" }, hosts).imageThumb).toBeNull();
});
```

  Use the real mapper names in each test file; add the same three cases for an order item and a specialist.
- [ ] **Step 2: Run** `npm run test`. Expected: FAIL.
- [ ] **Step 3:** `npm run gen:api`, then implement the mapper and type changes.
- [ ] **Step 4: Run** `npm run check`. Expected: PASS.
- [ ] **Step 5: Commit** `feat(api): map image variant URLs`

### Task 10: Use the variants

**Files:**
- Modify (thumbnail, slots up to 200 CSS px): `src/features/shop/pages/product.tsx:148`,
  `src/features/shop/pages/private/orders.tsx:144,404`, `src/features/shop/pages/private/booking.tsx:214`,
  `src/features/admin/products/products-table.tsx`, `src/features/admin/banners/product-picker.tsx`,
  `src/features/admin/banners/banners-view.tsx`, `src/features/admin/categories/categories-view.tsx`,
  `src/features/admin/orders/order-sections.tsx`, `src/features/admin/specialists/profile-card.tsx`,
  `src/features/admin/products/image-manager.tsx`
- Modify (medium, cards and grids): the product card, category tile and specialist card images in
  `src/features/shop/pages/home.tsx:95,170,264` and `src/features/shop/pages/specialists.tsx:194`
- Unchanged (original): the banner hero in `home.tsx:229`, the product page gallery, Open Graph, JSON-LD, and
  every local-preview `<img>` (object URLs)

**Interfaces:**
- Consumes: `XThumb` / `XMedium` (Task 9)

- [ ] **Step 1:** Change the `src` of each listed image by the rule: rendered width up to 200 CSS px uses
  `XThumb`; cards and grids use `XMedium`; leave `sizes` and everything else untouched. Where a listed slot
  turns out wider than 200 CSS px, use `XMedium` and note it in the commit message.
- [ ] **Step 2:** Update `e2e/catalog-media.spec.ts` if it asserts on an image `src`.
- [ ] **Step 3: Run** `npm run check`. Expected: PASS.
- [ ] **Step 4: Verify in the browser** against a backend with seeded data (`seed_demo --reset`): on the home
  page and the orders page, the network panel shows `/_next/image?url=...` requests whose `url` parameter ends
  in `_1000.webp` (cards) or `_400.webp` (rows), none returns 404, and cards look sharp on a 2x screen.
- [ ] **Step 5: Commit** `feat(shop): load image variants in lists and cards`

---

## Rollout

1. Deploy the backend.
2. Per environment: `manage.py backfill_image_variants --dry-run`, then without the flag. Confirm
   `missing_originals` is empty or explained.
3. Deploy the frontend.

Step 2 before step 3: an image uploaded before this change has no variants until the backfill has run.
