"""Upload paths of the accounts module (``MEDIA_ROOT/<kind>/<owner id>/<name>``).

The storage name itself comes from ``core.media.media_name`` (content hash + extension) in the upload
service; ``upload_to`` only places it under the owner's folder.
"""

from pathlib import PurePosixPath
from typing import Any

CUSTOMER_IMAGE_KIND = "customers"


def customer_image_path(instance: Any, filename: str) -> str:
    owner = instance.pk if instance.pk is not None else "new"
    return f"{CUSTOMER_IMAGE_KIND}/{owner}/{PurePosixPath(filename).name}"
