from pathlib import Path

import pytest
from django.conf import settings

from glowderma.core.tests.s3_storage import s3_storage  # noqa: F401 - the fixture

MEDIA_NAME = "categories/1/abc.webp"


@pytest.fixture
def media_file() -> str:
    """``categories/1/abc.webp`` written under ``MEDIA_ROOT`` (the local disk, not the default storage)."""
    path = Path(settings.MEDIA_ROOT) / MEDIA_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"webp-bytes")
    return MEDIA_NAME
