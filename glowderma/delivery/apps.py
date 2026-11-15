from typing import Any

from django.apps import AppConfig
from django.apps import apps as global_apps
from django.db import connections
from django.db.models.signals import post_migrate


def ensure_settings(sender: AppConfig, *, using: str, apps: Any = global_apps, **kwargs: Any) -> None:
    """Keep the ``DeliverySettings`` row with pk 1: ``delivery.0007`` inserts it, this re-inserts it after a
    ``flush`` (transactional tests, ``--reuse-db`` reruns), like ``site_config.apps.ensure_singleton``."""
    # ``migrate`` passes the post-migration state apps, ``flush`` passes nothing (the global registry)
    try:
        DeliverySettings = apps.get_model("delivery", "DeliverySettings")
    except LookupError:  # migrated below 0006: no table to fill
        return
    # a ``flush`` after ``migrate delivery 0005`` (or a dependency's ``zero``) passes the global registry,
    # which knows the model although its table is gone
    with connections[using].cursor() as cursor:
        if DeliverySettings._meta.db_table not in connections[using].introspection.table_names(cursor):
            return
    DeliverySettings.objects.using(using).get_or_create(pk=1)


class DeliveryConfig(AppConfig):
    name = "glowderma.delivery"
    verbose_name = "Yetkazib berish"

    def ready(self) -> None:
        post_migrate.connect(ensure_settings, sender=self, dispatch_uid="delivery.ensure_settings")
