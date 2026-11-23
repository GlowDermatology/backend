from django.apps import AppConfig


class AccountsConfig(AppConfig):
    name = "glowderma.accounts"
    verbose_name = "Hisoblar"

    def ready(self) -> None:
        from django.contrib import admin

        from glowderma.accounts import schema, tasks  # noqa: F401  (tasks: registers the outbox handlers)
        from glowderma.accounts.api import guard

        # The ops console's password step: wired once here, the login view never mutates the site.
        admin.site.login_form = guard.GuardedAdminAuthenticationForm
