from config.env import env_str

from .local import *  # noqa: F403

CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}
CELERY_TASK_ALWAYS_EAGER = True
CELERY_TASK_EAGER_PROPAGATES = True
CELERY_BROKER_URL = env_str("CELERY_BROKER_URL", "redis://localhost:6380/0")
PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
JWT_SIGNING_KEYS = {"test": b"test-jwt-signing-key-at-least-32-bytes"}
JWT_ACTIVE_KID = "test"
INTERNAL_API_SECRET = "test-internal-secret-at-least-32-characters"  # noqa: S105
