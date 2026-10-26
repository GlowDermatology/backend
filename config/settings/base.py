import re
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit, urlunsplit

from django.core.exceptions import ImproperlyConfigured

from config.env import Network, env_bool, env_int, env_json, env_list, env_networks, env_str
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
# to INTEGRATIONS_MODE. Production requires the credentials and each provider's redirect URI when live.
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
# The Next BFF callback URL of each provider (/admin/auth/oauth/{provider}/callback); when set, the body's
# redirect_uri must equal it (400 otherwise). Empty = not checked. OAUTH_REDIRECT_URI is the deprecated single
# value of M0b, kept for one release as the fallback of both.
OAUTH_REDIRECT_URI = env_str("OAUTH_REDIRECT_URI", "")
GOOGLE_OAUTH_REDIRECT_URI = env_str("GOOGLE_OAUTH_REDIRECT_URI", "") or OAUTH_REDIRECT_URI
FACEBOOK_OAUTH_REDIRECT_URI = env_str("FACEBOOK_OAUTH_REDIRECT_URI", "") or OAUTH_REDIRECT_URI
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
            # httpx logs every request URL at INFO; provider query strings can carry a phone number (Billz
            # GET /v1/client?phone_number=…), so its request lines never reach the logs.
            "httpx": {"level": "WARNING"},
            "httpcore": {"level": "WARNING"},
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
# --- Billz adapter (contract 2.3, M1a; live client M3, docs/integrations/billz.md) ---
# BILLZ_MODE: "fake" (deterministic in-process catalog, see integrations.billz.fake) or "live"
# (integrations.billz.live, the BILLZ 2.0 public API); defaults to INTEGRATIONS_MODE.
BILLZ_MODE = env_str("BILLZ_MODE", INTEGRATIONS_MODE)
if BILLZ_MODE not in {"fake", "live"}:
    raise ImproperlyConfigured("BILLZ_MODE must be 'fake' or 'live'")


def env_positive_float(name: str, default: float) -> float:
    try:
        value = float(env_str(name, default))
    except ValueError as exc:
        raise ImproperlyConfigured(f"{name} must be a number") from exc
    if not value > 0 or value == float("inf"):
        raise ImproperlyConfigured(f"{name} must be a positive number")
    return value


# Live client only; the fake needs none of these (config.settings.production requires the ids and the key when
# BILLZ_MODE=live). The base URL and the platform id are the documented public values of openapi.yaml.
BILLZ_BASE_URL = env_str("BILLZ_BASE_URL", "https://api-admin.billz.ai").rstrip("/")
if urlsplit(BILLZ_BASE_URL).scheme not in {"http", "https"} or not urlsplit(BILLZ_BASE_URL).netloc:
    raise ImproperlyConfigured("BILLZ_BASE_URL must be an absolute http(s) URL")
BILLZ_SECRET_KEY = env_str(
    "BILLZ_SECRET_KEY", ""
)  # the integration key (Settings → Company → Integration keys)
BILLZ_PLATFORM_ID = env_str("BILLZ_PLATFORM_ID", "7d4a4c38-dd84-4902-b744-0488b80a4c01")
BILLZ_SHOP_ID = env_str(
    "BILLZ_SHOP_ID", ""
)  # the shop whose stock and price the catalog shows (GET /v1/shop)
BILLZ_CASHBOX_ID = env_str(
    "BILLZ_CASHBOX_ID", ""
)  # the cash box online sales are created on (GET /v1/cash-box)
BILLZ_PAYMENT_TYPE_ID = env_str(
    "BILLZ_PAYMENT_TYPE_ID", ""
)  # online payment type (GET /v1/company-payment-type)
# Requests per second shared by every worker (Redis cache); Billz answers 429 above ~2/s per IP.
BILLZ_RATE_LIMIT_PER_SECOND = env_positive_float("BILLZ_RATE_LIMIT_PER_SECOND", 2.0)
BILLZ_CONNECT_TIMEOUT_SECONDS = env_positive_float("BILLZ_CONNECT_TIMEOUT_SECONDS", 5.0)
BILLZ_READ_TIMEOUT_SECONDS = env_positive_float("BILLZ_READ_TIMEOUT_SECONDS", 30.0)
# GET /v2/products page size (Billz refuses limit > 300).
BILLZ_PAGE_SIZE = env_int("BILLZ_PAGE_SIZE", 300)
if not 1 <= BILLZ_PAGE_SIZE <= 300:
    raise ImproperlyConfigured("BILLZ_PAGE_SIZE must be between 1 and 300")
# A full sync hides missing products only when Billz listed at least this share of the previous full count.
BILLZ_FULL_SYNC_MIN_RATIO = env_positive_float("BILLZ_FULL_SYNC_MIN_RATIO", 0.8)
if BILLZ_FULL_SYNC_MIN_RATIO > 1:
    raise ImproperlyConfigured("BILLZ_FULL_SYNC_MIN_RATIO must be in (0, 1]")
# Name of the Billz product attribute or custom field holding the volume ("30 ml"); empty = volume not synced.
BILLZ_VOLUME_FIELD = env_str("BILLZ_VOLUME_FIELD", "")
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
# --- Orders, payments and delivery adapters (contract 2.2 / 2.12, M1c) ---
# CLICK_MODE / PAYME_MODE / BTS_MODE: "fake" (deterministic in-process providers driven by Order.metadata
# ["fake"], see integrations.{click,payme,bts}.fake) or "live" (M3); each defaults to INTEGRATIONS_MODE.
CLICK_MODE = env_str("CLICK_MODE", INTEGRATIONS_MODE)
PAYME_MODE = env_str("PAYME_MODE", INTEGRATIONS_MODE)
BTS_MODE = env_str("BTS_MODE", INTEGRATIONS_MODE)
if CLICK_MODE not in {"fake", "live"}:
    raise ImproperlyConfigured("CLICK_MODE must be 'fake' or 'live'")
if PAYME_MODE not in {"fake", "live"}:
    raise ImproperlyConfigured("PAYME_MODE must be 'fake' or 'live'")
if BTS_MODE not in {"fake", "live"}:
    raise ImproperlyConfigured("BTS_MODE must be 'fake' or 'live'")
# --- Customer API (M2, customer contract; plan §7) ---
# A settings-module constant, never an environment variable: True only in config.settings.local (and therefore
# test). It gates every development switch (fixed OTP, dev simulate, scenario header, seeded POS keys, the
# OAuth fakes' "*" nonce) and decides what a fake provider may do (core.integration_modes.fake_in_production).
DEV_FEATURES_ALLOWED = False


def env_positive_int(name: str, default: int) -> int:
    value = env_int(name, default)
    if value < 1:
        raise ImproperlyConfigured(f"{name} must be a positive integer")
    return value


def env_non_negative_int(name: str, default: int) -> int:
    value = env_int(name, default)
    if value < 0:
        raise ImproperlyConfigured(f"{name} must be zero or a positive integer")
    return value


def env_mode(name: str) -> str:
    value = env_str(name, INTEGRATIONS_MODE)
    if value not in {"fake", "live"}:
        raise ImproperlyConfigured(f"{name} must be 'fake' or 'live'")
    return value


APP_VERSION_PATTERN = re.compile(r"^[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}$")


def env_app_version(name: str) -> str:
    value = env_str(name, "0.0.0")
    if not APP_VERSION_PATTERN.fullmatch(value):
        raise ImproperlyConfigured(f"{name} must look like 1.2.3")
    return value


# Sessions (C§1.9, §2.1.6): access JWT lifetime, sliding refresh window, refresh grace and re-rotation
# windows.
CUSTOMER_ACCESS_TTL = env_positive_int("CUSTOMER_ACCESS_TTL", 900)
CUSTOMER_REFRESH_TTL_DAYS = env_positive_int("CUSTOMER_REFRESH_TTL_DAYS", 60)
CUSTOMER_REFRESH_GRACE_SECONDS = env_positive_int("CUSTOMER_REFRESH_GRACE_SECONDS", 120)
CUSTOMER_REFRESH_UNCONFIRMED_SECONDS = env_positive_int("CUSTOMER_REFRESH_UNCONFIRMED_SECONDS", 600)
# OTP policy (A3, C§2.1.1).
CUSTOMER_OTP_TTL_SECONDS = env_positive_int("CUSTOMER_OTP_TTL_SECONDS", 120)
CUSTOMER_OTP_RESEND_SECONDS = env_positive_int("CUSTOMER_OTP_RESEND_SECONDS", 60)
CUSTOMER_OTP_MAX_ATTEMPTS = env_positive_int("CUSTOMER_OTP_MAX_ATTEMPTS", 5)
CUSTOMER_OTP_LOCKOUT_MINUTES = env_positive_int("CUSTOMER_OTP_LOCKOUT_MINUTES", 15)
CUSTOMER_OTP_DAILY_LIMIT = env_positive_int("CUSTOMER_OTP_DAILY_LIMIT", 10)
CUSTOMER_OTP_DAILY_WRONG_LIMIT = env_positive_int("CUSTOMER_OTP_DAILY_WRONG_LIMIT", 10)
CUSTOMER_OTP_DEVICE_DAILY_LIMIT = env_positive_int("CUSTOMER_OTP_DEVICE_DAILY_LIMIT", 10)
CUSTOMER_OTP_IP_DAILY_LIMIT = env_positive_int("CUSTOMER_OTP_IP_DAILY_LIMIT", 1000)
SMS_GLOBAL_HOURLY_LIMIT = env_positive_int("SMS_GLOBAL_HOURLY_LIMIT", 500)
CUSTOMER_VERIFICATION_TTL_SECONDS = env_positive_int("CUSTOMER_VERIFICATION_TTL_SECONDS", 600)
CUSTOMER_PASSWORD_MIN_LENGTH = env_positive_int("CUSTOMER_PASSWORD_MIN_LENGTH", 8)
CUSTOMER_DORMANT_DAYS = env_positive_int("CUSTOMER_DORMANT_DAYS", 180)
# Keys: config.settings.local ships development defaults, config.settings.production requires real values.
SMS_CODE_HMAC_KEY = ""
SECRETBOX_KEY = ""
LOG_PII_KEY = ""
PAYMENT_REF_KEY = ""
QR_HMAC_KEY = ""
BTS_WEBHOOK_SECRET = ""
CLICK_SECRET_KEY = ""
PAYME_KEY = ""
ANDROID_SMS_APP_HASH = env_str("ANDROID_SMS_APP_HASH", "")
if ANDROID_SMS_APP_HASH and len(ANDROID_SMS_APP_HASH) != 11:
    raise ImproperlyConfigured("ANDROID_SMS_APP_HASH must be the 11-character app hash or empty")
SOCIAL_TICKET_TTL_SECONDS = env_positive_int("SOCIAL_TICKET_TTL_SECONDS", 600)
SOCIAL_NONCE_TTL_SECONDS = env_positive_int("SOCIAL_NONCE_TTL_SECONDS", 600)
GOOGLE_APP_CLIENT_IDS = env_list("GOOGLE_APP_CLIENT_IDS", [])
# Sign in with Apple (product-owner decision 2026-10-26): empty = provider "apple" off (live) / fake
# (local/test).
APPLE_BUNDLE_IDS = env_list("APPLE_BUNDLE_IDS", [])
APPLE_TEAM_ID = env_str("APPLE_TEAM_ID", "")
APPLE_KEY_ID = env_str("APPLE_KEY_ID", "")
APPLE_PRIVATE_KEY_FILE = env_str("APPLE_PRIVATE_KEY_FILE", "")
MIN_APP_VERSION_IOS = env_app_version("MIN_APP_VERSION_IOS")
MIN_APP_VERSION_ANDROID = env_app_version("MIN_APP_VERSION_ANDROID")
APP_STORE_URL = env_str("APP_STORE_URL", "https://apps.apple.com/app/id0000000000")
PLAY_STORE_URL = env_str("PLAY_STORE_URL", "https://play.google.com/store/apps/details?id=uz.glowderma.app")
ADDRESS_MAX_PER_CUSTOMER = env_positive_int("ADDRESS_MAX_PER_CUSTOMER", 10)
# Eskiz SMS (C§2.14.1); the fixed OTP ESKIZ_FAKE_CODE is read by config.settings.local only.
ESKIZ_MODE = env_mode("ESKIZ_MODE")
ESKIZ_BASE_URL = env_str("ESKIZ_BASE_URL", "https://notify.eskiz.uz/api")
ESKIZ_EMAIL = env_str("ESKIZ_EMAIL", "")
ESKIZ_PASSWORD = env_str("ESKIZ_PASSWORD", "")
ESKIZ_SENDER = env_str("ESKIZ_SENDER", "4546")
ESKIZ_CALLBACK_URL = env_str("ESKIZ_CALLBACK_URL", "")
ESKIZ_FAKE_CODE = ""
# Cart and checkout (C§2.5, §2.7).
CART_MAX_LINES = env_positive_int("CART_MAX_LINES", 30)
CART_MAX_QTY_PER_LINE = env_positive_int("CART_MAX_QTY_PER_LINE", 10)
CHECKOUT_MAX_OPEN_ORDERS = env_positive_int("CHECKOUT_MAX_OPEN_ORDERS", 1)
CHECKOUT_MIN_PAYABLE = env_positive_int("CHECKOUT_MIN_PAYABLE", 1000)
PAYMENT_IN_FLIGHT_GRACE_SECONDS = env_positive_int("PAYMENT_IN_FLIGHT_GRACE_SECONDS", 300)
PAYMENT_RETURN_URL = env_str("PAYMENT_RETURN_URL", "https://glowderma.uz/pay/return")
PAYMENT_RETURN_FALLBACK_URL = env_str("PAYMENT_RETURN_FALLBACK_URL", "glowderma://payment/return")
# Click SHOP API (C§2.13.1).
CLICK_SERVICE_ID = env_str("CLICK_SERVICE_ID", "00000")
CLICK_MERCHANT_ID = env_str("CLICK_MERCHANT_ID", "00000")
CLICK_MERCHANT_USER_ID = env_str("CLICK_MERCHANT_USER_ID", "00000")
CLICK_PAY_URL = env_str("CLICK_PAY_URL", "https://my.click.uz/services/pay")
CLICK_ALLOWED_IPS = env_networks("CLICK_ALLOWED_IPS", [])
CLICK_INFLIGHT_SECONDS = env_positive_int("CLICK_INFLIGHT_SECONDS", 120)
if CLICK_INFLIGHT_SECONDS > PAYMENT_IN_FLIGHT_GRACE_SECONDS:
    raise ImproperlyConfigured("CLICK_INFLIGHT_SECONDS must not exceed PAYMENT_IN_FLIGHT_GRACE_SECONDS")
# Payme Merchant API (C§2.13.2).
PAYME_MERCHANT_ID = env_str("PAYME_MERCHANT_ID", "")
PAYME_CHECKOUT_URL = env_str("PAYME_CHECKOUT_URL", "https://checkout.paycom.uz")
PAYME_ACCOUNT_FIELD = env_str("PAYME_ACCOUNT_FIELD", "order_id")
if not re.fullmatch(r"[a-z][a-z0-9_]{0,31}", PAYME_ACCOUNT_FIELD):
    raise ImproperlyConfigured("PAYME_ACCOUNT_FIELD must be a lower-case identifier")
PAYME_ALLOWED_IPS = env_networks("PAYME_ALLOWED_IPS", [])
PAYME_FISCAL_DETAIL_ENABLED = env_bool("PAYME_FISCAL_DETAIL_ENABLED", False)
# Webhooks (C§2.13.0): stored rows per IP per minute for requests that fail provider authentication.
WEBHOOK_REJECTED_PER_IP_PER_MIN = env_positive_int("WEBHOOK_REJECTED_PER_IP_PER_MIN", 10)
# BTS (C§2.13.3, §2.14.3).
BTS_BASE_URL = env_str("BTS_BASE_URL", "https://api.bts.uz")
BTS_LOGIN = env_str("BTS_LOGIN", "")
BTS_PASSWORD = env_str("BTS_PASSWORD", "")
BTS_SENDER_CITY_CODE = env_str("BTS_SENDER_CITY_CODE", "1001")
BTS_DEFAULT_PARCEL_WEIGHT_G = env_positive_int("BTS_DEFAULT_PARCEL_WEIGHT_G", 1000)
BTS_POLL_STALE_MINUTES = env_positive_int("BTS_POLL_STALE_MINUTES", 30)
BTS_POLL_BATCH = env_positive_int("BTS_POLL_BATCH", 100)
BTS_POLL_MAX_SECONDS = env_positive_int("BTS_POLL_MAX_SECONDS", 300)
BTS_POLL_MAX_CONSECUTIVE_FAILURES = env_positive_int("BTS_POLL_MAX_CONSECUTIVE_FAILURES", 3)
# FCM live adapter (M3): HTTP v1 over httpx with a service-account JWT.
FCM_PROJECT_ID = env_str("FCM_PROJECT_ID", "")
FCM_CREDENTIALS_FILE = env_str("FCM_CREDENTIALS_FILE", "")
# Loyalty QR and POS (C§2.9.3, §2.10); POS_API_KEYS is read by config.settings.local only.
QR_TOKEN_TTL_SECONDS = env_positive_int("QR_TOKEN_TTL_SECONDS", 60)
QR_MAX_LIVE_TOKENS = env_positive_int("QR_MAX_LIVE_TOKENS", 2)
if QR_MAX_LIVE_TOKENS > 2:  # C§6.13: at most two live QR tokens per customer
    raise ImproperlyConfigured("QR_MAX_LIVE_TOKENS must be 1 or 2")
POS_FAILED_AUTH_PER_IP_PER_MIN = env_positive_int("POS_FAILED_AUTH_PER_IP_PER_MIN", 20)
POS_API_KEYS: list[tuple[str, str]] = []
# Appointments (C§2.11).
BOOKING_MIN_LEAD_MINUTES = env_non_negative_int("BOOKING_MIN_LEAD_MINUTES", 60)
BOOKING_HORIZON_DAYS = env_positive_int("BOOKING_HORIZON_DAYS", 30)
BOOKING_MAX_OPEN_PER_CUSTOMER = env_positive_int("BOOKING_MAX_OPEN_PER_CUSTOMER", 3)
BOOKING_CANCEL_MIN_LEAD_MINUTES = env_non_negative_int("BOOKING_CANCEL_MIN_LEAD_MINUTES", 0)
# Development switches: honoured only while DEV_FEATURES_ALLOWED (the production settings refuse them
# outright), and only with fake providers, so they can never sign a callback with a real key or reach a live
# provider.
FAKE_PAYMENTS_ENABLED = env_bool("FAKE_PAYMENTS_ENABLED", False)
FAKE_SCENARIO_HEADER_ENABLED = env_bool("FAKE_SCENARIO_HEADER_ENABLED", False)
if FAKE_PAYMENTS_ENABLED and not CLICK_MODE == PAYME_MODE == "fake":
    raise ImproperlyConfigured("FAKE_PAYMENTS_ENABLED requires CLICK_MODE=fake and PAYME_MODE=fake")
SCENARIO_PROVIDER_MODES = {
    "ESKIZ_MODE": ESKIZ_MODE,
    "CLICK_MODE": CLICK_MODE,
    "PAYME_MODE": PAYME_MODE,
    "BTS_MODE": BTS_MODE,
    "FCM_MODE": FCM_MODE,
}
if FAKE_SCENARIO_HEADER_ENABLED and any(mode != "fake" for mode in SCENARIO_PROVIDER_MODES.values()):
    raise ImproperlyConfigured(
        "FAKE_SCENARIO_HEADER_ENABLED requires every provider mode to be fake: "
        + ", ".join(SCENARIO_PROVIDER_MODES)
    )
