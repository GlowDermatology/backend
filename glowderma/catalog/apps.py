from django.apps import AppConfig


class CatalogConfig(AppConfig):
    name = "glowderma.catalog"
    verbose_name = "Katalog"

    def ready(self) -> None:
        # Registers the outbox handler of ``job.queued`` (and the Celery task) in every process that can run
        # ``core.dispatch_outbox`` — the worker autodiscovers it, the API process needs the import.
        from glowderma.catalog import tasks  # noqa: F401
