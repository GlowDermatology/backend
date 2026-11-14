"""``backfill_image_variants`` (spec 2026-11-13-image-variants): the variants of images stored before them."""

from io import BytesIO, StringIO

import pytest
from django.apps import apps
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.core.management import call_command
from PIL import Image

from glowderma.core.media import variant_name
from glowderma.core.variants import backfill

pytestmark = pytest.mark.django_db


def png() -> bytes:
    output = BytesIO()
    Image.new("RGB", (900, 600), "#d6b2c4").save(output, format="PNG")
    return output.getvalue()


def category_with_image(slug: str, *, store: bool = True) -> str:
    """A category row whose ``image`` names a file (written unless ``store`` is false); ``core`` tests read
    the model through the app registry (import-linter)."""
    name = f"backfill-{slug}/1/{slug}.png"
    if store:
        name = default_storage.save(name, ContentFile(png()))
    apps.get_model("catalog", "Category").objects.create(slug=slug, name_uz=slug, image=name)
    return name


def variants_exist(name: str) -> bool:
    return all(default_storage.exists(variant_name(name, variant)) for variant in ("thumbnail", "medium"))


def test_backfill_creates_missing_variants() -> None:
    name = category_with_image("first")
    report = backfill()
    assert (report.checked, report.created, report.missing_originals) == (1, 2, [])
    assert variants_exist(name)


def test_backfill_is_idempotent() -> None:
    category_with_image("again")
    backfill()
    report = backfill()
    assert (report.checked, report.created) == (1, 0)


def test_backfill_dry_run_writes_nothing() -> None:
    name = category_with_image("dry")
    report = backfill(dry_run=True)
    assert (report.checked, report.created) == (1, 2)
    assert not default_storage.exists(variant_name(name, "thumbnail"))


def test_backfill_reports_a_missing_original_and_goes_on() -> None:
    missing = category_with_image("gone", store=False)
    present = category_with_image("here")
    report = backfill()
    assert report.missing_originals == [missing]
    assert report.checked == 2 and report.created == 2
    assert variants_exist(present)


def test_backfill_skips_empty_fields() -> None:
    apps.get_model("catalog", "Category").objects.create(slug="empty", name_uz="empty")
    assert backfill().checked == 0


def test_command_prints_the_report() -> None:
    category_with_image("printed")
    category_with_image("lost", store=False)
    out = StringIO()
    call_command("backfill_image_variants", "--dry-run", stdout=out)
    text = out.getvalue()
    assert "checked 2" in text and "created 2" in text and "missing 1" in text
    assert "backfill-lost/1/lost.png" in text
    assert "dry run" in text


def test_backfill_reports_an_unreadable_original_and_goes_on() -> None:
    broken = default_storage.save("backfill-broken/1/broken.png", ContentFile(b"not an image"))
    apps.get_model("catalog", "Category").objects.create(slug="broken", name_uz="broken", image=broken)
    present = category_with_image("fine")
    report = backfill()
    assert report.failed == [broken]
    assert variants_exist(present)
