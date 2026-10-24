import base64
import binascii

from django.core.exceptions import ImproperlyConfigured

from config.env import env_bool, env_json, env_list, env_str

from .base import *  # noqa: F403
from .base import database_config

DEBUG = False
SECRET_KEY = env_str("SECRET_KEY")
INTERNAL_API_SECRET = env_str("INTERNAL_API_SECRET")
for name, secret in (("SECRET_KEY", SECRET_KEY), ("INTERNAL_API_SECRET", INTERNAL_API_SECRET)):
    if len(secret) < 32 or "change-me" in secret.lower():
        raise ImproperlyConfigured(f"{name} must be at least 32 characters without placeholders")
raw_keys = env_json("JWT_SIGNING_KEYS")
if not isinstance(raw_keys, dict) or not raw_keys:
    raise ImproperlyConfigured("JWT_SIGNING_KEYS must be a nonempty JSON object")
JWT_SIGNING_KEYS = {}
for kid, encoded in raw_keys.items():
    try:
        if not isinstance(encoded, str):
            raise ValueError
        decoded = base64.b64decode(encoded + "=" * (-len(encoded) % 4), altchars=b"-_", validate=True)
        if len(decoded) < 32:
            raise ValueError
    except (ValueError, binascii.Error) as exc:
        raise ImproperlyConfigured("JWT_SIGNING_KEYS values must decode to at least 32 bytes") from exc
    JWT_SIGNING_KEYS[kid] = decoded
JWT_ACTIVE_KID = env_str("JWT_ACTIVE_KID")
if JWT_ACTIVE_KID not in JWT_SIGNING_KEYS:
    raise ImproperlyConfigured("JWT_ACTIVE_KID must identify a signing key")
DATABASE_URL = env_str("DATABASE_URL")
DATABASES = {"default": database_config(DATABASE_URL)}
REDIS_URL = env_str("REDIS_URL")
CELERY_BROKER_URL = REDIS_URL
ALLOWED_HOSTS = env_list("ALLOWED_HOSTS")
ADMIN_API_ALLOWED_NETS = env_list("ADMIN_API_ALLOWED_NETS")
DJANGO_ADMIN_ENABLED = env_bool("DJANGO_ADMIN_ENABLED", False)
CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.redis.RedisCache",
        "LOCATION": REDIS_URL,
        "OPTIONS": {"socket_connect_timeout": 1, "socket_timeout": 1},
    }
}
