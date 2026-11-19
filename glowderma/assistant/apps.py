from typing import Any

from django.apps import AppConfig
from django.apps import apps as global_apps
from django.db import connections
from django.db.models.signals import post_migrate


def ensure_rows(sender: AppConfig, *, using: str, apps: Any = global_apps, **kwargs: Any) -> None:
    """Re-insert the rows ``assistant.0002`` seeds (settings, Gemini provider, five prompt templates with an
    active v1) after a ``flush`` (transactional tests, ``--reuse-db`` reruns); existing rows stay as they
    are."""
    from glowderma.assistant import defaults

    try:
        PromptVersion = apps.get_model("assistant", "PromptVersion")
    except LookupError:  # migrated below 0001
        return
    with connections[using].cursor() as cursor:
        if PromptVersion._meta.db_table not in connections[using].introspection.table_names(cursor):
            return
    defaults.ensure_rows(apps, using)


class AssistantConfig(AppConfig):
    name = "glowderma.assistant"
    verbose_name = "AI yordamchi"

    def ready(self) -> None:
        from glowderma.assistant import tasks  # noqa: F401  (registers the outbox handler)

        post_migrate.connect(ensure_rows, sender=self, dispatch_uid="assistant.ensure_rows")
