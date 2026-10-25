import base64
import importlib
import re
import secrets
import sys
from collections.abc import Callable, Iterator
from pathlib import Path
from types import ModuleType
from typing import Any, cast

import pytest
from django.core import checks
from django.core.exceptions import ImproperlyConfigured
from django.test import RequestFactory, override_settings

from config.settings.base import redis_db_url

REQUIRED = (
    "SECRET_KEY",
    "INTERNAL_API_SECRET",
    "JWT_SIGNING_KEYS",
    "JWT_ACTIVE_KID",
    "DATABASE_URL",
    "REDIS_URL",
    "ALLOWED_HOSTS",
    "ADMIN_API_ALLOWED_NETS",
    "PUBLIC_MEDIA_BASE_URL",
)


def load_production() -> ModuleType:
    sys.modules.pop("config.settings.production", None)
    return importlib.import_module("config.settings.production")


@pytest.fixture
def load_fresh_production() -> Iterator[Callable[[], ModuleType]]:
    """Load production after re-executing config.settings.base so base-level env validation runs again."""
    original = sys.modules["config.settings.base"]

    def load() -> ModuleType:
        sys.modules.pop("config.settings.base", None)
        return load_production()

    try:
        yield load
    finally:
        sys.modules["config.settings.base"] = original
        sys.modules["config.settings"].base = original  # type: ignore[attr-defined]


@pytest.fixture
def production_env(monkeypatch: pytest.MonkeyPatch) -> None:
    values = dict.fromkeys(REQUIRED, "valid-secret-value-at-least-32-characters")
    values.update(
        JWT_SIGNING_KEYS='{"test":"' + base64.urlsafe_b64encode(b"x" * 32).decode() + '"}',
        JWT_ACTIVE_KID="test",
        DATABASE_URL="postgres://u:p@localhost:5433/db",
        REDIS_URL="redis://localhost:6380/0",
        ALLOWED_HOSTS="localhost",
        ADMIN_API_ALLOWED_NETS="127.0.0.0/8",
        PUBLIC_MEDIA_BASE_URL="https://media.example.uz/",
    )
    for name, value in values.items():
        monkeypatch.setenv(name, value)


def test_production_settings_require_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in REQUIRED:
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(ImproperlyConfigured, match="SECRET_KEY"):
        load_production()


def test_production_rejects_placeholder_secrets(
    production_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SECRET_KEY", "change-me-" * 8)
    with pytest.raises(ImproperlyConfigured, match="SECRET_KEY"):
        load_production()


# Every secret-looking value that was ever committed (.env.example, CI workflow history).
COMMITTED_SECRET_KEY = "OFMmKSy50zW12DnugPKJmF4b0rNGeIvkm9SNWNjO9NRAN97Dmlr3CJH0tfi4Z6Kr"  # noqa: S105
COMMITTED_INTERNAL_SECRET = "slqp9tXiEipMGqSzlvPcW9p0hBnle7sDTJSOQKgmZHNHdwYK"  # noqa: S105
COMMITTED_JWT_KEYS = (
    "4zhPFokAL4QMFGTQpn-XIhwA3i4TYB9SgcekVIfjG2U",  # .env.example
    "oQAQ0h7x354qUJXY9NmXu9SBoKbHt5NF9oKkakDsjdk",  # former CI placeholder
)


@pytest.mark.parametrize(
    "variable,value",
    [
        ("SECRET_KEY", COMMITTED_SECRET_KEY),
        ("SECRET_KEY", COMMITTED_INTERNAL_SECRET),
        ("INTERNAL_API_SECRET", COMMITTED_INTERNAL_SECRET),
        ("INTERNAL_API_SECRET", COMMITTED_SECRET_KEY),
        *[("JWT_SIGNING_KEYS", '{"test":"' + key + '"}') for key in COMMITTED_JWT_KEYS],
        # the same key bytes with standard base64 padding/alphabet are still the same key
        *[
            ("JWT_SIGNING_KEYS", '{"test":"' + key.replace("-", "+").replace("_", "/") + "=" + '"}')
            for key in COMMITTED_JWT_KEYS
        ],
    ],
)
def test_production_rejects_committed_placeholder_values(
    production_env: None, monkeypatch: pytest.MonkeyPatch, variable: str, value: str
) -> None:
    """AGENTS.md: "No secrets in the repo ... .env.example lists them with dummy values" - a committed dummy
    that production accepts would let anyone with repo access mint staff tokens, so production refuses it."""
    monkeypatch.setenv(variable, value)
    with pytest.raises(ImproperlyConfigured, match=f"{variable}.*placeholder"):
        load_production()


def test_env_example_jwt_key_is_valid_but_rejected_by_production(production_env: None) -> None:
    """The .env.example key must stay a *valid* 32-byte base64url example (local boots with it) and must be
    one of the keys production refuses, otherwise `cp .env.example .env` could run production unchanged."""
    env_example = (Path(__file__).resolve().parents[3] / ".env.example").read_text()
    match = re.search(r'^JWT_SIGNING_KEYS=\{"dev":"([A-Za-z0-9_-]+)"\}$', env_example, re.MULTILINE)
    assert match is not None
    encoded = match.group(1)
    decoded = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
    assert len(decoded) == 32
    assert decoded in load_production().KNOWN_PLACEHOLDER_JWT_KEYS


def test_ci_workflow_commits_no_secret_literals() -> None:
    workflow = (Path(__file__).resolve().parents[3] / ".github" / "workflows" / "ci.yml").read_text()
    for literal in (COMMITTED_SECRET_KEY, COMMITTED_INTERNAL_SECRET, *COMMITTED_JWT_KEYS):
        assert literal not in workflow
    assert "openssl rand" in workflow
    assert 'SECURE_HSTS_PRELOAD: "true"' in workflow


def test_production_rejects_unknown_active_kid(production_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JWT_ACTIVE_KID", "unknown")
    with pytest.raises(ImproperlyConfigured, match="JWT_ACTIVE_KID"):
        load_production()


def test_production_rejects_short_jwt_key(production_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JWT_SIGNING_KEYS", '{"test":"eA"}')
    with pytest.raises(ImproperlyConfigured, match="JWT_SIGNING_KEYS"):
        load_production()


def test_empty_env_value_counts_as_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    from config.env import env_str

    monkeypatch.setenv("EMPTY", "")
    assert env_str("EMPTY", "fallback") == "fallback"
    with pytest.raises(ImproperlyConfigured, match="EMPTY"):
        env_str("EMPTY")


@override_settings(INTERNAL_API_SECRET="")
def test_empty_secret_never_matches() -> None:
    from glowderma.core.clientip import has_internal_secret

    assert not has_internal_secret(RequestFactory().get("/"))


@override_settings(INTERNAL_API_SECRET="x" * 32)
def test_non_ascii_secret_header_is_ignored() -> None:
    from glowderma.core.clientip import has_internal_secret

    assert not has_internal_secret(RequestFactory().get("/", HTTP_X_GD_INTERNAL_SECRET="\u2603"))  # noqa: S106


def test_api_defaults_deny() -> None:
    from rest_framework.settings import api_settings

    assert api_settings.DEFAULT_AUTHENTICATION_CLASSES == []
    from glowderma.core.permissions import DenyAll

    assert cast(object, api_settings.DEFAULT_PERMISSION_CLASSES) == [DenyAll]


@override_settings(INTERNAL_API_SECRET="x" * 32)
def test_internal_secret_matches_correct_value_only() -> None:
    from glowderma.core.clientip import has_internal_secret

    factory = RequestFactory()
    assert has_internal_secret(factory.get("/", HTTP_X_GD_INTERNAL_SECRET="x" * 32))
    assert not has_internal_secret(factory.get("/", HTTP_X_GD_INTERNAL_SECRET="y" * 32))


@pytest.mark.parametrize("module_name", ["local", "production"])
@pytest.mark.parametrize("variable", ["TRUSTED_PROXIES", "ADMIN_API_ALLOWED_NETS"])
@pytest.mark.parametrize("value", ["not-a-network", "10.0.0.1/8", "10.0.0.0/8,"])
def test_invalid_cidrs_fail_settings_startup(
    production_env: None, monkeypatch: pytest.MonkeyPatch, module_name: str, variable: str, value: str
) -> None:
    monkeypatch.setenv(variable, value)
    full_name = f"config.settings.{module_name}"
    original = sys.modules.pop(full_name, None)
    try:
        with pytest.raises(ImproperlyConfigured, match=variable):
            importlib.import_module(full_name)
    finally:
        sys.modules.pop(full_name, None)
        if original is not None:
            sys.modules[full_name] = original
            setattr(sys.modules["config.settings"], module_name, original)


def test_cidrs_are_compiled_to_network_objects(monkeypatch: pytest.MonkeyPatch) -> None:
    from ipaddress import ip_network

    from config.env import env_networks

    monkeypatch.setenv("TRUSTED_PROXIES", "10.0.0.0/8,2001:db8::/32")
    assert env_networks("TRUSTED_PROXIES") == (ip_network("10.0.0.0/8"), ip_network("2001:db8::/32"))


# ---- Redis split --------------------------------------------------------------


@pytest.mark.parametrize(
    "url,expected",
    [
        ("redis://localhost:6380/0", "redis://localhost:6380/1"),
        ("redis://redis:6379", "redis://redis:6379/1"),
        ("redis://:pw@redis:6379/5?socket_timeout=2", "redis://:pw@redis:6379/1?socket_timeout=2"),
        ("rediss://user:pw@cache.internal:6380/0", "rediss://user:pw@cache.internal:6380/1"),
    ],
)
def test_redis_db_url_replaces_database_path(url: str, expected: str) -> None:
    assert redis_db_url(url, 1) == expected
    assert redis_db_url(expected, 0) == expected.replace("/1", "/0")


@pytest.mark.parametrize(
    "url,db", [("http://localhost/0", 1), ("redis:///0", 1), ("redis://localhost/0", -1)]
)
def test_redis_db_url_rejects_invalid_values(url: str, db: int) -> None:
    with pytest.raises(ImproperlyConfigured, match="REDIS_URL"):
        redis_db_url(url, db)


def test_production_broker_defaults_to_db_1(production_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CELERY_BROKER_URL", raising=False)
    module = load_production()
    assert module.REDIS_URL == "redis://localhost:6380/0"
    assert module.CELERY_BROKER_URL == "redis://localhost:6380/1"
    assert module.CACHES["default"]["LOCATION"] == "redis://localhost:6380/0"
    assert module.CACHES["default"]["KEY_PREFIX"] == "gd"
    assert module.CACHES["default"]["VERSION"] == 1
    monkeypatch.setenv("CELERY_BROKER_URL", "redis://broker.internal:6379/3")
    assert load_production().CELERY_BROKER_URL == "redis://broker.internal:6379/3"


def test_production_pools_by_default(production_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    default = load_production().DATABASES["default"]
    assert default["OPTIONS"]["pool"]
    assert default["CONN_MAX_AGE"] == 0
    monkeypatch.setenv("DB_POOL", "false")
    assert "pool" not in load_production().DATABASES["default"]["OPTIONS"]


def test_test_settings_cache_and_broker() -> None:
    from django.conf import settings

    assert settings.CACHES["default"]["BACKEND"] == "django.core.cache.backends.locmem.LocMemCache"
    assert settings.CELERY_BROKER_URL == "redis://localhost:6380/1"
    assert settings.INTEGRATIONS_MODE == "fake"
    assert settings.THROTTLE_RATES == {}


# ---- base-level env validation ------------------------------------------------


@pytest.mark.parametrize("value,ok", [("fake", True), ("live", True), ("sandbox", False)])
def test_integrations_mode_validated_at_startup(
    production_env: None,
    load_fresh_production: Callable[[], ModuleType],
    monkeypatch: pytest.MonkeyPatch,
    value: str,
    ok: bool,
) -> None:
    monkeypatch.setenv("INTEGRATIONS_MODE", value)
    monkeypatch.setenv("OAUTH_MODE", "fake")  # live OAuth would also demand provider credentials
    if ok:
        assert load_fresh_production().INTEGRATIONS_MODE == value
    else:
        with pytest.raises(ImproperlyConfigured, match="INTEGRATIONS_MODE"):
            load_fresh_production()


def test_throttle_rates_env_validated_at_startup(
    production_env: None, load_fresh_production: Callable[[], ModuleType], monkeypatch: pytest.MonkeyPatch
) -> None:
    """§1.9 throttles are "defaults, configurable": a malformed THROTTLE_RATES fails settings import."""
    monkeypatch.setenv("THROTTLE_RATES", '{"login": "ten per minute"}')
    with pytest.raises(ImproperlyConfigured, match="THROTTLE_RATES"):
        load_fresh_production()
    monkeypatch.setenv("THROTTLE_RATES", "not json")
    with pytest.raises(ImproperlyConfigured, match="THROTTLE_RATES"):
        load_fresh_production()
    monkeypatch.setenv("THROTTLE_RATES", '{"login": "10/min"}')
    assert load_fresh_production().THROTTLE_RATES == {"login": "10/min"}


def test_log_level_env_validated_at_startup(
    production_env: None, load_fresh_production: Callable[[], ModuleType], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LOG_LEVEL", "warning")
    assert load_fresh_production().LOGGING["loggers"]["glowderma"]["level"] == "WARNING"
    monkeypatch.setenv("LOG_LEVEL", "loud")
    with pytest.raises(ImproperlyConfigured, match="LOG_LEVEL"):
        load_fresh_production()


# ---- deploy checks ------------------------------------------------------------

DEPLOY_ENV = {
    "SECURE_SSL_REDIRECT": "true",
    "SECURE_HSTS_SECONDS": "31536000",
    # Preload is an explicit opt-in (never implied by SECURE_HSTS_SECONDS); CI sets it the same way.
    "SECURE_HSTS_PRELOAD": "true",
    "PUBLIC_MEDIA_BASE_URL": "https://media.example.uz/media/",
    "ALLOWED_HOSTS": "api.example.uz",
    "ADMIN_API_ALLOWED_NETS": "10.0.0.0/8",
}
NOT_OVERRIDDEN = {"DATABASES", "CACHES", "LOGGING", "INSTALLED_APPS"}


# The system checks include Django 6.1's GeneratedField check, which asks the connection for the server
# version (catalog.Product.search_text): without DB access these tests only passed after another test had
# cached it.
@pytest.mark.django_db
def test_production_passes_deploy_checks(production_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    """Mirrors the CI step `manage.py check --deploy --fail-level WARNING` with production settings.
    Secrets are generated like in CI (>= 50 chars, >= 5 unique, no "django-insecure-" prefix for W009)."""
    for name, value in DEPLOY_ENV.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("SECRET_KEY", secrets.token_hex(32))
    monkeypatch.setenv("INTERNAL_API_SECRET", secrets.token_hex(24))
    module = load_production()
    assert module.SILENCED_SYSTEM_CHECKS == ["security.W008"]
    assert module.SECURE_HSTS_INCLUDE_SUBDOMAINS is True
    assert module.SECURE_HSTS_PRELOAD is True
    overrides = {
        name: getattr(module, name) for name in dir(module) if name.isupper() and name not in NOT_OVERRIDDEN
    }
    with override_settings(**overrides):
        messages = checks.run_checks(include_deployment_checks=True)
        silenced = {message.id for message in messages if message.is_silenced()}
        problems = [
            message for message in messages if message.level >= checks.WARNING and not message.is_silenced()
        ]
    assert silenced <= {"security.W008"}
    assert problems == [], [str(message) for message in problems]


def test_production_without_hsts_warns_only_about_hsts(
    production_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = load_production()
    assert module.SECURE_HSTS_SECONDS == 0
    assert module.SECURE_HSTS_INCLUDE_SUBDOMAINS is False
    assert module.SECURE_HSTS_PRELOAD is False


@pytest.mark.django_db  # see test_production_passes_deploy_checks
def test_hsts_preload_is_never_implied(production_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    """Turning HSTS on enables includeSubDomains (W005) but not preload: that commitment is near-irreversible
    and must be an explicit SECURE_HSTS_PRELOAD=true; until then `check --deploy` keeps reporting W021."""
    for name, value in DEPLOY_ENV.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("SECURE_HSTS_PRELOAD")
    monkeypatch.setenv("SECRET_KEY", secrets.token_hex(32))
    module = load_production()
    assert module.SECURE_HSTS_SECONDS == 31536000
    assert module.SECURE_HSTS_INCLUDE_SUBDOMAINS is True
    assert module.SECURE_HSTS_PRELOAD is False
    overrides = {
        name: getattr(module, name) for name in dir(module) if name.isupper() and name not in NOT_OVERRIDDEN
    }
    with override_settings(**overrides):
        messages = checks.run_checks(include_deployment_checks=True)
        ids = {m.id for m in messages if m.level >= checks.WARNING and not m.is_silenced()}
    assert ids == {"security.W021"}


def test_gunicorn_config_logs_request_id() -> None:
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parents[3] / "config" / "gunicorn.conf.py"
    spec = importlib.util.spec_from_file_location("gunicorn_conf", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.accesslog == "-"
    assert "%({x-request-id}o)s" in module.access_log_format
    assert (module.timeout, module.graceful_timeout, module.keepalive) == (60, 30, 5)
    assert module.worker_class == "gthread"


def test_production_requires_public_media_base_url(
    production_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Contract 1.4: media URLs are "built from `PUBLIC_MEDIA_BASE_URL`, never from the request Host"."""
    monkeypatch.delenv("PUBLIC_MEDIA_BASE_URL")
    with pytest.raises(ImproperlyConfigured, match="PUBLIC_MEDIA_BASE_URL"):
        load_production()


@pytest.mark.parametrize(
    "value", ["https://media.example.uz", "ftp://media.example.uz/", "media.example.uz/", "https:///media/"]
)
def test_production_rejects_malformed_public_media_base_url(
    production_env: None, monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("PUBLIC_MEDIA_BASE_URL", value)
    with pytest.raises(ImproperlyConfigured, match="PUBLIC_MEDIA_BASE_URL"):
        load_production()


def test_production_accepts_http_media_base_url_with_trailing_slash(production_env: None) -> None:
    module = load_production()
    assert module.PUBLIC_MEDIA_BASE_URL == "https://media.example.uz/"
    assert module.FILE_UPLOAD_PERMISSIONS == 0o644
    assert module.FILE_UPLOAD_MAX_MEMORY_SIZE == 6 * 1024 * 1024
    sys.modules.pop("config.settings.production", None)


LIVE_OAUTH_ENV = {
    "GOOGLE_OAUTH_CLIENT_ID": "123.apps.googleusercontent.com",
    "GOOGLE_OAUTH_CLIENT_SECRET": "google-secret-value",
    "FACEBOOK_APP_ID": "1234567890",
    "FACEBOOK_APP_SECRET": "facebook-secret-value",
    "OAUTH_REDIRECT_URI": "https://admin.example.uz/admin/auth/callback",
}


def test_oauth_mode_defaults_to_integrations_mode_and_fake_needs_no_credentials(
    production_env: None, load_fresh_production: Callable[[], ModuleType], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Contract 2.1 / M0b: fake providers by default; no OAuth variable is required then."""
    for name in (*LIVE_OAUTH_ENV, "OAUTH_MODE", "INTEGRATIONS_MODE", "GOOGLE_ALLOWED_HD"):
        monkeypatch.delenv(name, raising=False)
    production = load_fresh_production()
    assert production.OAUTH_MODE == production.INTEGRATIONS_MODE == "fake"
    assert production.GOOGLE_OAUTH_CLIENT_ID == ""
    assert production.GOOGLE_ALLOWED_HD == ""
    assert production.STAFF_GOOGLE_AUTOLINK is False
    assert production.FACEBOOK_GRAPH_VERSION == "v21.0"
    assert production.OAUTH_REDIRECT_URI == ""


def test_oauth_mode_is_validated(
    production_env: None, load_fresh_production: Callable[[], ModuleType], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OAUTH_MODE", "sandbox")
    with pytest.raises(ImproperlyConfigured, match="OAUTH_MODE"):
        load_fresh_production()


@pytest.mark.parametrize("missing", sorted(LIVE_OAUTH_ENV))
def test_live_oauth_requires_each_credential_by_name(
    production_env: None,
    load_fresh_production: Callable[[], ModuleType],
    monkeypatch: pytest.MonkeyPatch,
    missing: str,
) -> None:
    monkeypatch.setenv("OAUTH_MODE", "live")
    for name, value in LIVE_OAUTH_ENV.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv(missing)
    with pytest.raises(ImproperlyConfigured, match=missing):
        load_fresh_production()


def test_live_oauth_follows_integrations_mode_and_rejects_placeholders(
    production_env: None, load_fresh_production: Callable[[], ModuleType], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("OAUTH_MODE", raising=False)
    monkeypatch.setenv("INTEGRATIONS_MODE", "live")
    for name, value in LIVE_OAUTH_ENV.items():
        monkeypatch.setenv(name, value)
    production = load_fresh_production()
    assert production.OAUTH_MODE == "live"
    assert production.GOOGLE_OAUTH_CLIENT_ID == LIVE_OAUTH_ENV["GOOGLE_OAUTH_CLIENT_ID"]
    monkeypatch.setenv("FACEBOOK_APP_SECRET", "change-me-facebook-app-secret")
    with pytest.raises(ImproperlyConfigured, match="FACEBOOK_APP_SECRET.*placeholder"):
        load_fresh_production()
    monkeypatch.setenv("FACEBOOK_APP_SECRET", LIVE_OAUTH_ENV["FACEBOOK_APP_SECRET"])
    monkeypatch.setenv("OAUTH_REDIRECT_URI", "admin/auth/callback")
    with pytest.raises(ImproperlyConfigured, match="OAUTH_REDIRECT_URI"):
        load_fresh_production()


def test_env_example_documents_oauth_block_in_fake_mode() -> None:
    env_example = (Path(__file__).resolve().parents[3] / ".env.example").read_text()
    assert re.search(r"^OAUTH_MODE=fake$", env_example, re.MULTILINE)
    for name in LIVE_OAUTH_ENV:
        assert re.search(rf"^#?{name}=", env_example, re.MULTILINE), name
    assert "change-me" in env_example.split("OAUTH_MODE=fake", 1)[1]


def test_production_refuses_fake_now(production_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FAKE_NOW", "2026-10-24T12:05:00+05:00")
    with pytest.raises(ImproperlyConfigured, match="FAKE_NOW"):
        load_production()


def _load_local() -> Any:
    full_name = "config.settings.local"
    original = sys.modules.pop(full_name, None)
    try:
        return importlib.import_module(full_name)
    finally:
        sys.modules.pop(full_name, None)
        if original is not None:
            sys.modules[full_name] = original
            package: Any = sys.modules["config.settings"]
            package.local = original


def test_local_runs_celery_tasks_inline_unless_told_otherwise(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CELERY_TASK_ALWAYS_EAGER", raising=False)
    assert _load_local().CELERY_TASK_ALWAYS_EAGER is True  # a bare runserver has no worker
    monkeypatch.setenv("CELERY_TASK_ALWAYS_EAGER", "false")
    assert _load_local().CELERY_TASK_ALWAYS_EAGER is False
