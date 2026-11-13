"""The ``s3_storage`` fixture: the default storage on a moto-mocked bucket, built by ``media_storage()``.

Imported by the ``conftest.py`` of every test package that needs it.
"""

from collections.abc import Iterator
from typing import Any

import boto3  # type: ignore[import-untyped]
import pytest
from django.conf import settings
from django.core.files.storage import default_storage
from django.test import override_settings
from moto import mock_aws

from config.storage import media_storage

BUCKET = "glowderma-test"


@pytest.fixture
def s3_storage(monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    for key, value in {
        "USE_S3": "true",
        "S3_BUCKET_NAME": BUCKET,
        "S3_ACCESS_KEY_ID": "test-access-key",
        "S3_SECRET_ACCESS_KEY": "test-secret-key",  # noqa: S105 - test-only
    }.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("S3_ENDPOINT_URL", raising=False)
    with mock_aws():
        boto3.client("s3", region_name="us-east-1").create_bucket(Bucket=BUCKET)
        with override_settings(STORAGES={**settings.STORAGES, "default": media_storage()}):
            yield default_storage
