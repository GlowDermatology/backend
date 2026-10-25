from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit, urlunsplit

from django.core.exceptions import ImproperlyConfigured

from config.env import Network, env_bool, env_int, env_json, env_str
from glowderma.core.enums import enum_name_overrides
from glowderma.core.throttling import validate_throttle_rates

BASE_DIR = Path(__file__).resolve().parents[2]
INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.postgres",
    "rest_framework",
    "drf_spectacular",
    "drf_spectacular_sidecar",
    # Layer order (AGENTS.md "Architecture rules"); migrations depend across apps in this order.
    "glowderma.core",
    "glowderma.integrations",
    "glowderma.accounts",
    "glowderma.site_config",
    "glowderma.catalog",
    "glowderma.professionals",
    "glowderma.loyalty",
    "glowderma.payments",
    "glowderma.delivery",
    "glowderma.commerce",
    "glowderma.appointments",
    "glowderma.notifications",
    "glowderma.customers",
    "glowderma.reporting",
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
    # DRF's defaults with the JSON parser swapped for the one that refuses unpaired surrogates.
    "DEFAULT_PARSER_CLASSES": [
        "glowderma.core.api.parsers.JSONParser",
        "rest_framework.parsers.FormParser",
        "rest_framework.parsers.MultiPartParser",
    ],
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
# A pinned clock for demo data and browser e2e checks (glowderma.core.timeutils.now). Local/test settings read
# the FAKE_NOW variable; production refuses it.
FAKE_NOW: datetime | None = None
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
# --- Staff social sign-in (contract 2.1, M0b) ---
# OAUTH_MODE: "fake" (deterministic in-process providers, see integrations.oauth.fake) or "live"; defaults
# to INTEGRATIONS_MODE. Production requires the credentials and OAUTH_REDIRECT_URI when live.
OAUTH_MODE = env_str("OAUTH_MODE", INTEGRATIONS_MODE)
if OAUTH_MODE not in {"fake", "live"}:
    raise ImproperlyConfigured("OAUTH_MODE must be 'fake' or 'live'")
GOOGLE_OAUTH_CLIENT_ID = env_str("GOOGLE_OAUTH_CLIENT_ID", "")
GOOGLE_OAUTH_CLIENT_SECRET = env_str("GOOGLE_OAUTH_CLIENT_SECRET", "")
# Restrict Google sign-in to one Workspace domain (the ID token's ``hd`` claim); empty = any Google account.
GOOGLE_ALLOWED_HD = env_str("GOOGLE_ALLOWED_HD", "")
# First-use auto-link of a Google identity to the active staff user with the same email (Google-only, and
# only when Google is authoritative for the address: @gmail.com or hd == the email's domain). Default off.
STAFF_GOOGLE_AUTOLINK = env_bool("STAFF_GOOGLE_AUTOLINK", False)
FACEBOOK_APP_ID = env_str("FACEBOOK_APP_ID", "")
FACEBOOK_APP_SECRET = env_str("FACEBOOK_APP_SECRET", "")
FACEBOOK_GRAPH_VERSION = env_str("FACEBOOK_GRAPH_VERSION", "v21.0")
# The Next callback URL; when set, the body's redirect_uri must equal it (400 otherwise). Empty = not checked.
OAUTH_REDIRECT_URI = env_str("OAUTH_REDIRECT_URI", "")
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


# --- Media and schema (core-api) ---
# Contract 1.4: media URLs are absolute and built from this base, never from the request Host.
PUBLIC_MEDIA_BASE_URL = env_str("PUBLIC_MEDIA_BASE_URL", "http://127.0.0.1:8000/media/")
FILE_UPLOAD_PERMISSIONS = 0o644
FILE_UPLOAD_MAX_MEMORY_SIZE = 6 * 1024 * 1024
SPECTACULAR_SETTINGS["ENUM_NAME_OVERRIDES"] = {
    **enum_name_overrides(),
    "HealthStatusEnum": ["ok", "error"],
    "SocialProviderEnum": ["google", "facebook"],
}
# --- Billz adapter (contract 2.3, M1a) ---
# BILLZ_MODE: "fake" (deterministic in-process catalog, see integrations.billz.fake) or "live" (M3); defaults
# to INTEGRATIONS_MODE.
BILLZ_MODE = env_str("BILLZ_MODE", INTEGRATIONS_MODE)
if BILLZ_MODE not in {"fake", "live"}:
    raise ImproperlyConfigured("BILLZ_MODE must be 'fake' or 'live'")
# --- Notifications: FCM adapter and campaign batching (contract 2.8, M1d) ---
# FCM_MODE: "fake" (deterministic in-process push adapter, see integrations.fcm.fake; tokens ending in "-bad"
# fail) or "live" (M3); defaults to INTEGRATIONS_MODE. CAMPAIGN_BATCH_SIZE is the number of recipients one
# ``campaign.send_batch`` outbox message hands to the adapter (design 03 §3.4, default 500).
FCM_MODE = env_str("FCM_MODE", INTEGRATIONS_MODE)
if FCM_MODE not in {"fake", "live"}:
    raise ImproperlyConfigured("FCM_MODE must be 'fake' or 'live'")
CAMPAIGN_BATCH_SIZE = env_int("CAMPAIGN_BATCH_SIZE", 500)
if CAMPAIGN_BATCH_SIZE < 1:
    raise ImproperlyConfigured("CAMPAIGN_BATCH_SIZE must be a positive integer")
