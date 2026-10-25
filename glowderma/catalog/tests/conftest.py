from collections.abc import Iterator
from pathlib import Path

import pytest
from django.test import override_settings


@pytest.fixture(autouse=True)
def media_root(tmp_path: Path) -> Iterator[None]:
    """Image factories and the seeder write real files: keep them out of the repository's MEDIA_ROOT."""
    with override_settings(MEDIA_ROOT=tmp_path / "media"):
        yield
