from typing import Any

from django.apps import AppConfig
from django.apps import apps as global_apps
from django.db.models.signals import post_migrate

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


class IntegrationsConfig(AppConfig):
    name = "glowderma.integrations"
    label = "integrations"

    def ready(self) -> None:
        post_migrate.connect(
            ensure_provider_rows, sender=self, dispatch_uid="integrations.ensure_provider_rows"
        )
