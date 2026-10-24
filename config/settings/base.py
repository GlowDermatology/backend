from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

from django.core.exceptions import ImproperlyConfigured

from config.env import Network, env_str

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


def database_config(url: str) -> dict[str, Any]:
    try:
        parsed = urlsplit(url)
        name = unquote(parsed.path.lstrip("/"))
        if parsed.scheme not in {"postgres", "postgresql"} or not name:
            raise ValueError("Expected a PostgreSQL URL with a database name")
        return {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": name,
            "USER": unquote(parsed.username or ""),
            "PASSWORD": unquote(parsed.password or ""),
            "HOST": parsed.hostname,
            "PORT": parsed.port if parsed.port is not None else 5432,
            "OPTIONS": {"connect_timeout": 3},
        }
    except (ValueError, TypeError, AttributeError) as exc:
        raise ImproperlyConfigured(
            "DATABASE_URL must be a valid PostgreSQL URL with a database name"
        ) from exc
