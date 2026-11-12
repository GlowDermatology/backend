"""The ``django.contrib.admin`` app config of the project: its default site is the Unfold ops console.

Listed in ``INSTALLED_APPS`` instead of ``django.contrib.admin`` (after
``unfold.apps.BasicAppConfig``, which — unlike ``unfold``'s default config — leaves ``admin.site``
alone), so every ``admin.site.register(...)`` and ``register_read_only(...)`` call lands on
:class:`glowderma.core.admin_site.OpsAdminSite`.
"""

from django.contrib.admin.apps import AdminConfig


class OpsAdminConfig(AdminConfig):
    default_site = "glowderma.core.admin_site.OpsAdminSite"

    def ready(self) -> None:
        # The console's three languages (glowderma.core.console_i18n): the models' names, labels and choices
        # become lazy *before* the admins are discovered, because an admin copies them when it is registered
        # (``ReadOnlyAdmin``'s money and JSON columns take their header from the field's label).
        from glowderma.core.console_i18n import localize_model_metadata

        localize_model_metadata()
        super().ready()
