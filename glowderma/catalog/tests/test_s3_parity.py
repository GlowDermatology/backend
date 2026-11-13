"""The catalog image behaviours that must be identical on local disk and on S3."""

import re
from collections.abc import Iterator
from typing import Any

import pytest
from django.conf import settings
from django.core.files.storage import default_storage
from rest_framework.test import APIClient

from glowderma.catalog.models import Category, ProductImage
from glowderma.catalog.tests.factories import CategoryFactory, ProductFactory
from glowderma.catalog.tests.helpers import upload

pytestmark = pytest.mark.django_db

# the repeated upload is not overwritten: Django adds a ``_<random>`` suffix, on disk and on S3 alike
NAME_PATTERN = re.compile(r"^products/\d+/[0-9a-f]{16}(_\w+)?\.webp$")


@pytest.fixture(params=["disk", "s3"])
def storage(request: pytest.FixtureRequest) -> Iterator[Any]:
    if request.param == "s3":
        yield request.getfixturevalue("s3_storage")
    else:
        yield default_storage


def test_same_image_twice_gives_the_same_name_pattern(storage: Any, staff_client: APIClient) -> None:
    product = ProductFactory.create()
    url = f"/api/v1/admin/products/{product.pk}/images/"
    for _ in range(2):
        assert staff_client.post(url, {"file": upload("WEBP")}, format="multipart").status_code == 201
    first, second = ProductImage.objects.filter(product=product).order_by("position")
    assert NAME_PATTERN.match(str(first.file.name)) and NAME_PATTERN.match(str(second.file.name))
    assert storage.exists(first.file.name)


def test_replacing_a_category_image_deletes_the_previous_object(
    storage: Any, staff_client: APIClient, django_capture_on_commit_callbacks: Any
) -> None:
    CategoryFactory.create(slug="serums")
    url = "/api/v1/admin/categories/serums/"
    with django_capture_on_commit_callbacks(execute=True):
        first = staff_client.patch(url, {"image": upload("PNG", size=(40, 20))}, format="multipart")
    assert first.status_code == 200
    old = str(Category.objects.get(slug="serums").image.name)
    assert storage.exists(old)
    with django_capture_on_commit_callbacks(execute=True):
        second = staff_client.patch(url, {"image": upload("WEBP", size=(30, 30))}, format="multipart")
    assert second.status_code == 200
    new = str(Category.objects.get(slug="serums").image.name)
    assert new != old and storage.exists(new) and not storage.exists(old)


def test_deleting_a_product_image_removes_the_object(
    storage: Any, staff_client: APIClient, django_capture_on_commit_callbacks: Any
) -> None:
    product = ProductFactory.create(status="hidden")
    url = f"/api/v1/admin/products/{product.pk}/images/"
    response = staff_client.post(url, {"file": upload("WEBP")}, format="multipart")
    image = ProductImage.objects.get(pk=response.json()["id"])
    name = str(image.file.name)
    assert storage.exists(name)
    with django_capture_on_commit_callbacks(execute=True):
        assert staff_client.delete(f"{url}{image.pk}/").status_code == 204
    assert not storage.exists(name)


def test_api_image_url_is_the_public_base_plus_the_name(storage: Any, staff_client: APIClient) -> None:
    product = ProductFactory.create()
    response = staff_client.post(
        f"/api/v1/admin/products/{product.pk}/images/", {"file": upload("WEBP")}, format="multipart"
    )
    name = str(ProductImage.objects.get(pk=response.json()["id"]).file.name)
    assert response.json()["url"] == str(settings.PUBLIC_MEDIA_BASE_URL).rstrip("/") + "/" + name
