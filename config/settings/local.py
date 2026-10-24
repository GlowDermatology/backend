from config.env import env_bool, env_list, env_networks, env_str

from .base import *  # noqa: F403
from .base import SPECTACULAR_SETTINGS as BASE_SPECTACULAR_SETTINGS
from .base import database_config

DEBUG = True
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
DATABASES = {"default": database_config(DATABASE_URL)}
REDIS_URL = env_str("REDIS_URL", "redis://localhost:6380/0")
CELERY_BROKER_URL = REDIS_URL
CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.redis.RedisCache",
        "LOCATION": REDIS_URL,
        "OPTIONS": {"socket_connect_timeout": 1, "socket_timeout": 1},
    }
}
