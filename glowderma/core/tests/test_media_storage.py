"""``sync_media_to_s3`` and ``media_storage_check`` against a moto bucket."""

import os
import subprocess
import sys
from io import StringIO
from typing import Any

import pytest
from django.conf import settings
from django.core.files.base import ContentFile
from django.core.management import CommandError, call_command

from config.storage import S3_BACKEND
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


def test_use_s3_in_one_process_environment_switches_only_that_process() -> None:
    """The deployed `sync-media` runs `exec -e USE_S3=true` in the running API container (still on the disk):
    USE_S3 is read when the settings are imported, so that one process gets the S3 default storage."""
    env = {
        **os.environ,
        "DJANGO_SETTINGS_MODULE": "config.settings.test",
        "USE_S3": "true",
        "S3_BUCKET_NAME": "bucket",
        "S3_ACCESS_KEY_ID": "access",
        "S3_SECRET_ACCESS_KEY": "secret",
    }
    env.pop("DJANGO_SECRETS_FILE", None)
    code = (
        "import django; from django.conf import settings; django.setup(); "
        "print(settings.STORAGES['default']['BACKEND'])"
    )
    result = subprocess.run(  # noqa: S603 - our own interpreter and a fixed snippet
        [sys.executable, "-c", code],
        env=env,
        cwd=settings.BASE_DIR,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr[-2000:]
    assert result.stdout.strip() == S3_BACKEND
    assert settings.STORAGES["default"]["BACKEND"] != S3_BACKEND  # this (test) process is untouched


@pytest.mark.django_db
def test_check_ok_writes_reads_deletes_probe(s3_storage: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    # the probe only: the database scan has its own tests, so rows left by other tests cannot fail this one
    monkeypatch.setattr(media_storage, "stored_names", lambda: iter(()))
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
    assert "referenced file(s) missing" in err.getvalue() and NAME in err.getvalue()
    call_command("sync_media_to_s3", "--apply")
    assert NAME not in media_storage.missing_names(s3_storage)


@pytest.mark.django_db
def test_check_prints_at_most_twenty_missing_names(s3_storage: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    names = [f"categories/{number}/missing-{number:02d}.webp" for number in range(25)]
    monkeypatch.setattr(media_storage, "stored_names", lambda: iter(names))
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
