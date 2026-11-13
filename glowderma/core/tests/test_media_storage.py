"""``sync_media_to_s3`` and ``media_storage_check`` against a moto bucket."""

from io import StringIO
from typing import Any

import pytest
from django.core.files.base import ContentFile
from django.core.management import CommandError, call_command

from glowderma.catalog.tests.factories import CategoryFactory
from glowderma.commerce.models import OrderItem
from glowderma.commerce.tests.factories import OrderItemFactory
from glowderma.core import media_storage

NAME = "categories/1/abc.webp"


def test_sync_dry_run_uploads_nothing(s3_storage: Any, media_file: str) -> None:
    out = StringIO()
    call_command("sync_media_to_s3", stdout=out)
    assert not s3_storage.exists(NAME)
    assert f"would upload {NAME}" in out.getvalue() and "scanned=1 uploaded=0 already=0" in out.getvalue()


def test_sync_apply_uploads_missing_and_is_idempotent(s3_storage: Any, media_file: str) -> None:
    call_command("sync_media_to_s3", "--apply")
    out = StringIO()
    call_command("sync_media_to_s3", "--apply", stdout=out)
    assert s3_storage.exists(NAME)
    assert s3_storage.listdir("categories/1")[1] == ["abc.webp"]  # no abc_xyz.webp duplicate
    assert "scanned=1 uploaded=0 already=1" in out.getvalue()


def test_sync_refuses_when_default_storage_is_disk(media_file: str) -> None:
    with pytest.raises(CommandError, match="USE_S3"):
        call_command("sync_media_to_s3", "--apply")


@pytest.mark.django_db
def test_check_ok_writes_reads_deletes_probe(s3_storage: Any) -> None:
    out = StringIO()
    call_command("media_storage_check", stdout=out)
    assert s3_storage.listdir("healthcheck") == ([], [])
    assert out.getvalue().startswith("ok backend=S3Storage")


@pytest.mark.django_db
def test_check_fails_listing_missing_names(s3_storage: Any, media_file: str) -> None:
    CategoryFactory.create(image=NAME)  # on disk only, never uploaded
    err = StringIO()
    with pytest.raises(SystemExit) as exc:
        call_command("media_storage_check", stderr=err)
    assert exc.value.code == 1
    assert "1 referenced file(s) missing" in err.getvalue() and NAME in err.getvalue()
    call_command("sync_media_to_s3", "--apply")
    call_command("media_storage_check")


@pytest.mark.django_db
def test_check_prints_at_most_twenty_missing_names(s3_storage: Any) -> None:
    for number in range(25):
        CategoryFactory.create(image=f"categories/{number}/missing-{number:02d}.webp")
    err = StringIO()
    with pytest.raises(SystemExit):
        call_command("media_storage_check", stderr=err)
    assert "25 referenced file(s) missing" in err.getvalue()
    assert err.getvalue().count("missing-") == 20


@pytest.mark.django_db
def test_stored_names_cover_file_fields_and_order_item_paths(s3_storage: Any) -> None:
    CategoryFactory.create(image=ContentFile(b"x", name="a.webp"))
    CategoryFactory.create()  # no image: skipped
    OrderItemFactory.create(image_url="products/1/9f2c.webp")
    OrderItemFactory.create(image_url="")
    names = list(media_storage.stored_names())
    assert "products/1/9f2c.webp" in names and sum(name.endswith("a.webp") for name in names) == 1
    assert "" not in names and OrderItem.objects.count() == 2
