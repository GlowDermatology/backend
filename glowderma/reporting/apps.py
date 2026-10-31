from django.apps import AppConfig


class ReportingConfig(AppConfig):
    name = "glowderma.reporting"

    def ready(self) -> None:
        # The console index renders the «Ops holati» dashboard (UNFOLD["DASHBOARD_CALLBACK"] fills it): Unfold
        # resolves ``admin/index.html`` from its own app first, so the site is pointed at ours by name.
        from django.contrib import admin

        from glowderma.reporting.admin_views import OPS_TEMPLATE

        admin.site.index_template = OPS_TEMPLATE
