"""``upload_to`` callable of the professional photo (``professionals/<id>/<sha256[:16]>.<ext>``).

The service computes the content-addressed basename (``core.media.media_name``) and saves the file in a second
step, once the row has an id; this callable only prefixes the owner directory.
"""

from pathlib import PurePosixPath
from typing import Any


def professional_photo_path(instance: Any, filename: str) -> str:
    return f"professionals/{instance.pk}/{PurePosixPath(filename).name}"
