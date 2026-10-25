import base64
import binascii

from django.core.exceptions import ImproperlyConfigured

from config.env import env_bool, env_int, env_json, env_list, env_networks, env_str

from .base import *  # noqa: F403
from .base import database_config, redis_db_url

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
DATABASES = {"default": database_config(DATABASE_URL, pool=env_bool("DB_POOL", True))}
REDIS_URL = env_str("REDIS_URL")
CELERY_BROKER_URL = env_str("CELERY_BROKER_URL", redis_db_url(REDIS_URL, 1))
ALLOWED_HOSTS = env_list("ALLOWED_HOSTS") + ["localhost"]
ADMIN_API_ALLOWED_NETS = env_networks("ADMIN_API_ALLOWED_NETS")
ADMIN_API_REQUIRE_INTERNAL_SECRET = True
TRUSTED_PROXIES = env_networks("TRUSTED_PROXIES", [])
DJANGO_ADMIN_ENABLED = env_bool("DJANGO_ADMIN_ENABLED", False)
# TLS terminates at the edge proxy; Next calls the API over HTTP on the private network.
SECURE_SSL_REDIRECT = env_bool("SECURE_SSL_REDIRECT", False)
SILENCED_SYSTEM_CHECKS = ["security.W008"]
SECURE_HSTS_SECONDS = env_int("SECURE_HSTS_SECONDS", 0)
# Django's deploy checks (W005/W021) expect both once HSTS is on; the edge proxy forwards the header.
SECURE_HSTS_INCLUDE_SUBDOMAINS = env_bool("SECURE_HSTS_INCLUDE_SUBDOMAINS", SECURE_HSTS_SECONDS > 0)
SECURE_HSTS_PRELOAD = env_bool("SECURE_HSTS_PRELOAD", SECURE_HSTS_SECONDS > 0)
SESSION_COOKIE_SECURE = CSRF_COOKIE_SECURE = True
CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.redis.RedisCache",
        "LOCATION": REDIS_URL,
        "OPTIONS": {"socket_connect_timeout": 1, "socket_timeout": 1},
        "KEY_PREFIX": "gd",
        "VERSION": 1,
    }
}
