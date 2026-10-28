"""Provider adapter modes (``*_MODE`` settings, ``fake`` | ``live``): one place that lists them.

``core`` sits below ``integrations``, so the health check and the settings screen read the modes here, from
Django settings only. Fake providers are allowed in production (product owner decision 2026-10-26): the
process logs one warning at startup (``integrations`` ``AppConfig.ready``) and never refuses to start.

What a fake may do there is customer contract §6.14, decided by one predicate, :func:`fake_in_production`
(mode ``fake`` while ``settings.DEV_FEATURES_ALLOWED`` is false, i.e. under ``config.settings.production``,
which staging runs too); the other predicates derive from it.
"""

from __future__ import annotations

from urllib.parse import urlsplit

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
    "email": "EMAIL_MODE",
}
# Setting values that name the live adapter of a provider with its own vocabulary (``EMAIL_MODE=smtp``).
LIVE_MODE_ALIASES: dict[str, str] = {"smtp": "live"}
# Payment providers in the order ``settings/public/`` lists them (customer contract §2.4).
PAYMENT_PROVIDERS: tuple[str, ...] = (enums.PaymentProvider.CLICK.value, enums.PaymentProvider.PAYME.value)
# Providers whose callbacks (webhooks) are refused while they are fake under the production settings
# (§2.13.0).
CALLBACK_PROVIDERS: frozenset[str] = frozenset({"click", "payme", "bts"})
PRODUCTION_SETTINGS_MODULE = "config.settings.production"


def provider_modes() -> dict[str, str]:
    """``{provider: "fake" | "live"}`` for every provider with a ``*_MODE`` setting, in a fixed order."""
    return {provider: provider_mode(provider) for provider in PROVIDER_MODE_SETTINGS}


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
    """The mode of one provider (``fake`` | ``live``; ``EMAIL_MODE=smtp`` reads ``live``); an unknown name
    is a programming error (``KeyError``)."""
    value = str(getattr(settings, PROVIDER_MODE_SETTINGS[provider]))
    return LIVE_MODE_ALIASES.get(value, value)


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


# Live providers pointed at a sandbox: BTS's documented test host, Payme's sandbox checkout, Eskiz's test
# sender (the production settings refuse it; other settings only warn).
BTS_SANDBOX_HOSTS = frozenset({"apitest.bts.uz"})
PAYME_SANDBOX_HOSTS = frozenset({"test.paycom.uz"})
ESKIZ_TEST_SENDER = "4546"


def sandbox_providers() -> list[str]:
    """The live providers configured against a sandbox (a startup WARNING and the health check's
    ``sandbox`` list; M3): money and parcels there are not real."""
    found: list[str] = []
    if provider_mode("bts") == enums.IntegrationMode.LIVE:
        if (urlsplit(str(getattr(settings, "BTS_BASE_URL", ""))).hostname or "") in BTS_SANDBOX_HOSTS:
            found.append("bts")
    if provider_mode("payme") == enums.IntegrationMode.LIVE:
        host = urlsplit(str(getattr(settings, "PAYME_CHECKOUT_URL", ""))).hostname or ""
        if bool(getattr(settings, "PAYME_TEST_MODE", False)) or host in PAYME_SANDBOX_HOSTS:
            found.append("payme")
    if provider_mode("eskiz") == enums.IntegrationMode.LIVE:
        if str(getattr(settings, "ESKIZ_SENDER", "")) == ESKIZ_TEST_SENDER:
            found.append("eskiz")
    return found
