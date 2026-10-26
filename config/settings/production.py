import base64
import binascii
import json
import os
import re
from pathlib import Path
from urllib.parse import urlsplit

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from django.core.exceptions import ImproperlyConfigured

from config.env import env_bool, env_int, env_json, env_list, env_networks, env_str

from .base import *  # noqa: F403
from .base import (
    APPLE_BUNDLE_IDS,
    BILLZ_BASE_URL,
    BILLZ_CASHBOX_ID,
    BILLZ_MODE,
    BILLZ_PAYMENT_TYPE_ID,
    BILLZ_PLATFORM_ID,
    BILLZ_SECRET_KEY,
    BILLZ_SHOP_ID,
    BTS_BASE_URL,
    BTS_MODE,
    BTS_PICKUP_TYPE,
    BTS_SENDER_PHONE,
    CLICK_MERCHANT_API_URL,
    CLICK_MODE,
    CLICK_PAY_URL,
    ESKIZ_BASE_URL,
    ESKIZ_CALLBACK_URL,
    ESKIZ_MODE,
    FCM_MODE,
    OAUTH_MODE,
    PAYME_CHECKOUT_URL,
    PAYME_MODE,
    database_config,
    redis_db_url,
)

DEBUG = False
if os.environ.get("FAKE_NOW"):
    raise ImproperlyConfigured(
        "FAKE_NOW pins the clock for demos and tests; it must not be set in production"
    )
# Development switches exist only under the local/test settings (customer contract §6.14): refused here
# whatever the provider modes, so no internet-facing deployment (staging runs this module too) honours one.
DEV_FEATURES_ALLOWED = False
FAKE_PAYMENTS_ENABLED = False
FAKE_SCENARIO_HEADER_ENABLED = False
ESKIZ_FAKE_CODE = ""
POS_API_KEYS = []
DEV_ONLY_VARIABLES = (
    "FAKE_PAYMENTS_ENABLED",
    "FAKE_SCENARIO_HEADER_ENABLED",
    "ESKIZ_FAKE_CODE",
    "POS_API_KEYS",
)
for _name in DEV_ONLY_VARIABLES:
    if os.environ.get(_name):
        raise ImproperlyConfigured(f"{_name} is a local/test switch; the production settings refuse it")


def _decode_jwt_key(encoded: str) -> bytes:
    return base64.b64decode(encoded + "=" * (-len(encoded) % 4), altchars=b"-_", validate=True)


# Values that were ever committed to this repository (.env.example, CI workflow history). They are public, so
# production refuses to boot with them even though they satisfy the length checks below.
KNOWN_PLACEHOLDER_SECRETS = frozenset(
    {
        "OFMmKSy50zW12DnugPKJmF4b0rNGeIvkm9SNWNjO9NRAN97Dmlr3CJH0tfi4Z6Kr",
        "slqp9tXiEipMGqSzlvPcW9p0hBnle7sDTJSOQKgmZHNHdwYK",
        # config.settings.local / test defaults (committed, so public)
        "local-development-key-at-least-32-characters",
        "local-internal-secret-at-least-32-characters",
        "test-internal-secret-at-least-32-characters",
        "local-sms-code-hmac-key-at-least-32-characters",
        "Z2xvd2Rlcm1hLWxvY2FsLXNlY3JldGJveC1rZXktMzI",
        "local-log-pii-key-at-least-32-characters",
        "local-payment-ref-key-at-least-32-characters",
        "local-qr-hmac-key-at-least-32-characters",
        "local-bts-webhook-secret-at-least-32-characters",
        "local-click-secret-key",
        "local-payme-key",
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
# compose.prod.yaml builds DATABASE_URL from POSTGRES_PASSWORD; the placeholder of
# deploy/env.production.example must not initialise a real database.
if "change-me" in str(DATABASES["default"]["PASSWORD"]).lower():
    raise ImproperlyConfigured("DATABASE_URL has a placeholder password; set a generated POSTGRES_PASSWORD")
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
# Live staff social sign-in needs every provider credential and each provider's exact callback URL (admin
# contract 2.1): GOOGLE_OAUTH_REDIRECT_URI / FACEBOOK_OAUTH_REDIRECT_URI, or the deprecated OAUTH_REDIRECT_URI
# (one release) as the fallback of both.
LIVE_OAUTH_VARIABLES = (
    "GOOGLE_OAUTH_CLIENT_ID",
    "GOOGLE_OAUTH_CLIENT_SECRET",
    "FACEBOOK_APP_ID",
    "FACEBOOK_APP_SECRET",
)
LIVE_OAUTH_REDIRECT_VARIABLES = ("GOOGLE_OAUTH_REDIRECT_URI", "FACEBOOK_OAUTH_REDIRECT_URI")
if OAUTH_MODE == "live":
    for name in LIVE_OAUTH_VARIABLES:
        value = env_str(name)  # raises ImproperlyConfigured("<name> is required")
        if "change-me" in value.lower():
            raise ImproperlyConfigured(f"{name} is a placeholder value; set the real credential")
    for name in LIVE_OAUTH_REDIRECT_VARIABLES:
        value = env_str(name, "") or env_str("OAUTH_REDIRECT_URI", "")
        if not value:
            raise ImproperlyConfigured(f"{name} is required")
        _redirect = urlsplit(value)
        if _redirect.scheme not in {"http", "https"} or not _redirect.netloc or "change-me" in value.lower():
            raise ImproperlyConfigured(f"{name} must be an absolute http(s) URL")

# --- Customer API keys (plan §7) ---
# HMAC keys and our own webhook secret: required, at least 32 characters (used as their UTF-8 bytes).
CUSTOMER_HMAC_KEYS = (
    "SMS_CODE_HMAC_KEY",
    "LOG_PII_KEY",
    "PAYMENT_REF_KEY",
    "QR_HMAC_KEY",
    "BTS_WEBHOOK_SECRET",
)
# Issued by the providers: required in every mode (a fake Click still verifies the Completes of transactions
# attached while it was live, C§2.13.0), no length floor.
PROVIDER_SECRETS = ("CLICK_SECRET_KEY", "PAYME_KEY")


def _refuse_placeholder(name: str, value: str) -> None:
    if not value.strip():  # env_str returns a whitespace-only value unchanged
        raise ImproperlyConfigured(f"{name} is required")
    if "change-me" in value.lower():
        raise ImproperlyConfigured(f"{name} is a placeholder value; set the real secret")
    if value in KNOWN_PLACEHOLDER_SECRETS:
        raise ImproperlyConfigured(f"{name} is a committed placeholder value; generate a real secret")


SMS_CODE_HMAC_KEY = env_str("SMS_CODE_HMAC_KEY")
LOG_PII_KEY = env_str("LOG_PII_KEY")
PAYMENT_REF_KEY = env_str("PAYMENT_REF_KEY")
QR_HMAC_KEY = env_str("QR_HMAC_KEY")
BTS_WEBHOOK_SECRET = env_str("BTS_WEBHOOK_SECRET")
CLICK_SECRET_KEY = env_str("CLICK_SECRET_KEY")
PAYME_KEY = env_str("PAYME_KEY")
for _name in CUSTOMER_HMAC_KEYS:
    _value = env_str(_name)
    _refuse_placeholder(_name, _value)
    if len(_value.strip()) < 32:
        raise ImproperlyConfigured(f"{_name} must be at least 32 characters")
for _name in PROVIDER_SECRETS:
    _refuse_placeholder(_name, env_str(_name))
SECRETBOX_KEY = env_str("SECRETBOX_KEY")
_refuse_placeholder("SECRETBOX_KEY", SECRETBOX_KEY)
try:
    if not re.fullmatch(r"[A-Za-z0-9_-]+={0,2}", SECRETBOX_KEY):  # altchars alone also accept "+" and "/"
        raise ValueError("not base64url")
    _secretbox_raw = base64.b64decode(
        SECRETBOX_KEY + "=" * (-len(SECRETBOX_KEY) % 4), altchars=b"-_", validate=True
    )
except (ValueError, binascii.Error) as exc:
    raise ImproperlyConfigured("SECRETBOX_KEY must be base64url of exactly 32 bytes") from exc
if len(_secretbox_raw) != 32:
    raise ImproperlyConfigured("SECRETBOX_KEY must be base64url of exactly 32 bytes")


def _decoded_placeholders() -> set[bytes]:
    decoded: set[bytes] = set()
    for value in KNOWN_PLACEHOLDER_SECRETS:
        try:
            decoded.add(_decode_jwt_key(value))
        except ValueError, binascii.Error:
            continue
    return decoded


if _secretbox_raw in _decoded_placeholders():
    raise ImproperlyConfigured("SECRETBOX_KEY is a committed placeholder value; generate a real secret")
# Sign in with Apple, live: the ES256 client secret needs the team id, the key id and a P-256 private key.
if OAUTH_MODE == "live" and APPLE_BUNDLE_IDS:
    for _name in ("APPLE_TEAM_ID", "APPLE_KEY_ID"):
        _refuse_placeholder(_name, env_str(_name))
    try:
        _apple_pem = Path(env_str("APPLE_PRIVATE_KEY_FILE")).read_bytes()
        _apple_key = serialization.load_pem_private_key(_apple_pem, password=None)
    except (OSError, ValueError, TypeError) as exc:
        raise ImproperlyConfigured("APPLE_PRIVATE_KEY_FILE must be a readable PEM private key") from exc
    if not isinstance(_apple_key, ec.EllipticCurvePrivateKey) or not isinstance(
        _apple_key.curve, ec.SECP256R1
    ):
        raise ImproperlyConfigured("APPLE_PRIVATE_KEY_FILE must hold an EC P-256 key")


# --- Live provider adapters (M3) ---
# A provider whose mode is live needs every value its adapter sends; a fake one needs none (C§6.14). Values
# are checked for presence, placeholders and shape — the messages name the variable, never its value.
def _https_url(name: str, value: str) -> None:
    parts = urlsplit(value)
    if parts.scheme != "https" or not parts.netloc or "change-me" in value.lower():
        raise ImproperlyConfigured(f"{name} must be an https URL")


def _required(name: str) -> str:
    value = env_str(name, "")
    _refuse_placeholder(name, value)
    return value.strip()


# Values of .env.example (and Eskiz's test sender) that would reach the provider as if they were ours.
COMMITTED_EXAMPLES = {"ESKIZ_SENDER": {"4546"}, "BTS_SENDER_PHONE": {"+998901234567"}}


def _not_an_example(name: str, value: str) -> str:
    if value in COMMITTED_EXAMPLES.get(name, set()):
        raise ImproperlyConfigured(f"{name} is the committed example value; set the real one")
    return value


if CLICK_MODE == "live":
    for _name in ("CLICK_SERVICE_ID", "CLICK_MERCHANT_ID", "CLICK_MERCHANT_USER_ID"):
        _value = _required(_name)
        if not re.fullmatch(r"[0-9]{1,18}", _value) or not _value.strip("0"):
            raise ImproperlyConfigured(f"{_name} must be the numeric id Click issued")
    _https_url("CLICK_MERCHANT_API_URL", CLICK_MERCHANT_API_URL)
    _https_url("CLICK_PAY_URL", CLICK_PAY_URL)
if PAYME_MODE == "live":
    # the checkout parameters are "key=value" pairs joined by ";": an id with either would corrupt them
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", _required("PAYME_MERCHANT_ID")):
        raise ImproperlyConfigured(
            "PAYME_MERCHANT_ID must be the merchant (cash desk) id from the Payme cabinet"
        )
    _https_url("PAYME_CHECKOUT_URL", PAYME_CHECKOUT_URL)
if BTS_MODE == "live":
    for _name in (
        "BTS_LOGIN",
        "BTS_PASSWORD",
        "BTS_SENDER_NAME",
        "BTS_SENDER_ADDRESS",
        "BTS_SENDER_CITY_CODE",
    ):
        _required(_name)
    if not re.fullmatch(r"\+998[0-9]{9}", _not_an_example("BTS_SENDER_PHONE", BTS_SENDER_PHONE)):
        raise ImproperlyConfigured("BTS_SENDER_PHONE must be an E.164 Uzbek number (+998XXXXXXXXX)")
    if BTS_PICKUP_TYPE == "branch":
        _required("BTS_SENDER_BRANCH_CODE")
    _https_url("BTS_BASE_URL", BTS_BASE_URL)
if ESKIZ_MODE == "live":
    for _name in ("ESKIZ_EMAIL", "ESKIZ_PASSWORD", "ESKIZ_SENDER"):
        _not_an_example(_name, _required(_name))
    _https_url("ESKIZ_BASE_URL", ESKIZ_BASE_URL)
    if ESKIZ_CALLBACK_URL:
        _https_url("ESKIZ_CALLBACK_URL", ESKIZ_CALLBACK_URL)
if FCM_MODE == "live":
    if not re.fullmatch(r"[a-z][a-z0-9-]{4,29}", _required("FCM_PROJECT_ID")):
        raise ImproperlyConfigured("FCM_PROJECT_ID must be the Firebase project id")
    # The service-account key is checked, never echoed: every failure names the variable only.
    try:
        _account = json.loads(Path(_required("FCM_CREDENTIALS_FILE")).read_text(encoding="utf-8"))
        if not isinstance(_account, dict) or _account.get("type") != "service_account":
            raise ValueError("not a service-account key")
        if "@" not in str(_account.get("client_email") or ""):
            raise ValueError("no client_email")
        _fcm_key = serialization.load_pem_private_key(str(_account.get("private_key") or "").encode(), None)
    except OSError, ValueError, TypeError:
        raise ImproperlyConfigured(
            "FCM_CREDENTIALS_FILE must be a readable Google service-account JSON key"
        ) from None
    if not isinstance(_fcm_key, rsa.RSAPrivateKey):
        raise ImproperlyConfigured("FCM_CREDENTIALS_FILE must hold an RSA service-account key")
    del _account, _fcm_key


# --- Billz live client (M3, docs/integrations/billz.md) ---
# The fake needs nothing. Live needs the integration key and the ids of the shop, the cash box and the payment
# type; a process with BILLZ_MODE=live refuses to start without them (no silent "connected" with no shop).
_UUID_PATTERN = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
LIVE_BILLZ_SECRETS = {"BILLZ_SECRET_KEY": BILLZ_SECRET_KEY}
LIVE_BILLZ_IDS = {
    "BILLZ_SHOP_ID": BILLZ_SHOP_ID,
    "BILLZ_CASHBOX_ID": BILLZ_CASHBOX_ID,
    "BILLZ_PAYMENT_TYPE_ID": BILLZ_PAYMENT_TYPE_ID,
}
# The all-zero UUID is the placeholder of .env.example: syntactically valid, never a real Billz id.
_NIL_UUID = "00000000-0000-0000-0000-000000000000"
if BILLZ_MODE == "live":
    for _name, _value in {**LIVE_BILLZ_SECRETS, **LIVE_BILLZ_IDS}.items():
        _refuse_placeholder(_name, _value)
    for _name, _value in {**LIVE_BILLZ_IDS, "BILLZ_PLATFORM_ID": BILLZ_PLATFORM_ID}.items():
        # the exact value the adapter uses (base.py strips it), never a stripped copy of it
        if not _UUID_PATTERN.fullmatch(_value):
            raise ImproperlyConfigured(f"{_name} must be a Billz UUID")
        if _value == _NIL_UUID:
            raise ImproperlyConfigured(f"{_name} is the .env.example placeholder; set the real Billz id")
    if urlsplit(BILLZ_BASE_URL).scheme != "https":
        raise ImproperlyConfigured("BILLZ_BASE_URL must be an https URL")
# --- Alert webhook (core.alerts, docs/ops/monitoring.md) ---
# Optional. When set it must be https (alerts carry ids and counts, but the URL itself often embeds a token)
# and signed with a real secret of at least 32 characters.
ALERT_ENV = env_str("ALERT_ENV", "production").strip()
ALERT_WEBHOOK_URL = env_str("ALERT_WEBHOOK_URL", "").strip()
ALERT_WEBHOOK_SECRET = env_str("ALERT_WEBHOOK_SECRET", "")
if ALERT_WEBHOOK_URL:
    _alert_url = urlsplit(ALERT_WEBHOOK_URL)
    if _alert_url.scheme != "https" or not _alert_url.netloc:
        raise ImproperlyConfigured("ALERT_WEBHOOK_URL must be an https URL in production")
    _refuse_placeholder("ALERT_WEBHOOK_SECRET", ALERT_WEBHOOK_SECRET)
    if len(ALERT_WEBHOOK_SECRET.strip()) < 32:
        raise ImproperlyConfigured("ALERT_WEBHOOK_SECRET must be at least 32 characters")
