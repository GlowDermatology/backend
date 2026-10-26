"""Provider adapter modes (``*_MODE`` settings, ``fake`` | ``live``): one place that lists them.

``core`` sits below ``integrations``, so the health check and the settings screen read the modes here, from
Django settings only. Fake providers are allowed in production (product owner decision 2026-10-26): the
process logs one warning at startup (``integrations`` ``AppConfig.ready``) and never refuses to start.
"""

from __future__ import annotations

from django.conf import settings

from glowderma.core import enums

# API provider name → the Django setting holding its mode (each defaults to INTEGRATIONS_MODE).
PROVIDER_MODE_SETTINGS: dict[str, str] = {
    "oauth": "OAUTH_MODE",
    "billz": "BILLZ_MODE",
    "fcm": "FCM_MODE",
    "click": "CLICK_MODE",
    "payme": "PAYME_MODE",
    "bts": "BTS_MODE",
}
PRODUCTION_SETTINGS_MODULE = "config.settings.production"


def provider_modes() -> dict[str, str]:
    """``{provider: "fake" | "live"}`` for every provider with a ``*_MODE`` setting, in a fixed order."""
    return {provider: str(getattr(settings, name)) for provider, name in PROVIDER_MODE_SETTINGS.items()}


def fake_providers() -> list[str]:
    """The providers whose mode is ``fake``, in :data:`PROVIDER_MODE_SETTINGS` order."""
    return [provider for provider, mode in provider_modes().items() if mode == enums.IntegrationMode.FAKE]


def is_production() -> bool:
    """Whether the process runs the production settings module."""
    return str(getattr(settings, "SETTINGS_MODULE", "")) == PRODUCTION_SETTINGS_MODULE
