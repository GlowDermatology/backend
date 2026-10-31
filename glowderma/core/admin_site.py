"""The Glow Derma ops console: the project's default admin site (``admin.site``), styled by Unfold.

Seams for the later console work:

* ``UNFOLD`` (``config/settings/ops_admin.py``) holds the theme, the sidebar and the callbacks below;
  dashboards plug in through ``UNFOLD["DASHBOARD_CALLBACK"]`` and ``UNFOLD["SITE_VIEWS"]`` (custom
  pages), never by subclassing this site again.
* The login form is wired by ``glowderma.accounts.api.guard`` (the lockout lives above ``core``); a second
  login step (2FA) wraps :meth:`OpsAdminSite.login` / ``admin_view`` from ``accounts``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.conf import settings
from django.http import HttpRequest
from unfold.sites import UnfoldAdminSite

if TYPE_CHECKING:
    from django.contrib.admin import AdminSite as _BaseSite
else:
    _BaseSite = UnfoldAdminSite

SITE_NAME = "Glow Derma Ops"

# Label shown in the header badge and its Unfold colour variant.
ENVIRONMENT_VARIANTS = {"PRODUCTION": "danger", "STAGING": "warning", "LOCAL": "info", "TEST": "info"}


class OpsAdminSite(_BaseSite):
    site_header = SITE_NAME
    site_title = SITE_NAME
    index_title = "Boshqaruv"
    # No public "view site" link: the console has no storefront of its own.
    site_url = None


def environment_callback(request: HttpRequest) -> list[str]:
    """``UNFOLD["ENVIRONMENT"]``: the header badge ``[label, variant]`` from ``OPS_ENVIRONMENT_LABEL``."""
    label = str(getattr(settings, "OPS_ENVIRONMENT_LABEL", "LOCAL")).upper()
    return [label, ENVIRONMENT_VARIANTS.get(label, "warning")]


def environment_title_prefix(request: HttpRequest) -> str:
    """``UNFOLD["ENVIRONMENT_TITLE_PREFIX"]``: ``[STAGING]`` in the browser tab outside production."""
    label = str(getattr(settings, "OPS_ENVIRONMENT_LABEL", "LOCAL")).upper()
    return "" if label == "PRODUCTION" else f"[{label}]"
