from django.apps import AppConfig


class CoreConfig(AppConfig):
    name = "glowderma.core"
    verbose_name = "Tizim"

    def ready(self) -> None:
        # The alert.send outbox handler registers at import time; every process that dispatches the outbox
        # needs it (docs/ops/monitoring.md).
        from glowderma.core import alerts  # noqa: F401
        from glowderma.core.console_i18n import localize_model_metadata

        # The ops console's three languages: model metadata becomes lazy (glowderma.core.console_i18n).
        localize_model_metadata()
