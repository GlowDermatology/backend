"""Load runtime secrets from a JSON file into the process environment, before the settings read it.

In the deployed stack a Vault Agent sidecar renders every secret of this environment into one JSON file
(``DJANGO_SECRETS_FILE``, ``/run/secrets/django.json``), on a memory-only volume mounted read-only here. This
module turns that file into environment variables, so the settings keep reading ``os.environ`` through
``config.env`` exactly as before: development (``.env`` / exported variables), CI and tests need no file and
see no change.

Contract of the file:

- a JSON object whose keys are environment variable names (``[A-Z_][A-Z0-9_]*``) and whose values are strings;
  a JSON object or array value (for example ``JWT_SIGNING_KEYS``) is accepted and re-serialised to JSON text;
- quotes, backslashes, Unicode and newlines are preserved exactly (it is JSON, never parsed by a shell);
- keys owned by the Compose topology (hosts, networks, database location) are refused, so a typo in Vault can
  never re-route the application;
- the file wins over a variable of the same name already in the environment (Vault is the source of truth).

Errors name the file and the offending key, never a value. Nothing here logs.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping, MutableMapping
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured

SECRETS_FILE_VARIABLE = "DJANGO_SECRETS_FILE"
KEY_PATTERN = re.compile(r"[A-Z_][A-Z0-9_]*\Z")
# Set by compose/app.yaml from the topology; a secrets file must not override them.
TOPOLOGY_KEYS = frozenset(
    {
        "DJANGO_SETTINGS_MODULE",
        SECRETS_FILE_VARIABLE,
        "DATABASE_URL",
        "POSTGRES_NAME",
        "POSTGRES_USER",
        "POSTGRES_HOST",
        "POSTGRES_PORT",
        "ALLOWED_HOSTS",
        "TRUSTED_PROXIES",
        "ADMIN_API_ALLOWED_NETS",
        "FORWARDED_ALLOW_IPS",
        "PUBLIC_MEDIA_BASE_URL",
    }
)
# Key files rendered next to the secrets file by the same agent; any other *_FILE path is the deployment's.
SECRET_FILE_PATH_KEYS = frozenset({"FCM_CREDENTIALS_FILE", "APPLE_PRIVATE_KEY_FILE"})


def parse_secrets(text: str, *, source: str) -> dict[str, str]:
    """Validate the file's text and return the environment variables it defines."""
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        # Only the position: the decoder's message could quote file content.
        position = f"line {exc.lineno}, column {exc.colno}"
        raise ImproperlyConfigured(f"{source} is not valid JSON ({position})") from None
    if not isinstance(data, dict):
        raise ImproperlyConfigured(f"{source} must contain a JSON object of environment variables")
    values: dict[str, str] = {}
    for key, value in data.items():
        if not KEY_PATTERN.match(key):
            raise ImproperlyConfigured(f"{source}: {key!r} is not a valid environment variable name")
        if key in TOPOLOGY_KEYS or (key.endswith("_FILE") and key not in SECRET_FILE_PATH_KEYS):
            raise ImproperlyConfigured(f"{source}: {key} is set by the deployment, not by the secrets file")
        if isinstance(value, str):
            values[key] = value
        elif isinstance(value, dict | list):
            values[key] = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        else:
            raise ImproperlyConfigured(f"{source}: {key} must be a string (got {type(value).__name__})")
        if "\x00" in values[key]:
            raise ImproperlyConfigured(f"{source}: {key} contains a NUL character")
    return values


def load_secrets_file(environ: MutableMapping[str, str] | None = None) -> Mapping[str, str]:
    """Load ``$DJANGO_SECRETS_FILE`` into ``environ`` (default ``os.environ``); a no-op when it is unset.

    Returns the variables that were set (for tests). A configured but missing, unreadable or empty file is a
    hard error: a deployment that names a secrets file must never start without it.
    """
    env = os.environ if environ is None else environ
    path = env.get(SECRETS_FILE_VARIABLE, "")
    if not path:
        return {}
    source = f"{SECRETS_FILE_VARIABLE} ({path})"
    try:
        text = Path(path).read_text(encoding="utf-8")
    except FileNotFoundError:
        raise ImproperlyConfigured(f"{source} does not exist (is the Vault agent running?)") from None
    except (OSError, UnicodeDecodeError) as exc:
        raise ImproperlyConfigured(f"{source} cannot be read: {type(exc).__name__}") from None
    if not text.strip():
        raise ImproperlyConfigured(f"{source} is empty")
    values = parse_secrets(text, source=source)
    env.update(values)
    return values
