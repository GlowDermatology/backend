import base64
import binascii
from urllib.parse import urlsplit

from django.core.exceptions import ImproperlyConfigured

from config.env import env_bool, env_int, env_json, env_list, env_networks, env_str

from .base import *  # noqa: F403
from .base import OAUTH_MODE, database_config, redis_db_url

DEBUG = False


def _decode_jwt_key(encoded: str) -> bytes:
    return base64.b64decode(encoded + "=" * (-len(encoded) % 4), altchars=b"-_", validate=True)


# Values that were ever committed to this repository (.env.example, CI workflow history). They are public, so
# production refuses to boot with them even though they satisfy the length checks below.
KNOWN_PLACEHOLDER_SECRETS = frozenset(
    {
        "OFMmKSy50zW12DnugPKJmF4b0rNGeIvkm9SNWNjO9NRAN97Dmlr3CJH0tfi4Z6Kr",
        "slqp9tXiEipMGqSzlvPcW9p0hBnle7sDTJSOQKgmZHNHdwYK",
    }
)
KNOWN_PLACEHOLDER_JWT_KEYS = frozenset(
    _decode_jwt_key(encoded)
    for encoded in (
        "4zhPFokAL4QMFGTQpn-XIhwA3i4TYB9SgcekVIfjG2U",  # .env.example
        "oQAQ0h7x354qUJXY9NmXu9SBoKbHt5NF9oKkakDsjdk",  # former CI placeholder
    )
)

SECRET_KEY = env_str("SECRET_KEY")
INTERNAL_API_SECRET = env_str("INTERNAL_API_SECRET")
for name, secret in (("SECRET_KEY", SECRET_KEY), ("INTERNAL_API_SECRET", INTERNAL_API_SECRET)):
    if len(secret) < 32 or "change-me" in secret.lower():
        raise ImproperlyConfigured(f"{name} must be at least 32 characters without placeholders")
    if secret in KNOWN_PLACEHOLDER_SECRETS:
        raise ImproperlyConfigured(f"{name} is a committed placeholder value; generate a real secret")
raw_keys = env_json("JWT_SIGNING_KEYS")
if not isinstance(raw_keys, dict) or not raw_keys:
    raise ImproperlyConfigured("JWT_SIGNING_KEYS must be a nonempty JSON object")
JWT_SIGNING_KEYS = {}
for kid, encoded in raw_keys.items():
    try:
        if not isinstance(encoded, str):
            raise ValueError
        decoded = _decode_jwt_key(encoded)
        if len(decoded) < 32:
            raise ValueError
    except (ValueError, binascii.Error) as exc:
        raise ImproperlyConfigured("JWT_SIGNING_KEYS values must decode to at least 32 bytes") from exc
    if decoded in KNOWN_PLACEHOLDER_JWT_KEYS:
        raise ImproperlyConfigured(
            f"JWT_SIGNING_KEYS[{kid!r}] is a committed placeholder key; generate a new one"
        )
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
# includeSubDomains follows HSTS (deploy check W005). Preload is an explicit opt-in: submitting the host to
# the browser preload lists is close to irreversible, so `check --deploy` keeps warning (W021) until an
# operator sets SECURE_HSTS_PRELOAD=true deliberately.
SECURE_HSTS_INCLUDE_SUBDOMAINS = env_bool("SECURE_HSTS_INCLUDE_SUBDOMAINS", SECURE_HSTS_SECONDS > 0)
SECURE_HSTS_PRELOAD = env_bool("SECURE_HSTS_PRELOAD", False)
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
PUBLIC_MEDIA_BASE_URL = env_str("PUBLIC_MEDIA_BASE_URL")
_media = urlsplit(PUBLIC_MEDIA_BASE_URL)
if _media.scheme not in {"http", "https"} or not _media.netloc or not PUBLIC_MEDIA_BASE_URL.endswith("/"):
    raise ImproperlyConfigured("PUBLIC_MEDIA_BASE_URL must be an http(s) URL ending with /")
# Live staff social sign-in needs every provider credential and the exact callback URL (contract 2.1).
LIVE_OAUTH_VARIABLES = (
    "GOOGLE_OAUTH_CLIENT_ID",
    "GOOGLE_OAUTH_CLIENT_SECRET",
    "FACEBOOK_APP_ID",
    "FACEBOOK_APP_SECRET",
    "OAUTH_REDIRECT_URI",
)
if OAUTH_MODE == "live":
    for name in LIVE_OAUTH_VARIABLES:
        value = env_str(name)  # raises ImproperlyConfigured("<name> is required")
        if "change-me" in value.lower():
            raise ImproperlyConfigured(f"{name} is a placeholder value; set the real credential")
    _redirect = urlsplit(env_str("OAUTH_REDIRECT_URI"))
    if _redirect.scheme not in {"http", "https"} or not _redirect.netloc:
        raise ImproperlyConfigured("OAUTH_REDIRECT_URI must be an absolute http(s) URL")
