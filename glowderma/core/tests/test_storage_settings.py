import json
from collections.abc import Callable
from types import ModuleType

import pytest
from django.core.exceptions import ImproperlyConfigured

from config.secrets import parse_secrets
from config.storage import S3_BACKEND, media_storage
from glowderma.core.tests.test_settings import (  # noqa: F401 - fixtures shared with the settings tests
    load_fresh_production,
    production_env,
)

S3_KEYS = (
    "USE_S3",
    "S3_ENDPOINT_URL",
    "S3_BUCKET_NAME",
    "S3_ACCESS_KEY_ID",
    "S3_SECRET_ACCESS_KEY",
    "S3_REGION_NAME",
    "S3_ADDRESSING_STYLE",
    "S3_LOCATION",
    "S3_VERIFY",
)


@pytest.fixture(autouse=True)
def clean_s3_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in S3_KEYS:
        monkeypatch.delenv(key, raising=False)


@pytest.fixture
def s3_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("USE_S3", "true")
    monkeypatch.setenv("S3_BUCKET_NAME", "glowderma-media")
    monkeypatch.setenv("S3_ACCESS_KEY_ID", "AK")
    monkeypatch.setenv("S3_SECRET_ACCESS_KEY", "SK")
    monkeypatch.setenv("S3_ENDPOINT_URL", "http://minio:9000")


def test_default_is_filesystem() -> None:
    assert media_storage() == {"BACKEND": "django.core.files.storage.FileSystemStorage"}


def test_use_s3_builds_s3_options(s3_env: None) -> None:
    cfg = media_storage()
    assert cfg["BACKEND"] == S3_BACKEND == "storages.backends.s3.S3Storage"
    assert cfg["OPTIONS"] == {
        "bucket_name": "glowderma-media",
        "endpoint_url": "http://minio:9000",
        "access_key": "AK",
        "secret_key": "SK",
        "region_name": "us-east-1",
        "addressing_style": "path",
        "location": "media",
        "verify": True,
        "querystring_auth": False,
        "default_acl": None,
        "file_overwrite": False,
    }


def test_endpoint_optional_for_aws(s3_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("S3_ENDPOINT_URL")
    assert media_storage()["OPTIONS"]["endpoint_url"] is None


@pytest.mark.parametrize("key", ["S3_BUCKET_NAME", "S3_ACCESS_KEY_ID", "S3_SECRET_ACCESS_KEY"])
def test_missing_required_names_key_not_value(
    s3_env: None, monkeypatch: pytest.MonkeyPatch, key: str
) -> None:
    monkeypatch.delenv(key)
    with pytest.raises(ImproperlyConfigured, match=key) as exc:
        media_storage()
    assert "SK" not in str(exc.value)


def test_every_missing_key_is_listed_at_once(s3_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    for key in ("S3_BUCKET_NAME", "S3_ACCESS_KEY_ID", "S3_SECRET_ACCESS_KEY"):
        monkeypatch.delenv(key)
    with pytest.raises(ImproperlyConfigured) as exc:
        media_storage()
    assert all(
        key in str(exc.value) for key in ("S3_BUCKET_NAME", "S3_ACCESS_KEY_ID", "S3_SECRET_ACCESS_KEY")
    )


def test_bad_addressing_style_refused(s3_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("S3_ADDRESSING_STYLE", "sideways")
    with pytest.raises(ImproperlyConfigured, match="S3_ADDRESSING_STYLE"):
        media_storage()


def test_production_keeps_switch(
    load_fresh_production: Callable[[], ModuleType],  # noqa: F811
    s3_env: None,
    production_env: None,  # noqa: F811
) -> None:
    prod = load_fresh_production()
    assert prod.STORAGES["default"]["BACKEND"] == "storages.backends.s3.S3Storage"
    assert (
        prod.STORAGES["staticfiles"]["BACKEND"] == "whitenoise.storage.CompressedManifestStaticFilesStorage"
    )


@pytest.mark.parametrize("key", ["USE_S3", "S3_LOCATION"])
def test_secrets_file_cannot_set_s3_topology(key: str) -> None:
    with pytest.raises(ImproperlyConfigured, match=key):
        parse_secrets(json.dumps({key: "x"}), source="f")


def test_secrets_file_may_set_provider_settings() -> None:
    keys = [
        "S3_ENDPOINT_URL",
        "S3_BUCKET_NAME",
        "S3_REGION_NAME",
        "S3_ADDRESSING_STYLE",
        "S3_VERIFY",
        "PUBLIC_MEDIA_BASE_URL",
    ]
    assert parse_secrets(json.dumps(dict.fromkeys(keys, "x")), source="f") == dict.fromkeys(keys, "x")
