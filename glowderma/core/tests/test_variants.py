from io import BytesIO
from typing import Any

import pytest
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from PIL import Image

from glowderma.core.variants import delete_with_variants, store_variants


@pytest.fixture
def jpeg_bytes() -> bytes:
    stream = BytesIO()
    Image.new("RGB", (1200, 800), (200, 120, 90)).save(stream, format="JPEG")
    return stream.getvalue()


def _files(directory: str) -> list[str]:
    if not default_storage.exists(directory):
        return []
    return sorted(default_storage.listdir(directory)[1])


def test_store_variants_writes_both(jpeg_bytes: bytes) -> None:
    name = "variants-a/1/aaaaaaaaaaaaaaaa.jpg"
    written = store_variants(default_storage, name, jpeg_bytes)
    assert written == ["variants-a/1/aaaaaaaaaaaaaaaa_400.webp", "variants-a/1/aaaaaaaaaaaaaaaa_1000.webp"]
    assert all(default_storage.exists(path) for path in written)


def test_store_variants_keeps_an_existing_file_and_leaves_no_stray(jpeg_bytes: bytes) -> None:
    name = "variants-b/1/aaaaaaaaaaaaaaaa.jpg"
    store_variants(default_storage, name, jpeg_bytes)
    assert store_variants(default_storage, name, jpeg_bytes) == []
    assert _files("variants-b/1") == ["aaaaaaaaaaaaaaaa_1000.webp", "aaaaaaaaaaaaaaaa_400.webp"]


def test_store_variants_removes_a_renamed_file(jpeg_bytes: bytes, monkeypatch: pytest.MonkeyPatch) -> None:
    # exists() said "no" but save() still stored under another name (a race): the stray must go
    real_save = default_storage.save

    def save(name: str, content: Any, max_length: int | None = None) -> str:
        return real_save(name.replace(".webp", "_stray.webp"), content)

    monkeypatch.setattr(default_storage, "save", save)
    assert store_variants(default_storage, "variants-c/1/aaaaaaaaaaaaaaaa.jpg", jpeg_bytes) == []
    monkeypatch.undo()
    assert _files("variants-c/1") == []


def test_store_variants_cleans_up_when_the_second_write_fails(
    jpeg_bytes: bytes, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_save, calls = default_storage.save, []

    def save(name: str, content: Any, max_length: int | None = None) -> str:
        calls.append(name)
        if len(calls) == 2:
            raise OSError("disk full")
        return real_save(name, content, max_length=max_length)

    monkeypatch.setattr(default_storage, "save", save)
    with pytest.raises(OSError, match="disk full"):
        store_variants(default_storage, "variants-d/1/aaaaaaaaaaaaaaaa.jpg", jpeg_bytes)
    monkeypatch.undo()
    assert _files("variants-d/1") == []


def test_delete_with_variants(jpeg_bytes: bytes) -> None:
    name = "variants-e/1/aaaaaaaaaaaaaaaa.jpg"
    default_storage.save(name, ContentFile(jpeg_bytes))
    store_variants(default_storage, name, jpeg_bytes)
    delete_with_variants(default_storage, name)
    assert _files("variants-e/1") == []
    delete_with_variants(default_storage, name)  # missing files: no error
    delete_with_variants(default_storage, "")  # empty name: nothing


def test_delete_with_variants_leaves_a_sibling_with_a_collision_suffix(jpeg_bytes: bytes) -> None:
    first = default_storage.save("variants-f/1/aaaaaaaaaaaaaaaa.jpg", ContentFile(jpeg_bytes))
    second = default_storage.save("variants-f/1/aaaaaaaaaaaaaaaa.jpg", ContentFile(jpeg_bytes))
    assert first != second
    store_variants(default_storage, first, jpeg_bytes)
    store_variants(default_storage, second, jpeg_bytes)
    delete_with_variants(default_storage, first)
    remaining = _files("variants-f/1")
    assert len(remaining) == 3
    assert all(path.startswith(second.rsplit("/", 1)[1].rsplit(".", 1)[0]) for path in remaining)
