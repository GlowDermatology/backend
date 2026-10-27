"""``deploy/env.production.example`` read through ``compose.prod.yaml`` into the production settings.

The api, worker and beat containers get every ``.env`` line (``env_file``) overridden by the backend
``environment`` map of ``compose.prod.yaml`` (interpolated from the same ``.env``). These tests rebuild
that environment from the committed files and import ``config.settings.production`` with it: an unedited copy
of the example must never boot (compose refuses its empty passwords and missing release tags, the production
settings its placeholders), each required placeholder must be refused on its own, the example filled with
generated secrets must boot in the fake-provider posture, and each live provider block's commented
placeholders must be refused once its mode is live.
"""

import base64
import importlib
import ipaddress
import json
import re
import secrets
import sys
from collections.abc import Callable, Iterator
from pathlib import Path
from types import ModuleType

import pytest
import yaml
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

ROOT = Path(settings.BASE_DIR)
EXAMPLE = ROOT / "deploy" / "env.production.example"
COMPOSE = ROOT / "compose.prod.yaml"
# ${NAME}, ${NAME:-default}, ${NAME:?message} (the forms the backend environment map uses)
INTERPOLATION = re.compile(r"\$\{([A-Z][A-Z0-9_]*)(?::([-?])([^}]*))?\}")
REQUIRED_BY_COMPOSE = re.compile(r"\$\{([A-Z][A-Z0-9_]*):\?[^}]*\}")
# Compose-required, empty in the example: the infrastructure passwords; written by deploy/pin-release.sh.
EMPTY_PASSWORDS = ("POSTGRES_APP_PASSWORD", "POSTGRES_PASSWORD", "REDIS_PASSWORD")
RELEASE_TAGS = {"GIT_SHA": "abc1234", "FRONTEND_GIT_SHA": "def5678"}
REFUSED_WHEN_SET = (
    "FAKE_NOW",
    "FAKE_PAYMENTS_ENABLED",
    "FAKE_SCENARIO_HEADER_ENABLED",
    "ESKIZ_FAKE_CODE",
    "POS_API_KEYS",
)


def example() -> tuple[dict[str, str], dict[str, str]]:
    """The example's active ``NAME=value`` lines and its commented ``#NAME=value`` lines."""
    active: dict[str, str] = {}
    commented: dict[str, str] = {}
    for line in EXAMPLE.read_text(encoding="utf-8").splitlines():
        if match := re.match(r"^([A-Z][A-Z0-9_]*)=(.*)$", line):
            active[match.group(1)] = match.group(2)
        elif match := re.match(r"^#([A-Z][A-Z0-9_]*)=(.*)$", line):
            commented.setdefault(match.group(1), match.group(2))
    return active, commented


ACTIVE, COMMENTED = example()
PLACEHOLDERS = sorted(name for name, value in ACTIVE.items() if "change-me" in value)
EMPTY = sorted(name for name, value in ACTIVE.items() if value == "")


def interpolate(value: str, dotenv: dict[str, str]) -> str:
    def substitute(match: re.Match[str]) -> str:
        name, operator, argument = match.groups()
        if dotenv.get(name):
            return dotenv[name]
        if operator == "?":
            raise AssertionError(f"compose refuses to start: {argument}")
        return argument or ""

    return INTERPOLATION.sub(substitute, value)


def compose_refusals(dotenv: dict[str, str]) -> set[str]:
    """The ``${NAME:?…}`` variables of the whole compose file that ``dotenv`` leaves unset or empty."""
    text = COMPOSE.read_text(encoding="utf-8")
    return {name for name in REQUIRED_BY_COMPOSE.findall(text) if not dotenv.get(name)}


def container_environment(dotenv: dict[str, str]) -> dict[str, str]:
    """What a backend container sees: the env_file, overridden by the interpolated compose environment."""
    compose = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    backend = compose["x-backend"]
    assert backend["env_file"] == ".env"
    overrides = {name: interpolate(str(value), dotenv) for name, value in backend["environment"].items()}
    return {**dotenv, **overrides}


def generated_secrets() -> dict[str, str]:
    """Values made with the generators the example documents (fresh per test)."""
    return {
        "SECRET_KEY": secrets.token_urlsafe(50),
        "INTERNAL_API_SECRET": secrets.token_urlsafe(48),
        "JWT_SIGNING_KEYS": json.dumps({"k1": secrets.token_urlsafe(32)}),
        "POSTGRES_PASSWORD": secrets.token_hex(24),
        "POSTGRES_APP_PASSWORD": secrets.token_hex(24),
        "REDIS_PASSWORD": secrets.token_hex(24),
        "SMS_CODE_HMAC_KEY": secrets.token_hex(32),
        "LOG_PII_KEY": secrets.token_hex(32),
        "PAYMENT_REF_KEY": secrets.token_hex(32),
        "QR_HMAC_KEY": secrets.token_hex(32),
        "SECRETBOX_KEY": secrets.token_urlsafe(32),
        "CLICK_SECRET_KEY": secrets.token_hex(16),
        "PAYME_KEY": secrets.token_hex(16),
        "BTS_WEBHOOK_SECRET": secrets.token_hex(32),
    }


def filled() -> dict[str, str]:
    """The example with generated secrets and the tags deploy/pin-release.sh appends."""
    return {**ACTIVE, **generated_secrets(), **RELEASE_TAGS}


@pytest.fixture
def boot(monkeypatch: pytest.MonkeyPatch) -> Iterator[Callable[[dict[str, str]], ModuleType]]:
    """Import config.settings.production (and a fresh base) with exactly the container environment of a
    ``.env``; every variable the example names is cleared first."""
    original = sys.modules["config.settings.base"]

    def load(dotenv: dict[str, str]) -> ModuleType:
        for name in {*ACTIVE, *COMMENTED, *REFUSED_WHEN_SET}:
            monkeypatch.delenv(name, raising=False)
        for name, value in container_environment(dotenv).items():
            monkeypatch.setenv(name, value)
        sys.modules.pop("config.settings.base", None)
        sys.modules.pop("config.settings.production", None)
        return importlib.import_module("config.settings.production")

    try:
        yield load
    finally:
        sys.modules["config.settings.base"] = original
        sys.modules["config.settings"].base = original  # type: ignore[attr-defined]


def test_every_required_secret_of_the_example_is_a_placeholder_or_empty() -> None:
    assert EMPTY == sorted(EMPTY_PASSWORDS)
    assert sorted([*PLACEHOLDERS, *EMPTY]) == sorted(generated_secrets())


def test_compose_refuses_an_unedited_copy_before_anything_starts() -> None:
    """Empty passwords and missing release tags stop compose before PostgreSQL or Redis is initialised."""
    assert compose_refusals(dict(ACTIVE)) == {*EMPTY_PASSWORDS, *RELEASE_TAGS}
    assert compose_refusals(filled()) == set()


@pytest.mark.parametrize("name", [*EMPTY_PASSWORDS, *RELEASE_TAGS])
def test_compose_refuses_each_empty_password_and_tag_on_its_own(name: str) -> None:
    assert compose_refusals({**filled(), name: ""}) == {name}


def test_an_unedited_copy_of_the_example_never_boots(boot: Callable[[dict[str, str]], ModuleType]) -> None:
    passwords_and_tags = {
        name: value for name, value in filled().items() if name in {*EMPTY_PASSWORDS, *RELEASE_TAGS}
    }
    with pytest.raises(ImproperlyConfigured):
        boot({**ACTIVE, **passwords_and_tags})


@pytest.mark.parametrize("name", PLACEHOLDERS)
def test_each_placeholder_is_refused_on_its_own(
    boot: Callable[[dict[str, str]], ModuleType], name: str
) -> None:
    dotenv = {**filled(), name: ACTIVE[name]}
    with pytest.raises(ImproperlyConfigured, match=name):
        boot(dotenv)


def test_a_typed_placeholder_application_password_is_refused(
    boot: Callable[[dict[str, str]], ModuleType],
) -> None:
    """POSTGRES_APP_PASSWORD reaches Django inside the DATABASE_URL compose builds from it."""
    with pytest.raises(ImproperlyConfigured, match="DATABASE_URL"):
        boot({**filled(), "POSTGRES_APP_PASSWORD": "change-me"})


def test_the_backend_containers_never_see_the_infrastructure_passwords() -> None:
    """env_file passes every .env line; compose blanks the passwords (the superuser's never reaches
    Django)."""
    dotenv = filled()
    environment = container_environment(dotenv)
    for name in EMPTY_PASSWORDS:
        assert environment[name] == ""
    assert dotenv["POSTGRES_PASSWORD"] not in "".join(environment.values())


def test_the_filled_example_boots_with_the_compose_topology(
    boot: Callable[[dict[str, str]], ModuleType],
) -> None:
    dotenv = filled()
    module = boot(dotenv)
    assert database_password(module) == dotenv["POSTGRES_APP_PASSWORD"]
    api_domain = ACTIVE["API_DOMAIN"]
    admin_domain = ACTIVE["ADMIN_DOMAIN"]
    assert module.INTEGRATIONS_MODE == "fake"
    assert module.ALLOWED_HOSTS == [api_domain, "api", "localhost"]
    assert module.ADMIN_API_ALLOWED_NETS == (ipaddress.ip_network("172.30.0.20/32"),)
    assert module.TRUSTED_PROXIES == (ipaddress.ip_network("172.30.0.10/32"),)
    assert module.PUBLIC_MEDIA_BASE_URL == f"https://{api_domain}/media/"
    assert module.GOOGLE_OAUTH_REDIRECT_URI == f"https://{admin_domain}/admin/auth/oauth/google/callback"
    assert module.FACEBOOK_OAUTH_REDIRECT_URI == f"https://{admin_domain}/admin/auth/oauth/facebook/callback"
    database = module.DATABASES["default"]
    assert (database["HOST"], database["USER"], database["NAME"]) == (
        "postgres",
        "glowderma_app",
        "glowderma",
    )
    redis_password = container_environment(dotenv)["REDIS_URL"].split(":")[2].split("@")[0]
    assert module.CACHES["default"]["LOCATION"] == f"redis://:{redis_password}@redis:6379/0"
    assert module.CELERY_BROKER_URL == f"redis://:{redis_password}@redis:6379/1"
    assert module.GIT_SHA == RELEASE_TAGS["GIT_SHA"]


def database_password(module: ModuleType) -> str:
    return str(module.DATABASES["default"]["PASSWORD"])


def test_the_example_sets_nothing_production_refuses() -> None:
    assert not set(REFUSED_WHEN_SET) & set(ACTIVE)
    assert not set(REFUSED_WHEN_SET) & set(COMMENTED)


def uncommented(*prefixes: str) -> dict[str, str]:
    return {name: value for name, value in COMMENTED.items() if name.startswith(prefixes)}


# Live blocks: their commented lines, uncommented as they stand, with the block's mode live. The error must
# name a variable of the block.
LIVE_BLOCKS = {
    "billz": ({"BILLZ_MODE": "live"}, ("BILLZ_",)),
    "click": ({"CLICK_MODE": "live"}, ("CLICK_",)),
    "payme": ({"PAYME_MODE": "live"}, ("PAYME_",)),
    "bts": ({"BTS_MODE": "live"}, ("BTS_",)),
    "eskiz": ({"ESKIZ_MODE": "live"}, ("ESKIZ_",)),
    "fcm": ({"FCM_MODE": "live"}, ("FCM_",)),
    "oauth": ({"OAUTH_MODE": "live"}, ("GOOGLE_OAUTH_", "FACEBOOK_APP_")),
    "alerts": ({}, ("ALERT_WEBHOOK_",)),
}


@pytest.mark.parametrize("block", sorted(LIVE_BLOCKS))
def test_commented_live_placeholders_are_refused(
    boot: Callable[[dict[str, str]], ModuleType], block: str
) -> None:
    mode, prefixes = LIVE_BLOCKS[block]
    lines = uncommented(*prefixes)
    assert lines, f"no commented lines for {block}"
    names = "|".join(re.escape(name) for name in lines)
    with pytest.raises(ImproperlyConfigured, match=names):
        boot({**filled(), **lines, **mode})


def test_commented_apple_placeholders_are_refused(boot: Callable[[dict[str, str]], ModuleType]) -> None:
    real_looking = {
        "OAUTH_MODE": "live",
        "GOOGLE_OAUTH_CLIENT_ID": "1234567890-abc.apps.googleusercontent.com",
        "GOOGLE_OAUTH_CLIENT_SECRET": base64.urlsafe_b64encode(secrets.token_bytes(18)).decode(),
        "FACEBOOK_APP_ID": "1234567890",
        "FACEBOOK_APP_SECRET": secrets.token_hex(16),
    }
    lines = uncommented("APPLE_")
    with pytest.raises(ImproperlyConfigured, match="APPLE_"):
        boot({**filled(), **real_looking, **lines})
