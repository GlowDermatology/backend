import re

from django.core.exceptions import ImproperlyConfigured

from config.env import env_bool, env_datetime, env_list, env_networks, env_str

from .base import *  # noqa: F403
from .base import SPECTACULAR_SETTINGS as BASE_SPECTACULAR_SETTINGS
from .base import database_config, logging_config, redis_db_url

DEBUG = True
FAKE_NOW = env_datetime("FAKE_NOW", None)
SPECTACULAR_SETTINGS = {
    **BASE_SPECTACULAR_SETTINGS,
    "SERVE_AUTHENTICATION": [],
    "SERVE_PERMISSIONS": ["rest_framework.permissions.AllowAny"],
}
SECRET_KEY = env_str("SECRET_KEY", "local-development-key-at-least-32-characters")
INTERNAL_API_SECRET = env_str("INTERNAL_API_SECRET", "local-internal-secret-at-least-32-characters")
JWT_SIGNING_KEYS = {"dev": b"local-jwt-signing-key-at-least-32-bytes"}
JWT_ACTIVE_KID = "dev"
DJANGO_ADMIN_ENABLED = env_bool("DJANGO_ADMIN_ENABLED", True)
ALLOWED_HOSTS = env_list("ALLOWED_HOSTS", ["localhost", "127.0.0.1", "testserver"])
ADMIN_API_ALLOWED_NETS = env_networks("ADMIN_API_ALLOWED_NETS", ["0.0.0.0/0", "::/0"])
ADMIN_API_REQUIRE_INTERNAL_SECRET = False
TRUSTED_PROXIES = env_networks("TRUSTED_PROXIES", [])
OPS_ADMIN_ALLOWED_NETS = env_networks("OPS_ADMIN_ALLOWED_NETS", ["0.0.0.0/0", "::/0"])
DATABASE_URL = env_str("DATABASE_URL", "postgres://glowderma:glowderma@localhost:5433/glowderma")
DATABASES = {"default": database_config(DATABASE_URL, pool=env_bool("DB_POOL", False))}
REDIS_URL = env_str("REDIS_URL", "redis://localhost:6380/0")
CELERY_BROKER_URL = env_str("CELERY_BROKER_URL", redis_db_url(REDIS_URL, 1))
# A local runserver has no worker by default: run tasks inline (outbox dispatch on commit, Billz import
# jobs, campaign batches) so the fake-provider stack works end to end. Set CELERY_TASK_ALWAYS_EAGER=false and
# start a worker and a beat process (DJANGO_SETTINGS_MODULE=config.settings.local) to exercise the real queue;
# beat-only work (scheduled campaigns, reservation expiry) always needs the beat process.
CELERY_TASK_ALWAYS_EAGER = env_bool("CELERY_TASK_ALWAYS_EAGER", True)
LOGGING = logging_config(env_str("LOG_LEVEL", "DEBUG").upper())
CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.redis.RedisCache",
        "LOCATION": REDIS_URL,
        "OPTIONS": {"socket_connect_timeout": 1, "socket_timeout": 1},
        "KEY_PREFIX": "gd",
        "VERSION": 1,
    }
}
# --- Customer API (M2): development defaults and the switches only local/test settings may honour ---
# Every default below is a committed value: config.settings.production lists it in KNOWN_PLACEHOLDER_SECRETS
# and refuses to boot with it.
DEV_FEATURES_ALLOWED = True
LOCAL_KEY_DEFAULTS = {
    "SMS_CODE_HMAC_KEY": "local-sms-code-hmac-key-at-least-32-characters",
    "SECRETBOX_KEY": "Z2xvd2Rlcm1hLWxvY2FsLXNlY3JldGJveC1rZXktMzI",  # base64url of 32 bytes
    "LOG_PII_KEY": "local-log-pii-key-at-least-32-characters",
    "PAYMENT_REF_KEY": "local-payment-ref-key-at-least-32-characters",
    "QR_HMAC_KEY": "local-qr-hmac-key-at-least-32-characters",
    "BTS_WEBHOOK_SECRET": "local-bts-webhook-secret-at-least-32-characters",
    "CLICK_SECRET_KEY": "local-click-secret-key",
    "PAYME_KEY": "local-payme-key",
}
SMS_CODE_HMAC_KEY = env_str("SMS_CODE_HMAC_KEY", LOCAL_KEY_DEFAULTS["SMS_CODE_HMAC_KEY"])
SECRETBOX_KEY = env_str("SECRETBOX_KEY", LOCAL_KEY_DEFAULTS["SECRETBOX_KEY"])
LOG_PII_KEY = env_str("LOG_PII_KEY", LOCAL_KEY_DEFAULTS["LOG_PII_KEY"])
PAYMENT_REF_KEY = env_str("PAYMENT_REF_KEY", LOCAL_KEY_DEFAULTS["PAYMENT_REF_KEY"])
QR_HMAC_KEY = env_str("QR_HMAC_KEY", LOCAL_KEY_DEFAULTS["QR_HMAC_KEY"])
BTS_WEBHOOK_SECRET = env_str("BTS_WEBHOOK_SECRET", LOCAL_KEY_DEFAULTS["BTS_WEBHOOK_SECRET"])
CLICK_SECRET_KEY = env_str("CLICK_SECRET_KEY", LOCAL_KEY_DEFAULTS["CLICK_SECRET_KEY"])
PAYME_KEY = env_str("PAYME_KEY", LOCAL_KEY_DEFAULTS["PAYME_KEY"])
# Fixed OTP for black-box e2e (C§2.14.1); never read by the production settings, which refuse the variable.
ESKIZ_FAKE_CODE = env_str("ESKIZ_FAKE_CODE", "123456")
if not re.fullmatch(r"\d{6}", ESKIZ_FAKE_CODE):
    raise ImproperlyConfigured("ESKIZ_FAKE_CODE must be six digits")
# Seeded POS devices "name:key,…" (C§2.10); key format gdpos_<8-char prefix>_<32 base64url chars>.
POS_API_KEY_PATTERN = re.compile(r"^gdpos_[A-Za-z0-9]{8}_[A-Za-z0-9_-]{32}$")
POS_API_KEYS = []
for _entry in env_list("POS_API_KEYS", []):
    _name, _sep, _key = _entry.rpartition(":")
    if not _sep or not _name.strip() or not POS_API_KEY_PATTERN.fullmatch(_key.strip()):
        raise ImproperlyConfigured("POS_API_KEYS must be comma separated name:gdpos_<prefix>_<secret> pairs")
    POS_API_KEYS.append((_name.strip(), _key.strip()))
