"""
Build-only Django settings.

Used exclusively during Docker image builds
for collectstatic.

NEVER use these settings at runtime.
"""

import os
import secrets

# Temporary values required by production settings.
# Must be configured before importing production.py.

os.environ.setdefault(
    "PUBLIC_MEDIA_BASE_URL",
    "https://media.invalid/media/",
)

_SECRET_KEYS = (
    "SMS_CODE_HMAC_KEY",
    "LOG_PII_KEY",
    "PAYMENT_REF_KEY",
    "QR_HMAC_KEY",
    "BTS_WEBHOOK_SECRET",
    "CLICK_SECRET_KEY",
    "PAYME_KEY",
)

for key in _SECRET_KEYS:
    os.environ.setdefault(key, secrets.token_hex(32))

# Production requires base64url encoding of exactly 32 bytes for this key.
os.environ.setdefault("SECRETBOX_KEY", secrets.token_urlsafe(32))

os.environ.setdefault(
    "SECRET_KEY",
    "build-only-build-only-build-only-0000",
)

os.environ.setdefault(
    "JWT_SIGNING_KEYS",
    '{"build":"YnVpbGQtb25seS1rZXktbm90LXVzZWQtYXQtcnVudGltZSEh"}',
)

os.environ.setdefault("JWT_ACTIVE_KID", "build")

os.environ.setdefault(
    "INTERNAL_API_SECRET",
    "build-only-build-only-build-only-0000",
)

os.environ.setdefault(
    "DATABASE_URL",
    "postgres://b:b@localhost:5432/b",
)

os.environ.setdefault(
    "REDIS_URL",
    "redis://localhost:6379/0",
)

os.environ.setdefault("ALLOWED_HOSTS", "localhost")

os.environ.setdefault(
    "ADMIN_API_ALLOWED_NETS",
    "127.0.0.0/8",
)

# Import production settings after configuring
# the build-only environment.
from .production import *  # noqa: E402, F401, F403
