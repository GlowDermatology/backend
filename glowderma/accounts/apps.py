from django.apps import AppConfig


class AccountsConfig(AppConfig):
    name = "glowderma.accounts"

    def ready(self) -> None:
        from . import schema, tasks  # noqa: F401  (tasks: registers the outbox handlers)
