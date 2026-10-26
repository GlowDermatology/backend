"""Transactional writes of `catalog` (contract §2.3–§2.5; design 01 §4).

Every function runs in its own transaction, takes the row locks the design names (the product row for
image writes, an advisory lock for category / banner positions) and writes the audit row in the same
transaction. Views validate shapes with serializers and call these with validated data; the two business
rules that need the resulting state — the product **active rule** and the exact-set reorders — live here.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Collection, Iterable, Sequence
from dataclasses import dataclass
from hashlib import sha256
from pathlib import PurePosixPath
from typing import Any, Literal

from django.core.files.base import ContentFile
from django.db import IntegrityError, connection, transaction
from rest_framework.exceptions import NotFound, ValidationError

from glowderma.catalog import selectors
from glowderma.catalog.models import Banner, Category, Product, ProductImage, ProductStock
from glowderma.core import audit, enums, integration_modes, timeutils
from glowderma.core.api.exceptions import Conflict, field_error
from glowderma.core.images import ImageError, ProcessedImage, process_image
from glowderma.core.media import media_name
from glowderma.integrations.billz.ports import BillzListingIncomplete, BillzProduct

logger = logging.getLogger(__name__)

MAX_PRODUCT_IMAGES = 8
MIN_PRICE = 1_000
TEXT_FIELDS = ("name", "description", "ingredients", "usage")
LANGUAGES = ("uz", "ru", "en")
LAST_IMAGE_OF_ACTIVE = "Faol mahsulotda kamida bitta rasm bo‘lsin."
CATEGORY_HAS_PRODUCTS = "Kategoriyada mahsulotlar bor."
CATEGORY_LOCK = "catalog_category_position"
BANNER_LOCK = "catalog_banner_position"
BILLZ_ID_TAKEN = "Bu Billz ID bilan mahsulot allaqachon bor."
# Admin contract §2.3/§2.14, customer contract §2.3/§6.14: a fake Billz under the production settings never
# invents or refreshes Billz fields or stock.
BILLZ_FAKE_MODE = (
    "Billz sinov rejimida: Billz ma’lumotlarini olib bo‘lmaydi. Billz ulanganidan keyin qayta urinib ko‘ring."
)
BILLZ_FAKE_MODE_CODE = "billz_fake_mode"
# PostgreSQL names of the two ``unique=True`` columns (``<table>_<column>_key``, see the 0001 migration).
PRODUCT_BILLZ_ID_CONSTRAINT = "catalog_product_billz_id_key"
CATEGORY_SLUG_CONSTRAINT = "catalog_category_slug_key"

type UpsertOutcome = Literal["created", "updated", "unchanged"]


def _advisory_lock(key: str) -> None:
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", [key])


def _basename(name: str) -> str:
    return PurePosixPath(name).name


def _delete_file_on_commit(field: Any) -> None:
    name = field.name
    storage = field.storage
    if name:
        transaction.on_commit(lambda: storage.delete(name))


def _diff(before: dict[str, Any], after: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    changed = [key for key in after if before.get(key) != after[key]]
    return {key: before.get(key) for key in changed}, {key: after[key] for key in changed}


def _violates(exc: IntegrityError, constraint: str) -> bool:
    """Whether ``exc`` is the unique violation of ``constraint`` (psycopg ``diag``, else the message text)."""
    cause = exc.__cause__
    name = getattr(getattr(cause, "diag", None), "constraint_name", None)
    if name is not None:
        return bool(name == constraint)
    return constraint in str(exc)  # pragma: no cover - a driver without diagnostics


def _save_unique(save: Any, constraint: str, field: str, message: str | None = None) -> None:
    """Run ``save`` in a savepoint; a unique violation of ``constraint`` becomes 400 ``unique`` on ``field``.

    The serializers pre-check uniqueness for the ordinary case; this covers the race (two staff, or an admin
    create against the Billz import) where both pass the check and the loser would otherwise be a 500.
    """
    try:
        with transaction.atomic():
            save()
    except IntegrityError as exc:
        if not _violates(exc, constraint):
            raise
        raise ValidationError({field: [field_error("unique", message)]}) from exc


def _save_product(product: Product) -> None:
    _save_unique(product.save, PRODUCT_BILLZ_ID_CONSTRAINT, "billz_id", BILLZ_ID_TAKEN)


def _process(uploaded: Any, field: str) -> ProcessedImage:
    try:
        return process_image(uploaded)
    except ImageError as exc:
        raise ValidationError({field: [field_error(exc.code)]}) from exc


# --- products -----------------------------------------------------------------------------------------------


@dataclass(slots=True)
class ProductInput:
    """Validated body of ``POST products/`` / ``PATCH products/{id}/`` (unset keys stay ``None``)."""

    billz_id: str | None = None
    billz_product: BillzProduct | None = None  # fetched by the serializer when billz_id is set / changed
    texts: dict[str, dict[str, str]] | None = None  # language-first, leaves only
    price: int | None = None
    category: Category | None = None
    category_set: bool = False  # distinguishes "category": null from an absent key
    promo: bool | None = None
    status: str | None = None


def product_snapshot(product: Product) -> dict[str, Any]:
    """The admin-owned fields as flat dotted keys (the audit diff of ``product.updated``)."""
    snapshot: dict[str, Any] = {
        "billz_id": product.billz_id,
        "price": product.price,
        "category": product.category.slug if product.category_id and product.category else None,
        "promo": product.promo,
        "status": product.status,
    }
    for language in LANGUAGES:
        for field in TEXT_FIELDS:
            snapshot[f"texts.{language}.{field}"] = getattr(product, f"{field}_{language}")
    return snapshot


def _apply_billz(product: Product, row: BillzProduct) -> None:
    product.billz_id = row.billz_id
    product.billz_name = row.billz_name
    if row.volume is not None:  # None: Billz asserted no volume (no BILLZ_VOLUME_FIELD); keep the stored one
        product.volume = row.volume
    product.billz_price = row.price
    product.billz_synced_at = row.synced_at


def _apply_texts(product: Product, texts: dict[str, dict[str, str]]) -> None:
    for language, leaves in texts.items():
        for field, value in leaves.items():
            setattr(product, f"{field}_{language}", value)


def _check_active_rule(product: Product, image_count: int) -> None:
    """Contract §2.3: an active product needs a category and at least one image (and a sellable price)."""
    if product.status != enums.ProductStatus.ACTIVE:
        return
    errors: dict[str, list[Any]] = {}
    if product.category_id is None:
        errors["category"] = [field_error("required_for_active", "Faol mahsulot uchun kategoriyani tanlang.")]
    if image_count == 0:
        errors["images"] = [field_error("required_for_active", LAST_IMAGE_OF_ACTIVE)]
    if product.price < MIN_PRICE:
        errors["price"] = [field_error("min_value", "Narx kamida 1 000 so‘m bo‘lsin.")]
    if errors:
        raise ValidationError(errors)


def _change_kind(before: dict[str, Any], after: dict[str, Any]) -> str:
    """Contract §2.3 priority: created > price_changed > activated/hidden > promo_added/removed > updated."""
    if before["price"] != after["price"]:
        return enums.ProductChangeKind.PRICE_CHANGED
    if before["status"] != after["status"]:
        return (
            enums.ProductChangeKind.ACTIVATED
            if after["status"] == enums.ProductStatus.ACTIVE
            else enums.ProductChangeKind.HIDDEN
        )
    if before["promo"] != after["promo"]:
        return (
            enums.ProductChangeKind.PROMO_ADDED if after["promo"] else enums.ProductChangeKind.PROMO_REMOVED
        )
    return enums.ProductChangeKind.UPDATED


def _set_last_change(product: Product, kind: str, actor: Any, before: dict[str, Any] | None) -> None:
    product.last_change_at = timeutils.now()
    product.last_change_kind = kind
    product.last_change_actor = actor
    if kind == enums.ProductChangeKind.PRICE_CHANGED and before is not None:
        product.last_change_old_price = before["price"]
        product.last_change_new_price = product.price
    else:
        product.last_change_old_price = None
        product.last_change_new_price = None


class BillzNotLive(Conflict):
    """409 ``conflict``: the Billz port is fake under the production settings (admin contract §2.3)."""

    default_detail = BILLZ_FAKE_MODE


def require_live_billz() -> None:
    """Refuse (``BillzNotLive``) every write of Billz-owned data while Billz is fake under the production
    settings (``core.integration_modes.fake_in_production("billz")``): online selling needs live Billz
    stock."""
    if integration_modes.fake_in_production("billz"):
        raise BillzNotLive(BILLZ_FAKE_MODE)


def create_product(data: ProductInput, *, actor: Any, request: Any = None) -> Product:
    """``POST products/`` → the created product (the active rule always fails here: no images yet)."""
    require_live_billz()
    if data.billz_product is None or data.price is None:
        raise ValueError("create_product needs the Billz product and the price")
    with transaction.atomic():
        product = Product(
            price=data.price,
            category=data.category,
            promo=bool(data.promo),
            status=data.status or enums.ProductStatus.HIDDEN,
            is_new_from_billz=False,
        )
        _apply_billz(product, data.billz_product)
        if data.texts:
            _apply_texts(product, data.texts)
        _check_active_rule(product, image_count=0)
        _set_last_change(product, enums.ProductChangeKind.CREATED, actor, None)
        _save_product(product)
        ProductStock.objects.create(
            product=product, quantity=data.billz_product.stock, synced_at=data.billz_product.synced_at
        )
        audit.record(
            action="product.created",
            entity_type="product",
            entity_id=str(product.pk),
            actor=actor,
            request=request,
            after=product_snapshot(product),
        )
    return _reload_product(product.pk)


def update_product(product_id: int, data: ProductInput, *, actor: Any, request: Any = None) -> Product:
    """``PATCH products/{id}/``: fields optional, ``texts`` merged at leaf level, last_change by priority."""
    with transaction.atomic():
        product = (
            Product.objects.select_for_update(of=("self",))
            .select_related("category")
            .filter(pk=product_id)
            .first()
        )
        if product is None:
            raise NotFound
        before = product_snapshot(product)
        if data.billz_product is not None and data.billz_product.billz_id != product.billz_id:
            require_live_billz()
            _apply_billz(product, data.billz_product)
            sync_stock(product, data.billz_product.stock, data.billz_product.synced_at)
        if data.texts:
            _apply_texts(product, data.texts)
        if data.price is not None:
            product.price = data.price
        if data.category_set:
            product.category = data.category
        if data.promo is not None:
            product.promo = data.promo
        if data.status is not None:
            product.status = data.status
        _check_active_rule(product, image_count=product.images.count())
        after = product_snapshot(product)
        product.is_new_from_billz = False
        _set_last_change(product, _change_kind(before, after), actor, before)
        _save_product(product)
        diff_before, diff_after = _diff(before, after)
        audit.record(
            action="product.updated",
            entity_type="product",
            entity_id=str(product.pk),
            actor=actor,
            request=request,
            before=diff_before,
            after=diff_after,
        )
    return _reload_product(product.pk)


def _reload_product(product_id: int) -> Product:
    product = selectors.get_product(product_id)
    if product is None:  # pragma: no cover - the row was just written in a committed savepoint
        raise NotFound
    return product


# --- product images -----------------------------------------------------------------------------------------


def _locked_product(product_id: int) -> Product:
    product = Product.objects.select_for_update(of=("self",)).filter(pk=product_id).first()
    if product is None:
        raise NotFound
    return product


def _ordered_images(product: Product) -> list[ProductImage]:
    return list(product.images.order_by("position", "id"))


def _compact_positions(images: Sequence[ProductImage]) -> None:
    # One transaction; the unique (product, position) is DEFERRABLE INITIALLY DEFERRED, so intermediate
    # duplicates are fine until COMMIT.
    for position, image in enumerate(images):
        if image.position != position:
            ProductImage.objects.filter(pk=image.pk).update(position=position)
            image.position = position


def add_image(product_id: int, uploaded: Any, *, actor: Any, request: Any = None) -> ProductImage:
    """``POST products/{id}/images/``: re-encode, append last (≤ 8), audit ``product.image_added``."""
    processed = _process(uploaded, "file")
    with transaction.atomic():
        product = _locked_product(product_id)
        count = product.images.count()
        if count >= MAX_PRODUCT_IMAGES:
            raise ValidationError({"images": [field_error("max_items", "Ko‘pi bilan 8 ta rasm.")]})
        name = media_name("products", product.pk, processed.content, processed.extension)
        image = ProductImage(
            product=product,
            position=count,
            width=processed.width,
            height=processed.height,
            content_type=processed.content_type,
            size_bytes=len(processed.content),
            sha256=sha256(processed.content).hexdigest(),
            uploaded_by=actor,
        )
        image.file = ContentFile(processed.content, name=_basename(name))
        image.save()
        audit.record(
            action="product.image_added",
            entity_type="product",
            entity_id=str(product.pk),
            actor=actor,
            request=request,
            after={"image_id": image.pk, "position": image.position, "sha256": image.sha256},
        )
    return image


def delete_image(product_id: int, image_id: int, *, actor: Any, request: Any = None) -> None:
    """``DELETE products/{id}/images/{image_id}/``: 404 for another product's image, 409 for the last image
    of an active product; positions are compacted and the file removed after commit."""
    with transaction.atomic():
        product = _locked_product(product_id)
        images = _ordered_images(product)
        image = next((candidate for candidate in images if candidate.pk == image_id), None)
        if image is None:
            raise NotFound
        if product.status == enums.ProductStatus.ACTIVE and len(images) == 1:
            raise Conflict(LAST_IMAGE_OF_ACTIVE)
        remaining = [candidate for candidate in images if candidate.pk != image_id]
        position = image.position
        _delete_file_on_commit(image.file)
        image.delete()  # nulls ``image.pk``: the remaining list is computed above
        _compact_positions(remaining)
        audit.record(
            action="product.image_deleted",
            entity_type="product",
            entity_id=str(product.pk),
            actor=actor,
            request=request,
            before={"image_id": image_id, "position": position},
        )


def reorder_images(
    product_id: int, image_ids: Sequence[int], *, actor: Any, request: Any = None
) -> list[ProductImage]:
    """``PUT products/{id}/images/order/``: ``image_ids`` must be exactly the current set (``mismatch``)."""
    with transaction.atomic():
        product = _locked_product(product_id)
        images = _ordered_images(product)
        current = [image.pk for image in images]
        if sorted(image_ids) != sorted(current) or len(set(image_ids)) != len(image_ids):
            raise ValidationError({"image_ids": [field_error("mismatch")]})
        by_id = {image.pk: image for image in images}
        _compact_positions([by_id[image_id] for image_id in image_ids])
        audit.record(
            action="product.images_reordered",
            entity_type="product",
            entity_id=str(product.pk),
            actor=actor,
            request=request,
            before={"image_ids": current},
            after={"image_ids": list(image_ids)},
        )
        return _ordered_images(product)


# --- categories ---------------------------------------------------------------------------------------------


def _category_snapshot(category: Category) -> dict[str, Any]:
    return {
        "slug": category.slug,
        "name.uz": category.name_uz,
        "name.ru": category.name_ru,
        "name.en": category.name_en,
        "is_active": category.is_active,
        "position": category.position,
    }


def _next_position(model: type[Category] | type[Banner]) -> int:
    last = model.objects.order_by("-position").values_list("position", flat=True).first()
    return 0 if last is None else int(last) + 1


def create_category(
    slug: str, name: dict[str, str], *, is_active: bool = True, actor: Any, request: Any = None
) -> Category:
    with transaction.atomic():
        _advisory_lock(CATEGORY_LOCK)
        category = Category(
            slug=slug,
            name_uz=name["uz"],
            name_ru=name.get("ru", ""),
            name_en=name.get("en", ""),
            is_active=is_active,
            position=_next_position(Category),
        )
        _save_unique(category.save, CATEGORY_SLUG_CONSTRAINT, "slug")
        audit.record(
            action="category.created",
            entity_type="category",
            entity_id=category.slug,
            actor=actor,
            request=request,
            after=_category_snapshot(category),
        )
    return _reload_category(category.slug)


def update_category(
    category: Category,
    *,
    name: dict[str, str] | None = None,
    is_active: bool | None = None,
    actor: Any,
    request: Any = None,
) -> Category:
    with transaction.atomic():
        locked = Category.objects.select_for_update().get(pk=category.pk)
        before = _category_snapshot(locked)
        for language, value in (name or {}).items():
            setattr(locked, f"name_{language}", value)
        if is_active is not None:
            locked.is_active = is_active
        locked.save()
        diff_before, diff_after = _diff(before, _category_snapshot(locked))
        audit.record(
            action="category.updated",
            entity_type="category",
            entity_id=locked.slug,
            actor=actor,
            request=request,
            before=diff_before,
            after=diff_after,
        )
    return _reload_category(locked.slug)


def delete_category(category: Category, *, actor: Any, request: Any = None) -> None:
    """409 ``conflict`` while products reference it; banner links to it fall back to ``none``."""
    with transaction.atomic():
        _advisory_lock(CATEGORY_LOCK)
        locked = Category.objects.select_for_update().get(pk=category.pk)
        if locked.products.exists():
            raise Conflict(CATEGORY_HAS_PRODUCTS)
        Banner.objects.filter(link_category=locked).update(
            link_kind=enums.BannerLinkKind.NONE, link_category=None, updated_at=timeutils.now()
        )
        snapshot = _category_snapshot(locked)
        _delete_file_on_commit(locked.image)
        locked.delete()
        for position, remaining in enumerate(Category.objects.order_by("position", "id")):
            if remaining.position != position:
                Category.objects.filter(pk=remaining.pk).update(position=position)
        audit.record(
            action="category.deleted",
            entity_type="category",
            entity_id=snapshot["slug"],
            actor=actor,
            request=request,
            before=snapshot,
        )


def reorder_categories(slugs: Sequence[str], *, actor: Any, request: Any = None) -> list[Category]:
    with transaction.atomic():
        _advisory_lock(CATEGORY_LOCK)
        current = list(Category.objects.order_by("position", "id").values_list("slug", flat=True))
        if sorted(slugs) != sorted(current) or len(set(slugs)) != len(slugs):
            raise ValidationError({"slugs": [field_error("mismatch")]})
        for position, slug in enumerate(slugs):
            Category.objects.filter(slug=slug).update(position=position)
        audit.record(
            action="category.reordered",
            entity_type="category",
            entity_id="",
            actor=actor,
            request=request,
            before={"slugs": current},
            after={"slugs": list(slugs)},
        )
    return list(selectors.category_queryset())


def _reload_category(slug: str) -> Category:
    return selectors.category_queryset().get(slug=slug)


# --- banners ------------------------------------------------------------------------------------------------


@dataclass(slots=True)
class BannerInput:
    """Validated body of ``POST banners/`` / ``PATCH banners/{id}/`` (unset keys stay ``None``).

    ``link`` is already resolved: ``kind`` plus the target rows (the serializer checked they exist).
    """

    title: dict[str, str] | None = None
    link_kind: str | None = None
    link_product: Product | None = None
    link_category: Category | None = None
    is_active: bool | None = None
    starts_at: Any = None
    starts_at_set: bool = False
    ends_at: Any = None
    ends_at_set: bool = False


def _banner_snapshot(banner: Banner) -> dict[str, Any]:
    return {
        "title.uz": banner.title_uz,
        "title.ru": banner.title_ru,
        "title.en": banner.title_en,
        "image": banner.image.name,
        "link.kind": banner.link_kind,
        "link.product_id": banner.link_product_id,
        "link.category": banner.link_category.slug
        if banner.link_category_id and banner.link_category
        else None,
        "is_active": banner.is_active,
        "starts_at": banner.starts_at,
        "ends_at": banner.ends_at,
        "position": banner.position,
    }


def _apply_banner(banner: Banner, data: BannerInput) -> None:
    for language, value in (data.title or {}).items():
        setattr(banner, f"title_{language}", value)
    if data.link_kind is not None:
        banner.link_kind = data.link_kind
        banner.link_product = data.link_product if data.link_kind == enums.BannerLinkKind.PRODUCT else None
        banner.link_category = data.link_category if data.link_kind == enums.BannerLinkKind.CATEGORY else None
    if data.is_active is not None:
        banner.is_active = data.is_active
    if data.starts_at_set:
        banner.starts_at = data.starts_at
    if data.ends_at_set:
        banner.ends_at = data.ends_at


def _store_banner_image(banner: Banner, processed: ProcessedImage) -> None:
    previous = banner.image.name
    name = media_name("banners", banner.pk, processed.content, processed.extension)
    banner.image_width = processed.width
    banner.image_height = processed.height
    # Django 6.1 derives the dimension fields from the *content* object: it needs a name to count as a file.
    banner.image.save(_basename(name), ContentFile(processed.content, name=_basename(name)), save=True)
    if previous and previous != banner.image.name:
        storage = banner.image.storage
        transaction.on_commit(lambda: storage.delete(previous))


def create_banner(data: BannerInput, image: Any, *, actor: Any, request: Any = None) -> Banner:
    """``POST banners/``: two steps in one transaction — insert without a file, then store it under the id."""
    processed = _process(image, "image")
    with transaction.atomic():
        _advisory_lock(BANNER_LOCK)
        banner = Banner(
            position=_next_position(Banner), image_width=processed.width, image_height=processed.height
        )
        _apply_banner(banner, data)
        banner.save()
        _store_banner_image(banner, processed)
        audit.record(
            action="banner.created",
            entity_type="banner",
            entity_id=str(banner.pk),
            actor=actor,
            request=request,
            after=_banner_snapshot(banner),
        )
    return _reload_banner(banner.pk)


def update_banner(
    banner: Banner, data: BannerInput, image: Any = None, *, actor: Any, request: Any = None
) -> Banner:
    processed = _process(image, "image") if image is not None else None
    with transaction.atomic():
        locked = (
            Banner.objects.select_for_update(of=("self",)).select_related("link_category").get(pk=banner.pk)
        )
        before = _banner_snapshot(locked)
        _apply_banner(locked, data)
        locked.save()
        if processed is not None:
            _store_banner_image(locked, processed)
        diff_before, diff_after = _diff(before, _banner_snapshot(locked))
        audit.record(
            action="banner.updated",
            entity_type="banner",
            entity_id=str(locked.pk),
            actor=actor,
            request=request,
            before=diff_before,
            after=diff_after,
        )
    return _reload_banner(locked.pk)


def delete_banner(banner: Banner, *, actor: Any, request: Any = None) -> None:
    with transaction.atomic():
        _advisory_lock(BANNER_LOCK)
        locked = (
            Banner.objects.select_for_update(of=("self",)).select_related("link_category").get(pk=banner.pk)
        )
        snapshot = _banner_snapshot(locked)
        _delete_file_on_commit(locked.image)
        locked.delete()
        for position, remaining in enumerate(Banner.objects.order_by("position", "id")):
            if remaining.position != position:
                Banner.objects.filter(pk=remaining.pk).update(position=position)
        audit.record(
            action="banner.deleted",
            entity_type="banner",
            entity_id=str(banner.pk),
            actor=actor,
            request=request,
            before=snapshot,
        )


def reorder_banners(ids: Sequence[int], *, actor: Any, request: Any = None) -> list[Banner]:
    with transaction.atomic():
        _advisory_lock(BANNER_LOCK)
        current = list(Banner.objects.order_by("position", "id").values_list("id", flat=True))
        if sorted(ids) != sorted(current) or len(set(ids)) != len(ids):
            raise ValidationError({"ids": [field_error("mismatch")]})
        for position, banner_id in enumerate(ids):
            Banner.objects.filter(pk=banner_id).update(position=position)
        audit.record(
            action="banner.reordered",
            entity_type="banner",
            entity_id="",
            actor=actor,
            request=request,
            before={"ids": current},
            after={"ids": list(ids)},
        )
    return list(selectors.banner_queryset())


def _reload_banner(banner_id: int) -> Banner:
    return selectors.banner_queryset().get(pk=banner_id)


# --- Billz-owned data ---------------------------------------------------------------------------------------


def sync_stock(product: Product, quantity: int, synced_at: Any) -> bool:
    """Write the Billz stock of ``product``; returns whether the quantity changed (row always touched).

    The last layer of the fake-Billz guard: refused while Billz is fake under the production settings.
    """
    require_live_billz()
    stock, created = ProductStock.objects.get_or_create(
        product=product, defaults={"quantity": quantity, "synced_at": synced_at}
    )
    if created:
        return True
    changed = stock.quantity != quantity
    ProductStock.objects.filter(pk=stock.pk).update(quantity=quantity, synced_at=synced_at)
    return changed


def upsert_from_billz(row: BillzProduct) -> UpsertOutcome:
    """The import's upsert by ``billz_id`` (contract §2.3).

    New → ``hidden`` with ``is_new_from_billz=True``, ``price = billz price or 0``, empty texts, no images.
    Existing → only ``billz_name``, ``volume``, ``billz_price``, ``billz_synced_at`` and the stock row change
    (``volume`` only when Billz asserts one: ``row.volume is None`` leaves it alone); texts, images, price,
    status, promo, ``last_change`` and ``updated_at`` are never touched.
    """
    require_live_billz()
    with transaction.atomic():
        product = Product.objects.select_for_update(of=("self",)).filter(billz_id=row.billz_id).first()
        if product is None:
            product = Product(price=row.price or 0, status=enums.ProductStatus.HIDDEN, is_new_from_billz=True)
            _apply_billz(product, row)
            product.save()
            ProductStock.objects.create(product=product, quantity=row.stock, synced_at=row.synced_at)
            return "created"
        changed = (
            product.billz_name != row.billz_name
            or (row.volume is not None and product.volume != row.volume)
            or product.billz_price != row.price
        )
        fields: dict[str, Any] = {
            "billz_name": row.billz_name,
            "billz_price": row.price,
            "billz_synced_at": row.synced_at,
        }
        if row.volume is not None:  # None = not asserted (no BILLZ_VOLUME_FIELD): never blank a stored volume
            fields["volume"] = row.volume
        Product.objects.filter(pk=product.pk).update(**fields)
        stock_changed = sync_stock(product, row.stock, row.synced_at)
        return "updated" if changed or stock_changed else "unchanged"


# Audit reason of a product hidden by the reconciliation (docs/integrations/billz.md "Full sync").
BILLZ_MISSING_REASON = "billz_missing: Billz no longer lists this product (daily full sync)"


# A full sync may always hide this many products, whatever the share (a few discontinued items a day).
HIDE_ALWAYS_ALLOWED = 3


class TooManyMissing(BillzListingIncomplete):
    """Billz no longer lists an implausibly large share of the active catalog: nothing is hidden."""


def hide_missing_from_billz(listed: Collection[str], *, min_ratio: float = 1.0) -> list[int]:
    """The full sync's reconciliation: every **active** product whose ``billz_id`` is not in ``listed`` (the
    complete Billz listing) becomes ``hidden``; returns their ids. Products are never deleted, so a product
    Billz lists again keeps its texts, images and history and staff re-activate it.

    Second plausibility guard (the first is the adapter's, on Billz's own count): when more than
    ``max(HIDE_ALWAYS_ALLOWED, (1 − min_ratio) × active products)`` would be hidden — e.g. the catalog's
    Billz ids are not the barcodes Billz lists, or the wrong shop is configured — :class:`TooManyMissing` is
    raised and nothing is hidden.

    Hiding is an admin-owned change made by the system: ``last_change`` = ``hidden`` without an actor and the
    ``product.updated`` audit row (``status`` diff, actor ``system``) with :data:`BILLZ_MISSING_REASON`. Each
    product is hidden in its own transaction under its row lock (an admin edit in between wins: the row is
    re-read). The caller guarantees ``listed`` is complete (``BillzPort.iter_products("full")``).
    """
    require_live_billz()
    wanted = frozenset(listed)
    hidden: list[int] = []
    active = Product.objects.filter(status=enums.ProductStatus.ACTIVE)
    candidates = list(active.exclude(billz_id__in=wanted).order_by("id").values_list("pk", flat=True))
    total = active.count()
    allowed = max(HIDE_ALWAYS_ALLOWED, total - math.ceil(round(total * min_ratio, 6)))
    if len(candidates) > allowed:
        raise TooManyMissing(
            f"Billz no longer lists {len(candidates)} active products; refusing to hide more than {allowed} "
            "in one run"
        )
    for product_id in candidates:
        with transaction.atomic():
            product = (
                Product.objects.select_for_update(of=("self",))
                .select_related("category")
                .filter(pk=product_id, status=enums.ProductStatus.ACTIVE)
                .first()
            )
            if product is None or product.billz_id in wanted:
                continue
            before = product_snapshot(product)
            product.status = enums.ProductStatus.HIDDEN
            _set_last_change(product, enums.ProductChangeKind.HIDDEN, None, before)
            product.save()
            diff_before, diff_after = _diff(before, product_snapshot(product))
            audit.record(
                action="product.updated",
                entity_type="product",
                entity_id=str(product.pk),
                before=diff_before,
                after=diff_after,
                reason=BILLZ_MISSING_REASON,
            )
            hidden.append(product.pk)
    if hidden:
        logger.warning("Billz full sync hid %d products Billz no longer lists: %s", len(hidden), hidden)
    return hidden


def import_products(rows: Iterable[BillzProduct]) -> dict[str, int]:
    """Upsert every row; returns the contract §2.14 result counters (used by tests and the task)."""
    counters = {"created": 0, "updated": 0, "unchanged": 0, "new_from_billz": 0}
    for row in rows:
        outcome = upsert_from_billz(row)
        counters[outcome] += 1
        if outcome == "created":
            counters["new_from_billz"] += 1
    return counters
