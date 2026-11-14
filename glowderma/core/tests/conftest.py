from collections.abc import Iterator
from pathlib import Path

import pytest
from django.test import override_settings

from glowderma.core.tests.s3_storage import s3_storage  # noqa: F401 - the fixture

MEDIA_NAME = "categories/1/abc.webp"


@pytest.fixture
def media_file(tmp_path: Path) -> Iterator[str]:
    """``categories/1/abc.webp`` alone under a fresh ``MEDIA_ROOT`` (the local disk, not the default storage):
    the session-wide one holds every file earlier tests wrote, and ``sync_media_to_s3`` walks all of it."""
    with override_settings(MEDIA_ROOT=tmp_path):
        path = tmp_path / MEDIA_NAME
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"webp-bytes")
        yield MEDIA_NAME
