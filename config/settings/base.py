from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit, urlunsplit

from django.core.exceptions import ImproperlyConfigured

from config.env import Network, env_json, env_str
from glowderma.core.throttling import validate_throttle_rates

BASE_DIR = Path(__file__).resolve().parents[2]
INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "rest_framework",
    "drf_spectacular",
    "drf_spectacular_sidecar",
    "glowderma.core",
    "glowderma.accounts",
]
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "glowderma.core.middleware.RequestIdMiddleware",
    "glowderma.core.clientip.ClientIPMiddleware",
    "glowderma.core.network.AdminNetworkMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]
ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"
AUTH_USER_MODEL = "accounts.StaffUser"
AUTHENTICATION_BACKENDS = ["django.contrib.auth.backends.ModelBackend"]
TIME_ZONE = "Asia/Tashkent"
CELERY_TIMEZONE = TIME_ZONE
USE_TZ = True
LANGUAGE_CODE = "uz"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
DATA_UPLOAD_MAX_MEMORY_SIZE = 6 * 1024 * 1024
STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
MEDIA_URL = "/media/"
MEDIA_ROOT = BASE_DIR / "media"
TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ]
        },
    }
]
PASSWORD_HASHERS = ["django.contrib.auth.hashers.Argon2PasswordHasher"]
AUTH_PASSWORD_VALIDATORS = [
    {
        "NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator",
        "OPTIONS": {"user_attributes": ["email", "full_name"]},
    },
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator", "OPTIONS": {"min_length": 12}},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]
JWT_ISSUER = "glowderma"
STAFF_ACCESS_TTL = 900
STAFF_REFRESH_TTL = 43200
REST_FRAMEWORK = {
    "EXCEPTION_HANDLER": "glowderma.core.api.exceptions.envelope_exception_handler",
    "DEFAULT_PAGINATION_CLASS": "glowderma.core.api.pagination.EnvelopePagination",
    "DEFAULT_AUTHENTICATION_CLASSES": [],
    "DEFAULT_PERMISSION_CLASSES": ["glowderma.core.permissions.DenyAll"],
    "DEFAULT_THROTTLE_CLASSES": [],
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
    "DEFAULT_RENDERER_CLASSES": ["rest_framework.renderers.JSONRenderer"],
    "DATETIME_FORMAT": "%Y-%m-%dT%H:%M:%S%:z",
}
SPECTACULAR_SETTINGS = {
    "TITLE": "Glow Derma API",
    "VERSION": "1.0.0",
    "COMPONENT_SPLIT_REQUEST": True,
    "SERVE_INCLUDE_SCHEMA": False,
    "SWAGGER_UI_DIST": "SIDECAR",
    "SWAGGER_UI_FAVICON_HREF": "SIDECAR",
    "SERVE_AUTHENTICATION": ["glowderma.accounts.authentication.StaffJWTAuthentication"],
    "SERVE_PERMISSIONS": ["glowderma.accounts.permissions.IsActiveStaff"],
}
GIT_SHA = env_str("GIT_SHA", "dev")
TRUSTED_PROXIES: tuple[Network, ...] = ()
# Celery: at-least-once delivery (acks late, requeue on worker loss), one task in flight per worker
# process, hard limits so a stuck provider call cannot hold a worker forever.
CELERY_TASK_ACKS_LATE = True
CELERY_TASK_REJECT_ON_WORKER_LOST = True
CELERY_WORKER_PREFETCH_MULTIPLIER = 1
CELERY_TASK_TIME_LIMIT = 600
CELERY_TASK_SOFT_TIME_LIMIT = 540
CELERY_BROKER_CONNECTION_RETRY_ON_STARTUP = True
CELERY_TASK_DEFAULT_QUEUE = "default"
CELERY_WORKER_HIJACK_ROOT_LOGGER = False  # keep the LOGGING dictConfig below in workers
# Provider adapters: "fake" (in-process stubs) or "live"; per-provider overrides arrive in later slices.
INTEGRATIONS_MODE = env_str("INTEGRATIONS_MODE", "fake")
if INTEGRATIONS_MODE not in {"fake", "live"}:
    raise ImproperlyConfigured("INTEGRATIONS_MODE must be 'fake' or 'live'")
# Throttle overrides (contract §1.9), e.g. THROTTLE_RATES={"login": "10/min"}; validated at startup.
THROTTLE_RATES = validate_throttle_rates(env_json("THROTTLE_RATES", {}))
LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")
LOG_LEVEL = env_str("LOG_LEVEL", "INFO").upper()


def logging_config(app_level: str) -> dict[str, Any]:
    """dictConfig: one console handler, key=value lines, request_id from the request context."""
    if app_level not in LOG_LEVELS:
        raise ImproperlyConfigured(f"LOG_LEVEL must be one of {', '.join(LOG_LEVELS)}")
    return {
        "version": 1,
        "disable_existing_loggers": False,
        "filters": {"request_id": {"()": "glowderma.core.logging.RequestIdFilter"}},
        "formatters": {
            "keyvalue": {
                "format": (
                    "time=%(asctime)s level=%(levelname)s logger=%(name)s "
                    "request_id=%(request_id)s message=%(message)s"
                ),
                "datefmt": "%Y-%m-%dT%H:%M:%S%z",
            }
        },
        "handlers": {
            "console": {
                "class": "logging.StreamHandler",
                "stream": "ext://sys.stdout",
                "filters": ["request_id"],
                "formatter": "keyvalue",
            }
        },
        "root": {"handlers": ["console"], "level": "WARNING"},
        "loggers": {
            "django": {"level": "INFO"},
            "django.request": {"level": "WARNING"},
            "django.security": {"level": "WARNING"},
            "glowderma": {"level": app_level},
            "celery": {"level": "INFO"},
        },
    }


LOGGING = logging_config(LOG_LEVEL)


def redis_db_url(url: str, db: int) -> str:
    """``url`` with its database path replaced by ``/db`` (cache on DB 0, Celery broker on DB 1)."""
    parsed = urlsplit(url)
    if not parsed.scheme.startswith("redis") or not parsed.netloc or db < 0:
        raise ImproperlyConfigured("REDIS_URL must be a redis:// or rediss:// URL")
    return urlunsplit(parsed._replace(path=f"/{db}"))


def database_config(url: str, *, pool: bool = False) -> dict[str, Any]:
    try:
        parsed = urlsplit(url)
        name = unquote(parsed.path.lstrip("/"))
        if parsed.scheme not in {"postgres", "postgresql"} or not name:
            raise ValueError("Expected a PostgreSQL URL with a database name")
        options: dict[str, Any] = {"connect_timeout": 3}
        if pool:
            # psycopg_pool.ConnectionPool kwargs; Django forbids persistent connections with the pool.
            options["pool"] = {"min_size": 1, "max_size": 4, "timeout": 10}
        return {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": name,
            "USER": unquote(parsed.username or ""),
            "PASSWORD": unquote(parsed.password or ""),
            "HOST": parsed.hostname,
            "PORT": parsed.port if parsed.port is not None else 5432,
            "CONN_MAX_AGE": 0,
            "CONN_HEALTH_CHECKS": True,
            "OPTIONS": options,
        }
    except (ValueError, TypeError, AttributeError) as exc:
        raise ImproperlyConfigured(
            "DATABASE_URL must be a valid PostgreSQL URL with a database name"
        ) from exc
