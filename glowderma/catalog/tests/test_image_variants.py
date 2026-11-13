"""Image variants (spec 2026-11-13-image-variants): catalog uploads write them, deletes remove them."""

from typing import Any
from unittest.mock import patch

import pytest
from django.core.files.storage import default_storage
from rest_framework.test import APIClient

from glowderma.accounts.models import StaffUser
from glowderma.catalog import services
from glowderma.catalog.models import Banner, Category, ProductImage
from glowderma.catalog.tests.factories import CategoryFactory, ProductFactory
from glowderma.catalog.tests.helpers import upload
from glowderma.core.media import variant_name

pytestmark = pytest.mark.django_db

PRODUCTS = "/api/v1/admin/products/"
CATEGORIES = "/api/v1/admin/categories/"
BANNERS = "/api/v1/admin/banners/"


def names(original: str) -> list[str]:
    return [original, variant_name(original, "thumbnail"), variant_name(original, "medium")]


def all_exist(original: str) -> bool:
    return bool(original) and all(default_storage.exists(name) for name in names(original))


def none_exist(original: str) -> bool:
    return not any(default_storage.exists(name) for name in names(original))


# --- product images ----------------------------------------------------------------------------------------


def test_product_image_upload_writes_variants_and_delete_removes_them(
    staff_client: APIClient, django_capture_on_commit_callbacks: Any
) -> None:
    product = ProductFactory.create(status="hidden")
    response = staff_client.post(
        f"{PRODUCTS}{product.pk}/images/", {"file": upload("JPEG", size=(1200, 900))}, format="multipart"
    )
    assert response.status_code == 201, response.json()
    image = ProductImage.objects.get(pk=response.json()["id"])
    original = str(image.file.name)
    assert all_exist(original)
    with django_capture_on_commit_callbacks(execute=True):
        assert staff_client.delete(f"{PRODUCTS}{product.pk}/images/{image.pk}/").status_code == 204
    assert none_exist(original)


def test_deleting_one_of_two_identical_images_keeps_the_others_files(
    staff_client: APIClient, django_capture_on_commit_callbacks: Any
) -> None:
    product = ProductFactory.create(status="hidden")
    content = upload("PNG", size=(500, 400)).read()
    ids = [
        staff_client.post(
            f"{PRODUCTS}{product.pk}/images/", {"file": upload("PNG", content=content)}, format="multipart"
        ).json()["id"]
        for _ in range(2)
    ]
    first, second = (str(ProductImage.objects.get(pk=pk).file.name) for pk in ids)
    assert first != second  # the storage collision suffix
    with django_capture_on_commit_callbacks(execute=True):
        assert staff_client.delete(f"{PRODUCTS}{product.pk}/images/{ids[0]}/").status_code == 204
    assert none_exist(first)
    assert all_exist(second)


def test_a_failed_variant_write_fails_the_product_upload_whole(staff: StaffUser) -> None:
    product = ProductFactory.create(status="hidden")
    with (
        patch("glowderma.catalog.services.store_variants", side_effect=OSError("disk full")),
        pytest.raises(OSError, match="disk full"),
    ):
        services.add_image(product.pk, upload("PNG"), actor=staff)
    assert not ProductImage.objects.filter(product=product).exists()
    directory = f"products/{product.pk}"
    assert not default_storage.exists(directory) or default_storage.listdir(directory)[1] == []


# --- categories --------------------------------------------------------------------------------------------


def test_category_create_replace_and_remove(
    staff_client: APIClient, django_capture_on_commit_callbacks: Any
) -> None:
    response = staff_client.post(
        CATEGORIES,
        {"slug": "masks", "name.uz": "Niqoblar", "image": upload("PNG", size=(800, 600))},
        format="multipart",
    )
    assert response.status_code == 201, response.json()
    first = str(Category.objects.get(slug="masks").image.name)
    assert all_exist(first)
    with django_capture_on_commit_callbacks(execute=True):
        response = staff_client.patch(
            f"{CATEGORIES}masks/", {"image": upload("JPEG", size=(640, 480))}, format="multipart"
        )
    assert response.status_code == 200, response.json()
    second = str(Category.objects.get(slug="masks").image.name)
    assert all_exist(second) and none_exist(first)
    with django_capture_on_commit_callbacks(execute=True):
        assert staff_client.patch(f"{CATEGORIES}masks/", {"remove_image": True}).status_code == 200
    assert none_exist(second)


def test_a_failed_variant_write_fails_the_category_update_and_leaves_no_file(staff: StaffUser) -> None:
    category = CategoryFactory.create(slug="serums", name_uz="Serumlar")
    with (
        patch("glowderma.catalog.services.store_variants", side_effect=OSError("disk full")),
        pytest.raises(OSError, match="disk full"),
    ):
        services.update_category(category, image=upload("PNG"), actor=staff)
    assert Category.objects.get(pk=category.pk).image.name == ""
    directory = f"categories/{category.pk}"
    assert not default_storage.exists(directory) or default_storage.listdir(directory)[1] == []


# --- banners -----------------------------------------------------------------------------------------------


def test_banner_create_replace_and_delete(
    staff_client: APIClient, django_capture_on_commit_callbacks: Any
) -> None:
    response = staff_client.post(
        BANNERS, {"image": upload("WEBP", size=(1600, 600)), "title.uz": "Kuz"}, format="multipart"
    )
    assert response.status_code == 201, response.json()
    banner = Banner.objects.get(pk=response.json()["id"])
    first = str(banner.image.name)
    assert all_exist(first)
    with django_capture_on_commit_callbacks(execute=True):
        response = staff_client.patch(
            f"{BANNERS}{banner.pk}/", {"image": upload("PNG", size=(1600, 600))}, format="multipart"
        )
    assert response.status_code == 200, response.json()
    banner.refresh_from_db()
    second = str(banner.image.name)
    assert all_exist(second) and none_exist(first)
    with django_capture_on_commit_callbacks(execute=True):
        assert staff_client.delete(f"{BANNERS}{banner.pk}/").status_code == 204
    assert none_exist(second)
