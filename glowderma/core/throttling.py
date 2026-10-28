"""Throttle rates (contract §1.9): defaults, env overrides and validation.

``DEFAULT_THROTTLE_RATES`` holds the contract defaults; ``settings.THROTTLE_RATES`` (env ``THROTTLE_RATES``,
a JSON object) overrides single scopes. Every ``SimpleRateThrottle`` subclass resolves its rate through
``rate_for(scope)`` from ``get_rate()`` so a change in the environment applies without a code change.
"""

import re
from typing import Any, Final

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

DEFAULT_THROTTLE_RATES: Final[dict[str, str]] = {
    "login": "30/min",
    "auth_client_ip": "600/min",
    "refresh": "120/min",
    "staff": "1200/min",
    "export": "10/min",
    "oauth": "30/min",
    "customer": "600/min",
    "sms_phone": "5/min",
    # Customer contract §1.8: a CGNAT backstop; the scope is used only by auth/send-code/.
    "sms_ip": "600/min",
    "checkout": "30/min",
    "pay": "30/min",
    "pos": "120/min",
    # M2 (customer contract §1.8): device-first keys because of carrier-grade NAT, pure-IP ceilings as
    # backstops.
    "sms_device": "10/min",
    "customer_auth_device": "30/min",
    "customer_auth_ip": "1200/min",
    "refresh_unknown": "60/min",
    "qr": "6/min",
    "public": "300/min",
    "public_ip": "3000/min",
    # B-W1: the website's server-side calls made on its own behalf (secret, no forwarded browser IP: cache
    # revalidation, sitemap builds); with a forwarded IP the per-IP scopes key on the browser's IP instead.
    "public_bff": "6000/min",
}

# DRF ``SimpleRateThrottle.parse_rate``: "<requests>/<period>" where the period's first letter is one of
# s (second), m (minute), h (hour), d (day); the rest of the period word is ignored ("30/min", "5/hour").
_RATE_RE: Final = re.compile(r"^[1-9]\d*/(?:s|m|h|d)[a-z]*$")


def validate_throttle_rates(rates: Any) -> dict[str, str]:
    """Return ``rates`` as a ``{scope: rate}`` dict or raise ``ImproperlyConfigured``.

    Called at settings import so a bad ``THROTTLE_RATES`` fails startup, not the first throttled request.
    """
    if not isinstance(rates, dict):
        raise ImproperlyConfigured("THROTTLE_RATES must be a JSON object of {scope: rate}")
    validated: dict[str, str] = {}
    for scope, rate in rates.items():
        if scope not in DEFAULT_THROTTLE_RATES:
            known = ", ".join(sorted(DEFAULT_THROTTLE_RATES))
            raise ImproperlyConfigured(f"THROTTLE_RATES: unknown scope {scope!r} (known: {known})")
        if not isinstance(rate, str) or not _RATE_RE.fullmatch(rate):
            raise ImproperlyConfigured(
                f"THROTTLE_RATES[{scope!r}] must look like '30/min', '5/hour' or '1000/day', got {rate!r}"
            )
        validated[str(scope)] = rate
    return validated


def rate_for(scope: str | None) -> str:
    """Rate for ``scope``: ``settings.THROTTLE_RATES`` merged over ``DEFAULT_THROTTLE_RATES``."""
    overrides: Any = getattr(settings, "THROTTLE_RATES", {})
    rates = {**DEFAULT_THROTTLE_RATES, **(overrides if isinstance(overrides, dict) else {})}
    if scope is None or scope not in rates:
        raise ImproperlyConfigured(f"No throttle rate configured for scope {scope!r}")
    return str(rates[scope])
