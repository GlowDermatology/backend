import logging
from typing import Any

from django.apps import AppConfig
from django.apps import apps as global_apps
from django.db.models.signals import post_migrate

from glowderma.core import integration_modes

logger = logging.getLogger("glowderma.integrations")
_fake_mode_warned = False

PROVIDERS = ("billz", "bts", "click", "payme", "eskiz", "fcm")


def ensure_provider_rows(sender: AppConfig, *, using: str, apps: Any = global_apps, **kwargs: Any) -> None:
    """Keep one ``IntegrationState`` row per provider (design 01 §2.3: reads never write).

    ``0003_seed_integration_state`` inserts them; this ``post_migrate`` receiver re-inserts them after a
    ``flush`` (transactional tests, ``--reuse-db`` reruns), the way ``contenttypes`` restores its rows.
    """
    # ``migrate`` passes the post-migration state apps, ``flush`` passes nothing (the global registry)
    try:
        IntegrationState = apps.get_model("integrations", "IntegrationState")
    except LookupError:  # migrated to zero: no table to fill
        return
    for provider in PROVIDERS:
        IntegrationState.objects.using(using).get_or_create(provider=provider)


def warn_fake_providers() -> bool:
    """Log one WARNING per process listing the providers in ``fake`` mode when the settings are production.

    Fake providers are allowed in production (product owner decision 2026-10-26), so this is a log line,
    never a system check (CI runs ``check --deploy --fail-level WARNING``) and never a refusal to start.
    Returns whether the line was emitted by this call.
    """
    global _fake_mode_warned
    if _fake_mode_warned or not integration_modes.is_production():
        return False
    fakes = integration_modes.fake_providers()
    if not fakes:
        return False
    _fake_mode_warned = True
    logger.warning("integrations running in fake mode in production: %s", ", ".join(fakes))
    return True


class IntegrationsConfig(AppConfig):
    name = "glowderma.integrations"
    label = "integrations"

    def ready(self) -> None:
        post_migrate.connect(
            ensure_provider_rows, sender=self, dispatch_uid="integrations.ensure_provider_rows"
        )
        warn_fake_providers()
