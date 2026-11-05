"""config/secrets.py: the deployed stack's JSON secrets file becomes environment variables before settings."""

import base64
import json
import os
import secrets
import subprocess
import sys
from pathlib import Path

import pytest
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

from config.secrets import SECRETS_FILE_VARIABLE, load_secrets_file, parse_secrets


def write(tmp_path: Path, content: str) -> Path:
    path = tmp_path / "django.json"
    path.write_text(content, encoding="utf-8")
    return path


def test_unset_variable_is_a_no_op() -> None:
    env: dict[str, str] = {"KEEP": "1"}
    assert load_secrets_file(env) == {}
    assert env == {"KEEP": "1"}


def test_values_are_loaded_exactly(tmp_path: Path) -> None:
    tricky = 'quote " backslash \\ newline\nunicode ü 🌸 $HOME `id` $(id)'
    path = write(tmp_path, json.dumps({"SECRET_KEY": tricky, "EMPTY_OK": ""}))
    env = {SECRETS_FILE_VARIABLE: str(path)}
    loaded = load_secrets_file(env)
    assert env["SECRET_KEY"] == tricky  # never interpreted by a shell
    assert loaded == {"SECRET_KEY": tricky, "EMPTY_OK": ""}


def test_the_file_wins_over_the_environment(tmp_path: Path) -> None:
    path = write(tmp_path, json.dumps({"LOG_LEVEL": "DEBUG"}))
    env = {SECRETS_FILE_VARIABLE: str(path), "LOG_LEVEL": "INFO"}
    load_secrets_file(env)
    assert env["LOG_LEVEL"] == "DEBUG"


def test_json_values_are_reserialised(tmp_path: Path) -> None:
    path = write(tmp_path, json.dumps({"JWT_SIGNING_KEYS": {"k1": "abc"}, "LIST": ["a", "b"]}))
    env = {SECRETS_FILE_VARIABLE: str(path)}
    load_secrets_file(env)
    assert json.loads(env["JWT_SIGNING_KEYS"]) == {"k1": "abc"}
    assert json.loads(env["LIST"]) == ["a", "b"]


@pytest.mark.parametrize(
    ("content", "message"),
    [
        ("", "is empty"),
        ("   \n", "is empty"),
        ('{"A": "1",}', "is not valid JSON (line 1"),
        ('["A"]', "must contain a JSON object"),
        ('{"lower": "x"}', "not a valid environment variable name"),
        ('{"A-B": "x"}', "not a valid environment variable name"),
        ('{"PORT": 5432}', "PORT must be a string (got int)"),
        ('{"FLAG": true}', "FLAG must be a string (got bool)"),
        ('{"NOTHING": null}', "NOTHING must be a string (got NoneType)"),
        ('{"X": "a\\u0000b"}', "X contains a NUL character"),
    ],
)
def test_invalid_files_are_refused(tmp_path: Path, content: str, message: str) -> None:
    path = write(tmp_path, content)
    with pytest.raises(ImproperlyConfigured, match=message.replace("(", r"\(").replace(")", r"\)")):
        load_secrets_file({SECRETS_FILE_VARIABLE: str(path)})


@pytest.mark.parametrize(
    "key", ["POSTGRES_HOST", "POSTGRES_USER", "DATABASE_URL", "ALLOWED_HOSTS", "TRUSTED_PROXIES", "SOME_FILE"]
)
def test_topology_keys_are_refused(key: str) -> None:
    with pytest.raises(ImproperlyConfigured, match=f"{key} is set by the deployment"):
        parse_secrets(json.dumps({key: "x"}), source="test")


def test_key_file_paths_are_allowed() -> None:
    values = parse_secrets(json.dumps({"FCM_CREDENTIALS_FILE": "/run/secrets/fcm.json"}), source="t")
    assert values == {"FCM_CREDENTIALS_FILE": "/run/secrets/fcm.json"}


def test_missing_file_is_a_hard_error(tmp_path: Path) -> None:
    with pytest.raises(ImproperlyConfigured, match="does not exist"):
        load_secrets_file({SECRETS_FILE_VARIABLE: str(tmp_path / "absent.json")})


def test_errors_never_contain_values(tmp_path: Path) -> None:
    value = "super-secret-value-0123456789"
    cases = [
        f'{{"GOOD": "{value}", "bad key": "{value}"}}',
        f'{{"GOOD": "{value}", "N": 1}}',
        f'{{"GOOD": "{value}" "broken"}}',
        f'{{"POSTGRES_HOST": "{value}"}}',
    ]
    for content in cases:
        path = write(tmp_path, content)
        with pytest.raises(ImproperlyConfigured) as info:
            load_secrets_file({SECRETS_FILE_VARIABLE: str(path)})
        assert value not in str(info.value)
        assert info.value.__cause__ is None  # no chained exception carrying the text either


def b64url(size: int) -> str:
    return base64.urlsafe_b64encode(secrets.token_bytes(size)).rstrip(b"=").decode()


def production_secrets() -> dict[str, str]:
    return {
        "SECRET_KEY": secrets.token_urlsafe(48),
        "INTERNAL_API_SECRET": secrets.token_hex(32),
        "JWT_SIGNING_KEYS": json.dumps({"k1": b64url(32)}),
        "JWT_ACTIVE_KID": "k1",
        "SMS_CODE_HMAC_KEY": secrets.token_hex(32),
        "LOG_PII_KEY": secrets.token_hex(32),
        "PAYMENT_REF_KEY": secrets.token_hex(32),
        "QR_HMAC_KEY": secrets.token_hex(32),
        "BTS_WEBHOOK_SECRET": secrets.token_hex(32),
        "SECRETBOX_KEY": b64url(32),
        "CLICK_SECRET_KEY": secrets.token_hex(16),
        "PAYME_KEY": secrets.token_hex(16),
        "POSTGRES_PASSWORD": secrets.token_hex(24),
        "REDIS_URL": "redis://:pw@redis:6379/0",
        # Read by config/settings/base.py at import time: proves the loader runs before base.py.
        "GOOGLE_OAUTH_CLIENT_SECRET": "google-secret-from-vault",
        "EMAIL_HOST_PASSWORD": "smtp-password-from-vault",
    }


def boot_production(secrets_file: Path | None) -> subprocess.CompletedProcess[str]:
    keep = ("PATH", "HOME", "LANG", "SSL_CERT_FILE", "TMPDIR")
    env = {name: os.environ[name] for name in keep if name in os.environ}
    env.update(
        {
            "DJANGO_SETTINGS_MODULE": "config.settings.production",
            # What compose/app.yaml sets (topology), never in the secrets file.
            "POSTGRES_NAME": "glowderma",
            "POSTGRES_USER": "glowderma_app",
            "POSTGRES_HOST": "postgres",
            "ALLOWED_HOSTS": "example.com,172.30.0.30",
            "ADMIN_API_ALLOWED_NETS": "172.30.0.20/32",
            "TRUSTED_PROXIES": "172.30.0.10/32",
            "PUBLIC_MEDIA_BASE_URL": "https://example.com/media/",
        }
    )
    if secrets_file is not None:
        env[SECRETS_FILE_VARIABLE] = str(secrets_file)
    script = (
        "import json, django; django.setup(); from django.conf import settings as s; "
        "print(json.dumps({'google': s.GOOGLE_OAUTH_CLIENT_SECRET, 'smtp': s.SMTP_PASSWORD, "
        "'db': s.DATABASES['default']['PASSWORD'], 'redis': s.REDIS_URL}))"
    )
    return subprocess.run(  # noqa: S603 - this interpreter, a fixed script
        [sys.executable, "-c", script],
        cwd=settings.BASE_DIR,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def test_production_settings_boot_from_the_secrets_file_alone(tmp_path: Path) -> None:
    values = production_secrets()
    result = boot_production(write(tmp_path, json.dumps(values)))
    assert result.returncode == 0, result.stderr[-2000:]
    seen = json.loads(result.stdout.strip().splitlines()[-1])
    assert seen == {
        "google": values["GOOGLE_OAUTH_CLIENT_SECRET"],
        "smtp": values["EMAIL_HOST_PASSWORD"],
        "db": values["POSTGRES_PASSWORD"],
        "redis": values["REDIS_URL"],
    }
    for value in values.values():
        assert value not in result.stderr


def test_production_refuses_to_start_when_a_secret_is_missing(tmp_path: Path) -> None:
    values = production_secrets()
    secret = values.pop("SECRET_KEY")
    result = boot_production(write(tmp_path, json.dumps(values)))
    assert result.returncode != 0
    assert "SECRET_KEY is required" in result.stderr
    assert secret not in result.stderr


def test_production_refuses_to_start_when_the_named_file_is_absent(tmp_path: Path) -> None:
    result = boot_production(tmp_path / "missing.json")
    assert result.returncode != 0
    assert "does not exist" in result.stderr
