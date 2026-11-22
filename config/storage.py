"""Build the default storage configuration for an S3-compatible bucket."""

from __future__ import annotations

import os
from typing import Any

from django.core.exceptions import ImproperlyConfigured

from config.env import env_bool, env_str

S3_BACKEND = "storages.backends.s3.S3Storage"
ADDRESSING_STYLES = ("path", "virtual", "auto")
REQUIRED_KEYS = ("S3_BUCKET_NAME", "S3_ACCESS_KEY_ID", "S3_SECRET_ACCESS_KEY")


def media_storage(defaults: dict[str, str] | None = None) -> dict[str, Any]:
    """The value of ``STORAGES["default"]``."""
    defaults = defaults or {}
    required = {key: os.environ.get(key) or defaults.get(key) for key in REQUIRED_KEYS}
    missing = [key for key, value in required.items() if not value]
    if missing:
        raise ImproperlyConfigured(f"S3 media requires {', '.join(missing)}")
    addressing_style = env_str("S3_ADDRESSING_STYLE", "path")
    if addressing_style not in ADDRESSING_STYLES:
        raise ImproperlyConfigured(f"S3_ADDRESSING_STYLE must be one of {', '.join(ADDRESSING_STYLES)}")
    return {
        "BACKEND": S3_BACKEND,
        "OPTIONS": {
            "bucket_name": required["S3_BUCKET_NAME"],
            "endpoint_url": env_str("S3_ENDPOINT_URL", defaults.get("S3_ENDPOINT_URL", "")) or None,
            "access_key": required["S3_ACCESS_KEY_ID"],
            "secret_key": required["S3_SECRET_ACCESS_KEY"],
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
