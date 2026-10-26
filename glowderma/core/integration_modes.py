"""Provider adapter modes (``*_MODE`` settings, ``fake`` | ``live``): one place that lists them.

``core`` sits below ``integrations``, so the health check and the settings screen read the modes here, from
Django settings only. Fake providers are allowed in production (product owner decision 2026-10-26): the
process logs one warning at startup (``integrations`` ``AppConfig.ready``) and never refuses to start.

What a fake may do there is customer contract §6.14, decided by one predicate, :func:`fake_in_production`
(mode ``fake`` while ``settings.DEV_FEATURES_ALLOWED`` is false, i.e. under ``config.settings.production``,
which staging runs too); the other predicates derive from it.
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
    "eskiz": "ESKIZ_MODE",
}
# Payment providers in the order ``settings/public/`` lists them (customer contract §2.4).
PAYMENT_PROVIDERS: tuple[str, ...] = (enums.PaymentProvider.CLICK.value, enums.PaymentProvider.PAYME.value)
# Providers whose callbacks (webhooks) are refused while they are fake under the production settings
# (§2.13.0).
CALLBACK_PROVIDERS: frozenset[str] = frozenset({"click", "payme", "bts"})
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


def dev_features_allowed() -> bool:
    """``settings.DEV_FEATURES_ALLOWED``: ``True`` only under the local/test settings modules."""
    return bool(getattr(settings, "DEV_FEATURES_ALLOWED", False))


def provider_mode(provider: str) -> str:
    """The mode of one provider; an unknown name is a programming error (``KeyError``)."""
    return str(getattr(settings, PROVIDER_MODE_SETTINGS[provider]))


def fake_in_production(provider: str) -> bool:
    """The one predicate of customer contract §6.14: ``provider`` runs its fake adapter outside local/test."""
    return provider_mode(provider) == enums.IntegrationMode.FAKE and not dev_features_allowed()


def callbacks_refused(provider: str) -> bool:
    """Whether the provider's callbacks start no transaction and honour no capture (§2.13.0)."""
    if provider not in CALLBACK_PROVIDERS:
        raise KeyError(provider)
    return fake_in_production(provider)


def payable_providers() -> list[str]:
    """Payment providers that can take a payment now, Click then Payme (§2.4): fakes only under local/test."""
    return [provider for provider in PAYMENT_PROVIDERS if not callbacks_refused(provider)]


def delivery_available() -> bool:
    """Home delivery is offered unless BTS is fake under the production settings (§2.6)."""
    return not fake_in_production("bts")


def fulfilment_methods() -> list[str]:
    """``FulfilmentMethod`` literals offered at checkout, ``delivery`` (when available) then ``pickup``."""
    methods = [enums.FulfilmentMethod.DELIVERY.value] if delivery_available() else []
    return [*methods, enums.FulfilmentMethod.PICKUP.value]


def online_sales_enabled() -> bool:
    """Online selling needs live Billz stock (§2.3): off while Billz is fake under the production settings."""
    return not fake_in_production("billz")
