"""The e2e seed banner has its image variants (spec 2026-11-13-image-variants): the admin list loads them."""

import pytest
from django.core.files.storage import default_storage

from glowderma.catalog.models import Banner
from glowderma.core.media import variant_name
from glowderma.reporting.e2e_seed import fill_empty_models

pytestmark = pytest.mark.django_db


def test_the_seeded_banner_has_its_variants() -> None:
    fill_empty_models()
    name = str(Banner.objects.get(title_uz="E2E banner").image.name)
    assert default_storage.exists(variant_name(name, "thumbnail"))
    assert default_storage.exists(variant_name(name, "medium"))
