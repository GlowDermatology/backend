from config.env import env_bool, env_list, env_str

from .base import *  # noqa: F403
from .base import database_config

DEBUG = True
SECRET_KEY = env_str("SECRET_KEY", "local-development-key-at-least-32-characters")
INTERNAL_API_SECRET = env_str("INTERNAL_API_SECRET", "local-internal-secret-at-least-32-characters")
JWT_SIGNING_KEYS = {"dev": b"local-jwt-signing-key-at-least-32-bytes"}
JWT_ACTIVE_KID = "dev"
DJANGO_ADMIN_ENABLED = env_bool("DJANGO_ADMIN_ENABLED", True)
ALLOWED_HOSTS = env_list("ALLOWED_HOSTS", ["localhost", "127.0.0.1", "testserver"])
ADMIN_API_ALLOWED_NETS = env_list("ADMIN_API_ALLOWED_NETS", ["127.0.0.0/8", "::1/128"])
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
