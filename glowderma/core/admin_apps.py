"""The ``django.contrib.admin`` app config of the project: its default site is the Unfold ops console.

Listed in ``INSTALLED_APPS`` instead of ``django.contrib.admin`` (after
``unfold.apps.BasicAppConfig``, which — unlike ``unfold``'s default config — leaves ``admin.site``
alone), so every ``admin.site.register(...)`` and ``register_read_only(...)`` call lands on
:class:`glowderma.core.admin_site.OpsAdminSite`.
"""

from django.contrib.admin.apps import AdminConfig


class OpsAdminConfig(AdminConfig):
    default_site = "glowderma.core.admin_site.OpsAdminSite"
