"""``upload_to`` of the campaign image (``MEDIA_ROOT/campaigns/<campaign id>/<name>``).

The storage name itself comes from ``core.media.media_name`` (content hash + extension) in the service;
``upload_to`` only places it under the owner's folder. The file is saved in a second step, after the row has
an id (as ``catalog`` does for banners).
"""

from pathlib import PurePosixPath
from typing import Any

CAMPAIGN_IMAGE_KIND = "campaigns"


def campaign_image_path(instance: Any, filename: str) -> str:
    owner = instance.pk if instance.pk is not None else "new"
    return f"{CAMPAIGN_IMAGE_KIND}/{owner}/{PurePosixPath(filename).name}"
