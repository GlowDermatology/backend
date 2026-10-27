"""Contract §2.3 product images: upload pipeline, delete (compaction, 409, 404) and exact-set reorder."""

import threading
from io import BytesIO
from pathlib import Path
from typing import Any

import pytest
from django.conf import settings
from django.db import connection
from PIL import Image
from rest_framework.test import APIClient

from glowderma.accounts.models import StaffUser
from glowderma.catalog import services
from glowderma.catalog.models import Product, ProductImage
from glowderma.catalog.tests.factories import ProductFactory, ProductImageFactory
from glowderma.catalog.tests.helpers import errors_of, jpeg_with_exif, upload
from glowderma.core.images import MAX_BYTES
from glowderma.core.models import AuditEntry

pytestmark = pytest.mark.django_db

PRODUCTS = "/api/v1/admin/products/"


def images_url(product: Product) -> str:
    return f"{PRODUCTS}{product.pk}/images/"


def media_url(name: str | None) -> str:
    return f"{settings.PUBLIC_MEDIA_BASE_URL.rstrip('/')}/{name}"


# --- upload -------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("fmt", "extension", "content_type"),
    [("PNG", "png", "image/png"), ("JPEG", "jpg", "image/jpeg"), ("WEBP", "webp", "image/webp")],
)
def test_upload_stores_reencoded_file_under_media_root(
    staff_client: APIClient, staff: StaffUser, fmt: str, extension: str, content_type: str
) -> None:
    product = ProductFactory.create(status="hidden")
    response = staff_client.post(images_url(product), {"file": upload(fmt)}, format="multipart")
    assert response.status_code == 201, response.json()
    image = ProductImage.objects.get(product=product)
    assert response.json() == {"id": image.pk, "url": media_url(image.file.name), "position": 0}
    assert image.file.name == f"products/{product.pk}/{image.sha256[:16]}.{extension}"
    assert image.content_type == content_type and (image.width, image.height) == (24, 16)
    assert image.uploaded_by_id == staff.pk and image.size_bytes == Path(image.file.path).stat().st_size
    assert Path(image.file.path).is_relative_to(settings.MEDIA_ROOT)
    with Image.open(image.file.path) as stored:
        assert stored.format == fmt
    entry = AuditEntry.objects.get(action="product.image_added")
    assert entry.entity_id == str(product.pk) and (entry.after or {})["image_id"] == image.pk


def test_upload_strips_exif_and_transposes(staff_client: APIClient) -> None:
    product = ProductFactory.create(status="hidden")
    part = upload("JPEG", name="photo.png", content_type="image/png", content=jpeg_with_exif((60, 40)))
    response = staff_client.post(images_url(product), {"file": part}, format="multipart")
    assert response.status_code == 201
    image = ProductImage.objects.get(product=product)
    assert image.content_type == "image/jpeg" and str(image.file.name).endswith(".jpg")
    assert (image.width, image.height) == (40, 60)  # orientation 6 applied
    with Image.open(image.file.path) as stored:
        assert dict(stored.getexif()) == {}


def test_upload_rejects_spoofed_and_unsupported_images(staff_client: APIClient) -> None:
    product = ProductFactory.create(status="hidden")
    spoofed = upload(content=b"GIF89a not really an image", name="x.png", content_type="image/png")
    response = staff_client.post(images_url(product), {"file": spoofed}, format="multipart")
    assert response.status_code == 400
    assert errors_of(response)["file"] == ("unsupported_image", "Faqat JPEG, PNG yoki WebP rasm.")
    gif = BytesIO()
    Image.new("RGB", (4, 4)).save(gif, format="GIF")
    response = staff_client.post(
        images_url(product), {"file": upload(content=gif.getvalue(), name="a.gif")}, format="multipart"
    )
    assert errors_of(response)["file"] == ("unsupported_image", "Faqat JPEG, PNG yoki WebP rasm.")
    assert not ProductImage.objects.exists()


def test_upload_rejects_five_mib_plus_one_byte(staff_client: APIClient) -> None:
    product = ProductFactory.create(status="hidden")
    part = upload(content=b"\x89PNG" + b"\0" * (MAX_BYTES + 1 - 4), name="big.png")
    response = staff_client.post(images_url(product), {"file": part}, format="multipart")
    assert response.status_code == 400
    assert errors_of(response)["file"] == ("file_too_large", "Rasm 5 MB dan oshmasin.")


def test_upload_requires_the_file_part_and_multipart(staff_client: APIClient) -> None:
    product = ProductFactory.create(status="hidden")
    response = staff_client.post(images_url(product), {"other": "x"}, format="multipart")
    assert response.status_code == 400
    assert errors_of(response)["file"] == ("required", "Majburiy maydon.")
    response = staff_client.post(images_url(product), {"file": "x"}, format="json")
    assert response.status_code == 415
    assert (
        staff_client.post(f"{PRODUCTS}999999/images/", {"file": upload()}, format="multipart").status_code
        == 404
    )


def test_upload_appends_last_and_caps_at_eight(staff_client: APIClient) -> None:
    product = ProductFactory.create(status="hidden")
    for position in range(8):
        response = staff_client.post(images_url(product), {"file": upload()}, format="multipart")
        assert response.status_code == 201
        assert response.json()["position"] == position
    ninth = staff_client.post(images_url(product), {"file": upload()}, format="multipart")
    assert ninth.status_code == 400
    assert errors_of(ninth)["images"] == ("max_items", "Ko‘pi bilan 8 ta rasm.")
    assert ProductImage.objects.filter(product=product).count() == 8
    detail = staff_client.get(f"{PRODUCTS}{product.pk}/").json()
    assert [image["position"] for image in detail["images"]] == list(range(8))
    assert detail["main_image_url"] == detail["images"][0]["url"]


def test_image_calls_do_not_touch_last_change(staff_client: APIClient) -> None:
    product = ProductFactory.create(status="hidden")
    staff_client.post(images_url(product), {"file": upload()}, format="multipart")
    product.refresh_from_db()
    assert product.last_change_at is None


# --- delete -------------------------------------------------------------------------------------------------


def test_delete_compacts_positions_and_removes_file(
    staff_client: APIClient, django_capture_on_commit_callbacks: Any
) -> None:
    product = ProductFactory.create(status="hidden")
    first, second, third = (ProductImageFactory.create(product=product) for _ in range(3))
    path = Path(second.file.path)
    assert path.exists()
    with django_capture_on_commit_callbacks(execute=True):
        response = staff_client.delete(f"{images_url(product)}{second.pk}/")
    assert response.status_code == 204
    assert not path.exists()
    assert list(product.images.order_by("position").values_list("id", "position")) == [
        (first.pk, 0),
        (third.pk, 1),
    ]
    entry = AuditEntry.objects.get(action="product.image_deleted")
    assert entry.before == {"image_id": second.pk, "position": 1}


def test_delete_last_image_of_active_product_is_conflict(staff_client: APIClient) -> None:
    product = ProductFactory.create(status="active")
    image = ProductImageFactory.create(product=product)
    response = staff_client.delete(f"{images_url(product)}{image.pk}/")
    assert response.status_code == 409
    assert response.json() == {
        "code": "conflict",
        "message": "Faol mahsulotda kamida bitta rasm bo‘lsin.",
        "errors": [],
        "request_id": response.json()["request_id"],
    }
    assert ProductImage.objects.filter(pk=image.pk).exists()
    hidden = ProductFactory.create(status="hidden")
    only = ProductImageFactory.create(product=hidden)
    assert staff_client.delete(f"{images_url(hidden)}{only.pk}/").status_code == 204


def test_delete_image_of_another_product_is_404(staff_client: APIClient) -> None:
    product = ProductFactory.create(status="hidden")
    other = ProductFactory.create(status="hidden")
    foreign = ProductImageFactory.create(product=other)
    assert staff_client.delete(f"{images_url(product)}{foreign.pk}/").status_code == 404
    assert staff_client.delete(f"{images_url(product)}999999/").status_code == 404
    assert staff_client.delete(f"{PRODUCTS}999999/images/{foreign.pk}/").status_code == 404
    assert ProductImage.objects.filter(pk=foreign.pk).exists()


# --- reorder ------------------------------------------------------------------------------------------------


def test_reorder_exact_set(staff_client: APIClient) -> None:
    product = ProductFactory.create(status="hidden")
    a, b, c = (ProductImageFactory.create(product=product) for _ in range(3))
    url = f"{images_url(product)}order/"
    response = staff_client.put(url, {"image_ids": [c.pk, a.pk, b.pk]})
    assert response.status_code == 200, response.json()
    assert response.json() == {
        "images": [
            {"id": c.pk, "url": media_url(c.file.name), "position": 0},
            {"id": a.pk, "url": media_url(a.file.name), "position": 1},
            {"id": b.pk, "url": media_url(b.file.name), "position": 2},
        ]
    }
    entry = AuditEntry.objects.get(action="product.images_reordered")
    assert entry.before == {"image_ids": [a.pk, b.pk, c.pk]} and entry.after == {
        "image_ids": [c.pk, a.pk, b.pk]
    }
    for body in ([a.pk, b.pk], [a.pk, b.pk, c.pk, 999], [a.pk, a.pk, b.pk], []):
        response = staff_client.put(url, {"image_ids": body})
        assert response.status_code == 400, body
        assert errors_of(response)["image_ids"] == ("mismatch", "Ro‘yxat joriy elementlarga mos emas."), body
    assert errors_of(staff_client.put(url, {}))["image_ids"] == ("required", "Majburiy maydon.")
    assert staff_client.put(f"{PRODUCTS}999999/images/order/", {"image_ids": [1]}).status_code == 404


def test_reorder_empty_list_is_the_exact_set_of_a_product_without_images(staff_client: APIClient) -> None:
    """``[]`` is a valid body (never DRF's ``empty``): a 200 no-op on an empty product, else ``mismatch``."""
    product = ProductFactory.create(status="hidden")
    response = staff_client.put(f"{images_url(product)}order/", {"image_ids": []})
    assert response.status_code == 200, response.json()
    assert response.json() == {"images": []}
    ProductImageFactory.create(product=product)
    response = staff_client.put(f"{images_url(product)}order/", {"image_ids": []})
    assert response.status_code == 400
    assert errors_of(response)["image_ids"] == ("mismatch", "Ro‘yxat joriy elementlarga mos emas.")


@pytest.mark.django_db(transaction=True)
def test_concurrent_reorders_end_consistent(staff: StaffUser) -> None:
    product = ProductFactory.create(status="hidden")
    a, b, c = (ProductImageFactory.create(product=product) for _ in range(3))
    orders = ([c.pk, a.pk, b.pk], [b.pk, c.pk, a.pk])
    start = threading.Event()
    failures: list[BaseException] = []

    def worker(order: list[int]) -> None:
        try:
            assert start.wait(10)
            services.reorder_images(product.pk, order, actor=staff)
        except BaseException as exc:  # noqa: BLE001 - surfaced to the main thread below
            failures.append(exc)
        finally:
            connection.close()

    threads = [threading.Thread(target=worker, args=(order,)) for order in orders]
    for thread in threads:
        thread.start()
    start.set()
    for thread in threads:
        thread.join(10)
        assert not thread.is_alive()
    assert not failures
    final = list(product.images.order_by("position").values_list("id", flat=True))
    assert sorted(product.images.values_list("position", flat=True)) == [0, 1, 2]
    assert final in orders
    assert AuditEntry.objects.filter(action="product.images_reordered").count() == 2
