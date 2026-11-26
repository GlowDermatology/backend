"""Screen-fit renditions: the crop math (``core.renditions``), ``PUT …/images/{image_id}/renditions/``, the
customer API's ``renditions`` / ``main_image_renditions``, and file cleanup."""

from __future__ import annotations

from io import BytesIO
from pathlib import Path
from typing import Any

import pytest
from django.conf import settings
from django.core.files.storage import default_storage
from PIL import Image
from rest_framework.test import APIClient

from glowderma.catalog import services
from glowderma.catalog.models import Product, ProductImage
from glowderma.catalog.tests.factories import ProductFactory
from glowderma.catalog.tests.helpers import errors_of, upload
from glowderma.core import renditions
from glowderma.core.models import AuditEntry

pytestmark = pytest.mark.django_db

PRODUCTS = "/api/v1/admin/products/"


def png(size: tuple[int, int], colour: tuple[int, int, int] = (200, 30, 60)) -> bytes:
    output = BytesIO()
    Image.new("RGB", size, colour).save(output, format="PNG")
    return output.getvalue()


def uploaded(staff_client: APIClient, product: Product, size: tuple[int, int] = (1200, 1600)) -> ProductImage:
    response = staff_client.post(
        f"{PRODUCTS}{product.pk}/images/", {"file": upload("PNG", content=png(size))}, format="multipart"
    )
    assert response.status_code == 201, response.json()
    return ProductImage.objects.get(pk=response.json()["id"])


def put(staff_client: APIClient, image: ProductImage, body: dict[str, object]) -> object:
    return staff_client.put(
        f"{PRODUCTS}{image.product_id}/images/{image.pk}/renditions/", body, format="json"
    )


# --- core.renditions ----------------------------------------------------------------------------------------


def test_auto_crop_without_a_box_is_the_largest_centred_crop() -> None:
    assert renditions.auto_crop("1:1", 1200, 1600) == (0.0, 0.125, 1.0, 0.75)
    assert renditions.auto_crop("4:5", 1600, 1200) == (0.2, 0.0, 0.6, 1.0)


def test_auto_crop_frames_the_product_box_and_stays_inside() -> None:
    x, y, w, h = renditions.auto_crop("4:5", 1200, 1600, [0.4, 0.3, 0.6, 0.7])
    assert x <= 0.4 and x + w >= 0.6 and y <= 0.3 and y + h >= 0.7
    assert abs((w * 1200) / (h * 1600) - 0.8) < 0.01
    # a product at the edge: the crop is pushed back inside the image
    x, y, w, h = renditions.auto_crop("1:1", 1200, 1600, [0.0, 0.0, 0.2, 0.2])
    assert (x, y) == (0.0, 0.0) and w <= 1 and h <= 1
    # a box wider than the image allows: the widest crop
    assert renditions.auto_crop("1:1", 1000, 2000, [0.0, 0.4, 1.0, 0.6])[2] == 1.0


@pytest.mark.parametrize(
    ("crop", "code"),
    [
        ([0, 0, 1, 1], "invalid_crop"),  # a square crop of a 3:4 image is not square
        ([0.5, 0, 0.75, 0.5625], "invalid_crop"),  # outside
        ("full", "invalid_crop"),
        ([0, 0, 0.1, 0.075], "crop_too_small"),
    ],
)
def test_check_crop_refusals(crop: object, code: str) -> None:
    with pytest.raises(renditions.CropError) as caught:
        renditions.check_crop("1:1", crop, 1200, 1600)
    assert caught.value.code == code


def test_render_has_the_exact_shape_and_a_bounded_size() -> None:
    data = renditions.render(png((3000, 4000)), "4:5", (0.0, 0.0, 1.0, 0.9375))
    with Image.open(BytesIO(data)) as image:
        assert image.format == "WEBP" and image.size == (1600, 2000)
    data = renditions.render(png((600, 600)), "1:1", (0.0, 0.0, 1.0, 1.0))
    with Image.open(BytesIO(data)) as image:
        assert image.size == (600, 600)  # never enlarged


def test_api_shapes() -> None:
    assert renditions.api_urls(None) == {"portrait": None, "square": None}
    stored = {"1:1": {"crop": [0, 0, 1, 1], "mode": "manual", "name": "products/1/a_r11-x.webp"}}
    urls = renditions.api_urls(stored)
    assert urls["portrait"] is None and urls["square"] is not None
    assert str(urls["square"]["medium_url"]).endswith("a_r11-x_1000.webp")
    detail = renditions.api_detail(stored)["square"]
    assert detail is not None and detail["mode"] == "manual" and detail["crop"] == [0, 0, 1, 1]


# --- admin API ----------------------------------------------------------------------------------------------


def test_set_auto_and_manual_renditions(staff_client: APIClient) -> None:
    product = ProductFactory.create(status="hidden")
    image = uploaded(staff_client, product)
    response = put(staff_client, image, {"portrait": "auto", "square": [0.1, 0.2, 0.8, 0.6]})
    assert response.status_code == 200, response.json()  # type: ignore[attr-defined]
    body = response.json()  # type: ignore[attr-defined]
    assert body["renditions"]["portrait"]["mode"] == "auto"
    assert body["renditions"]["square"]["mode"] == "manual"
    assert body["renditions"]["square"]["crop"] == [0.1, 0.2, 0.8, 0.6]
    image.refresh_from_db()
    for entry in image.renditions.values():
        for suffix in ("", "_400", "_1000"):
            name = entry["name"] if not suffix else entry["name"].replace(".webp", f"{suffix}.webp")
            assert default_storage.exists(name)
    with Image.open(Path(settings.MEDIA_ROOT) / image.renditions["1:1"]["name"]) as square:
        assert square.width == square.height
    assert AuditEntry.objects.filter(action="product.image_renditions").count() == 1


def test_changing_and_removing_a_shape_deletes_its_old_files(
    staff_client: APIClient, django_capture_on_commit_callbacks: Any
) -> None:
    product = ProductFactory.create(status="hidden")
    image = uploaded(staff_client, product)
    put(staff_client, image, {"square": "auto"})
    image.refresh_from_db()
    first = image.renditions["1:1"]["name"]
    with django_capture_on_commit_callbacks(execute=True):
        put(staff_client, image, {"square": [0.0, 0.0, 1.0, 0.75]})
    image.refresh_from_db()
    assert image.renditions["1:1"]["name"] != first and not default_storage.exists(first)
    response = put(staff_client, image, {"square": None})
    assert response.json()["renditions"]["square"] is None  # type: ignore[attr-defined]
    image.refresh_from_db()
    assert image.renditions == {}


def test_bad_crops_are_refused_per_shape(staff_client: APIClient) -> None:
    product = ProductFactory.create(status="hidden")
    image = uploaded(staff_client, product)
    response = put(staff_client, image, {"portrait": [0, 0, 1, 1], "square": [0, 0, 0.1, 0.075]})
    assert response.status_code == 400  # type: ignore[attr-defined]
    errors = errors_of(response)
    assert errors["portrait"][0] == "invalid_crop" and errors["square"][0] == "crop_too_small"
    image.refresh_from_db()
    assert image.renditions == {}


def test_another_products_image_is_404(staff_client: APIClient) -> None:
    image = uploaded(staff_client, ProductFactory.create(status="hidden"))
    other = ProductFactory.create(status="hidden")
    response = staff_client.put(
        f"{PRODUCTS}{other.pk}/images/{image.pk}/renditions/", {"square": "auto"}, format="json"
    )
    assert response.status_code == 404


def test_deleting_an_image_removes_its_renditions(
    staff_client: APIClient, django_capture_on_commit_callbacks: Any
) -> None:
    product = ProductFactory.create(status="hidden")
    image = uploaded(staff_client, product)
    put(staff_client, image, {"portrait": "auto", "square": "auto"})
    image.refresh_from_db()
    names = renditions.names(image.renditions)
    with django_capture_on_commit_callbacks(execute=True):
        services.delete_image(product.pk, image.pk, actor=None)
    assert not any(default_storage.exists(name) for name in names)


def test_add_generated_image_keeps_the_original_and_can_lead(staff_client: APIClient) -> None:
    product = ProductFactory.create(status="hidden")
    original = uploaded(staff_client, product)
    scene = services.add_generated_image(
        product.pk,
        png((928, 1160), (240, 235, 225)),
        source="studio",
        derived_from_id=original.pk,
        product_box=[0.18, 0.22, 0.82, 0.82],
        crops={"4:5": (0.0, 0.0, 1.0, 1.0), "1:1": (0.0, 0.1, 1.0, 0.8)},
        make_main=True,
        actor=None,
    )
    original.refresh_from_db()
    assert (scene.position, original.position) == (0, 1)
    assert scene.source == "studio" and scene.derived_from_id == original.pk
    assert set(scene.renditions) == {"4:5", "1:1"}
    entry = AuditEntry.objects.filter(action="product.image_added").latest("id")
    assert (entry.after or {})["source"] == "studio"


# --- customer API -------------------------------------------------------------------------------------------


def test_customer_api_exposes_renditions(staff_client: APIClient, client: APIClient) -> None:
    product = ProductFactory.create(status="active")
    image = uploaded(staff_client, product)
    detail = client.get(f"/api/v1/catalog/products/{product.slug}/").json()
    assert detail["main_image_renditions"] == {"portrait": None, "square": None}
    assert detail["images"][0]["renditions"] == {"portrait": None, "square": None}
    put(staff_client, image, {"portrait": "auto"})
    detail = client.get(f"/api/v1/catalog/products/{product.slug}/").json()
    portrait = detail["main_image_renditions"]["portrait"]
    assert portrait["url"].endswith(".webp") and portrait["thumbnail_url"].endswith("_400.webp")
    assert detail["images"][0]["renditions"]["portrait"] == portrait
    assert detail["main_image_renditions"]["square"] is None
