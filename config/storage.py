"""The media storage backend: local disk by default, any S3-compatible bucket when ``USE_S3`` is true.

Read from ``os.environ`` through ``config.env``: ``USE_S3`` and ``S3_LOCATION`` are topology keys (the secrets
file cannot set them); the provider settings (endpoint, bucket, keys, region, addressing style, verify) may
come from Vault. Errors name the keys, never a value.
"""

from __future__ import annotations

import os
from typing import Any

from django.core.exceptions import ImproperlyConfigured

from config.env import env_bool, env_str

S3_BACKEND = "storages.backends.s3.S3Storage"
FILESYSTEM_BACKEND = "django.core.files.storage.FileSystemStorage"
ADDRESSING_STYLES = ("path", "virtual", "auto")
REQUIRED_KEYS = ("S3_BUCKET_NAME", "S3_ACCESS_KEY_ID", "S3_SECRET_ACCESS_KEY")


def media_storage() -> dict[str, Any]:
    """The value of ``STORAGES["default"]``."""
    if not env_bool("USE_S3", "false"):
        return {"BACKEND": FILESYSTEM_BACKEND}
    missing = [key for key in REQUIRED_KEYS if not os.environ.get(key)]
    if missing:
        raise ImproperlyConfigured(f"USE_S3 is true, so {', '.join(missing)} must be set")
    addressing_style = env_str("S3_ADDRESSING_STYLE", "path")
    if addressing_style not in ADDRESSING_STYLES:
        raise ImproperlyConfigured(f"S3_ADDRESSING_STYLE must be one of {', '.join(ADDRESSING_STYLES)}")
    return {
        "BACKEND": S3_BACKEND,
        "OPTIONS": {
            "bucket_name": env_str("S3_BUCKET_NAME"),
            "endpoint_url": env_str("S3_ENDPOINT_URL", "") or None,
            "access_key": env_str("S3_ACCESS_KEY_ID"),
            "secret_key": env_str("S3_SECRET_ACCESS_KEY"),
            "region_name": env_str("S3_REGION_NAME", "us-east-1"),
            "addressing_style": addressing_style,
            "location": env_str("S3_LOCATION", "media"),
            "verify": env_bool("S3_VERIFY", "true"),
            # Public media behind PUBLIC_MEDIA_BASE_URL: unsigned URLs, no per-object ACL, never overwrite.
            "querystring_auth": False,
            "default_acl": None,
            "file_overwrite": False,
        },
    }
