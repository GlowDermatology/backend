from typing import Any

from django.apps import AppConfig
from django.apps import apps as global_apps
from django.db.models.signals import post_migrate


def ensure_singleton(sender: AppConfig, *, using: str, apps: Any = global_apps, **kwargs: Any) -> None:
    """Keep the ``SiteSettings`` row with pk 1 (design 01 §3.1: ``GET settings/`` never writes).

    ``0002_seed_singleton`` inserts it; this ``post_migrate`` receiver re-inserts it after a ``flush``
    (transactional tests, ``--reuse-db`` reruns), the way ``contenttypes`` restores its rows.
    """
    # ``migrate`` passes the post-migration state apps, ``flush`` passes nothing (the global registry)
    try:
        SiteSettings = apps.get_model("site_config", "SiteSettings")
    except LookupError:  # migrated to zero: no table to fill
        return
    SiteSettings.objects.using(using).get_or_create(pk=1)


class SiteConfigConfig(AppConfig):
    name = "glowderma.site_config"
    verbose_name = "Sayt sozlamalari"

    def ready(self) -> None:
        post_migrate.connect(ensure_singleton, sender=self, dispatch_uid="site_config.ensure_singleton")
