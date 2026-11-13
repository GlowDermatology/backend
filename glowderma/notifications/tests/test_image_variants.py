"""Image variants of the campaign image (spec 2026-11-13-image-variants): written with it, removed with it."""

from io import BytesIO
from typing import Any

import pytest
from django.core.files.storage import default_storage
from django.core.files.uploadedfile import SimpleUploadedFile
from PIL import Image
from rest_framework.test import APIClient

from glowderma.accounts.models import StaffUser
from glowderma.core.media import variant_name
from glowderma.notifications.models import Campaign
from glowderma.notifications.tests.factories import CampaignFactory

pytestmark = pytest.mark.django_db

URL = "/api/v1/admin/notifications/"


def image(colour: str) -> SimpleUploadedFile:
    output = BytesIO()
    Image.new("RGB", (1200, 600), colour).save(output, format="PNG")
    return SimpleUploadedFile("promo.png", output.getvalue(), content_type="image/png")


def names(original: str) -> list[str]:
    return [original, variant_name(original, "thumbnail"), variant_name(original, "medium")]


def all_exist(original: str) -> bool:
    return bool(original) and all(default_storage.exists(name) for name in names(original))


def none_exist(original: str) -> bool:
    return not any(default_storage.exists(name) for name in names(original))


def patch(client: APIClient, campaign_id: int, data: dict[str, Any]) -> Any:
    return client.patch(f"{URL}{campaign_id}/", data, format="multipart")


def stored(campaign: Campaign) -> str:
    campaign.refresh_from_db()
    return campaign.image.name or ""


def test_replace_remove_and_delete_draft(staff_client: APIClient, staff: StaffUser) -> None:
    campaign = CampaignFactory.create(author=staff)
    response = patch(staff_client, campaign.pk, {"expected_revision": "1", "image": image("red")})
    assert response.status_code == 200, response.data
    first = stored(campaign)
    assert all_exist(first)
    response = patch(staff_client, campaign.pk, {"expected_revision": "2", "image": image("blue")})
    assert response.status_code == 200, response.data
    second = stored(campaign)
    assert all_exist(second) and none_exist(first)
    response = patch(staff_client, campaign.pk, {"expected_revision": "3", "remove_image": "true"})
    assert response.status_code == 200, response.data
    assert none_exist(second)
    response = patch(staff_client, campaign.pk, {"expected_revision": "4", "image": image("green")})
    third = stored(campaign)
    assert all_exist(third)
    assert staff_client.delete(f"{URL}{campaign.pk}/").status_code == 204
    assert none_exist(third)


def test_admin_api_variant_urls(staff_client: APIClient, staff: StaffUser) -> None:
    from glowderma.core.media import variant_url

    campaign = CampaignFactory.create(author=staff)
    body = staff_client.get(f"{URL}{campaign.pk}/").json()
    assert body["image_thumbnail_url"] is None and body["image_medium_url"] is None
    body = patch(staff_client, campaign.pk, {"expected_revision": "1", "image": image("red")}).json()
    name = stored(campaign)
    assert body["image_thumbnail_url"] == variant_url(name, "thumbnail")
    assert body["image_medium_url"] == variant_url(name, "medium")
