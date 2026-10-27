"""factory-boy factories for the catalog models (call them as ``XFactory.create(...)``).

``ProductImageFactory`` and ``BannerFactory`` write a tiny real WebP through the ``ImageField`` so the
``width``/``height`` dimension fields and the content-addressed name behave as in production; the package
``conftest.py`` points ``MEDIA_ROOT`` at a temporary directory.
"""

from hashlib import sha256
from io import BytesIO
from typing import Any

from django.core.files.base import ContentFile
from factory.declarations import LazyAttribute, LazyFunction, Sequence, SubFactory
from factory.django import DjangoModelFactory
from PIL import Image

from glowderma.catalog.models import Banner, Category, Product, ProductImage, ProductStock
from glowderma.core import enums


def image_bytes(fmt: str = "WEBP", size: tuple[int, int] = (24, 16), colour: str = "#d6b2c4") -> bytes:
    """A real, tiny image of ``fmt`` (``WEBP`` | ``PNG`` | ``JPEG``)."""
    output = BytesIO()
    Image.new("RGB", size, colour).save(output, format=fmt)
    return output.getvalue()


def _content_file(content: bytes, extension: str) -> ContentFile[bytes]:
    return ContentFile(content, name=f"{sha256(content).hexdigest()[:16]}.{extension}")


class CategoryFactory(DjangoModelFactory[Category]):
    class Meta:
        model = Category

    slug = Sequence(lambda n: f"category-{n + 1}")  # type: ignore[no-untyped-call]
    name_uz = Sequence(lambda n: f"Kategoriya {n + 1}")  # type: ignore[no-untyped-call]
    position = Sequence(lambda n: n)  # type: ignore[no-untyped-call]


class ProductFactory(DjangoModelFactory[Product]):
    class Meta:
        model = Product

    billz_id = Sequence(lambda n: str(4607100000 + n))  # type: ignore[no-untyped-call]
    billz_name = Sequence(lambda n: f"TEST PRODUCT {n + 1} 30ML")  # type: ignore[no-untyped-call]
    volume = "30 ml"
    price = 185_000
    billz_price = 185_000
    category = SubFactory(CategoryFactory)  # type: ignore[no-untyped-call]
    status = enums.ProductStatus.ACTIVE
    name_uz = Sequence(lambda n: f"Test mahsulot {n + 1}")  # type: ignore[no-untyped-call]


class ProductStockFactory(DjangoModelFactory[ProductStock]):
    class Meta:
        model = ProductStock

    product = SubFactory(ProductFactory)  # type: ignore[no-untyped-call]
    quantity = 10


class ProductImageFactory(DjangoModelFactory[ProductImage]):
    class Meta:
        model = ProductImage

    class Params:
        content = LazyFunction(image_bytes)  # type: ignore[no-untyped-call]

    product = SubFactory(ProductFactory)  # type: ignore[no-untyped-call]
    position = LazyAttribute(lambda o: o.product.images.count())  # type: ignore[no-untyped-call]
    file = LazyAttribute(lambda o: _content_file(o.content, "webp"))  # type: ignore[no-untyped-call]
    width = 24
    height = 16
    content_type = "image/webp"
    size_bytes = LazyAttribute(lambda o: len(o.content))  # type: ignore[no-untyped-call]
    sha256 = LazyAttribute(lambda o: sha256(o.content).hexdigest())  # type: ignore[no-untyped-call]


class BannerFactory(DjangoModelFactory[Banner]):
    class Meta:
        model = Banner

    class Params:
        content = LazyFunction(lambda: image_bytes(size=(32, 12), colour="#b2ced6"))  # type: ignore[no-untyped-call]

    title_uz = Sequence(lambda n: f"Banner {n + 1}")  # type: ignore[no-untyped-call]
    image = LazyAttribute(lambda o: _content_file(o.content, "webp"))  # type: ignore[no-untyped-call]
    image_width = 32
    image_height = 12
    position = Sequence(lambda n: n)  # type: ignore[no-untyped-call]

    @classmethod
    def _create(cls, model_class: type[Banner], *args: Any, **kwargs: Any) -> Banner:
        # The id-based upload path needs a pk: insert first, then attach the file (design 01 §4.5).
        image = kwargs.pop("image")
        banner = model_class(**kwargs)
        banner.image.name = ""
        banner.save()
        banner.image.save(image.name, image, save=True)
        return banner
