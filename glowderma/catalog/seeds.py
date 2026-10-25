"""Demo catalog (design 03 §7.5): five categories and the 248 products of the frontend mock, byte for byte.

Products 1–8 are the rows drawn on the products screen, 9–18 the products the sample orders reference, 19–248
the generated ones (``frontend/src/data/mock/catalog.ts`` ``buildCatalog()``), reproduced with a Python port
of the frontend's ``createRandom`` (mulberry32) so names, prices, stock and flags equal the frontend's
fixture. Idempotent by natural keys: category ``slug``, product ``billz_id`` (a product that exists is
skipped entirely, images included). Design rows take their mock ids explicitly; ``seed_demo`` re-syncs the
sequences afterwards.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from hashlib import sha256
from io import BytesIO
from typing import TypeVar

from django.core.files.base import ContentFile
from django.db import transaction
from PIL import Image, ImageDraw

from glowderma.accounts.models import StaffUser
from glowderma.catalog.models import Category, Product, ProductImage, ProductStock
from glowderma.core import enums
from glowderma.core.images import process_image
from glowderma.core.media import media_name
from glowderma.core.seeding import SeedContext, register_seeder
from glowderma.core.timeutils import TASHKENT

T = TypeVar("T")

SEED_ADMIN_EMAIL = "admin@glowderma.uz"
TOTAL_PRODUCTS = 248
_MASK = 0xFFFFFFFF


class MockRandom:
    """Port of the frontend's ``createRandom`` (mulberry32 with JavaScript 32-bit semantics).

    ``Math.imul`` is a 32-bit multiply, ``>>>`` a logical shift and ``|``/``^`` operate on the 32-bit pattern,
    so every intermediate is masked to 32 bits; the final division by 2**32 is exact in IEEE doubles on both
    sides, which makes ``int``/``pick``/``chance``/``shuffle`` return the same values as the TypeScript.
    """

    def __init__(self, seed: int) -> None:
        self.state = seed & _MASK

    def next(self) -> float:
        self.state = (self.state + 0x6D2B79F5) & _MASK
        t = self.state
        t = ((t ^ (t >> 15)) * (t | 1)) & _MASK
        t ^= (t + (((t ^ (t >> 7)) * (t | 61)) & _MASK)) & _MASK
        return ((t ^ (t >> 14)) & _MASK) / 4294967296

    def int(self, low: int, high: int) -> int:
        return low + math.floor(self.next() * (high - low + 1))

    def chance(self, probability: float) -> bool:
        return self.next() < probability

    def pick(self, items: Sequence[T]) -> T:
        return items[math.floor(self.next() * len(items))]

    def shuffle(self, items: Sequence[T]) -> list[T]:
        copy = list(items)
        for i in range(len(copy) - 1, 0, -1):
            j = math.floor(self.next() * (i + 1))
            copy[i], copy[j] = copy[j], copy[i]
        return copy


# --- categories (03 §7.5; uz labels = CATEGORY_LABEL in the frontend, ru/en invented) -----------------------

CATEGORIES: tuple[tuple[str, str, str, str], ...] = (
    ("serums", "Serumlar", "Сыворотки", "Serums"),
    ("cleansing", "Tozalash", "Очищение", "Cleansing"),
    ("moisturizing", "Namlantirish", "Увлажнение", "Moisturising"),
    ("sun", "Quyoshdan himoya", "Защита от солнца", "Sun care"),
    ("toners", "Tonerlar", "Тонеры", "Toners"),
)

# --- products -----------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Texts:
    name: str = ""
    description: str = ""
    ingredients: str = ""
    usage: str = ""


@dataclass(slots=True)
class ProductSpec:
    billz_id: str
    billz_name: str
    volume: str
    category: str | None
    price: int
    stock: int
    art: tuple[str, str] | None  # (kind, tone) or None → no image
    uz: Texts = field(default_factory=Texts)
    ru: Texts = field(default_factory=Texts)
    en: Texts = field(default_factory=Texts)
    promo: bool = False
    status: str = enums.ProductStatus.ACTIVE
    is_new_from_billz: bool = False
    image_kinds: tuple[str, ...] = ()  # extra gallery (product 1); default = one image of ``art``


HYALURON_INCI = (
    "Aqua, Sodium Hyaluronate, Hydrolyzed Hyaluronic Acid, Sodium Acetylated Hyaluronate, Panthenol, "
    "Glycerin, Allantoin, Betaine, 1,2-Hexanediol."
)

DESIGN_PRODUCTS: tuple[ProductSpec, ...] = (
    ProductSpec(
        "4607012301",
        "HYALURONIC SERUM 30ML",
        "30 ml",
        "serums",
        185_000,
        12,
        ("dropper", "default"),
        image_kinds=("dropper", "box", "texture"),
        uz=Texts(
            "Gialuron kislotali serum",
            "Gialuron kislotasining uch xil molekulasi terining turli qatlamlarini namlaydi. Yengil tekstura "
            "tez so‘riladi va yopishqoqlik qoldirmaydi.",
            HYALURON_INCI,
            "Tozalangan yuzga 2–3 tomchi surting va barmoq uchlari bilan yengil singdiring. So‘ngra "
            "namlantiruvchi krem qo‘llang.",
        ),
        ru=Texts(
            "Сыворотка с гиалуроновой кислотой",
            "Три вида молекул гиалуроновой кислоты увлажняют разные слои кожи. Лёгкая текстура быстро "
            "впитывается и не оставляет липкости.",
            HYALURON_INCI,
            "Нанесите 2–3 капли на очищенную кожу и мягко распределите кончиками пальцев. Затем нанесите "
            "увлажняющий крем.",
        ),
        en=Texts("Hyaluronic acid serum", "", HYALURON_INCI, ""),
    ),
    ProductSpec(
        "4607012401",
        "SUN CREAM SPF50 50ML",
        "50 ml",
        "sun",
        240_000,
        8,
        ("tube", "default"),
        promo=True,
        uz=Texts("Quyoshdan himoya kremi SPF 50"),
        ru=Texts("Солнцезащитный крем SPF 50"),
        en=Texts("Sunscreen SPF 50"),
    ),
    ProductSpec(
        "4607012388",
        "GENTLE FOAM 150ML",
        "150 ml",
        "cleansing",
        128_000,
        3,
        ("pump", "default"),
        uz=Texts("Yumshoq tozalovchi ko‘pik"),
        ru=Texts("Мягкая очищающая пенка"),
        en=Texts("Gentle cleansing foam"),
    ),
    ProductSpec(
        "4607012345",
        "CERAMIDE CREAM 50ML",
        "50 ml",
        "moisturizing",
        312_000,
        5,
        ("jar", "default"),
        uz=Texts("Seramidli namlantiruvchi krem"),
        ru=Texts("Увлажняющий крем с керамидами"),
        en=Texts("Ceramide moisturising cream"),
    ),
    ProductSpec(
        "4607012422",
        "NIACINAMIDE TONER 200ML",
        "200 ml",
        "toners",
        156_000,
        0,
        ("toner", "default"),
        uz=Texts("Niatsinamidli toner"),
        ru=Texts("Тонер с ниацинамидом"),
        en=Texts("Niacinamide toner"),
    ),
    ProductSpec(
        "4607012399",
        "RETINOL NIGHT SERUM 30ML",
        "30 ml",
        "serums",
        276_000,
        6,
        ("dropper", "night"),
        uz=Texts("Retinolli tungi serum"),
        ru=Texts("Ночная сыворотка с ретинолом"),
        en=Texts("Retinol night serum"),
    ),
    ProductSpec(
        "4607012455",
        "VIT C SERUM 20ML",
        "20 ml",
        None,
        230_000,
        14,
        None,
        status=enums.ProductStatus.HIDDEN,
        is_new_from_billz=True,
    ),
    ProductSpec(
        "4607012366",
        "AZELAIC CREAM 40ML",
        "40 ml",
        "moisturizing",
        198_000,
        9,
        ("tube", "dark-cap"),
        status=enums.ProductStatus.HIDDEN,
        uz=Texts("Azelain kislotali krem"),
        ru=Texts("Крем с азелаиновой кислотой"),
        en=Texts("Azelaic acid cream"),
    ),
)

# billz_id, billz_name, volume, category, price, stock, art kind, uz, ru, en
_SUPPORTING: tuple[tuple[str, str, str, str, int, int, str, str, str, str], ...] = (
    (
        "4607012410",
        "PANTHENOL BALM 50ML",
        "50 ml",
        "moisturizing",
        96_000,
        21,
        "tube",
        "Pantenolli tiklovchi balzam",
        "Восстанавливающий бальзам с пантенолом",
        "Panthenol repair balm",
    ),
    (
        "4607012411",
        "SALICYLIC GEL 200ML",
        "200 ml",
        "cleansing",
        142_000,
        17,
        "pump",
        "Salitsil kislotali tozalovchi gel",
        "Очищающий гель с салициловой кислотой",
        "Salicylic acid cleansing gel",
    ),
    (
        "4607012412",
        "CENTELLA CREAM 50ML",
        "50 ml",
        "moisturizing",
        214_000,
        11,
        "jar",
        "Sentella ekstraktli krem",
        "Крем с экстрактом центеллы",
        "Centella cream",
    ),
    (
        "4607012413",
        "MICELLAR WATER 400ML",
        "400 ml",
        "cleansing",
        89_000,
        34,
        "toner",
        "Mitsellyar suv",
        "Мицеллярная вода",
        "Micellar water",
    ),
    (
        "4607012414",
        "PEPTIDE EYE CREAM 20ML",
        "20 ml",
        "moisturizing",
        168_000,
        7,
        "tube",
        "Peptidli ko‘z atrofi kremi",
        "Крем для кожи вокруг глаз с пептидами",
        "Peptide eye cream",
    ),
    (
        "4607012415",
        "PEPTIDE SERUM 30ML",
        "30 ml",
        "serums",
        298_000,
        10,
        "dropper",
        "Peptidli serum",
        "Сыворотка с пептидами",
        "Peptide serum",
    ),
    (
        "4607012416",
        "HYALURON SHEET MASK",
        "1 dona",
        "moisturizing",
        45_000,
        120,
        "jar",
        "Gialuron kislotali niqob",
        "Маска с гиалуроновой кислотой",
        "Hyaluronic acid sheet mask",
    ),
    (
        "4607012417",
        "PANTHENOL LIP BALM",
        "10 ml",
        "moisturizing",
        39_000,
        58,
        "tube",
        "Pantenolli lab balzami",
        "Бальзам для губ с пантенолом",
        "Panthenol lip balm",
    ),
    (
        "4607012418",
        "SPF30 FLUID 40ML",
        "40 ml",
        "sun",
        205_000,
        13,
        "tube",
        "Quyoshdan himoya fluidi SPF 30",
        "Солнцезащитный флюид SPF 30",
        "Sun fluid SPF 30",
    ),
    (
        "4607012419",
        "ENZYME POWDER 50G",
        "50 g",
        "cleansing",
        174_000,
        9,
        "jar",
        "Fermentli tozalovchi pudra",
        "Энзимная пудра для умывания",
        "Enzyme cleansing powder",
    ),
)

SUPPORTING_PRODUCTS: tuple[ProductSpec, ...] = tuple(
    ProductSpec(
        billz_id,
        billz_name,
        volume,
        category,
        price,
        stock,
        (kind, "default"),
        Texts(uz),
        Texts(ru),
        Texts(en),
    )
    for billz_id, billz_name, volume, category, price, stock, kind, uz, ru, en in _SUPPORTING
)

INGREDIENTS: tuple[tuple[str, str, str], ...] = (
    ("Gialuron kislotali", "с гиалуроновой кислотой", "Hyaluronic acid"),
    ("Niatsinamidli", "с ниацинамидом", "Niacinamide"),
    ("Seramidli", "с керамидами", "Ceramide"),
    ("Retinolli", "с ретинолом", "Retinol"),
    ("Peptidli", "с пептидами", "Peptide"),
    ("Pantenolli", "с пантенолом", "Panthenol"),
    ("Sentella ekstraktli", "с центеллой", "Centella"),
    ("Salitsil kislotali", "с салициловой кислотой", "Salicylic acid"),
    ("Kollagenli", "с коллагеном", "Collagen"),
    ("Skvalanli", "со скваланом", "Squalane"),
    ("Aloe verali", "с алоэ вера", "Aloe vera"),
    ("Glikol kislotali", "с гликолевой кислотой", "Glycolic acid"),
    ("C vitaminli", "с витамином C", "Vitamin C"),
    ("Choy daraxti moyli", "с маслом чайного дерева", "Tea tree"),
    ("Probiotikli", "с пробиотиками", "Probiotic"),
    ("Azelain kislotali", "с азелаиновой кислотой", "Azelaic acid"),
    ("Fermentli", "с энзимами", "Enzyme"),
    ("Ko‘mirli", "с углём", "Charcoal"),
    ("Arbutinli", "с арбутином", "Arbutin"),
    ("Bakuchiolli", "с бакучиолом", "Bakuchiol"),
)


@dataclass(frozen=True, slots=True)
class Form:
    uz: str
    ru: str
    en: str
    category: str
    art: str
    volumes: tuple[int, ...]
    price: tuple[int, int]


FORMS: tuple[Form, ...] = (
    Form("serum", "Сыворотка", "serum", "serums", "dropper", (15, 20, 30, 50), (150, 360)),
    Form("krem", "Крем", "cream", "moisturizing", "jar", (30, 50, 75), (140, 340)),
    Form("tungi krem", "Ночной крем", "night cream", "moisturizing", "jar", (50,), (180, 380)),
    Form("toner", "Тонер", "toner", "toners", "toner", (100, 150, 200), (110, 220)),
    Form("essensiya", "Эссенция", "essence", "toners", "toner", (100, 150), (150, 280)),
    Form("tozalovchi gel", "Очищающий гель", "cleansing gel", "cleansing", "pump", (150, 200), (90, 190)),
    Form(
        "tozalovchi ko‘pik", "Очищающая пенка", "cleansing foam", "cleansing", "pump", (120, 150), (90, 180)
    ),
    Form("mitsellyar suv", "Мицеллярная вода", "micellar water", "cleansing", "toner", (200, 400), (70, 150)),
    Form("quyoshdan himoya kremi", "Солнцезащитный крем", "sunscreen", "sun", "tube", (50,), (170, 290)),
    Form("ko‘z atrofi kremi", "Крем для век", "eye cream", "moisturizing", "tube", (15, 20), (140, 260)),
    Form("niqob", "Маска", "mask", "moisturizing", "jar", (50, 75, 100), (60, 190)),
    Form("balzam", "Бальзам", "balm", "moisturizing", "tube", (30, 50), (60, 150)),
    Form("SPF 30 fluid", "Флюид SPF 30", "SPF 30 fluid", "sun", "tube", (40, 50), (160, 260)),
)

GENERATED_SOLD_OUT = (11, 37, 64, 102, 151, 199)  # indexes into the generated list → stock 0
GENERATED_NEW_FROM_BILLZ = (5, 88, 160)  # → hidden, new from Billz, no category/texts/images


def generate_products(count: int, taken: set[str]) -> list[ProductSpec]:
    """``catalog.ts generateProducts()``: the same PRNG calls in the same order."""
    random = MockRandom(248)
    combos = random.shuffle([(ingredient, form) for ingredient in INGREDIENTS for form in FORMS])
    specs: list[ProductSpec] = []
    billz = 4607012500
    for ingredient, form in combos:
        if len(specs) == count:
            break
        uz = f"{ingredient[0]} {form.uz}"
        if uz in taken:
            continue
        taken.add(uz)
        volume = random.pick(form.volumes)
        fully_translated = random.chance(0.55)
        price = random.int(form.price[0], form.price[1]) * 1000
        stock = random.int(1, 40)
        promo = random.chance(0.08)
        status = enums.ProductStatus.HIDDEN if random.chance(0.05) else enums.ProductStatus.ACTIVE
        tone = "dark-cap" if random.chance(0.15) else "default"
        specs.append(
            ProductSpec(
                str(billz),
                f"{ingredient[2]} {form.en} {volume}ML".upper(),
                f"{volume} ml",
                form.category,
                price,
                stock,
                (form.art, tone),
                uz=Texts(
                    uz,
                    f"{uz} — kundalik parvarish uchun.",
                    "Aqua, Glycerin, Butylene Glycol, Panthenol, Allantoin.",
                    "Tozalangan teriga kuniga 1–2 marta qo‘llang.",
                ),
                ru=Texts(f"{form.ru} {ingredient[1]}"),
                en=Texts(f"{ingredient[2]} {form.en}") if fully_translated else Texts(),
                promo=promo,
                status=status,
            )
        )
        billz += 1
    return specs


def build_catalog() -> list[ProductSpec]:
    """The 248 product specs in id order (``catalog.ts buildCatalog()``)."""
    fixed = [*DESIGN_PRODUCTS, *SUPPORTING_PRODUCTS]
    taken = {spec.uz.name for spec in fixed if spec.uz.name}
    generated = generate_products(TOTAL_PRODUCTS - len(fixed), taken)
    for index in GENERATED_SOLD_OUT:
        generated[index].stock = 0
    for index in GENERATED_NEW_FROM_BILLZ:
        spec = generated[index]
        spec.status = enums.ProductStatus.HIDDEN
        spec.is_new_from_billz = True
        spec.category = None
        spec.art = None
        spec.uz = spec.ru = spec.en = Texts()
    return [*fixed, *generated]


# --- placeholder art ----------------------------------------------------------------------------------------

_ART_COLOURS: dict[str, tuple[int, int, int]] = {
    "dropper": (214, 178, 196),
    "box": (226, 206, 186),
    "texture": (239, 226, 220),
    "tube": (178, 206, 214),
    "pump": (188, 214, 196),
    "jar": (226, 196, 176),
    "toner": (196, 206, 226),
}
_TONE_COLOURS: dict[str, tuple[int, int, int]] = {
    "default": (255, 255, 255),
    "night": (52, 48, 86),
    "dark-cap": (48, 48, 52),
}
PLACEHOLDER_SIDE = 96


def placeholder_art(kind: str, tone: str = "default") -> bytes:
    """A small WebP per product-art kind/tone (the design's ``ctx.media_placeholder``)."""
    background = _ART_COLOURS.get(kind, (220, 220, 220))
    accent = _TONE_COLOURS.get(tone, _TONE_COLOURS["default"])
    image = Image.new("RGB", (PLACEHOLDER_SIDE, PLACEHOLDER_SIDE), background)
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((28, 20, 68, 84), radius=10, fill=accent)
    draw.rectangle((36, 10, 60, 22), fill=accent)
    output = BytesIO()
    image.save(output, format="WEBP", quality=80, method=4)
    return output.getvalue()


# --- seeding ------------------------------------------------------------------------------------------------


def product_created_at(product_id: int) -> datetime:
    """Deterministic creation instants decreasing with the id, so ``ordering=default`` lists 1…248 in order
    (product 1 leads through its ``last_change_at``); every product predates the first seeded order."""
    return datetime(2025, 3, 1, 9, 0, tzinfo=TASHKENT) + timedelta(hours=TOTAL_PRODUCTS - product_id)


def _seed_categories() -> dict[str, Category]:
    categories: dict[str, Category] = {}
    for position, (slug, uz, ru, en) in enumerate(CATEGORIES):
        category, _ = Category.objects.get_or_create(
            slug=slug, defaults={"name_uz": uz, "name_ru": ru, "name_en": en, "position": position}
        )
        categories[slug] = category
    return categories


def _add_image(product: Product, position: int, kind: str, tone: str, at: datetime) -> ProductImage:
    processed = process_image(placeholder_art(kind, tone))
    digest = sha256(processed.content).hexdigest()
    name = media_name("products", product.pk, processed.content, processed.extension).rsplit("/", 1)[-1]
    image = ProductImage(
        product=product,
        position=position,
        content_type=processed.content_type,
        size_bytes=len(processed.content),
        sha256=digest,
        width=processed.width,
        height=processed.height,
        created_at=at,
    )
    image.file = ContentFile(processed.content, name=name)
    image.save()
    ProductImage.objects.filter(pk=image.pk).update(updated_at=at)
    return image


def _seed_product(
    product_id: int,
    spec: ProductSpec,
    categories: dict[str, Category],
    synced_at: datetime,
    admin: StaffUser | None,
) -> None:
    created_at = product_created_at(product_id)
    product = Product(
        id=product_id,
        billz_id=spec.billz_id,
        billz_name=spec.billz_name,
        volume=spec.volume,
        billz_price=spec.price,
        billz_synced_at=synced_at,
        price=spec.price,
        category=categories[spec.category] if spec.category else None,
        promo=spec.promo,
        status=spec.status,
        is_new_from_billz=spec.is_new_from_billz,
        name_uz=spec.uz.name,
        name_ru=spec.ru.name,
        name_en=spec.en.name,
        description_uz=spec.uz.description,
        description_ru=spec.ru.description,
        description_en=spec.en.description,
        ingredients_uz=spec.uz.ingredients,
        ingredients_ru=spec.ru.ingredients,
        ingredients_en=spec.en.ingredients,
        usage_uz=spec.uz.usage,
        usage_ru=spec.ru.usage,
        usage_en=spec.en.usage,
        created_at=created_at,
    )
    updated_at = created_at
    if product_id == 1:
        # Design: "narx 175 000 → 185 000" by Administrator on 2026-10-22 18:04.
        updated_at = datetime(2026, 10, 22, 18, 4, tzinfo=TASHKENT)
        product.last_change_at = updated_at
        product.last_change_kind = enums.ProductChangeKind.PRICE_CHANGED
        product.last_change_old_price = 175_000
        product.last_change_new_price = 185_000
        product.last_change_actor = admin
    product.save()
    Product.objects.filter(pk=product.pk).update(updated_at=updated_at)
    ProductStock.objects.create(product=product, quantity=spec.stock, synced_at=synced_at)
    if spec.art is not None:
        kind, tone = spec.art
        kinds = spec.image_kinds or (kind,)
        for position, image_kind in enumerate(kinds):
            _add_image(product, position, image_kind, tone, created_at)


@register_seeder("catalog", order=40)
def seed_catalog(context: SeedContext) -> None:
    with transaction.atomic():
        categories = _seed_categories()
        # = IntegrationState(billz).last_sync_at of integrations.seed (anchor − 2 min, 03 §7.3)
        synced_at = context.anchor - timedelta(minutes=2)
        admin = StaffUser.objects.filter(email=SEED_ADMIN_EMAIL).first()
        existing = set(Product.objects.values_list("billz_id", flat=True))
        for index, spec in enumerate(build_catalog()):
            if spec.billz_id in existing:
                continue
            _seed_product(index + 1, spec, categories, synced_at, admin)
