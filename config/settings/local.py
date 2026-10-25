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
