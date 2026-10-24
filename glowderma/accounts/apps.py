from django.apps import AppConfig


class AccountsConfig(AppConfig):
    name = "glowderma.accounts"

    def ready(self) -> None:
        from . import schema  # noqa: F401
