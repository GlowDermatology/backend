import json
import os
import re
from datetime import datetime
from ipaddress import IPv4Network, IPv6Network, ip_network
from typing import Any

from django.core.exceptions import ImproperlyConfigured

MISSING = object()
type Network = IPv4Network | IPv6Network


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


def env_datetime(name: str, default: Any = MISSING) -> datetime | None:
    """An aware ISO 8601 datetime (naive values are refused so a pinned clock is never ambiguous)."""
    if not os.environ.get(name) and default is not MISSING:
        return default  # type: ignore[no-any-return]
    try:
        value = datetime.fromisoformat(env_str(name))
    except ValueError as exc:
        raise ImproperlyConfigured(f"{name} must be an ISO 8601 datetime") from exc
    if value.tzinfo is None:
        raise ImproperlyConfigured(f"{name} must carry a timezone offset")
    return value


def env_networks(name: str, default: Any = MISSING) -> tuple[Network, ...]:
    """CIDRs separated by commas and/or whitespace (the edge's ``OPS_ALLOWED_IPS`` list is space separated:
    Caddy splits ``{$VAR}`` on spaces, so one value can feed both)."""
    values = (
        re.split(r"\s*,\s*|\s+", env_str(name).strip()) if os.environ.get(name) else env_list(name, default)
    )
    try:
        return tuple(ip_network(value, strict=True) for value in values)
    except ValueError as exc:
        raise ImproperlyConfigured(f"{name} must contain valid networks without host bits") from exc
