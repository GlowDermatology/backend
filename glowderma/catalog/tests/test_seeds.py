"""catalog.seed (design 03 §7.5): the frontend's 248 products byte for byte, idempotent, keyed by billz_id."""

from datetime import datetime, timedelta

import pytest
from django.core.management import call_command

from glowderma.accounts.tests.factories import StaffUserFactory
from glowderma.catalog import seeds
from glowderma.catalog.models import Category, Product, ProductImage, ProductStock
from glowderma.catalog.seeds import MockRandom, build_catalog, seed_catalog
from glowderma.core import enums, seeding
from glowderma.core.seeding import SeedContext
from glowderma.core.timeutils import TASHKENT

ANCHOR = datetime.fromisoformat(seeding.DEFAULT_ANCHOR)
CONTEXT = SeedContext(anchor=ANCHOR, keep_open=True)


def test_mock_random_matches_the_frontend_prng() -> None:
    """First values of ``createRandom(248)`` as printed by node (``random.ts``)."""
    random = MockRandom(248)
    assert [round(random.next(), 6) for _ in range(5)] == [0.925356, 0.713616, 0.181883, 0.928428, 0.139227]
    random = MockRandom(7)
    assert random.int(1, 6) in range(1, 7)
    assert sorted(random.shuffle([1, 2, 3, 4])) == [1, 2, 3, 4]
    assert random.pick(["a"]) == "a"
    assert random.chance(1.0) and not random.chance(0.0)


def test_build_catalog_reproduces_the_frontend_fixture() -> None:
    """Vectors read off ``PRODUCTS`` from ``frontend/src/data/mock/catalog.ts`` (node dump)."""
    specs = build_catalog()
    assert len(specs) == 248
    assert [s.billz_id for s in specs[:8]] == [
        "4607012301",
        "4607012401",
        "4607012388",
        "4607012345",
        "4607012422",
        "4607012399",
        "4607012455",
        "4607012366",
    ]
    assert specs[8].billz_id == "4607012410" and specs[17].billz_id == "4607012419"
    first_generated = specs[18]
    assert (first_generated.billz_id, first_generated.billz_name) == ("4607012500", "ALOE VERA SERUM 30ML")
    assert (first_generated.price, first_generated.stock, first_generated.category) == (166_000, 36, "serums")
    assert (
        first_generated.uz.name == "Aloe verali serum" and first_generated.ru.name == "Сыворотка с алоэ вера"
    )
    assert first_generated.en.name == "" and first_generated.art == ("dropper", "default")
    assert specs[-1].billz_id == "4607012729"
    assert sum(1 for s in specs if s.stock == 0) == 7
    new = [i + 1 for i, s in enumerate(specs) if s.is_new_from_billz]
    assert new == [7, 24, 107, 179]
    assert all(specs[i - 1].status == "hidden" and specs[i - 1].art is None for i in new)
    assert len({s.billz_id for s in specs}) == 248 and len({s.uz.name for s in specs if s.uz.name}) == 244


@pytest.mark.django_db
def test_seed_catalog_is_idempotent_and_matches_the_design() -> None:
    admin = StaffUserFactory.create(email=seeds.SEED_ADMIN_EMAIL, full_name="Administrator")
    seed_catalog(CONTEXT)
    counts = (Product.objects.count(), Category.objects.count(), ProductImage.objects.count())
    assert counts == (248, 5, 246)  # 244 single images + two extra for product 1
    assert ProductStock.objects.count() == 248
    seed_catalog(CONTEXT)
    assert counts == (Product.objects.count(), Category.objects.count(), ProductImage.objects.count())

    assert list(Category.objects.values_list("slug", "name_uz", "position")) == [
        ("serums", "Serumlar", 0),
        ("cleansing", "Tozalash", 1),
        ("moisturizing", "Namlantirish", 2),
        ("sun", "Quyoshdan himoya", 3),
        ("toners", "Tonerlar", 4),
    ]
    serum = Product.objects.select_related("category", "stock").get(pk=1)
    assert (serum.billz_id, serum.price, serum.billz_price, serum.stock.quantity) == (
        "4607012301",
        185_000,
        185_000,
        12,
    )
    assert serum.category is not None and serum.category.slug == "serums"
    assert serum.name_uz == "Gialuron kislotali serum" and serum.name_en == "Hyaluronic acid serum"
    assert serum.last_change_at == datetime(2026, 10, 22, 18, 4, tzinfo=TASHKENT)
    assert serum.last_change_kind == enums.ProductChangeKind.PRICE_CHANGED
    assert (serum.last_change_old_price, serum.last_change_new_price) == (175_000, 185_000)
    assert serum.last_change_actor == admin and serum.updated_at == serum.last_change_at
    assert serum.billz_synced_at == ANCHOR - timedelta(minutes=2) == serum.stock.synced_at
    assert [img.position for img in serum.images.all()] == [0, 1, 2]
    assert all(img.file.storage.exists(str(img.file.name)) for img in serum.images.all())
    assert serum.images.first().file.name.startswith("products/1/")  # type: ignore[union-attr]

    spf = Product.objects.get(pk=2)
    assert spf.promo and spf.status == enums.ProductStatus.ACTIVE and spf.category.slug == "sun"  # type: ignore[union-attr]
    vit_c = Product.objects.get(pk=7)
    assert vit_c.status == enums.ProductStatus.HIDDEN and vit_c.is_new_from_billz and vit_c.category is None
    assert vit_c.name_uz == "" and not vit_c.images.exists() and vit_c.last_change_at is None
    assert Product.objects.filter(is_new_from_billz=True).count() == 4
    assert ProductStock.objects.filter(quantity__lte=0).count() == 7
    assert Product.objects.filter(stock__quantity__lte=0, pk=5).exists()
    assert Product.objects.filter(stock__quantity__lte=0, pk__gt=18).count() == 6
    assert Product.objects.get(pk=248).billz_id == "4607012729"
    # Creation instants decrease with the id: ordering=default shows 1 (edited), 2, 3, … 248.
    created = list(Product.objects.order_by("id").values_list("created_at", flat=True))
    assert created == sorted(created, reverse=True) and created[-1] == datetime(
        2025, 3, 1, 9, 0, tzinfo=TASHKENT
    )
    generated = Product.objects.get(pk=19)
    generated.refresh_from_db()
    assert generated.search_text == "aloe verali serum сыворотка с алоэ вера  aloe vera serum 30ml 4607012500"


@pytest.mark.django_db
def test_seed_catalog_without_the_admin_leaves_the_actor_null() -> None:
    seed_catalog(CONTEXT)
    serum = Product.objects.get(pk=1)
    assert serum.last_change_actor is None and serum.last_change_kind == enums.ProductChangeKind.PRICE_CHANGED


@pytest.mark.django_db
def test_seed_demo_command_runs_the_catalog_seeder() -> None:
    call_command("seed_demo", force=True, verbosity=0)
    assert Product.objects.count() == 248
    # Sequences were re-synced after the explicit ids: the next product continues after 248.
    product = Product.objects.create(billz_id="4607019999")
    assert product.pk == 249
