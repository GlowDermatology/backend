import json
import os
from typing import Any

from django.core.exceptions import ImproperlyConfigured

MISSING = object()


def env_str(name: str, default: Any = MISSING) -> str:
    value = os.environ.get(name)
    if value:
        return value
    if default is MISSING:
        raise ImproperlyConfigured(f"{name} is required")
    return str(default)


def env_int(name: str, default: Any = MISSING) -> int:
    try:
        return int(env_str(name, default))
    except ValueError as exc:
        raise ImproperlyConfigured(f"{name} must be an integer") from exc


def env_bool(name: str, default: Any = MISSING) -> bool:
    value = env_str(name, default).lower()
    if value not in {"true", "false", "1", "0"}:
        raise ImproperlyConfigured(f"{name} must be a boolean")
    return value in {"true", "1"}


def env_list(name: str, default: Any = MISSING) -> list[str]:
    if not os.environ.get(name) and default is not MISSING:
        return list(default)
    return [item.strip() for item in env_str(name).split(",") if item.strip()]


def env_json(name: str, default: Any = MISSING) -> Any:
    if not os.environ.get(name) and default is not MISSING:
        return default
    try:
        return json.loads(env_str(name))
    except ValueError as exc:
        raise ImproperlyConfigured(f"{name} must be JSON") from exc
