"""Variants of the specialist photo (spec 2026-11-13-image-variants): written and removed with it."""

from collections.abc import Callable
from typing import Any

import pytest
from django.core.files.storage import default_storage
from rest_framework.test import APIClient

from glowderma.catalog.tests.helpers import upload
from glowderma.core.media import variant_name
from glowderma.professionals import services
from glowderma.professionals.models import Professional
from glowderma.professionals.tests.factories import ProfessionalFactory

pytestmark = pytest.mark.django_db

SPECIALISTS = "/api/v1/admin/specialists/"


def names(original: str) -> list[str]:
    return [original, variant_name(original, "thumbnail"), variant_name(original, "medium")]


def all_exist(original: str) -> bool:
    return bool(original) and all(default_storage.exists(name) for name in names(original))


def none_exist(original: str) -> bool:
    return not any(default_storage.exists(name) for name in names(original))


def test_photo_upload_replace_and_delete(
    staff_client: APIClient, django_capture_on_commit_callbacks: Callable[..., Any]
) -> None:
    professional = ProfessionalFactory.create()
    url = f"{SPECIALISTS}{professional.pk}/photo/"
    assert (
        staff_client.put(url, {"file": upload("PNG", size=(900, 900))}, format="multipart").status_code == 200
    )
    first = Professional.objects.get().photo.name or ""
    assert all_exist(first)
    with django_capture_on_commit_callbacks(execute=True):
        response = staff_client.put(url, {"file": upload("JPEG", size=(700, 900))}, format="multipart")
    assert response.status_code == 200, response.json()
    second = Professional.objects.get().photo.name or ""
    assert all_exist(second) and none_exist(first)
    with django_capture_on_commit_callbacks(execute=True):
        assert staff_client.delete(url).status_code == 204
    assert none_exist(second)


def test_store_photo_writes_variants() -> None:
    professional = ProfessionalFactory.create()
    services.store_photo(professional, upload("PNG", size=(300, 300)))
    assert all_exist(Professional.objects.get().photo.name or "")


def test_admin_api_variant_urls(staff_client: APIClient) -> None:
    from glowderma.core.media import variant_url

    professional = ProfessionalFactory.create()
    url = f"{SPECIALISTS}{professional.pk}/"
    body = staff_client.get(url).json()
    assert body["photo_thumbnail_url"] is None and body["photo_medium_url"] is None
    staff_client.put(f"{url}photo/", {"file": upload("PNG")}, format="multipart")
    name = Professional.objects.get().photo.name or ""
    body = staff_client.get(url).json()
    assert body["photo_thumbnail_url"] == variant_url(name, "thumbnail")
    assert body["photo_medium_url"] == variant_url(name, "medium")


def test_reuploading_the_same_photo_repairs_a_missing_variant(staff_client: APIClient) -> None:
    professional = ProfessionalFactory.create()
    url = f"{SPECIALISTS}{professional.pk}/photo/"
    content = upload("PNG", size=(500, 500)).read()
    staff_client.put(url, {"file": upload("PNG", content=content)}, format="multipart")
    name = Professional.objects.get().photo.name or ""
    default_storage.delete(variant_name(name, "medium"))
    assert (
        staff_client.put(url, {"file": upload("PNG", content=content)}, format="multipart").status_code == 200
    )
    assert (Professional.objects.get().photo.name or "") == name
    assert all_exist(name)
